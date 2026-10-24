"""Patch scope validation for delegated diffs (Phase 7).

Phase 7 adds the *boundary check* between the local model's returned patch and
Codex's accepted working tree. The local worker may only touch the files named
in the handoff packet's ``allowed_files`` (and may read but not change the
``read_only_files``). This module parses the returned unified diff, extracts
every changed path, and flags anything that violates the scope or carries a
change type that must not be auto-applied:

* a path outside ``allowed_files``/``read_only_files`` (unexpected change);
* a change to a ``read_only_files`` entry;
* path traversal (``..`` components, absolute paths);
* new/changed dependency or lock files;
* configuration changes (``.env`, ``config.*`, ...);
* binary files;
* file deletions.

The result is a machine-readable :class:`PatchValidationResult`. ``valid`` is the
single gate: True only when the patch is within scope and carries no flagged
change type. A False result must be rejected (hard violations) or escalated to
Codex for review (soft flags such as dependency/config changes or deletions).

This module is pure (string parsing only); it never touches the filesystem or
the network, so it satisfies the project invariant that tests run without a
live Unsloth instance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .schemas import HandoffRequest, PatchValidationResult, WorkerResult


_DEV_NULL = "/dev/null"

# Exact file names that indicate a dependency or dependency lock file.
_DEPENDENCY_NAMES = {
    "requirements.txt", "pyproject.toml", "setup.py", "setup.cfg",
    "package.json", "package-lock.json", "yarn.lock",
    "npm-shrinkwrap.json", "composer.json", "composer.lock",
    "go.mod", "go.sum", "Gemfile", "Gemfile.lock",
    "Pipfile", "Pipfile.lock", "pom.xml",
    "Cargo.toml", "Cargo.lock",
}

# Explicit configuration-file anchors (exact names).
_CONFIG_EXACT = {"Makefile", "docker-compose.yml", "docker-compose.yaml"}
_CONFIG_PREFIXES = ("config", "settings", "app")
_CONFIG_EXTS = (".ini", ".cfg", ".conf", ".yml", ".yaml", ".json", ".toml", ".xml")
_CONFIG_BARE_EXTS = (".ini", ".cfg", ".conf")

# Extensions treated as binary (text diffs cannot be safely auto-applied).
_BINARY_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".tiff", ".tif",
    ".mp3", ".wav", ".mp4", ".avi", ".mov", ".webm", ".ogg",
    ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".bin", ".dat",
    ".pdf", ".zip", ".tar", ".gz", ".7z", ".rar",
    ".psd", ".raw", ".db", ".sqlite", ".sqlite3",
}


@dataclass
class DiffFile:
    """One file block of a unified diff (internal)."""
    old_path: Optional[str] = None
    new_path: Optional[str] = None
    kind: str = "unknown"
    is_binary: bool = False
    body_lines: List[str] = field(default_factory=list)


class PatchValidator:
    """Parse a returned unified diff and validate it against the handoff scope."""

    def __init__(self, config=None):
        # ``config`` is accepted for API compatibility with the rest of the
        # bridge; the detection sets below are intentionally fixed.
        self.config = config

    # -- public API ----------------------------------------------------------

    def validate(self, worker_result: WorkerResult, request: HandoffRequest) -> PatchValidationResult:
        """Validate ``worker_result.diff`` against the scope in ``request``."""
        rc = request.repository_context
        return self.validate_diff(
            worker_result.diff,
            rc.allowed_files,
            rc.read_only_files,
        )

    def validate_diff(
        self,
        diff: str,
        allowed_files: Optional[Sequence[str]] = None,
        read_only_files: Optional[Sequence[str]] = None,
    ) -> PatchValidationResult:
        """Core validation: parse ``diff`` and check every changed path against
        ``allowed_files``/``read_only_files`` and the flagged change types."""
        allowed = {self._normalize(p) for p in (allowed_files or []) if p}
        allowed.discard("")
        read_only = {self._normalize(p) for p in (read_only_files or []) if p}
        read_only.discard("")

        raw = (diff or "")
        text = raw.strip()
        files = self._parse_diff(raw)
        result = PatchValidationResult()

        if text and not files:
            # Non-empty patch with no recognizable file headers: not a diff.
            result.parse_error = "no diff file headers found in patch"
            result.notes = "Patch could not be parsed: no diff file headers found."
            result.valid = False
            return result

        changed: List[str] = []
        for f in files:
            path = self._changed_path(f)
            if path is None:
                continue
            path = self._normalize(path)
            if not path:
                continue
            changed.append(path)
            if self._is_dependency(path):
                result.dependency_changes.append(path)
            if self._is_config(path):
                result.config_changes.append(path)
            if self._is_binary_path(path) or f.is_binary:
                result.binary_changes.append(path)
            if f.kind == "deleted":
                result.deletions.append(path)
            if self._has_traversal(path):
                result.path_traversals.append(path)
            # Scope check (conservative order: allowed wins, then read-only,
            # then unexpected).
            if path not in allowed:
                if path in read_only:
                    result.read_only_changes.append(path)
                else:
                    result.unexpected_changes.append(path)

        result.files_changed = list(dict.fromkeys(changed))
        result.valid = self._compute_valid(result)
        result.notes = self._build_notes(result)
        return result

    # -- diff parsing --------------------------------------------------------

    def _parse_diff(self, diff: str) -> List[DiffFile]:
        """Split a unified diff into file blocks. Returns [] for empty/no-diff."""
        if not diff:
            return []
        files: List[DiffFile] = []
        current: Optional[DiffFile] = None
        for line in diff.splitlines():
            if line.startswith("--- "):
                if current is not None:
                    files.append(current)
                current = DiffFile(old_path=self._clean_path(line[4:]))
            elif line.startswith("+++ "):
                if current is None:
                    current = DiffFile()
                current.new_path = self._clean_path(line[4:])
            else:
                if current is not None:
                    current.body_lines.append(line)
        if current is not None:
            files.append(current)
        for f in files:
            f.kind = self._kind(f)
            cp = self._changed_path(f)
            f.is_binary = self._binary_from_body(f.body_lines) or self._is_binary_path(cp or "")
        return files

    @staticmethod
    def _clean_path(raw: str) -> str:
        """Strip the git ``a/``/``b/`` prefix from a diff header path."""
        p = (raw or "").strip()
        if p == _DEV_NULL:
            return _DEV_NULL
        if p.startswith("a/"):
            p = p[2:]
        elif p.startswith("b/"):
            p = p[2:]
        return p

    @staticmethod
    def _kind(f: DiffFile) -> str:
        if PatchValidator._is_null(f.old_path) and PatchValidator._is_null(f.new_path):
            return "unknown"
        if PatchValidator._is_null(f.old_path):
            return "added"
        if PatchValidator._is_null(f.new_path):
            return "deleted"
        return "renamed" if f.old_path != f.new_path else "modified"

    @staticmethod
    def _changed_path(f: DiffFile):
        if not PatchValidator._is_null(f.new_path):
            return f.new_path
        if not PatchValidator._is_null(f.old_path):
            return f.old_path
        return None

    @staticmethod
    def _is_null(path) -> bool:
        return path is None or path == _DEV_NULL

    @staticmethod
    def _binary_from_body(body_lines: List[str]) -> bool:
        return any(l.startswith("Binary files") for l in body_lines)

    # -- path helpers --------------------------------------------------------

    @staticmethod
    def _normalize(p) -> str:
        p = (p or "").strip().replace("\\", "/")
        if p.startswith("./"):
            p = p[2:]
        return p

    @staticmethod
    def _basename(p: str) -> str:
        p = (p or "").replace("\\", "/")
        return p.rsplit("/", 1)[-1] if "/" in p else p

    def _has_traversal(self, path: str) -> bool:
        """True for absolute paths or paths containing a ``..`` component."""
        if not path or path == _DEV_NULL:
            return False
        if path.startswith("/"):
            return True
        if len(path) >= 2 and path[0].isalpha() and path[1] == ":":
            return True
        parts = [part for part in path.replace("\\", "/").split("/") if part != ""]
        return ".." in parts

    # -- change-type detection ----------------------------------------------

    def _is_dependency(self, path: str) -> bool:
        name = self._basename(path)
        if name in _DEPENDENCY_NAMES:
            return True
        if name.startswith("requirements-") and name.endswith(".txt"):
            return True
        if name.endswith(".lock"):
            return True
        return False

    def _is_config(self, path: str) -> bool:
        name = self._basename(path)
        if name in _CONFIG_EXACT:
            return True
        if name.startswith(".env"):
            return True
        if name.startswith(_CONFIG_PREFIXES) and name.endswith(_CONFIG_EXTS):
            return True
        if name.endswith(_CONFIG_BARE_EXTS):
            return True
        return False

    def _is_binary_path(self, path: str) -> bool:
        name = self._basename(path)
        if "." in name:
            ext = name.rsplit(".", 1)[-1]
            return "." + ext in _BINARY_EXTS
        return False

    # -- result assembly -----------------------------------------------------

    def _compute_valid(self, result: PatchValidationResult) -> bool:
        return not (
            result.parse_error
            or result.path_traversals
            or result.unexpected_changes
            or result.read_only_changes
            or result.binary_changes
            or result.dependency_changes
            or result.config_changes
            or result.deletions
        )

    def _build_notes(self, result: PatchValidationResult) -> str:
        if result.parse_error:
            return "Patch could not be parsed: " + result.parse_error
        if not result.files_changed:
            return "No file changes in patch."
        if result.valid:
            return ("Patch within allowed scope; no flagged change type "
                    "(dependency/config/binary/deletion/traversal).")
        parts = []
        if result.path_traversals:
            parts.append("path traversal: " + ", ".join(result.path_traversals))
        if result.unexpected_changes:
            parts.append("unexpected files: " + ", ".join(result.unexpected_changes))
        if result.read_only_changes:
            parts.append("read-only changes: " + ", ".join(result.read_only_changes))
        if result.binary_changes:
            parts.append("binary changes: " + ", ".join(result.binary_changes))
        if result.dependency_changes:
            parts.append("dependency changes (Codex review required): "
                         + ", ".join(result.dependency_changes))
        if result.config_changes:
            parts.append("config changes: " + ", ".join(result.config_changes))
        if result.deletions:
            parts.append("deletions: " + ", ".join(result.deletions))
        return "Patch rejected/escalated: " + "; ".join(parts)


__all__ = ["PatchValidator", "DiffFile"]
