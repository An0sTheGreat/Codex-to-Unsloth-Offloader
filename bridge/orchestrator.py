"""Phase 9 automatic handoff orchestration (``bridge.orchestrator``).

Connects the existing Phase 3-8 components into one bounded, deterministic
workflow:

    assess -> route -> package -> send -> parse -> validate -> verify -> fallback

The orchestrator is the single place that decides *whether* and *how* a task
is delegated, and it applies the handoff-protocol safeguards:

* The **Phase 7 scope gate** (``bridge.validate``) runs before any patch is
  accepted; a patch that touches a file outside the request's allowlist is
  rejected and never applied.
* The **Phase 8 independent verification** (``bridge.verification``) runs in a
  worktree and a separate process; the orchestrator only accepts a
  ``ACCEPTED_UNCHANGED`` outcome. A self-reported ``verified`` flag is ignored.
* **Bounded retries** (handoff.md section 10): a needs-context reply is
  retried at most ``max_context_retries`` times; a task that fails
  validation/verification is retried at most ``max_validation_failures``
  times. After the budget is exhausted the delegation stops and the task
  falls back to Codex.
* **Two-failure Codex fallback**: after the validation/verification retry
  budget is exhausted, the task is returned to Codex (recorded in the result)
  rather than being forced or marked complete.
* **No silent acceptance**: a subtask is ``accepted`` only when it passed the
  scope gate AND independent verification.

The :class:`OrchestrationResult` carries the routing decision, the per-subtask
outcomes, the final disposition, and the Coding Task Assessment data (rendered
as the markdown block by the Phase 10 footer).
"""
from __future__ import annotations

import logging
from typing import Optional

from .schemas import (
    TaskAssessment,
    RoutingDecision,
    RoutingDecisionKind,
    HandoffRequest,
    WorkerResult,
    ResultState,
    FailureKind,
    FinalState,
    SubtaskOutcome,
    OrchestrationResult,
    ValidationOutcomeKind,
)
from .routing import Router, PolicyRouter
from .transport import Transport
from .validate import PatchValidator
from .verification import verify
from .config import OffloaderConfig
from .errors import UnslothError, ModelNotLoadedError, ApiUnavailableError

logger = logging.getLogger(__name__)


def send_with_context_retry(transport: Transport, request: HandoffRequest,
                            max_context_retries: int = 1) -> WorkerResult:
    """Send ``request`` with the bounded needs-context retry (handoff.md s.10).

    The first send is always made. If (and only if) the worker replies
    ``needs_context`` with a non-empty ``context_needed``, the requested
    context is appended to the request's summary and the task is sent again,
    up to ``max_context_retries`` extra times. It never loops beyond that
    bound, and a non-needs-context state is returned unchanged.
    """
    result = transport.send(request)
    running_summary = request.repository_context.summary
    for _ in range(max_context_retries):
        if result.state is ResultState.COMPLETED:
            break
        if result.state is not ResultState.NEEDS_CONTEXT or not result.context_needed.strip():
            break
        req2 = request.model_copy(deep=True)
        running_summary += "\nWorker requested missing context: " + result.context_needed
        req2.repository_context.summary = running_summary
        result = transport.send(req2)
    return result


class Orchestrator:
    """Phase 9 automatic handoff orchestration.

    Wraps the existing Phase 3-8 components (classifier, router, context
    packager, transport, patch validator, verification) into one controlled
    flow and returns a :class:`OrchestrationResult`.
    """

    def __init__(self, config: Optional[OffloaderConfig] = None,
                 transport: Optional[Transport] = None,
                 *, router: Optional[Router] = None,
                 validator: Optional[PatchValidator] = None,
                 verify_fn=None):
        self.config = config or OffloaderConfig()
        self.transport = transport
        self.router = router or PolicyRouter()
        self.validator = validator or PatchValidator()
        self._verify = verify_fn if verify_fn is not None else verify

    # -- public -----------------------------------------------------------

    def offload(self, task_description: str, *,
                scope: Optional[dict] = None,
                project_root=None,
                context: Optional[dict] = None,
                dry_run: Optional[bool] = None,
                max_context_retries: Optional[int] = None,
                max_validation_failures: Optional[int] = None) -> OrchestrationResult:
        """Run the full orchestration for ``task_description``.

        Parameters
        ----------
        task_description:
            Free-text description of the coding task.
        scope:
            Optional task-scope dict (allowed_files, read_only_files,
            snippets, summary, acceptance_criteria) used for context packaging.
        project_root:
            Path to the project root for scope packaging (Path or str).
        context:
            Optional context dict passed to the classifier (e.g. a large
            file-list that should trigger ExcessiveContext).
        dry_run:
            Override the config's dry_run for this run. In dry-run, no local
            call is made; the result shows what would be offloaded.
        max_context_retries:
            Override for the needs-context retry budget (default 1).
        max_validation_failures:
            Override for the validation/verification retry budget (default from
            config, 2).
        """
        config = self.config
        if dry_run is not None:
            config = config.model_copy(update={"dry_run": bool(dry_run)})
        if max_context_retries is None:
            max_context_retries = 1
        if max_validation_failures is None:
            max_validation_failures = config.max_validation_failures

        notes: list = []

        # 1. Assess (Phase 3).
        assessment = self.router.assess(task_description, context=context)
        summary = dict(
            difficulty=assessment.difficulty,
            difficulty_label=assessment.difficulty_label,
            length=assessment.length,
        )

        # 2. Discover the model (live) then route (Phase 4).
        decision = self._route(assessment, task_description, config, scope,
                               project_root, **summary)
        decision.assessment = assessment
        notes.append(decision.reason)

        # 3. KEEP_IN_CODEX: no local call occurs.
        if decision.kind is RoutingDecisionKind.KEEP_IN_CODEX:
            return self._result(task_description, assessment, decision,
                                subtask_outcomes=[],
                                final_state=FinalState.KEEP_IN_CODEX,
                                notes=notes)

        # 4. Dry-run: report what would be offloaded; no local call.
        if config.dry_run:
            would_be = {
                RoutingDecisionKind.KEEP_IN_CODEX: FinalState.KEEP_IN_CODEX,
                RoutingDecisionKind.FULL_LOCAL: FinalState.COMPLETED,
                RoutingDecisionKind.PARTIAL_LOCAL: FinalState.PARTIAL,
            }[decision.kind]
            notes.append("dry-run: no local call made; this is what would be offloaded.")
            return self._result(task_description, assessment, decision,
                                subtask_outcomes=[],
                                final_state=would_be,
                                notes=notes)

        # 5. Send / parse / validate / verify / fallback for each subrequest.
        try:
            subtask_outcomes: list = []
            transport_failed = False
            for request in decision.subrequests:
                outcome = self._process_subtask(request, max_context_retries,
                                                max_validation_failures)
                subtask_outcomes.append(outcome)
                if outcome.failure_kind is FailureKind.TRANSPORT:
                    transport_failed = True
                    notes.append("transport error during delegation; stopping and "
                                 "returning the task to Codex.")
                    break

            accepted_count = sum(1 for o in subtask_outcomes if o.accepted)
            if transport_failed:
                final_state = FinalState.KEEP_IN_CODEX
            elif decision.kind is RoutingDecisionKind.FULL_LOCAL:
                final_state = (FinalState.COMPLETED
                               if accepted_count == len(subtask_outcomes)
                               else FinalState.KEEP_IN_CODEX)
            else:  # PARTIAL_LOCAL
                final_state = FinalState.PARTIAL if accepted_count > 0 else FinalState.KEEP_IN_CODEX

            for o in subtask_outcomes:
                if o.notes:
                    notes.append(o.notes)

            return self._result(task_description, assessment, decision,
                                subtask_outcomes=subtask_outcomes,
                                final_state=final_state,
                                notes=notes)
        except KeyboardInterrupt:
            return self._result(task_description, assessment, decision,
                                subtask_outcomes=subtask_outcomes,
                                final_state=FinalState.CANCELLED,
                                notes=list(notes) + ["run cancelled by user; partial results retained."])

    # -- private ----------------------------------------------------------

    def _route(self, assessment, task_description, config, scope, project_root,
               *, difficulty, difficulty_label, length) -> RoutingDecision:
        """Discover the model (live) then route. Returns a RoutingDecision.

        If the transport is unavailable (Unsloth down), returns a
        KEEP_IN_CODEX decision so the task continues in Codex without data
        loss (no local call is made).
        """
        loaded_model = None
        if not config.dry_run:
            if self.transport is None:
                return self._keep_decision(
                    model_alias="",
                    reason="No transport configured; task stays in Codex.",
                    difficulty=difficulty, difficulty_label=difficulty_label,
                    length=length)
            try:
                loaded_model = self.transport.discover_loaded_model()
            except (ModelNotLoadedError, ApiUnavailableError) as exc:
                return self._keep_decision(
                    model_alias="",
                    reason="Unsloth unavailable: %s; task stays in Codex (no local call)."
                           % exc.message,
                    difficulty=difficulty, difficulty_label=difficulty_label,
                    length=length)
            except UnslothError as exc:
                return self._keep_decision(
                    model_alias="",
                    reason="Unsloth unavailable: %s; task stays in Codex (no local call)."
                           % exc.message,
                    difficulty=difficulty, difficulty_label=difficulty_label,
                    length=length)
            except Exception as exc:
                return self._keep_decision(
                    model_alias="",
                    reason=("Unsloth unavailable (%s); task stays in Codex (no "
                            "local call).") % type(exc).__name__,
                    difficulty=difficulty, difficulty_label=difficulty_label,
                    length=length)
        return self.router.route(
            assessment, config=config, loaded_model=loaded_model,
            validation_failures=0, task_description=task_description,
            scope=scope, project_root=project_root)

    def _keep_decision(self, model_alias, reason, *, difficulty, difficulty_label,
                       length) -> RoutingDecision:
        return RoutingDecision(
            kind=RoutingDecisionKind.KEEP_IN_CODEX,
            model_alias=model_alias,
            reason=reason,
            subrequests=[],
            difficulty=difficulty,
            difficulty_label=difficulty_label,
            length=length,
        )

    def _process_subtask(self, request, max_context_retries,
                         max_validation_failures) -> SubtaskOutcome:
        """Send -> parse -> validate -> verify, with bounded retries.

        ``accepted`` is True only when the patch passed the Phase 7 scope gate
        AND Phase 8 independent verification (ACCEPTED_UNCHANGED) -- never a
        self-reported pass.
        """
        accepted = False
        worker = None
        validation = None
        verification = None
        failure_kind = FailureKind.NONE
        notes_parts: list = []
        attempts = 0

        for attempt in range(1, max_validation_failures + 1):
            attempts = attempt
            try:
                worker = send_with_context_retry(self.transport, request,
                                                 max_context_retries)
            except (ModelNotLoadedError, ApiUnavailableError) as exc:
                return SubtaskOutcome(
                    request_id=request.request_id, task=request.task,
                    attempts=attempt, failure_kind=FailureKind.TRANSPORT,
                    notes="transport unavailable: %s" % exc.message)
            except UnslothError as exc:
                return SubtaskOutcome(
                    request_id=request.request_id, task=request.task,
                    attempts=attempt, failure_kind=FailureKind.TRANSPORT,
                    notes="transport error: %s" % exc.message)
            except Exception as exc:
                return SubtaskOutcome(
                    request_id=request.request_id, task=request.task,
                    attempts=attempt, failure_kind=FailureKind.TRANSPORT,
                    notes="transport error (%s)" % type(exc).__name__)

            validation = None
            verification = None
            if worker.state is ResultState.COMPLETED:
                validation = self.validator.validate(worker, request)
                if validation.valid:
                    verification = self._verify(worker, request)
                    if verification.outcome is ValidationOutcomeKind.ACCEPTED_UNCHANGED:
                        accepted = True
                        break
                if not validation.valid:
                    failure_kind = FailureKind.SCOPE
                    notes_parts.append("attempt %d: scope gate rejected (%s)"
                                       % (attempt, validation.notes))
                elif verification is not None:
                    failure_kind = FailureKind.VERIFICATION
                    notes_parts.append("attempt %d: independent verification "
                                       "rejected (%s)" % (attempt, verification.notes))
            else:
                failure_kind = FailureKind.WORKER
                detail = (worker.context_needed or worker.decline_reason
                          or worker.error or "")
                return SubtaskOutcome(
                    request_id=request.request_id, task=request.task,
                    attempts=attempt, worker_result=worker, failure_kind=failure_kind,
                    accepted=False, validation=None, verification=None, notes=
"attempt %d: terminal worker state=%s (%s); no further sends." % (attempt,
                worker.state.value, detail))

        return SubtaskOutcome(
            request_id=request.request_id,
            task=request.task,
            attempts=attempts,
            worker_result=worker,
            failure_kind=FailureKind.NONE if accepted else failure_kind,
            validation=validation,
            verification=verification,
            accepted=accepted,
            notes="; ".join(notes_parts),
        )

    def _result(self, task_description, assessment, decision, *,
                subtask_outcomes, final_state, notes) -> OrchestrationResult:
        return OrchestrationResult(
            task_description=task_description,
            assessment=assessment,
            routing=decision,
            subtask_outcomes=subtask_outcomes,
            final_state=final_state,
            notes=notes,
        )