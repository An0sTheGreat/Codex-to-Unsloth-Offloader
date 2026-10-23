"""Phase 5 context packager and secret guard.

Packages the *minimum* repository context needed for a delegated task and
guards against leaking secrets or exceeding the context budget.

Implements the Phase 5 objective (development_plan.md) and the task-packet
semantics in handoff.md section 6:

* **Allowlisted collection.** Only files explicitly named in the task scope
  (``allowed_files`` / ``read_only_files``) are read from the project root.
  Unrelated repository files are never included by default.
* **Excluded-file/glob policy.** Paths matching ``config.excluded_files``
  (secret/credential globs) are blocked and recorded, never read.
* **Secret guard.** Secret-bearing content (API keys, tokens, passwords,
  private-key blocks, Bearer tokens) is redacted from snippet text; secret
  *files* (via the glob policy) are blocked outright. Environment variable
  values are never leaked.
* **Context budget.** Packaged context must fit ``config.context_budget_tokens``.
  Required (allowed) context is always kept; optional context (read-only files,
  user snippets) is trimmed to fit. If even the required context overflows the
  budget, the packager returns ``needs_scope_reduction``.
* **Project-scoped paths.** Every path in the output is relative to the project
  root. Absolute paths under the root are converted to relative; absolute
  paths outside the root and any ``..`` traversal are blocked.

The packager is pure except for reading files from the project root. It makes
no network or model calls, so it always runs *before* any transport invocation
(acceptance: "context overflow is handled before model invocation").
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .config import OffloaderConfig, get_project_root
from .schemas import (
    BlockedFile,
    ContextPackage,
    PackageStatus,
    RepositoryContext,
    Snippet,
)

# ---------------------------------------------------------------------------
# Token estimation (deterministic, ~4 chars per token, rounded up)
# ---------------------------------------------------------------------------

CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    """Deterministic rough token estimate: ~4 chars per token (rounded up)."""
    if not text:
        return 0
    return -(-len(text) // CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# Secret guard: content redaction patterns
# ---------------------------------------------------------------------------

# (compiled_regex, replacement). Applied to all user-supplied text and file
# contents before token estimation, so inline secrets (e.g. env values) are
# redacted even in files that are not themselves secret files.
_SECRET_CONTENT_PATTERNS = [
    # labelled key/value pairs (env values, config secrets)
    (re.compile(
        r"(?i)\b(?:api[_-]?key|apikey|secret(?:s)?|token(?:s)?|password|"
        r"passwd|pwd|client[_-]?secret|client[_-]?id|access[_-]?key|"
        r"auth[_-]?token|auth[_-]?header|private[_-]?key|signing[_-]?key|"
        r"session[_-]?id|refresh[_-]?token|db[_-]?(?:password|passwd|dsn))"
        r"\s*[=:]\s*\S+"),
     "[REDACTED]"),
    # long bare keys/tokens with well-known prefixes
    (re.compile(
        r"(?i)\b(?:sk|ghp_|gho_|ghu_|ghs_|AKIA|AIza|xoxb|xoxa)"
        r"[A-Za-z0-9_\-]{16,}\b"),
     "[REDACTED]"),
    # full private-key blocks
    (re.compile(
        r"-----BEGIN (?:RSA|DSA|EC|OPENSSH|PGP) PRIVATE KEY[\s\S]*?"
        r"-----END (?:RSA|DSA|EC|OPENSSH|PGP) PRIVATE KEY"),
     "[REDACTED_PRIVATE_KEY]"),
    # Authorization: Bearer <long token>
    (re.compile(r"(?i)\b(?:Bearer|Token)\s+[A-Za-z0-9._\-/+=]{20,}\b"),
     "[REDACTED]"),
]


def redact_content(text: str) -> tuple[str, int]:
    """Return (text, n_redactions) with secret-bearing spans redacted."""
    if not text:
        return text, 0
    out = text
    count = 0
    for pattern, repl in _SECRET_CONTENT_PATTERNS:
        out, n = pattern.subn(repl, out)
        count += n
    return out, count


def _seg_regex(seg: str) -> str:
    """Translate a single glob path segment to a regex (``*``/``?`` within a
    segment; other chars escaped)."""
    out = []
    for ch in seg:
        if ch == "*":
            out.append("[^/]*")
        elif ch == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(ch))
    return "".join(out)


def _glob_to_regex(pattern: str) -> str:
    """Translate a gitignore-style glob to a regex.

    ``**`` spans any number of path components (including zero); ``*`` spans
    within a single component.
    """
    parts = pattern.split("/")
    segments = []
    i = 0
    while i < len(parts):
        seg = parts[i]
        if seg == "**":
            if i + 1 < len(parts) and parts[i + 1] != "":
                # "**/next": optional any-depth prefix, then the next segment
                segments.append("(?:.*/)?" + _seg_regex(parts[i + 1]))
                i += 2
            else:
                # "**" at the end: match everything
                segments.append(".*")
                i += 1
        else:
            segments.append(_seg_regex(seg))
            i += 1
    return "^" + "/".join(segments) + "$"


# ---------------------------------------------------------------------------
# Context packager
# ---------------------------------------------------------------------------

class ContextPackager:
    """Package the minimum repository context for a handoff (Phase 5).

    :param config: :class:`~bridge.config.OffloaderConfig`; supplies
        ``excluded_files`` (glob policy) and ``context_budget_tokens``.
    :param project_root: The project/repo root to read files from. Defaults to
        :func:`~bridge.config.get_project_root`.
    """

    def __init__(self, config: Optional[OffloaderConfig] = None,
                 project_root: Optional[Path] = None):
        self.config = config or OffloaderConfig()
        self.project_root = (Path(project_root) if project_root is not None
                             else get_project_root()).resolve()
        self.excluded_files = list(getattr(self.config, "excluded_files", []) or [])
        self.budget_tokens = int(self.config.context_budget_tokens)
        self._glob_re = [re.compile(_glob_to_regex(p)) for p in self.excluded_files
                         if p]

    # -- public --

    def package(self, scope: dict) -> "ContextPackage":
        """Collect, filter, and budget the context for the given task scope.

        :param scope: Mapping with optional keys:
            * ``allowed_files`` -- paths the worker may change (required context).
            * ``read_only_files`` -- paths the worker may read (optional context).
            * ``snippets`` -- list of ``{"path","content","note"}`` pre-extracted
              excerpts (optional context).
            * ``summary`` -- repository summary (required context).
            * ``acceptance_criteria`` -- list of criteria (carried to the packet).
        :return: A validated :class:`~bridge.schemas.ContextPackage`.
        """
        scope = scope or {}
        blocked: list[dict] = []
        notes: list[str] = []
        redacted_paths: list[str] = []

        allowed = self._resolve_files(scope.get("allowed_files") or [],
                                      blocked=blocked)
        read_only = self._resolve_files(scope.get("read_only_files") or [],
                                        blocked=blocked)

        summary = scope.get("summary") or ""
        summary, n = redact_content(summary)
        summary_tokens = estimate_tokens(summary)
        if n:
            notes.append("summary redacted for inline secrets")

        # Required context: allowed files + summary.
        required_snippets = []
        required_tokens = summary_tokens
        for rel in allowed:
            content, _, ok = self._read_file(rel)
            if not ok:
                notes.append(f"file not found (skipped): {rel}")
                continue
            content, n = redact_content(content)
            if n:
                redacted_paths.append(rel)
            required_snippets.append(Snippet(path=rel, content=content,
                                             note="full file - worker may modify"))
            required_tokens += estimate_tokens(content)

        if required_tokens > self.budget_tokens:
            return self._package_needs_reduction(
                allowed, read_only, required_snippets, summary, blocked, notes,
                redacted_paths, required_tokens,
                scope.get("acceptance_criteria") or [])

        # Optional context: read-only files + user snippets, trimmed to fit.
        optional_snippets = []
        used_paths = set(allowed)
        for rel in read_only:
            if rel in used_paths:
                continue
            content, _, ok = self._read_file(rel)
            if not ok:
                notes.append(f"file not found (skipped): {rel}")
                continue
            content, n = redact_content(content)
            if n:
                redacted_paths.append(rel)
            optional_snippets.append(
                (rel, Snippet(path=rel, content=content, note="read-only context")))
        for raw in (scope.get("snippets") or []):
            if not isinstance(raw, dict):
                continue
            path = raw.get("path")
            rel, reason = self._resolve_path(path)
            if rel is None:
                blocked.append({"path": str(path), "reason": reason})
                continue
            if rel in used_paths:
                continue
            content, n = redact_content(raw.get("content", "") or "")
            if n:
                redacted_paths.append(rel)
            note = raw.get("note") or ""
            optional_snippets.append((rel, Snippet(path=rel, content=content, note=note)))

        # Fit optional context within the remaining budget.
        total = required_tokens
        included_optional = []
        for rel, sn in optional_snippets:
            if total + estimate_tokens(sn.content) <= self.budget_tokens:
                included_optional.append(sn)
                total += estimate_tokens(sn.content)
            else:
                notes.append(f"skipped (too large for budget): {rel}")
        if optional_snippets and len(included_optional) < len(optional_snippets):
            status = PackageStatus.REDUCED
        else:
            status = PackageStatus.OK

        return ContextPackage(
            status=status,
            repository_context=RepositoryContext(
                summary=summary,
                allowed_files=list(allowed),
                read_only_files=list(read_only),
                relevant_snippets=required_snippets + included_optional,
                context_budget_tokens=self.budget_tokens),
            acceptance_criteria=list(scope.get("acceptance_criteria") or []),
            blocked=[BlockedFile(path=p["path"], reason=p["reason"])
                     for p in blocked],
            tokens_used=total,
            budget_tokens=self.budget_tokens,
            notes=notes,
            redacted_paths=redacted_paths,
        )

    # -- internals --

    def _package_needs_reduction(self, allowed, read_only, required_snippets,
                                 summary, blocked, notes, redacted_paths,
                                 required_tokens, acceptance_criteria):
        notes.append(
            f"required context ~{required_tokens} tokens exceeds budget "
            f"{self.budget_tokens} tokens; needs scope reduction")
        return ContextPackage(
            status=PackageStatus.NEEDS_SCOPE_REDUCTION,
            repository_context=RepositoryContext(
                summary=summary,
                allowed_files=list(allowed),
                read_only_files=list(read_only),
                relevant_snippets=required_snippets,
                context_budget_tokens=self.budget_tokens),
            acceptance_criteria=list(acceptance_criteria),
            blocked=[BlockedFile(path=p["path"], reason=p["reason"])
                     for p in blocked],
            tokens_used=required_tokens,
            budget_tokens=self.budget_tokens,
            notes=notes,
            redacted_paths=redacted_paths,
        )

    def _resolve_files(self, paths, blocked):
        """Resolve a list of paths to project-relative paths.

        Unresolvable / traversal / outside-root / excluded paths are recorded
        in ``blocked`` and omitted.
        """
        resolved = []
        for raw in paths:
            rel, reason = self._resolve_path(raw)
            if rel is None:
                blocked.append({"path": str(raw), "reason": reason})
                continue
            if self._is_excluded(rel):
                blocked.append({"path": str(raw),
                                "reason": "excluded (secret/credential file)"})
                continue
            resolved.append(rel)
        return resolved

    def _resolve_path(self, raw):
        """Resolve a path to a project-relative path.

        Returns (relative_path, ""). If the path cannot be made project-scoped
        (absolute outside the root, or ``..`` traversal), returns (None, reason).
        """
        if raw is None:
            return None, "empty path"
        text = str(raw).strip()
        if not text:
            return None, "empty path"
        p = Path(text)
        if p.is_absolute():
            try:
                rel_text = p.resolve().relative_to(self.project_root).as_posix()
            except ValueError:
                return None, "absolute path outside project root"
        else:
            rel_text = p.as_posix()
            rel_text = rel_text.lstrip("/")
            while rel_text.startswith("./"):
                rel_text = rel_text[2:]
        if rel_text in ("", "."):
            return None, "path resolves to project root (not a file)"
        if ".." in rel_text.split("/"):
            return None, "path traversal (..)"
        return rel_text, ""

    def _is_excluded(self, rel: str) -> bool:
        """True when ``rel`` matches any configured excluded-file glob."""
        return any(rx.match(rel) for rx in self._glob_re)

    def _read_file(self, rel: str) -> tuple[str, Path, bool]:
        """Read a project file. Returns (content, path, ok)."""
        path = self.project_root / rel
        if not path.is_file():
            return "", path, False
        try:
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return "", path, False
        return content, path, True
