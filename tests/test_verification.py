"""Tests for bridge.verification (Phase 8 independent verification).

Uses temporary Git repositories so the tests never depend on the developer\'s
actual repository state. No live Unsloth instance is required.
"""
import io
import json
import os
from contextlib import redirect_stdout
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from bridge.schemas import (HandoffRequest, LengthLevel, RepositoryContext,
                            ValidationOutcomeKind, WorkerResult)
from bridge.transport import MockTransport
from bridge.offload import EXIT_VALIDATION_FAILED, run_handoff
from bridge.verification import verify

FOO = "def value():\n    return 0\n"
TEST_FOO = (
    "import unittest\n"
    "\n"
    "from foo import value\n"
    "\n"
    "\nclass T(unittest.TestCase):\n"
    "    def test_zero(self):\n"
    "        self.assertEqual(value(), 0)\n"
)
TEST_FOO_GENERATES = (
    "import unittest\n"
    "\n"
    "from foo import value\n"
    "\n"
    "\nclass T(unittest.TestCase):\n"
    "    def test_zero(self):\n"
    "        self.assertEqual(value(), 0)\n"
    "    def test_generate(self):\n"
    "        open(\'generated.txt\', \'w\').write(\'x\')\n"
)
FOO_OTHER = "def value():\n    return 0\n\ndef other():\n    return 42\n"
class BaseVerificationTest(unittest.TestCase):
    def setUp(self):
        self._repos = []

    def tearDown(self):
        for r in self._repos:
            shutil.rmtree(r, ignore_errors=True)

    def _git(self, args, cwd, inp=None):
        p = subprocess.run(["git"] + list(args), cwd=cwd, input=inp,
                       capture_output=True, text=True)
        return p.returncode, p.stdout, p.stderr

    """Creates a throwaway Git repo with a tiny module + its test."""

    def _make_repo(self, foo=FOO, test_foo=TEST_FOO):
        root = Path(tempfile.mkdtemp(prefix="verifyrepo-"))
        (root / "tests").mkdir()
        (root / "foo.py").write_text(foo, encoding="utf-8")
        (root / "tests/__init__.py").write_text("", encoding="utf-8")
        (root / "tests/test_foo.py").write_text(test_foo, encoding="utf-8")
        (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        self._git(["init", "-b", "main"], root)
        self._git(["config", "user.name", "t"], root)
        self._git(["config", "user.email", "t@t.t"], root)
        self._git(["add", "-A"], root)
        self._repos.append(root)
        self._git(["commit", "-m", "init"], root, inp="x\n")
        return root

    def _diff_after(self, root, changes):
        for path, content in changes.items():
            (root / path).write_text(content, encoding="utf-8")
        rc, out, _ = self._git(["diff"], root)
        self.assertEqual(rc, 0)
        return out

    def _worker(self, diff):
        return WorkerResult(state="completed", summary="ok", diff=diff)

    def _request(self, allowed):
        return HandoffRequest(
            task="t", objective="o", difficulty=2, length=LengthLevel.SHORT,
            repository_context=RepositoryContext(
                summary="single module", allowed_files=list(allowed),
                read_only_files=[]),
        )

    def _packet_json(self, allowed):
        return json.dumps({
            "task": "t", "objective": "o", "difficulty": 2, "length": "Short",
            "repository_context": {"summary": "single module",
                                   "allowed_files": allowed},
        })
class TestCleanCandidate(BaseVerificationTest):
    def test_accepted_unchanged(self):
        root = self._make_repo()
        diff = self._diff_after(root, {
            "foo.py": "def value():\n    return 1\n",
            "tests/test_foo.py": TEST_FOO.replace("value(), 0", "value(), 1"),
        })
        res = verify(self._worker(diff),
                     self._request(["foo.py", "tests/test_foo.py"]), repo_root=root)
        self.assertEqual(res.outcome, ValidationOutcomeKind.ACCEPTED_UNCHANGED)
        self.assertTrue(res.applied)
        self.assertTrue(res.tests_passed)
        self.assertEqual(res.unexpected_changes, [])
        self.assertEqual(res.lint_run, False)
        self.assertEqual(res.type_checks_run, False)


class TestFailingCandidate(BaseVerificationTest):
    def test_rejected_when_tests_fail(self):
        root = self._make_repo()
        # Change the module but leave the test expecting the old value.
        diff = self._diff_after(root, {"foo.py": "def value():\n    return 1\n"})
        res = verify(self._worker(diff), self._request(["foo.py"]), repo_root=root)
        self.assertEqual(res.outcome, ValidationOutcomeKind.REJECTED)
        self.assertTrue(res.applied)
        self.assertFalse(res.tests_passed)
        self.assertIn("unit tests FAILED", res.notes)
class TestUnapplicableCandidate(BaseVerificationTest):
    def test_rejected_when_preflight_fails(self):
        root = self._make_repo()
        # Old content that does not match the repository.
        diff = ("--- a/foo.py\n"
                "+++ b/foo.py\n"
                "@@ -1,2 +1,2 @@\n"
                " def value():\n"
                "-    return 999\n"
                "+    return 1\n")
        res = verify(self._worker(diff), self._request(["foo.py"]), repo_root=root)
        self.assertEqual(res.outcome, ValidationOutcomeKind.REJECTED)
        self.assertFalse(res.applied)
        self.assertFalse(res.tests_passed)
        self.assertIn("preflight", res.notes.lower())


class TestUnexpectedChange(BaseVerificationTest):
    def test_rejected_on_unexpected_tracked_change(self):
        root = self._make_repo(test_foo=TEST_FOO_GENERATES)
        # A benign in-scope edit; the test suite itself creates a new file.
        diff = self._diff_after(root, {"foo.py": FOO_OTHER})
        res = verify(self._worker(diff), self._request(["foo.py"]), repo_root=root)
        self.assertEqual(res.outcome, ValidationOutcomeKind.REJECTED)
        self.assertTrue(res.tests_passed)
        self.assertIn("generated.txt", res.unexpected_changes)
        self.assertIn("unexpected tracked changes", res.notes)
class TestCleanup(BaseVerificationTest):
    def test_worktree_removed_after_validation(self):
        root = self._make_repo()
        diff = self._diff_after(root, {
            "foo.py": "def value():\n    return 1\n",
            "tests/test_foo.py": TEST_FOO.replace("value(), 0", "value(), 1"),
        })
        verify(self._worker(diff),
               self._request(["foo.py", "tests/test_foo.py"]), repo_root=root)
        rc, out, _ = self._git(["worktree", "list"], root)
        self.assertEqual(rc, 0)
        lines = [l for l in out.splitlines() if l.strip()]
        # Only the primary worktree remains; the review worktree was cleaned.
        self.assertEqual(len(lines), 1)


class TestScopeGateFirst(BaseVerificationTest):
    def test_scope_failure_bypasses_verification(self):
        root = self._make_repo()
        diff = self._diff_after(root, {"foo.py": "def value():\n    return 1\n"})
        packet = self._packet_json(["other.py"])  # foo.py not allowed -> scope fail
        mt = MockTransport(responses={"*": {"state": "completed",
                                            "summary": "x", "diff": diff}})
        with patch("bridge.offload.verify") as m_verify:
            with redirect_stdout(io.StringIO()):
                code = run_handoff(mt, packet)
        self.assertEqual(code, EXIT_VALIDATION_FAILED)
        m_verify.assert_not_called()
class TestBoundedValidation(BaseVerificationTest):
    def test_validation_failure_is_bounded_no_loop(self):
        root = self._make_repo()
        # A completed result whose diff fails independent validation.
        diff = self._diff_after(root, {"foo.py": "def value():\n    return 1\n"})
        packet = self._packet_json(["foo.py"])
        mt = MockTransport(responses={"*": {"state": "completed",
                                            "summary": "x", "diff": diff}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, packet)
        self.assertEqual(code, EXIT_VALIDATION_FAILED)
        # Bounded: the transport is called once (no retry/verification loop).
        self.assertEqual(len(mt.sent), 1)
        data = json.loads(buf.getvalue())
        self.assertIn("verification", data)
        self.assertEqual(
            data["verification"]["outcome"], "rejected and reimplemented by Codex")
