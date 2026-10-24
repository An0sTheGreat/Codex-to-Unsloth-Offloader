"""Tests for the offload CLI (bridge.offload).

Uses the in-memory MockTransport, so no live Unsloth instance is required
(consistent with the rest of the suite).
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest.mock import patch

from bridge.schemas import (ResultState, ValidationOutcome,
                            ValidationOutcomeKind, WorkerResult)
from bridge.transport import MockTransport
from bridge.offload import (
    EXIT_COMPLETED,
    EXIT_INVALID_PACKET,
    EXIT_UNSLOTH,
    EXIT_WORKER_STATE,
    EXIT_VALIDATION_FAILED,
    main,
    run_handoff,
    run_smoke,
)


IN_SCOPE_DIFF = (
    "--- a/src/foo.py\n"
    "+++ b/src/foo.py\n"
    "@@ -1,1 +1,2 @@\n"
    " def foo():\n"
    "+    pass\n"
)

OUT_OF_SCOPE_DIFF = (
    "--- a/src/bar.py\n"
    "+++ b/src/bar.py\n"
    "@@ -0,0 +1,1 @@\n"
    "+x\n"
)

TRAVERSAL_DIFF = (
    "--- a/../../secret/key\n"
    "+++ b/../../secret/key\n"
    "@@ -0,0 +1,1 @@\n"
    "+x\n"
)

DEP_DIFF = (
    "--- a/requirements.txt\n"
    "+++ b/requirements.txt\n"
    "@@ -1,1 +1,2 @@\n"
    "flask==2.0\n"
    "+requests==2.31\n"
)

# A passing independent verification, used to isolate the scope-gate / retry
# orchestration tests from Phase 8's real verification (the actual verify()
# behaviour with temporary Git repositories lives in tests/test_verification.py).
PASSING_VERIFICATION = ValidationOutcome(
    outcome=ValidationOutcomeKind.ACCEPTED_UNCHANGED,
    files_changed=["src/foo.py"],
    unexpected_changes=[],
    tests_passed=True,
    applied=True,
    notes="verified",
)

class TestRunHandoff(unittest.TestCase):
    def _packet(self, allowed=None, read_only=None, **kw):
        return json.dumps({
            "task": "Add helper",
            "objective": "parse an int",
            "difficulty": 2,
            "length": "Short",
            "repository_context": {
                "summary": "single module",
                "allowed_files": list(allowed or []),
                "read_only_files": list(read_only or []),
            },
            **kw,
        })

    @patch("bridge.offload.verify")
    def test_completed_exit_zero(self, _verify):
        _verify.return_value = PASSING_VERIFICATION
        mt = MockTransport(responses={
            "*": {
                "state": "completed",
                "summary": "added helper",
                "diff": IN_SCOPE_DIFF,
            }
        })
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet(allowed=["src/foo.py"]))
        self.assertEqual(code, EXIT_COMPLETED)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["state"], "completed")
        self.assertTrue(data["validation"]["valid"])

    def test_needs_context_exit_one(self):
        mt = MockTransport(responses={"*": {"state": "needs_context",
                                           "context_needed": "file A.py"}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet())
        self.assertEqual(code, EXIT_WORKER_STATE)
        self.assertEqual(json.loads(buf.getvalue())["state"], "needs_context")
        self.assertNotIn("validation", json.loads(buf.getvalue()))

    def test_declined_exit_one(self):
        mt = MockTransport(responses={"*": {"state": "declined",
                                            "decline_reason": "too large"}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet())
        self.assertEqual(code, EXIT_WORKER_STATE)

    @patch("bridge.offload.verify")
    def test_validation_gate_accepts_in_scope_patch(self, _verify):
        _verify.return_value = PASSING_VERIFICATION
        mt = MockTransport(responses={"*": {
            "state": "completed", "summary": "added helper", "diff": IN_SCOPE_DIFF
        }})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet(allowed=["src/foo.py"]))
        self.assertEqual(code, EXIT_COMPLETED)
        data = json.loads(buf.getvalue())
        self.assertTrue(data["validation"]["valid"])
        self.assertEqual(data["validation"]["files_changed"], ["src/foo.py"])

    def test_validation_gate_rejects_unauthorized_file(self):
        mt = MockTransport(responses={"*": {
            "state": "completed", "summary": "added helper", "diff": OUT_OF_SCOPE_DIFF
        }})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet(allowed=["src/foo.py"]))
        self.assertEqual(code, EXIT_VALIDATION_FAILED)
        data = json.loads(buf.getvalue())
        self.assertFalse(data["validation"]["valid"])
        self.assertEqual(data["validation"]["unexpected_changes"], ["src/bar.py"])

    def test_validation_gate_rejects_traversal(self):
        mt = MockTransport(responses={"*": {
            "state": "completed", "summary": "evil", "diff": TRAVERSAL_DIFF
        }})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet(allowed=["secret/key"]))
        self.assertEqual(code, EXIT_VALIDATION_FAILED)
        data = json.loads(buf.getvalue())
        self.assertFalse(data["validation"]["valid"])
        self.assertEqual(data["validation"]["path_traversals"], ["../../secret/key"])

    def test_validation_gate_escalates_dependency_change(self):
        mt = MockTransport(responses={"*": {
            "state": "completed", "summary": "added dep", "diff": DEP_DIFF
        }})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet(allowed=["requirements.txt"]))
        self.assertEqual(code, EXIT_VALIDATION_FAILED)
        data = json.loads(buf.getvalue())
        self.assertFalse(data["validation"]["valid"])
        self.assertEqual(data["validation"]["dependency_changes"], ["requirements.txt"])
        self.assertIn("Codex review required", data["validation"]["notes"])

    def test_needs_context_retries_once_then_escalates(self):
        mt = MockTransport(responses={"*": {
            "state": "needs_context", "context_needed": "file A.py"
        }})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet(allowed=["src/foo.py"]))
        self.assertEqual(code, EXIT_WORKER_STATE)
        self.assertEqual(len(mt.sent), 2)
        self.assertEqual(json.loads(buf.getvalue())["state"], "needs_context")
        self.assertNotIn("validation", json.loads(buf.getvalue()))

    @patch("bridge.offload.verify")
    def test_needs_context_retry_yields_completed(self, _verify):
        _verify.return_value = PASSING_VERIFICATION
        class _Stateful(MockTransport):
            def __init__(self):
                super().__init__()
                self._resp = [
                    {"state": "needs_context", "context_needed": "file A.py"},
                    {"state": "completed",
                     "summary": "added helper after context",
                     "diff": IN_SCOPE_DIFF},
                ]
                self._i = 0

            def send(self, request):
                self.sent.append(request)
                canned = self._resp[self._i]
                self._i += 1
                return WorkerResult.model_validate(canned)

        mt = _Stateful()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet(allowed=["src/foo.py"]))
        self.assertEqual(code, EXIT_COMPLETED)
        self.assertEqual(len(mt.sent), 2)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["state"], "completed")
        self.assertTrue(data["validation"]["valid"])
        self.assertIn("file A.py", mt.sent[1].repository_context.summary)

    def test_invalid_packet_raises_value_error(self):
        mt = MockTransport()
        with self.assertRaises(ValueError):
            run_handoff(mt, "{ not json")

    def test_schema_mismatch_raises_value_error(self):
        mt = MockTransport()
        with self.assertRaises(ValueError):
            run_handoff(mt, '{"task": "x"}')  # missing required fields


class TestRunSmoke(unittest.TestCase):
    def test_completed_exit_zero(self):
        mt = MockTransport(capability=True,
                           model_id="tb-27b",
                           model_display="Ternary Bonsai 2 27B")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_smoke(mt)
        self.assertEqual(code, EXIT_COMPLETED)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["model"]["id"], "tb-27b")
        self.assertEqual(data["worker_result"]["state"], "completed")
        self.assertEqual(len(mt.sent), 1)

    def test_declined_exit_one(self):
        mt = MockTransport(responses={"*": {"state": "declined",
                                            "decline_reason": "too large"}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_smoke(mt)
        self.assertEqual(code, EXIT_WORKER_STATE)

    def test_capability_error_propagates(self):
        mt = MockTransport(capability=False)
        with self.assertRaises(Exception):
            run_smoke(mt)


class TestMain(unittest.TestCase):
    @contextmanager
    def _patch_transport(self, mt):
        with patch("bridge.offload.UnslothApiTransport",
                   side_effect=lambda cfg: mt):
            yield mt

    def test_main_smoke_success(self):
        mt = MockTransport(capability=True)
        buf = io.StringIO()
        with self._patch_transport(mt):
            with redirect_stdout(buf):
                code = main(["--smoke"])
        self.assertEqual(code, EXIT_COMPLETED)
        json.loads(buf.getvalue())

    def test_main_smoke_server_down(self):
        mt = MockTransport(capability=False)
        buf = io.StringIO()
        with self._patch_transport(mt):
            with redirect_stdout(buf):
                code = main(["--smoke"])
        self.assertEqual(code, EXIT_UNSLOTH)

    @patch("bridge.offload.verify")
    def test_main_packet_file(self, _verify):
        _verify.return_value = PASSING_VERIFICATION
        mt = MockTransport()
        packet = json.dumps({
            "task": "Add helper",
            "objective": "parse an int",
            "difficulty": 2,
            "length": "Short",
            "repository_context": {"summary": "single module",
                                   "allowed_files": ["Add helper"]},
        })
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(packet)
            path = f.name
        try:
            buf = io.StringIO()
            with self._patch_transport(mt):
                with redirect_stdout(buf):
                    code = main([path])
            self.assertEqual(code, EXIT_COMPLETED)
            self.assertEqual(json.loads(buf.getvalue())["state"], "completed")
        finally:
            os.unlink(path)

    def test_main_no_args_usage_error(self):
        with self._patch_transport(MockTransport()):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main([])

    def test_main_smoke_and_packet_conflict(self):
        with self._patch_transport(MockTransport()):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["--smoke", "{}"])


