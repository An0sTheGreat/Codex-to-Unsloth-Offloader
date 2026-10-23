"""Tests for the validated contract schemas (bridge.schemas)."""

import unittest
from datetime import datetime, timezone

from bridge.schemas import (
    DifficultyLevel,
    Execution,
    HandoffRequest,
    LengthLevel,
    ModelMetadata,
    OffloadDecision,
    OffloadResult,
    RepositoryContext,
    TaskAssessment,
    ValidationOutcome,
    ValidationOutcomeKind,
    WorkerResult,
    ResultState,
    RoutingDecision,
)
from bridge.schemas import RoutingDecisionKind


class TestTaskAssessment(unittest.TestCase):
    def test_valid_assessment(self):
        ta = TaskAssessment(
            difficulty=2,
            difficulty_label="Easy",
            length=LengthLevel.SHORT,
            length_note="one isolated file change",
            offload=OffloadDecision.YES,
            offload_note="small boilerplate",
            execution=Execution.LOCAL_HANDOFF,
        )
        self.assertEqual(ta.difficulty, 2)
        self.assertIs(ta.difficulty_label, DifficultyLevel.EASY)
        self.assertIs(ta.execution, Execution.LOCAL_HANDOFF)

    def test_difficulty_label_must_match_difficulty(self):
        with self.assertRaises(ValueError):
            TaskAssessment(
                difficulty=2,
                difficulty_label="Expert",
                length=LengthLevel.SHORT,
                length_note="x",
                offload=OffloadDecision.YES,
                offload_note="x",
                execution=Execution.LOCAL_HANDOFF,
            )

    def test_offload_and_execution_must_agree_yes(self):
        with self.assertRaises(ValueError):
            TaskAssessment(
                difficulty=2,
                difficulty_label="Easy",
                length=LengthLevel.SHORT,
                length_note="x",
                offload=OffloadDecision.YES,
                offload_note="x",
                execution=Execution.KEEP_IN_CODEX,
            )

    def test_offload_and_execution_must_agree_partial(self):
        with self.assertRaises(ValueError):
            TaskAssessment(
                difficulty=2,
                difficulty_label="Easy",
                length=LengthLevel.SHORT,
                length_note="x",
                offload=OffloadDecision.PARTIAL,
                offload_note="x",
                execution=Execution.LOCAL_HANDOFF,
            )

    def test_offload_and_execution_must_agree_no(self):
        with self.assertRaises(ValueError):
            TaskAssessment(
                difficulty=2,
                difficulty_label="Easy",
                length=LengthLevel.SHORT,
                length_note="x",
                offload=OffloadDecision.NO,
                offload_note="x",
                execution=Execution.LOCAL_HANDOFF,
            )

    def test_difficulty_must_be_in_range(self):
        for bad in (0, 6, -1):
            with self.assertRaises(ValueError):
                TaskAssessment(
                    difficulty=bad,
                    difficulty_label="Easy",
                    length=LengthLevel.SHORT,
                    length_note="x",
                    offload=OffloadDecision.YES,
                    offload_note="x",
                    execution=Execution.LOCAL_HANDOFF,
                )

    def test_invalid_length_rejected(self):
        with self.assertRaises(ValueError):
            TaskAssessment(
                difficulty=2,
                difficulty_label="Easy",
                length="Gigantic",
                length_note="x",
                offload=OffloadDecision.YES,
                offload_note="x",
                execution=Execution.LOCAL_HANDOFF,
            )


class TestHandoffRequest(unittest.TestCase):
    def _ctx(self):
        return RepositoryContext(
            summary="single module",
            allowed_files=["a.py"],
            read_only_files=["b.py"],
        )

    def test_defaults(self):
        req = HandoffRequest(
            task="Add helper",
            objective="parse an int",
            difficulty=2,
            length=LengthLevel.SHORT,
            repository_context=self._ctx(),
        )
        self.assertTrue(req.request_id)
        # request_id is a valid uuid
        from uuid import UUID
        UUID(req.request_id)
        self.assertIsInstance(req.created_at, datetime)
        self.assertIsNotNone(req.created_at.tzinfo)

    def test_default_output_contract(self):
        req = HandoffRequest(
            task="t",
            objective="o",
            difficulty=1,
            length=LengthLevel.SHORT,
            repository_context=self._ctx(),
        )
        self.assertEqual(req.output_contract.format.value, "unified_diff")
        self.assertTrue(req.output_contract.include_summary)


class TestWorkerResult(unittest.TestCase):
    def test_completed_valid(self):
        wr = WorkerResult(state="completed", summary="ok", diff="x")
        self.assertEqual(wr.state.value, "completed")

    def test_needs_context_requires_context_needed(self):
        with self.assertRaises(ValueError):
            WorkerResult(state="needs_context")
        wr = WorkerResult(state="needs_context", context_needed="missing file A")
        self.assertEqual(wr.state.value, "needs_context")

    def test_declined_requires_decline_reason(self):
        with self.assertRaises(ValueError):
            WorkerResult(state="declined")
        wr = WorkerResult(state="declined", decline_reason="too large")
        self.assertEqual(wr.state.value, "declined")

    def test_failed_requires_error(self):
        with self.assertRaises(ValueError):
            WorkerResult(state="failed")
        wr = WorkerResult(state="failed", error="timeout")
        self.assertEqual(wr.state.value, "failed")

    def test_worker_result_fields_default(self):
        wr = WorkerResult(state="completed")
        self.assertEqual(wr.assumptions, [])
        self.assertEqual(wr.uncertainties, [])
        self.assertEqual(wr.tests_run, [])


class TestValidationOutcome(unittest.TestCase):
    def test_valid(self):
        vo = ValidationOutcome(
            outcome=ValidationOutcomeKind.ACCEPTED_UNCHANGED,
            files_changed=["a.py"],
            unexpected_changes=[],
            tests_passed=True,
        )
        self.assertEqual(vo.outcome.value, "accepted unchanged")
        self.assertTrue(vo.applied is False or vo.applied is True)  # just a bool


class TestModelMetadata(unittest.TestCase):
    def test_valid(self):
        mm = ModelMetadata(id="x", display_name="X", loaded=True, backend="llama-server")
        self.assertEqual(mm.id, "x")
        self.assertTrue(mm.loaded)


class TestOffloadResult(unittest.TestCase):
    def test_valid(self):
        wr = WorkerResult(state="completed", summary="ok")
        vo = ValidationOutcome(outcome=ValidationOutcomeKind.ACCEPTED_UNCHANGED)
        orr = OffloadResult(
            request_id="r1",
            worker_result=wr,
            validation=vo,
            decision=ValidationOutcomeKind.ACCEPTED_UNCHANGED,
        )
        self.assertEqual(orr.request_id, "r1")
        self.assertEqual(orr.worker_result.state.value, "completed")


class TestRoutingDecision(unittest.TestCase):
    def test_valid_full_local(self):
        req = HandoffRequest(
            task="t",
            objective="o",
            difficulty=2,
            length=LengthLevel.SHORT,
            repository_context=RepositoryContext(summary="x"),
        )
        rd = RoutingDecision(
            kind=RoutingDecisionKind.FULL_LOCAL,
            model_alias="ternary-bonsai-2-27b",
            reason="small safe task",
            subrequests=[req],
        )
        self.assertEqual(rd.kind.value, "full_local")
        self.assertEqual(len(rd.subrequests), 1)

    def test_valid_keep_in_codex(self):
        rd = RoutingDecision(
            kind=RoutingDecisionKind.KEEP_IN_CODEX,
            reason="architecture-heavy",
        )
        self.assertEqual(rd.kind.value, "keep_in_codex")
        self.assertEqual(rd.subrequests, [])


if __name__ == "__main__":
    unittest.main()
