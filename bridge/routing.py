"""Routing layer for the Unsloth offloader.

Routing decides, for a classified task, whether and how to offload it to the
local model. It is intentionally a thin interface in Phase 1: the *classification*
(:class:`TaskAssessment`) and the *routing policy* are separate implementations
added in Phase 3 (classifier) and Phase 4 (routing policy). This module defines
the contract both must satisfy, so they can be swapped, tested, and mocked
independently.

The router takes a :class:`TaskAssessment` and produces a :class:`RoutingDecision`:

* ``full_local``  -> one :class:`HandoffRequest` covering the whole task;
* ``partial_local`` -> one :class:`HandoffRequest` per bounded subtask;
* ``keep_in_codex`` -> no handoff; Codex executes it itself.

The router must never make the offload decision silently: every decision carries a
``reason`` that is surfaced in the Coding Task Assessment footer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from .classifier import TaskClassifier
from .config import OffloaderConfig
from .context import ContextPackager
from .schemas import (
    HandoffRequest,
    LengthLevel,
    ModelProfile,
    PackageStatus,
    RepositoryContext,
    RoutingDecision,
    RoutingDecisionKind,
    TaskAssessment,
)


class Router(ABC):
    """Abstract task classifier + router.

    Implementations keep the two concerns separate: :meth:`assess` classifies a
task (Phase 3), :meth:`route` turns an assessment into a decision (Phase 4).
    Splitting them lets Codex call ``route`` with a *user-provided* assessment
    (e.g. from the footer) without re-running the classifier.
    """

    @abstractmethod
    def assess(self, task_description: str, *, context: Optional[dict] = None) -> TaskAssessment:
        """Classify a coding task into a :class:`TaskAssessment`. (Phase 3.)

        :param task_description: The user's coding request (or the relevant excerpt).
        :param context: Optional structured context (repo summary, files, etc.).
        :return: A validated :class:`TaskAssessment`.
        """

    @abstractmethod
    def route(self, assessment: TaskAssessment, *, config=None) -> RoutingDecision:
        """Turn an assessment into a :class:`RoutingDecision`. (Phase 4.)

        :param assessment: The :class:`TaskAssessment` to route.
        :param config: Optional :class:`OffloaderConfig`; used to read routing
        thresholds (max difficulty / length for offload, model alias).
        :return:
            * :class:`RoutingDecision` with ``kind=full_local`` and one
              ``subrequests`` entry (the whole task) for small, safe tasks;
            * ``kind=partial_local`` with multiple ``subrequests`` (bounded
              subtasks) for medium tasks; or
            * ``kind=keep_in_codex`` with no subrequests for large / high-risk
              work.
        """

    # -- convenience --

    def plan(self, task_description: str, *, context: Optional[dict] = None, config=None,
             scope: Optional[dict] = None, project_root=None) -> RoutingDecision:
        """Assess then route, in one call (for the Codex orchestration path).

        ``scope``/``project_root`` are forwarded to :meth:`route` only when
        provided, so routers whose ``route`` predates Phase 5 (signature
        ``route(assessment, *, config=None)``) remain compatible.
        """
        assessment = self.assess(task_description, context=context)
        if scope is not None or project_root is not None:
            decision = self.route(assessment, config=config, scope=scope,
                                  project_root=project_root)
        else:
            decision = self.route(assessment, config=config)
        decision.assessment = assessment
        return decision


# ---------------------------------------------------------------------------
# Concrete routing policy (Phase 4)
# ---------------------------------------------------------------------------

# str-enum comparison is lexicographic, not ordinal, so length comparisons use
# an explicit ordinal map.
_LENGTH_ORDINAL = {
    LengthLevel.SHORT: 0,
    LengthLevel.MEDIUM: 1,
    LengthLevel.LONG: 2,
    LengthLevel.EXTENDED: 3,
}

# Bounded subtasks emitted for PARTIAL_LOCAL (handoff.md section 4: repetitive
# implementation, tests/docs, and boilerplate are delegated independently).
_PARTIAL_SUBTASKS = (
    ("implementation", "Implement the bounded implementation portion of the task."),
    ("tests", "Add or extend unit tests that prove the implementation."),
    ("docs", "Update code comments and docs for the changed code."),
)


class PolicyRouter(Router):
    """Phase 4 routing policy.

    Consumes the Phase 3 :class:`TaskClassifier` assessment and the
    :class:`~bridge.config.OffloaderConfig` model profile to produce a
    :class:`RoutingDecision`. Checks run in conservative order:

    1. Model availability — a *detected* model must match
       ``config.local_model_match`` (unknown capability degrades to Codex);
       when nothing is detectable, the configured profile is trusted.
    2. ``config.auto_handoff_enabled`` must be True.
    3. Validation failures for the same task must be below
       ``config.max_validation_failures``.
    4. Packaged context must fit ``config.context_budget_tokens``.
    5. High-risk flags force Keep in Codex.
    6. Difficulty must lie in the profile's ``use_for_difficulty``; for the
       built-in default profile it must also be at or below
       ``config.max_difficulty_full_handoff``. A custom profile's
       ``use_for_difficulty`` is fully authoritative (it may enable difficulties
       above the default ceiling).
    7. Scope within ``profile.max_default_length`` -> full local handoff;
       otherwise partial handoff of bounded subtasks.
    """

    def __init__(self, classifier: Optional[TaskClassifier] = None):
        self.classifier = classifier or TaskClassifier()

    def assess(self, task_description: str, *, context: Optional[dict] = None) -> TaskAssessment:
        """Classify a task (delegates to :class:`TaskClassifier`, Phase 3)."""
        return self.classifier.assess(task_description, context=context)

    def route(self, assessment: TaskAssessment, *, config=None, loaded_model=None,
              validation_failures: int = 0, context_tokens: Optional[int] = None,
              task_description: Optional[str] = None, scope: Optional[dict] = None,
              project_root=None) -> RoutingDecision:
        """Turn an assessment into a :class:`RoutingDecision` (Phase 4).

        :param assessment: The :class:`TaskAssessment` to route.
        :param config: Optional :class:`~bridge.config.OffloaderConfig`.
        :param loaded_model: Optional :class:`~bridge.schemas.ModelMetadata` from
            transport discovery; when present, the model must match the profile
            (unknown capability -> keep in Codex). When None, the configured
            profile is used.
        :param validation_failures: How many times this task failed Codex
            validation so far.
        :param context_tokens: Estimated token size of the packaged context.
        :param task_description: Original task text, used to phrase subrequests.
        :param scope: Optional task-scope dict for Phase 5 context packaging
            (``allowed_files``, ``read_only_files``, ``snippets``, ``summary``,
            ``acceptance_criteria``). When given, the
            :class:`~bridge.context.ContextPackager` runs before routing; on
            ``needs_scope_reduction`` or budget overflow the decision is
            KEEP_IN_CODEX (overflow is handled before model invocation).
        :param project_root: Optional project root for packaging; defaults to
            :func:`~bridge.config.get_project_root`.
        :return: A :class:`RoutingDecision` for FULL_LOCAL, PARTIAL_LOCAL, or
            KEEP_IN_CODEX, with a concise ``reason`` and ``dry_run`` flag.
        """
        config = config or OffloaderConfig()
        profile = config.model_profile
        available, model_alias, model_note = self._model_available(loaded_model, config)

        # Lightweight summary carried on the decision so explain() can report
        # difficulty/length even though the full assessment is not attached.
        summary = dict(
            difficulty=assessment.difficulty,
            difficulty_label=assessment.difficulty_label,
            length=assessment.length,
        )

        if not available:
            return self._keep(model_alias, model_note, config, **summary)
        if not config.auto_handoff_enabled:
            return self._keep(model_alias,
                              "automatic handoff disabled (auto_handoff_enabled=false)",
                              config, **summary)
        if validation_failures >= config.max_validation_failures:
            return self._keep(model_alias,
                              f"validation failed {validation_failures}x for this task "
                              f"(limit {config.max_validation_failures}); keep in Codex",
                              config, **summary)
        # Phase 5: package context from the task scope before the budget check,
        # so overflow is handled before any model invocation.
        packaged = None
        if scope is not None:
            packager = ContextPackager(config, project_root)
            packaged = packager.package(scope)
            if packaged.status is PackageStatus.NEEDS_SCOPE_REDUCTION:
                return self._keep(model_alias,
                                  f"context overflow: required context ~"
                                  f"{packaged.tokens_used} tokens exceeds "
                                  f"budget {packaged.budget_tokens} tokens "
                                  f"(needs scope reduction); keep in Codex",
                                  config, **summary)
            if context_tokens is None:
                context_tokens = packaged.tokens_used

        if context_tokens is not None and context_tokens > config.context_budget_tokens:
            return self._keep(model_alias,
                              f"packaged context ~{context_tokens} tokens exceeds "
                              f"budget {config.context_budget_tokens} tokens; keep in Codex",
                              config, **summary)
        if assessment.is_high_risk:
            flags = ", ".join(f.value for f in assessment.risk_flags)
            return self._keep(model_alias, f"high-risk flags ({flags}); keep in Codex", config, **summary)
        if not self._difficulty_eligible(assessment, profile, config):
            return self._keep(model_alias,
                              f"difficulty {assessment.difficulty} not eligible "
                              f"(profile use_for_difficulty={profile.use_for_difficulty}, "
                              f"max_difficulty_full_handoff={config.max_difficulty_full_handoff}); "
                              f"keep in Codex",
                              config, **summary)

        if _LENGTH_ORDINAL[assessment.length] <= _LENGTH_ORDINAL[profile.max_default_length]:
            reason = (f"difficulty {assessment.difficulty} eligible "
                      f"(use_for_difficulty={profile.use_for_difficulty}), "
                      f"{assessment.length.value} scope within max "
                      f"{profile.max_default_length.value}; routing to {model_alias}; {model_note}")
            return RoutingDecision(
                kind=RoutingDecisionKind.FULL_LOCAL,
                model_alias=model_alias,
                reason=reason,
                subrequests=[self._build_request(assessment, task_description, model_alias,
                                                 profile=profile, packaged=packaged)],
                dry_run=config.dry_run,
                **summary,
            )

        reason = (f"{assessment.length.value} scope exceeds profile max "
                  f"{profile.max_default_length.value}; routing to {model_alias}; partial handoff of "
                  f"bounded subtasks; {model_note}")
        return RoutingDecision(
            kind=RoutingDecisionKind.PARTIAL_LOCAL,
            model_alias=model_alias,
            reason=reason,
            subrequests=self._partial_subrequests(assessment, task_description, profile,
                                                  model_alias, packaged=packaged),
            dry_run=config.dry_run,
            **summary,
        )

    def plan(self, task_description: str, *, context: Optional[dict] = None, config=None,
             loaded_model=None, validation_failures: int = 0,
             context_tokens: Optional[int] = None, scope: Optional[dict] = None,
             project_root=None) -> RoutingDecision:
        """Assess then route, in one call (for the Codex orchestration path)."""
        assessment = self.assess(task_description, context=context)
        decision = self.route(assessment, config=config, loaded_model=loaded_model,
                              validation_failures=validation_failures,
                              context_tokens=context_tokens,
                              task_description=task_description, scope=scope,
                              project_root=project_root)
        decision.assessment = assessment
        return decision

    # -- helpers --

    def _model_available(self, loaded_model, config):
        """Return (available, model_alias, model_note).

        Unknown or mismatched capability degrades conservatively (keep in
        Codex). A detected matching model uses the *actual* loaded name as the
        alias; an undetected model falls back to the configured profile.
        """
        if loaded_model is None:
            return True, config.model_profile.name, "model not detected; using configured profile"
        if not loaded_model.loaded:
            return False, "", "no model loaded in Unsloth"
        match = config.local_model_match
        haystack = f"{loaded_model.id} {loaded_model.display_name}".lower()
        if match and match.lower() not in haystack:
            return False, "", (f"loaded model {loaded_model.display_name!r} does not match "
                               f"configured profile (local_model_match={match!r}); "
                               f"capability unknown - conservative fallback")
        return True, loaded_model.display_name, f"loaded model {loaded_model.display_name!r} matches profile"

    def _keep(self, model_alias, reason, config, *, difficulty=None, difficulty_label=None,
              length=None):
        return RoutingDecision(kind=RoutingDecisionKind.KEEP_IN_CODEX, model_alias=model_alias,
                               reason=reason, subrequests=[], dry_run=config.dry_run,
                               difficulty=difficulty, difficulty_label=difficulty_label,
                               length=length)

    def _difficulty_eligible(self, assessment, profile, config) -> bool:
        """Return True when the assessment's difficulty is eligible for a full
        local handoff.

        The profile's ``use_for_difficulty`` is the set of difficulties this
        local model is good for. ``config.max_difficulty_full_handoff`` is a
        safety ceiling that applies to the built-in default profile; a custom
        profile (one whose ``use_for_difficulty`` differs from the default) is
        fully authoritative and may enable difficulties above the default
        ceiling.
        """
        if profile.use_for_difficulty != ModelProfile().use_for_difficulty:
            return assessment.difficulty in profile.use_for_difficulty
        return (assessment.difficulty in profile.use_for_difficulty
                and assessment.difficulty <= config.max_difficulty_full_handoff)

    def _build_request(self, assessment, task_description, model_alias, *, profile,
                       length=None, difficulty=None, note="", packaged=None):
        task = (task_description or assessment.reason).strip()
        if packaged is not None:
            repository_context = packaged.repository_context
            acceptance_criteria = list(packaged.acceptance_criteria or [])
        else:
            repository_context = RepositoryContext(
                summary=(f"Routed to {model_alias}. Difficulty {assessment.difficulty} "
                         f"({assessment.difficulty_label.value}), length "
                         f"{assessment.length.value}. {note}").strip())
            acceptance_criteria = []
        return HandoffRequest(
            task=task,
            objective=f"Complete the bounded task: {task}",
            difficulty=difficulty if difficulty is not None else assessment.difficulty,
            length=length or assessment.length,
            repository_context=repository_context,
            constraints=[
                "Do not modify files outside allowed scope",
                "Do not add dependencies unless explicitly requested",
                "Do not access secrets or credentials",
                "Return a unified diff or structured patch",
            ],
            acceptance_criteria=acceptance_criteria or [
                "Requested behavior is implemented",
                "Existing tests still pass",
            ],
        )

    def _partial_subrequests(self, assessment, task_description, profile, model_alias,
                             packaged=None):
        sub_difficulty = min(assessment.difficulty, 3)
        subrequests = []
        for focus, _ in _PARTIAL_SUBTASKS:
            task = f"{task_description} - {focus}" if task_description else focus
            subrequests.append(
                self._build_request(
                    assessment,
                    task,
                    model_alias,
                    profile=profile,
                    length=LengthLevel.SHORT,
                    difficulty=sub_difficulty,
                    note=f"Bounded subtask ({focus}) of a larger task.",
                    packaged=packaged,
                )
            )
        return subrequests


__all__ = ["Router", "PolicyRouter"]
