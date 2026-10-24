"""Tests for patch scope validation (bridge.validate, Phase 7).

Pure string parsing; no live Unsloth instance required.
"""

import unittest

from bridge.schemas import (
    HandoffRequest,
    LengthLevel,
    PatchValidationResult,
    RepositoryContext,
    ResultState,
    WorkerResult,
)
from bridge.validate import PatchValidator


V = PatchValidator()


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


def _worker_result(diff, state=ResultState.COMPLETED):
    return WorkerResult(state=state, diff=diff)


class TestPatchValidator(unittest.TestCase):
    def test_within_scope_is_valid(self):
        d = """--- a/src/foo.py
+++ b/src/foo.py
@@ -1,1 +1,2 @@
 def foo():
+    pass
"""
        r = V.validate_diff(d, ["src/foo.py"], [])
        self.assertTrue(r.valid)
        self.assertEqual(r.files_changed, ["src/foo.py"])
        self.assertEqual(r.notes,
            "Patch within allowed scope; no flagged change type "
            "(dependency/config/binary/deletion/traversal).",
        )

    def test_non_allowed_file_rejected(self):
        d = """--- a/src/bar.py
+++ b/src/bar.py
@@ -0,0 +1,1 @@
+x
"""
        r = V.validate_diff(d, ["src/foo.py"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.unexpected_changes, ["src/bar.py"])

    def test_dotdot_traversal_rejected(self):
        d = """--- a/../../secret/key
+++ b/../../secret/key
@@ -0,0 +1,1 @@
+x
"""
        r = V.validate_diff(d, ["secret/key"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.path_traversals, ["../../secret/key"])

    def test_absolute_path_rejected(self):
        d = """--- /etc/passwd
+++ /etc/passwd
@@ -1,1 +1,2 @@
root:x:0:0:root:/root:/bin/bash
+evil:x:9999:9999:evil:/etc/evil:/bin/sh
"""
        r = V.validate_diff(d, ["/etc/passwd"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.path_traversals, ["/etc/passwd"])

    def test_windows_absolute_rejected(self):
        d = r"""--- C:\Windows\secret
+++ b/C:\Windows\secret
@@ -0,0 +1,1 @@
+x
"""
        r = V.validate_diff(d, [r'C:\Windows\secret'], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.path_traversals, ["C:/Windows/secret"])

    def test_dependency_change_requires_review(self):
        d = """--- a/requirements.txt
+++ b/requirements.txt
@@ -1,1 +1,2 @@
flask==2.0
+requests==2.31
"""
        r = V.validate_diff(d, ["requirements.txt"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.dependency_changes, ["requirements.txt"])
        self.assertIn("Codex review required", r.notes)

    def test_dependency_lock_file(self):
        d = """--- a/package-lock.json
+++ b/package-lock.json
@@ -1,1 +1,2 @@
"a": 1,
"b": 2
"""
        r = V.validate_diff(d, ["package-lock.json"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.dependency_changes, ["package-lock.json"])

    def test_dependency_requirements_extra(self):
        d = """--- a/requirements-dev.txt
+++ b/requirements-dev.txt
@@ -0,0 +1,1 @@
+x
"""
        r = V.validate_diff(d, ["requirements-dev.txt"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.dependency_changes, ["requirements-dev.txt"])

    def test_config_env_change(self):
        d = """--- a/.env
+++ b/.env
@@ -1,1 +1,2 @@
DB=postgres
+SECRET=x
"""
        r = V.validate_diff(d, [".env"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.config_changes, [".env"])

    def test_config_ini_change(self):
        d = """--- a/myconfig.ini
+++ b/myconfig.ini
@@ -1,1 +1,2 @@
a=1
+b=2
"""
        r = V.validate_diff(d, ["myconfig.ini"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.config_changes, ["myconfig.ini"])


    def test_binary_extension_rejected(self):
        d = """--- a/img/logo.png
+++ b/img/logo.png
Binary files a/img/logo.png and b/img/logo.png differ
"""
        r = V.validate_diff(d, ["img/logo.png"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.binary_changes, ["img/logo.png"])

    def test_binary_marker_in_body(self):
        d = """--- a/img/photo.jpg
+++ b/img/photo.jpg
Binary files a/img/photo.jpg and b/img/photo.jpg differ
"""
        r = V.validate_diff(d, ["img/photo.jpg"], [])
        self.assertFalse(r.valid)
        self.assertIn("img/photo.jpg", r.binary_changes)

    def test_deletion_flagged(self):
        d = """--- a/src/legacy.py
+++ /dev/null
@@ -1,2 +0,0 @@
-old
-old2
"""
        r = V.validate_diff(d, ["src/legacy.py"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.deletions, ["src/legacy.py"])
        self.assertEqual(r.files_changed, ["src/legacy.py"])

    def test_read_only_change_rejected(self):
        d = """--- a/config.yaml
+++ b/config.yaml
@@ -1,1 +1,2 @@
a: 1
+b: 2
"""
        r = V.validate_diff(d, [], ["config.yaml"])
        self.assertFalse(r.valid)
        self.assertEqual(r.read_only_changes, ["config.yaml"])

    def test_malformed_diff_no_headers(self):
        r = V.validate_diff("This is prose, not a diff at all.", ["src/foo.py"], [])
        self.assertFalse(r.valid)
        self.assertIsNotNone(r.parse_error)
        self.assertIn("diff file headers", r.notes)

    def test_empty_diff_valid(self):
        r = V.validate_diff("", ["src/foo.py"], [])
        self.assertTrue(r.valid)
        self.assertEqual(r.files_changed, [])
        self.assertEqual(r.notes, "No file changes in patch.")

    def test_added_file_in_scope(self):
        d = """--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,1 @@
+x
"""
        r = V.validate_diff(d, ["src/new.py"], [])
        self.assertTrue(r.valid)
        self.assertEqual(r.files_changed, ["src/new.py"])

    def test_added_file_out_of_scope(self):
        d = """--- /dev/null
+++ b/src/new.py
@@ -0,0 +1,1 @@
+x
"""
        r = V.validate_diff(d, ["src/other.py"], [])
        self.assertFalse(r.valid)
        self.assertEqual(r.unexpected_changes, ["src/new.py"])

    def test_rename_new_path_checked(self):
        d = """rename a/old.py b/new.py
--- a/old.py
+++ b/new.py
@@ -1,1 +1,1 @@
x
"""
        r = V.validate_diff(d, ["new.py"], [])
        self.assertTrue(r.valid)
        self.assertEqual(r.files_changed, ["new.py"])

    def test_multiple_violations(self):
        d = """--- a/../../etc/passwd
+++ b/../../etc/passwd
@@ -0,0 +1,1 @@
+x
"""
        r = V.validate_diff(d, [])
        self.assertFalse(r.valid)
        self.assertTrue(r.path_traversals)
        self.assertTrue(r.unexpected_changes)

    def test_normalize_dot_slash(self):
        d = """--- a/./src/foo.py
+++ b/./src/foo.py
@@ -1,1 +1,2 @@
a
+b
"""
        r = V.validate_diff(d, ["src/foo.py"], [])
        self.assertTrue(r.valid)
        self.assertEqual(r.files_changed, ["src/foo.py"])

    def test_normalize_backslash(self):
        d = r"""--- a/src\foo.py
+++ b/src\foo.py
@@ -1,1 +1,2 @@
a
+b
"""
        r = V.validate_diff(d, ["src/foo.py"], [])
        self.assertTrue(r.valid)
        self.assertEqual(r.files_changed, ["src/foo.py"])

    def test_files_changed_deduped(self):
        d = """--- a/src/foo.py
+++ b/src/foo.py
@@ -1,1 +1,1 @@
a
"""
        r = V.validate_diff(d * 2, ["src/foo.py"], [])
        self.assertEqual(r.files_changed, ["src/foo.py"])

    def test_validate_convenience_method(self):
        d = """--- a/src/foo.py
+++ b/src/foo.py
@@ -1,1 +1,2 @@
 def foo():
+    pass
"""
        wr = _worker_result(d)
        req = _request(["src/foo.py"])
        r = V.validate(wr, req)
        self.assertTrue(r.valid)
        self.assertIsInstance(r, PatchValidationResult)

    def test_validate_convenience_read_only(self):
        d = """--- a/config.yaml
+++ b/config.yaml
@@ -1,1 +1,2 @@
a: 1
+b: 2
"""
        wr = _worker_result(d)
        req = _request([], ["config.yaml"])
        r = V.validate(wr, req)
        self.assertFalse(r.valid)
        self.assertEqual(r.read_only_changes, ["config.yaml"])

    def test_notes_informative(self):
        d = """--- a/requirements.txt
+++ b/requirements.txt
@@ -1,1 +1,2 @@
flask==2.0
+requests==2.31
"""
        r = V.validate_diff(d, ["requirements.txt"], [])
        self.assertFalse(r.valid)
        self.assertIn("dependency changes", r.notes.lower())
        self.assertIn("requirements.txt", r.notes)


if __name__ == "__main__":
    unittest.main()
