"""Tests for structured, log-safe diagnostics (bridge.diagnostics, Phase 8).

No live Unsloth instance required; the emitter only builds and logs the
non-secret metadata template from handoff.md section 12.
"""
import unittest

from bridge.schemas import HandoffRequest, LengthLevel, RepositoryContext, ResultState, WorkerResult
from bridge.validate import PatchValidator
from bridge.diagnostics import log_handoff


def _request(allowed=None, read_only=None):
    return HandoffRequest(
        task="Add helper",
        objective="parse an int",
        difficulty=2,
        length=LengthLevel.SHORT,
        repository_context=RepositoryContext(
            summary="single module",
            allowed_files=list(allowed or []),
            read_only_files=list(read_only or []),
        ),
    )


def _worker_result(state, diff="", **fields):
    return WorkerResult(state=state, diff=diff, **fields)


class TestLogHandoff(unittest.TestCase):
    def test_in_scope_completed_logs_accepted(self):
        req = _request(allowed=["src/foo.py"])
        res = _worker_result(
            ResultState.COMPLETED,
            diff="--- a/src/foo.py\n+++ b/src/foo.py\n@@ -1,1 +1,2 @@\n def foo():\n+    pass\n",
        )
        val = PatchValidator().validate(res, req)
        line = log_handoff(req, res, val, model_alias="Ternary Bonsai 2 27B")
        self.assertIn("Result state: completed", line)
        self.assertIn("Fallback/escalation: none", line)
        self.assertIn("src/foo.py", line)
        self.assertIn("Validation result:", line)
        self.assertIn("Ternary Bonsai 2 27B", line)

    def test_unauthorized_patch_logs_escalation(self):
        req = _request(allowed=["src/foo.py"])
        res = _worker_result(
            ResultState.COMPLETED,
            diff="--- a/src/bar.py\n+++ b/src/bar.py\n@@ -0,0 +1,1 @@\n+x\n",
        )
        val = PatchValidator().validate(res, req)
        line = log_handoff(req, res, val)
        self.assertIn("Fallback/escalation: escalate to Codex", line)
        self.assertIn("unexpected files", line)

    def test_needs_context_logs_continue_in_codex(self):
        req = _request()
        res = _worker_result(ResultState.NEEDS_CONTEXT, context_needed="file A.py")
        line = log_handoff(req, res)
        self.assertIn("Result state: needs_context", line)
        self.assertIn("Fallback/escalation: continue in Codex", line)
        self.assertNotIn("Validation result:", line)

    def test_truncates_long_reason(self):
        req = _request()
        res = _worker_result(ResultState.COMPLETED)
        long_reason = "reason " + "x" * 500
        line = log_handoff(req, res, reason=long_reason)
        self.assertNotIn("x" * 500, line)
        self.assertIn("Reason for offload:", line)

    def test_diff_body_never_logged(self):
        req = _request(allowed=["secret/key.txt"])
        res = _worker_result(
            ResultState.COMPLETED,
            diff="--- a/secret/key.txt\n+++ b/secret/key.txt\n@@ -0,0 +1,1 @@\n+pass=x\n",
        )
        val = PatchValidator().validate(res, req)
        line = log_handoff(req, res, val)
        # Only the path is logged; the diff body is never embedded.
        self.assertNotIn("pass=x", line)
        self.assertIn("secret/key.txt", line)

    def test_returns_formatted_template(self):
        req = _request(allowed=["a.py"])
        res = _worker_result(ResultState.COMPLETED)
        line = log_handoff(req, res)
        self.assertIn("Request ID:", line)
        self.assertIn("Timestamp:", line)
        self.assertIn("Difficulty: 2", line)
        self.assertIn("Length: Short", line)
        self.assertIn("Files supplied:", line)
        self.assertIn("Files allowed to change:", line)


if __name__ == "__main__":
    unittest.main()
