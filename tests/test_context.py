"""Tests for the Phase 5 context packager and secret guard (bridge.context).

Covers the Phase 5 acceptance criteria:

* Secret fixtures are blocked (excluded-file/glob policy + inline redaction).
* Unrelated repository files are never included by default.
* Context overflow (required context over budget) is handled as
  ``needs_scope_reduction`` before any model invocation.
* All paths in the packet are relative/project-scoped; traversal and
  out-of-root absolute paths are blocked.

No live Unsloth instance is required; the packager is pure except for reading
files from a temp project root.
"""

import os
import shutil
import tempfile
import unittest

from bridge.config import OffloaderConfig
from bridge.context import ContextPackager, estimate_tokens
from bridge.schemas import PackageStatus


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project():
    """Create a temp project root with a handful of files. Returns the path."""
    root = tempfile.TemporaryDirectory().name
    os.makedirs(os.path.join(root, "src"), exist_ok=True)
    os.makedirs(os.path.join(root, "credentials"), exist_ok=True)
    os.makedirs(os.path.join(root, "secrets"), exist_ok=True)
    os.makedirs(os.path.join(root, ".aws"), exist_ok=True)

    def write(rel, content):
        path = os.path.join(root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)

    # A normal, non-secret source file.
    write("src/main.py",
          "def add(a, b):\n    return a + b\n\nif __name__ == '__main__':\n    print(add(1, 2))\n")
    write("src/util.py",
          "def helper(x):\n    return x * 2\n")
    # Unrelated files that must NOT be included by default.
    write("docs/README.md", "# Project\nThis is a big unrelated doc."
              * 50)
    write("vendor/biglib.py",
          "# generated\n" + "# filler line " + "x" * 80 + "\n" * 200)
    # Secret-bearing files (must be blocked by the glob policy).
    write(".env", "API_KEY=sk-abc123secrettoken\nDB_PASSWORD=hunter2\n")
    write("credentials/api_key.txt", "sk-abc123secrettoken\n")
    write("credentials/private_key",
          "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAG..\n-----END OPENSSH PRIVATE KEY-----\n")
    write("secrets/token", "ghp_abcdefghijklmnopqrstuvwxyz012345\n")
    write(".aws/credentials",
          "[default]\naws_access_key_id=AKIAIOSFODNN7EXAMPLE\naws_secret_access_key=secret\n")
    return root


class TestContextPackager(unittest.TestCase):
    def setUp(self):
        self.root = _make_project()
        self.config = OffloaderConfig(context_budget_tokens=16384)
        self.packager = ContextPackager(self.config, self.root)

    def tearDown(self):
        shutil.rmtree(self.root)

    # -- 1. secret fixtures are blocked --

    def test_secret_files_blocked_by_globs(self):
        """Files matching config.excluded_files are blocked, never read."""
        pkg = self.packager.package({
            "allowed_files": [".env", "credentials/api_key.txt",
                               "secrets/token", ".aws/credentials"],
        })
        self.assertEqual(len(pkg.blocked), 4)
        blocked_paths = {b.path for b in pkg.blocked}
        self.assertEqual(blocked_paths,
                         {".env", "credentials/api_key.txt",
                          "secrets/token", ".aws/credentials"})
        # Blocked files are not read into the packet.
        for b in pkg.blocked:
            self.assertIn("excluded", b.reason)
        # Nothing secret is in the relevant snippets.
        self.assertEqual(pkg.repository_context.allowed_files, [])
        self.assertEqual(pkg.repository_context.relevant_snippets, [])

    def test_secret_globs_match_deep_paths(self):
        """Glob patterns match at any depth (e.g. **/id_rsa)."""
        pkg = self.packager.package({
            "allowed_files": ["nested/.ssh/id_rsa"],
        })
        self.assertEqual(len(pkg.blocked), 1)
        self.assertEqual(pkg.blocked[0].path, "nested/.ssh/id_rsa")
        self.assertIn("excluded", pkg.blocked[0].reason)

    def test_inline_secrets_redacted_not_blocked(self):
        """Secrets inside a *normal* file are redacted from content (not the
        whole file blocked) and recorded in redacted_paths."""
        self.packager.config.excluded_files = []  # no glob policy for this case
        # A normal (non-excluded) file that contains inline secrets.
        with open(os.path.join(self.root, "src", "config.py"), "w") as f:
            f.write("API_KEY=sk-abc123secrettoken\nDB_PASSWORD=hunter2\n")
        pkg = self.packager.package({
            "allowed_files": ["src/config.py"],
            "summary": "repo summary api_key=inline-secret-token",
        })
        snips = pkg.repository_context.relevant_snippets
        self.assertEqual(len(snips), 1)
        self.assertNotIn("sk-abc123secrettoken", snips[0].content)
        self.assertNotIn("hunter2", snips[0].content)
        self.assertIn("[REDACTED]", snips[0].content)
        self.assertNotIn("inline-secret-token", pkg.repository_context.summary)
        self.assertIn("src/config.py", pkg.redacted_paths)

    def test_private_key_redacted(self):
        self.packager.config.excluded_files = []
        path = os.path.join(self.root, "src/keys.py")
        with open(path, "w") as f:
            f.write("key = '''-----BEGIN RSA PRIVATE KEY-----\nAAAAG..\n" 
                      "-----END RSA PRIVATE KEY-----'''")
        pkg = self.packager.package({"allowed_files": ["src/keys.py"]})
        snips = pkg.repository_context.relevant_snippets
        self.assertEqual(len(snips), 1)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", snips[0].content)
        self.assertIn("[REDACTED_PRIVATE_KEY]", snips[0].content)

    # -- 2. unrelated repository files not included by default --

    def test_only_allowed_files_included(self):
        """Unrelated repo files are never pulled in just because they exist."""
        pkg = self.packager.package({
            "allowed_files": ["src/main.py"],
        })
        rel = pkg.repository_context.relevant_snippets
        self.assertEqual([s.path for s in rel], ["src/main.py"])
        self.assertEqual(pkg.repository_context.allowed_files, ["src/main.py"])
        # Unrelated files are absent.
        for s in rel:
            self.assertNotIn("docs/README.md", s.path)
            self.assertNotIn("vendor/biglib.py", s.path)
        self.assertEqual(pkg.blocked, [])

    def test_read_only_files_are_optional_and_separate(self):
        pkg = self.packager.package({
            "allowed_files": ["src/main.py"],
            "read_only_files": ["src/util.py"],
        })
        paths = {s.path for s in pkg.repository_context.relevant_snippets}
        self.assertEqual(paths, {"src/main.py", "src/util.py"})
        self.assertEqual(pkg.repository_context.allowed_files, ["src/main.py"])
        self.assertEqual(pkg.repository_context.read_only_files, ["src/util.py"])

    def test_read_only_files_not_changable(self):
        pkg = self.packager.package({
            "allowed_files": [],
            "read_only_files": ["src/util.py"],
        })
        self.assertEqual(pkg.repository_context.allowed_files, [])
        self.assertEqual(pkg.repository_context.read_only_files, ["src/util.py"])

    # -- 3. context overflow handled before model invocation --

    def test_required_context_overflow_is_needs_scope_reduction(self):
        """Required (allowed) context over the budget -> needs_scope_reduction,
        handled before any model call."""
        # A big required file.
        big = "x" * (50000)
        big_path = os.path.join(self.root, "src", "huge.py")
        with open(big_path, "w") as f:
            f.write(big)
        cfg = OffloaderConfig(context_budget_tokens=1000)
        pack = ContextPackager(cfg, self.root)
        pkg = pack.package({"allowed_files": ["src/huge.py"]})
        self.assertEqual(pkg.status, PackageStatus.NEEDS_SCOPE_REDUCTION)
        self.assertGreater(pkg.tokens_used, pkg.budget_tokens)
        self.assertIn("needs scope reduction",
                      pkg.notes[0] if pkg.notes else "")

    def test_optional_context_truncated_to_fit(self):
        """Required fits, optional (read-only / snippets) trimmed to budget ->
        REDUCED."""
        # Required small file.
        small = "y" * 400
        with open(os.path.join(self.root, "src", "small.py"), "w") as f:
            f.write(small)
        # A very large optional read-only file (7500 tokens > budget). Budget
        # is 16384 tokens; 80000 chars ~= 20000 tokens, which cannot fit.
        big = "z" * (80000)
        with open(os.path.join(self.root, "src", "big_option.py"), "w") as f:
            f.write(big)
        cfg = OffloaderConfig(context_budget_tokens=16384)
        pack = ContextPackager(cfg, self.root)
        pkg = pack.package({
            "allowed_files": ["src/small.py"],
            "read_only_files": ["src/big_option.py"],
        })
        self.assertEqual(pkg.status, PackageStatus.REDUCED)
        # Required kept, optional omitted.
        paths = {s.path for s in pkg.repository_context.relevant_snippets}
        self.assertIn("src/small.py", paths)
        self.assertNotIn("src/big_option.py", paths)
        self.assertLess(pkg.tokens_used, pkg.budget_tokens)
        self.assertIn("skipped (too large for budget)", " ".join(pkg.notes))

    def test_all_optional_fits_is_ok(self):
        pkg = self.packager.package({
            "allowed_files": ["src/main.py"],
            "read_only_files": ["src/util.py"],
        })
        self.assertEqual(pkg.status, PackageStatus.OK)
        self.assertEqual(len(pkg.repository_context.relevant_snippets), 2)
        self.assertLessEqual(pkg.tokens_used, pkg.budget_tokens)

    def test_user_snippets_included_and_redacted(self):
        pkg = self.packager.package({
            "allowed_files": ["src/main.py"],
            "snippets": [{
                "path": "src/util.py",
                "content": "def h(x): return x + 1\napi_key=snipey\n",
                "note": "helper signature",
            }],
        })
        paths = {s.path for s in pkg.repository_context.relevant_snippets}
        self.assertIn("src/util.py", paths)
        # Inline secret in user snippet content is redacted.
        util = next(s for s in pkg.repository_context.relevant_snippets if s.path == "src/util.py")
        self.assertNotIn("snipey", util.content)
        self.assertIn("[REDACTED]", util.content)

    # -- 4. all paths relative / project-scoped --

    def test_relative_paths_stay_relative(self):
        pkg = self.packager.package({"allowed_files": ["src/main.py"]})
        self.assertEqual(pkg.repository_context.allowed_files, ["src/main.py"])

    def test_absolute_path_under_root_converted(self):
        abs_path = os.path.join(self.root, "src", "main.py")
        pkg = self.packager.package({"allowed_files": [abs_path]})
        self.assertEqual(pkg.repository_context.allowed_files, ["src/main.py"])
        self.assertEqual(len(pkg.blocked), 0)

    def test_path_traversal_blocked(self):
        pkg = self.packager.package({
            "allowed_files": ["src/../.env"],
        })
        self.assertEqual(len(pkg.blocked), 1)
        self.assertIn("traversal", pkg.blocked[0].reason.lower())

    def test_absolute_path_outside_root_blocked(self):
        other = os.path.join(self.root, "..", "sibling", "leak.py")
        pkg = self.packager.package({"allowed_files": [other]})
        self.assertEqual(len(pkg.blocked), 1)
        self.assertIn("outside project root",
                      pkg.blocked[0].reason)

    def test_all_packet_paths_project_scoped(self):
        """Every path anywhere in the packet is project-relative (no absolute,
        no ..)."""
        pkg = self.packager.package({
            "allowed_files": ["src/main.py"],
            "read_only_files": ["src/util.py"],
            "snippets": [{"path": "src/main.py", "content": "x"}],
        })
        all_paths = (pkg.repository_context.allowed_files
                     + pkg.repository_context.read_only_files
                     + [s.path for s in pkg.repository_context.relevant_snippets]
                     + [b.path for b in pkg.blocked])
        for p in all_paths:
            # Must not be absolute and must not contain a traversal component.
            self.assertFalse(os.path.isabs(p))
            self.assertNotIn("..", p.split("/"))

    # -- acceptance criteria + summary carried through --

    def test_acceptance_criteria_carried(self):
        pkg = self.packager.package({
            "allowed_files": ["src/main.py"],
            "acceptance_criteria": ["add() returns 3 for (1,2)"],
        })
        self.assertEqual(pkg.acceptance_criteria, ["add() returns 3 for (1,2)"])

    def test_summary_carried_and_redacted(self):
        pkg = self.packager.package({
            "allowed_files": ["src/main.py"],
            "summary": "small repo; api_key=leakedvalue",
        })
        self.assertIn("small repo", pkg.repository_context.summary)
        self.assertNotIn("leakedvalue", pkg.repository_context.summary)

    def test_repository_summary_default_empty(self):
        pkg = self.packager.package({"allowed_files": ["src/main.py"]})
        self.assertEqual(pkg.repository_context.summary, "")

    # -- budget bookkeeping --

    def test_tokens_used_and_budget_present(self):
        pkg = self.packager.package({"allowed_files": ["src/main.py"]})
        self.assertEqual(pkg.budget_tokens, 16384)
        self.assertGreaterEqual(pkg.tokens_used, 0)
        self.assertLessEqual(pkg.tokens_used, pkg.budget_tokens)

    def test_empty_scope_ok(self):
        pkg = self.packager.package({})
        self.assertEqual(pkg.status, PackageStatus.OK)
        self.assertEqual(pkg.repository_context.relevant_snippets, [])
        self.assertEqual(pkg.tokens_used, 0)

    def test_nonexistent_file_noted_not_blocked(self):
        pkg = self.packager.package({"allowed_files": ["src/does_not_exist.py"]})
        # A missing file is not a *blocked* path (that's for traversal/
        # out-of-root/excluded); it's simply skipped and noted.
        self.assertEqual(pkg.blocked, [])
        self.assertEqual(pkg.repository_context.relevant_snippets, [])
        self.assertIn("file not found", " ".join(pkg.notes))

    # -- estimate_tokens helper --

    def test_estimate_tokens(self):
        self.assertEqual(estimate_tokens(""), 0)
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("abcde"), 2)  # 5 chars -> ceil(5/4)=2
        self.assertEqual(estimate_tokens("x" * 16), 4)  # 16 chars -> exactly 4
        self.assertGreater(estimate_tokens("x" * 17), 4)


class TestGlobToRegex(unittest.TestCase):
    def setUp(self):
        self.root = _make_project()
        self.config = OffloaderConfig(context_budget_tokens=16384)

    def test_single_file_glob(self):
        pack = ContextPackager(self.config, self.root)
        self.assertTrue(any(rx.match(".env") for rx in pack._glob_re))

    def test_double_star_globs(self):
        pack = ContextPackager(self.config, self.root)
        self.assertTrue(any(rx.match("vendor/.ssh/id_rsa") for rx in pack._glob_re))


if __name__ == "__main__":
    unittest.main()
""
