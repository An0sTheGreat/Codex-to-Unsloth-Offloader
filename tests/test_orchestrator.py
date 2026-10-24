import json
import unittest

from unittest.mock import patch

import pytest
from bridge.schemas import (HandoffRequest as HR, RepositoryContext as RC, WorkerResult as WR, ResultState, RoutingDecisionKind, FinalState, PatchValidationResult as PV, ValidationOutcome as VO, FailureKind, LengthLevel, RoutingDecision)
from bridge.classifier import TaskClassifier
from bridge.config import OffloaderConfig
from bridge.errors import ApiUnavailableError, ModelNotLoadedError, UnslothError
from bridge.orchestrator import Orchestrator


def make_req(task="tiny change", objective="Change one line in a.py"):
    return HR(
        task=task, objective=objective, difficulty=2, length=LengthLevel.SHORT,
        repository_context=RC(summary="repo summary", allowed_files=["a.py"], read_only_files=[], relevant_snippets=[]))


def _ta():
    return TaskClassifier().assess("Bump a version number in one file")


class FakeRouter:
    def __init__(self, kind, n=1):
        self.kind = kind
        self.n = n

    def assess(self, t, c=None):
        return _ta()

    def route(self, assessment, **kw):
        reqs = [make_req() for _ in range(self.n)]
        return RoutingDecision(kind=self.kind, model_alias="m", reason="ok",
                               subrequests=reqs, assessment=assessment,
                               difficulty=2, length=LengthLevel.SHORT)


class FakeTransport:
    def __init__(self, canned=None, model_error=None):
        self.canned = list(canned or [WR(state=ResultState.COMPLETED)])
        self.sent = 0
        self.summaries = []
        self.model_error = model_error

    def discover_loaded_model(self):
        if self.model_error is not None:
            raise self.model_error
        return "m"

    def send(self, r):
        self.sent += 1
        self.summaries.append(r.repository_context.summary)
        v = self.canned[self.sent - 1] if self.sent <= len(self.canned) else WR(state=ResultState.COMPLETED)
        if isinstance(v, Exception):
            raise v
        return v


class StubValidator:
    def __init__(self, valid=True):
        self.valid = valid

    def validate(self, w, r):
        return PV(valid=self.valid, files_changed=["a.py"], unexpected_changes=[], read_only_changes=[], path_traversals=[])


# Bounded-instrumentation used by the tests below: every independent-verification
# call is recorded so we can assert verification runs exactly as often as its
# retry budget allows (and never after a terminal worker state).
calls = []


def fv_accept(w, r):
    """Passing independent-verification stub (Phase 8).""
    calls.append("ok")
    return VO(outcome=ValidationOutcomeKind.ACCEPTED_UNCHANGED,
              files_changed=["a.py"], unexpected_changes=[], tests_passed=True)


def fv_reject():
    def _fv(w, r):
        calls.append("rejected")
        return VO(outcome=ValidationOutcomeKind.REJECTED,
                  files_changed=["a.py"], unexpected_changes=[], tests_passed=False)
    return _fv


def _reset_calls():
    del calls[:]

# ==========================================================================
# Phase 9 orchestration unit tests: assess -> route -> package -> send ->
# parse -> validate -> verify -> bounded fallback.
# ==============================================================================


class TestKeepInCodexRouting(unittest.TestCase):
    """Routings that never reach the local model (Phase 4 / handoff.md s.9)."""

    def test_no_transport_configured_stays_in_codex(self):
        o = Orchestrator(config=OffloaderConfig())
        res = o.offload("Bump a version number in one file")
        self.assertEqual(res.routing.kind, RoutingDecisionKind.KEEP_IN_CODEX)
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)
        self.assertFalse(res.is_success)
        self.assertEqual(len(res.subtask_outcomes), 0)

    def test_model_not_loaded_stays_in_codex(self):
        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(model_error=ModelNotLoadedError("no model loaded")))
        res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)

    def test_api_unavailable_stays_in_codex(self):
        o = Orchestrator(OffloaderConfig(), FakeTransport(model_error=ApiUnavailableError("503")))
        res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)

    def test_generic_discovery_failure_stays_in_codex(self):
        o = Orchestrator(OffloaderConfig(), FakeTransport(model_error=RuntimeError("boom")))
        res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)

    def test_keep_decision_carries_assessment_and_reason(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(), FakeTransport(model_error=ModelNotLoadedError("x")))
        res = o.offload("Bump a version number")
        self.assertIsNotNone(res.routing.assessment)
        self.assertEqual(len(calls), 0)  # no local model interaction occurred


class TestDryRun(unittest.TestCase):
    """dry_run: routing decision only; no local call is ever made."""

    def test_full_local_dry_run_no_sends(self):
        o = Orchestrator(OffloaderConfig(dry_run=True), FakeTransport())
        res = o.offload("Bump a version number")
        self.assertEqual(res.routing.kind, RoutingDecisionKind.FULL_LOCAL)
        self.assertEqual(res.final_state, FinalState.COMPLETED)
        self.assertTrue(any("dry-run" in n for n in res.notes))

    def test_partial_local_dry_run_reports_partial(self):
        o = Orchestrator(OffloaderConfig(dry_run=True), FakeTransport(),
                         router=FakeRouter(kind=RoutingDecisionKind.PARTIAL_LOCAL, n=2))
        res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.PARTIAL)
        self.assertTrue(res.is_success)  # partial offload is still an accepted result

    def test_keep_in_codex_dry_run_no_sends(self):
        o = Orchestrator(OffloaderConfig(dry_run=True), FakeTransport(),
                         router=FakeRouter(kind=RoutingDecisionKind.KEEP_IN_CODEX))
        res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)


class TestFullLocalAcceptance(unittest.TestCase):
    """A full-local subrequest is accepted ONLY after both the Phase 7 scope
    gate and the Phase 8 independent verification pass; otherwise it retries
    within budget and then falls back to Codex. The worker's self-report is
    never trusted."""

    def test_accepts_scope_valid_independently_verified_patch(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(), FakeTransport(canned=[WR(state=ResultState.COMPLETED)]),
                         verify_fn=fv_accept)
        res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.COMPLETED)
        out0 = res.subtask_outcomes[0]
        self.assertTrue(out0.accepted)
        self.assertIs(out0.failure_kind, FailureKind.NONE)
        self.assertEqual(out0.attempts, 1)
        self.assertEqual(o.transport.sent, 1)
        self.assertEqual(calls, ["ok"])

    def test_two_failed_verification_attempts_fall_back_to_codex(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(canned=[WR(state=ResultState.COMPLETED)] * 4),
                         verify_fn=fv_reject())
        res = o.offload("Bump a version number")
        out0 = res.subtask_outcomes[0]
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)
        self.assertFalse(out0.accepted)
        self.assertIs(out0.failure_kind, FailureKind.VERIFICATION)
        self.assertEqual(out0.attempts, 2)                      # default budget = 2
        self.assertIn("attempt 1: independent verification rejected", out0.notes)
        self.assertIn("attempt 2: independent verification rejected", out0.notes)
        self.assertEqual(o.transport.sent, 2)                 # bounded: one send per attempt
        self.assertEqual(calls, ["rejected", "rejected"])

    def test_two_failed_scope_gate_attempts_fall_back_to_codex(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(canned=[WR(state=ResultState.COMPLETED)] * 4),
                         validator=StubValidator(valid=False))
        res = o.offload("Bump a version number")
        out0 = res.subtask_outcomes[0]
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)
        self.assertFalse(out0.accepted)
        self.assertIs(out0.failure_kind, FailureKind.SCOPE)
        self.assertEqual(out0.attempts, 2)
        self.assertIn("attempt 1: scope gate rejected", out0.notes)
        self.assertEqual(calls, [])          # independent verify never runs on a failed patch

    def test_second_attempt_can_be_accepted(self):
        _reset_calls()
        stateful = {"rejections": 1}

        def fv(w, r):
            if stateful["rejections"]:
                stateful["rejections"] -= 1
                return VO(outcome=ValidationOutcomeKind.REJECTED,
                          files_changed=["a.py"], unexpected_changes=[], tests_passed=False)
            calls.append("ok")
            return fv_accept(w, r) if False else VO(
                outcome=ValidationOutcomeKind.ACCEPTED_UNCHANGED,
                files_changed=["a.py"], unexpected_changes=[], tests_passed=True)

        o = Orchestrator(OffloaderConfig(), FakeTransport(canned=[WR(state=ResultState.COMPLETED)] * 4),
                         verify_fn=fv)
        res = o.offload("Bump a version number")
        out0 = res.subtask_outcomes[0]
        self.assertTrue(out0.accepted)
        self.assertEqual(res.final_state, FinalState.COMPLETED)
        self.assertEqual(out0.attempts, 2)   # bounded retry recovered

    def test_budget_override_lowers_retry_limit(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(canned=[WR(state=ResultState.COMPLETED)] * 4),
                         verify_fn=fv_reject())
        res = o.offload("Bump a version number", max_validation_failures=1)
        out0 = res.subtask_outcomes[0]
        self.assertEqual(out0.attempts, 1)      # only one attempt allowed
        self.assertEqual(o.transport.sent, 1)
        self.assertEqual(calls, ["rejected"])


class TestPartialLocal(unittest.TestCase):
    """PARTIAL_LOCAL: independent subrequests; per-subtask outcomes are kept
    even when later ones fail or the run is interrupted."""

    def test_mixed_acceptance_reports_partial(self):
        _reset_calls()
        fv_count = {"n": 0}

        def fv(w, r):
            fv_count["n"] += 1
            calls.append("ok")
            if fv_count["n"] == 1:
                return VO(outcome=ValidationOutcomeKind.ACCEPTED_UNCHANGED,
                          files_changed=["a.py"], unexpected_changes=[], tests_passed=True)
            return VO(outcome=ValidationOutcomeKind.REJECTED,
                      files_changed=["a.py"], unexpected_changes=[], tests_passed=False)

        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(canned=[WR(state=ResultState.COMPLETED)] * 4),
                         router=FakeRouter(kind=RoutingDecisionKind.PARTIAL_LOCAL, n=2),
                         verify_fn=fv)
        res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.PARTIAL)
        self.assertTrue(res.is_success)              # partial is still an accepted result
        self.assertEqual(len(res.subtask_outcomes), 2)
        self.assertTrue(res.subtask_outcomes[0].accepted)
        self.assertFalse(res.subtask_outcomes[1].accepted)
        self.assertIs(res.subtask_outcomes[1].failure_kind, FailureKind.VERIFICATION)

    def test_transport_error_stops_loop_and_falls_back(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(canned=[WR(state=ResultState.COMPLETED), UnslothError("connection reset")]),
                         router=FakeRouter(kind=RoutingDecisionKind.PARTIAL_LOCAL, n=2),
                         verify_fn=fv_accept)
        res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)   # transport failure stops the run
        out1 = res.subtask_outcomes[0]
        out2 = res.subtask_outcomes[1]
        self.assertTrue(out1.accepted)
        self.assertIs(out2.failure_kind, FailureKind.TRANSPORT)
        self.assertEqual(o.transport.sent, 2)                        # exactly one send per subrequest
        self.assertTrue(any("transport error during delegation" in n for n in res.notes))

    def test_all_subrequests_fail_gives_keep_in_codex(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(canned=[WR(state=ResultState.COMPLETED)] * 4),
                         router=FakeRouter(kind=RoutingDecisionKind.PARTIAL_LOCAL, n=2),
                         validator=StubValidator(valid=False))
        res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)  # zero accepted -> Codex fallback
        self.assertFalse(res.is_success)
        self.assertEqual(len(res.subtask_outcomes), 2)


class TestTerminalWorkerStates(unittest.TestCase):
    """A terminal worker state (declined / error / exhausted needs_context)
    ends that subrequest immediately: no further sends, never forced."""

    def test_declined_worker_no_further_sends(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(canned=[WR(state=ResultState.DECLINED, decline_reason="too large")]),
                         router=FakeRouter(kind=RoutingDecisionKind.FULL_LOCAL))
        res = o.offload("Bump a version number")
        out0 = res.subtask_outcomes[0]
        self.assertEqual(o.transport.sent, 1)          # bounded: no retry after terminal state
        self.assertIs(out0.failure_kind, FailureKind.WORKER)
        self.assertIn("no further sends", out0.notes)
        self.assertTrue(out0.worker_result.decline_reason == "too large")
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)

    def test_exhausted_context_retries_are_terminal(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(canned=[WR(state=ResultState.NEEDS_CONTEXT, context_needed="file A.py"),
                                              WR(state=ResultState.NEEDS_CONTEXT, context_needed="file B.py")]),
                         router=FakeRouter(kind=RoutingDecisionKind.FULL_LOCAL))
        res = o.offload("Bump a version number")  # default budget: one retry only
        self.assertEqual(o.transport.sent, 2)      # bounded: never forces more context requests
        out0 = res.subtask_outcomes[0]
        self.assertIs(out0.failure_kind, FailureKind.WORKER)
        self.assertIn("needs_context", out0.notes)
        self.assertFalse(out0.accepted)
        self.assertEqual(res.final_state, FinalState.KEEP_IN_CODEX)

    def test_zero_context_retries_mean_no_extra_send(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(canned=[WR(state=ResultState.NEEDS_CONTEXT, context_needed="x")]))
        res = o.offload("Bump a version number", max_context_retries=0)
    def test_send_with_context_retry_needs_context_budget(self):
        _reset_calls()
        t = FakeTransport(canned=[WR(state=ResultState.NEEDS_CONTEXT, context_needed="A"),
                                  WR(state=ResultState.COMPLETED)])
        res = send_with_context_retry(t, make_req(), max_context_retries=1)
        self.assertEqual(res.state, ResultState.COMPLETED)
        self.assertEqual(t.sent, 2)   # initial + one bounded retry
        self.assertIs(out0.failure_kind, FailureKind.WORKER)

    def test_partial_local_survives_terminal_state_on_one_subrequest(self):
        _reset_calls()
        o = Orchestrator(OffloaderConfig(),
                         FakeTransport(canned=[WR(state=ResultState.DECLINED, decline_reason="nope"),
                                              WR(state=ResultState.COMPLETED)]),
                         router=FakeRouter(kind=RoutingDecisionKind.PARTIAL_LOCAL, n=2),
                         verify_fn=fv_accept)
        res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.PARTIAL)  # the good subrequest still lands
        self.assertTrue(res.subtask_outcomes[0].failure_kind is FailureKind.WORKER)
        self.assertTrue(res.subtask_outcomes[1].accepted)


class TestSendWithContextRetry(unittest.TestCase):
    """send_with_context_retry: bounded needs-context loop (Phase 9 step 5)."""

    def test_completed_short_circuits(self):
        t = FakeTransport(canned=[WR(state=ResultState.COMPLETED),
                                  WR(state=ResultState.NEEDS_CONTEXT, context_needed="y")])
        res = send_with_context_retry(t, make_req(), max_context_retries=3)
        self.assertEqual(res.state, ResultState.COMPLETED)
        self.assertEqual(t.sent, 1)   # no retry after a completed result

    def test_whitespace_only_context_need_not_retried(self):
        t = FakeTransport(canned=[WR(state=ResultState.NEEDS_CONTEXT, context_needed="   "),
                                  WR(state=ResultState.COMPLETED)])
        res = send_with_context_retry(t, make_req(), max_context_retries=3)
        self.assertEqual(res.state, ResultState.NEEDS_CONTEXT)  # empty need is terminal
        self.assertEqual(t.sent, 1)

    def test_bounded_retries_grow_summary_only_by_requested_context(self):
        t = FakeTransport(canned=[WR(state=ResultState.NEEDS_CONTEXT, context_needed="file A.py"),
                                  WR(state=ResultState.COMPLETED)])
        res = send_with_context_retry(t, make_req(), max_context_retries=1)
        self.assertEqual(res.state, ResultState.COMPLETED)
        self.assertEqual(t.sent, 2)   # initial send + one bounded retry
        self.assertEqual(t.summaries[0], "repo summary")
        self.assertEqual(t.summaries[1],
                         "repo summary\nWorker requested missing context: file A.py")

    def test_no_retry_when_budget_exhausted(self):
        t = FakeTransport(canned=[WR(state=ResultState.NEEDS_CONTEXT, context_needed="A"),
                                  WR(state=ResultState.COMPLETED)])
        res = send_with_context_retry(t, make_req(), max_context_retries=0)
        self.assertEqual(res.state, ResultState.NEEDS_CONTEXT)  # terminal; never forced
        self.assertEqual(t.sent, 1)

    def test_original_request_not_mutated(self):
        req = make_req()
        t = FakeTransport(canned=[WR(state=ResultState.NEEDS_CONTEXT, context_needed="A"),
                                  WR(state=ResultState.COMPLETED)])
        send_with_context_retry(t, req, max_context_retries=2)
        self.assertEqual(req.repository_context.summary, "repo summary")  # deep-copied request


class TestKeyboardInterrupt(unittest.TestCase):
    """Ctrl+C mid-run yields CANCELLED with partial results retained (no crash)."""

    def _orchestrator(self, kind=RoutingDecisionKind.PARTIAL_LOCAL, n=3):
        return Orchestrator(OffloaderConfig(),
                            FakeTransport(canned=[WR(state=ResultState.COMPLETED)] * 6),
                            router=FakeRouter(kind=kind, n=n), verify_fn=fv_accept)

    def test_interruption_after_first_subtask_reports_cancelled(self):
        o = self._orchestrator()
        orig = type(o)._process_subtask
        count = {"n": 0}

        def fake(req, mcr=None, mvf=None):
            count["n"] += 1
            if count["n"] > 1:
                raise KeyboardInterrupt()
            return orig(o, req)

        with patch.object(Orchestrator, "_process_subtask", fake):
            res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.CANCELLED)
        self.assertFalse(res.is_success)
        self.assertEqual(len(res.subtask_outcomes), 1)   # partial results retained
        self.assertTrue(any("run cancelled by user" in n for n in res.notes))

    def test_interruption_before_any_send_reports_cancelled(self):
        o = self._orchestrator()
        with patch.object(Orchestrator, "_process_subtask", side_effect=KeyboardInterrupt()):
            res = o.offload("Bump a version number")
        self.assertEqual(res.final_state, FinalState.CANCELLED)
        self.assertEqual(len(res.subtask_outcomes), 0)    # nothing retained
        self.assertTrue(any("run cancelled by user" in n for n in res.notes))


class TestResultIntegrity(unittest.TestCase):
    """OrchestrationResult stays serializable and self-consistent (Phase 9 contract)."""

    def test_json_round_trip_preserves_disposition(self):
        o = Orchestrator(OffloaderConfig(), FakeTransport(canned=[WR(state=ResultState.COMPLETED)]),
                         verify_fn=fv_accept)
        res = o.offload("Bump a version number")
        dumped = json.dumps(res.model_dump(mode="json"), indent=2)   # no raise: all fields serializable
        loaded = OrchestrationResult(**json.loads(dumped))
        self.assertEqual(loaded.final_state, FinalState.COMPLETED)
        self.assertEqual(len(loaded.subtask_outcomes), 1)





