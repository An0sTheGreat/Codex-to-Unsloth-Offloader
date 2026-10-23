"""Deterministic coding-task classifier (Phase 3).

:class:`TaskClassifier` implements the Phase 3 objective: turn a free-text
 coding request (plus optional structured context) into a normalized,
 machine-readable :class:`~bridge.schemas.TaskAssessment` using fixed,
 inspectable rules.

The same input always produces the same output: no randomization, no network
 calls, no model calls. Every decision is driven by explicit pattern lists so
 that Codex (or a user) can audit *why* a task got its difficulty, length,
 risk flags, and offload eligibility.

Classification rules (mirror handoff.md):

* **Difficulty 1-5** (Trivial..Expert). Base level comes from risk flags
  (destructive / deployment / architecture-heavy start at 4, security /
  secrets at 3), then complexity indicators add one level per match, clamped
  to 1..5.
* **Length** Short/Medium/Long/Extended from keyword indicators, checked in
  order Extended > Long > Medium > Short > default Short.
* **Risk flags**: security-sensitive, secrets/credentials, destructive
  actions, deployment/release, architecture-heavy, excessive context. A flag
  may come from the task text or from oversized structured ``context`` (more
  than :attr:`MAX_CONTEXT_FILES` files supplied).
* **Offload eligibility**: any risk flag or difficulty 5 -> Keep in Codex
  (high-risk flags override offload eligibility, handoff.md section 4).
  Difficulty 4 -> Partial handoff. Difficulty 3 -> Local handoff for Short/
  Medium scope, Partial for Long/Extended. Difficulty 1-2 -> Local handoff,
  except Extended scope -> Partial.

The classifier is intentionally a standalone class: Phase 4 wires it into a
concrete :class:`~bridge.routing.Router` implementation, keeping ``assess``
 (classification) separate from ``route`` (policy).
"""

from __future__ import annotations

from typing import Optional

from .schemas import (
    DifficultyLevel,
    Execution,
    LengthLevel,
    OffloadDecision,
    RiskFlag,
    TaskAssessment,
)

# ---------------------------------------------------------------------------
# Difficulty
# ---------------------------------------------------------------------------

_DIFFICULTY_LEVEL = [
    None,
    DifficultyLevel.TRIVIAL,
    DifficultyLevel.EASY,
    DifficultyLevel.MODERATE,
    DifficultyLevel.HARD,
    DifficultyLevel.EXPERT,
]

# Base difficulty contributed by risk flags (max wins). Security and secrets
# work is still usually moderate; destructive, deployment, and architecture-
# heavy work starts at Hard.
_FLAG_BASE_DIFFICULTY = {
    RiskFlag.DESTRUCTIVE_ACTIONS: 4,
    RiskFlag.DEPLOYMENT_RELEASE: 4,
    RiskFlag.ARCHITECTURE_HEAVY: 4,
    RiskFlag.SECURITY_SENSITIVE: 3,
    RiskFlag.SECRETS_CREDENTIALS: 3,
}

# Substring indicators (lowercased task text) that raise difficulty by one
# level each.
_COMPLEXITY_INDICATORS = (
    "multiple files", "multi-file", "several files", "a few files",
    "two or more", "three or more",
    "debug", "investigat", "root cause", "track down",
    "refactor",
    "complex", "tricky",
    "integration", "integrate",
    "end-to-end", "e2e",
    "rewrite",
    "large", "big",
    "api", "endpoint",
    "expert",
)

# ---------------------------------------------------------------------------
# Risk flags
# ---------------------------------------------------------------------------

# Lowercased substring patterns per flag. Prefixes (e.g. ``migrat``) are
# intentional so that ``migrate``, ``migration`` and ``migrating`` all match.
_RISK_PATTERNS = {
    RiskFlag.SECURITY_SENSITIVE: (
        "security",
        "vulnerab",            # vulnerable / vulnerability
        "authentication",
        "authoriz",            # authorization / authorize
        "permission",
        "encrypt",             # encryption / encrypted
        "decrypt",
        "cryptograph",
        "inject",              # inject / injection
        "xss",
        "csrf",
        "malicious",
        "attack",
        "pii",
        "sensitive",
    ),
    RiskFlag.SECRETS_CREDENTIALS: (
        "api key", "api-key", "api_key", "apikey",
        "credential",
        "password",
        "passwd",
        "bearer",
        "jwt",
        "oauth",
        "secret",
        "private key", "private-key",
        "env var", "environment variable", "dotenv",
        ".env",
    ),
    RiskFlag.DESTRUCTIVE_ACTIONS: (
        "delete",
        "drop ",               # trailing space avoids "dropdown"
        "wipe",
        "purge",
        "destroy",
        "truncat",             # truncate / truncation
        "rm -rf",
        "remove all",
        "delete all",
        "zero out",
        "nuke",
    ),
    RiskFlag.DEPLOYMENT_RELEASE: (
        "deploy",              # deploy / deployment / deployed
        "release",
        "publish",
        "rollout",
        "production",
        "ship",
        "git push",
        "push to",
    ),
    RiskFlag.ARCHITECTURE_HEAVY: (
        "architect",           # architecture / architectural
        "restructure",
        "redesign",
        "migrat",              # migrate / migration
        "framework",
        "infrastructure",
        "database schema",
        "data model",
        "microservice",
        "event-driven", "event driven",
        "state machine",
        "system design",
        "system-wide",
        "monolith",
        "core system",
    ),
    RiskFlag.EXCESSIVE_CONTEXT: (
        "entire repository", "whole repository",
        "entire codebase", "whole codebase",
        "codebase-wide", "repository-wide", "repo-wide",
        "all files",
        "every file",
        "everything",
        "thousands of files",
    ),
}

# More than this many files in the structured context counts as excessive.
MAX_CONTEXT_FILES = 20

# ---------------------------------------------------------------------------
# Length
# ---------------------------------------------------------------------------

# Checked in order; first match wins. Default is Short.
_LENGTH_PATTERNS = (
    (LengthLevel.EXTENDED, (
        "repository-wide", "repo-wide", "codebase-wide",
        "entire codebase", "entire repository",
        "whole codebase", "whole repository",
        "multi-phase", "multiple phases",
        "from scratch", "greenfield",
    )),
    (LengthLevel.LONG, (
        "across the codebase", "across the repository",
        "multiple modules", "several modules",
        "many files",
        "large-scale", "large scale",
        "whole project", "entire project",
        "major refactor", "big refactor",
    )),
    (LengthLevel.MEDIUM, (
        "multiple files", "multi-file", "several files", "a few files",
        "two or more", "three or more",
        "module", "subsystem", "package",
        "couple",
        "few files",
    )),
    (LengthLevel.SHORT, (
        "single file", "one file", "this file", "a file",
        "single", "isolated", "small",
        "function", "test", "comment", "doc",
        "typo", "rename", "variable", "constant", "helper",
    )),
)

_LENGTH_NOTES = {
    LengthLevel.SHORT: "small, isolated change",
    LengthLevel.MEDIUM: "multi-file change",
    LengthLevel.LONG: "multi-module change",
    LengthLevel.EXTENDED: "repository-wide change",
}


class TaskClassifier:
    """Deterministic task classifier (Phase 3).

    Mirrors :class:`~bridge.routing.Router`'s ``assess`` contract so the
    Phase 4 router can reuse it directly (``route`` stays a Phase 4 concern).
    """

    def assess(self, task_description: str, *, context: Optional[dict] = None) -> TaskAssessment:
        """Classify a coding task into a :class:`~bridge.schemas.TaskAssessment`. (Phase 3.)

        :param task_description: The user's coding request (or the relevant excerpt).
        :param context: Optional structured context (e.g. ``{"files": [...]}``);
            an oversized file list sets :class:`RiskFlag.EXCESSIVE_CONTEXT`.
        :return: A validated :class:`~bridge.schemas.TaskAssessment`.
        """
        if not task_description or not task_description.strip():
            raise ValueError("task_description must be a non-empty string")
        text = task_description.lower()
        flags = self._risk_flags_from_text(text) | self._risk_flags_from_context(context)
        difficulty = self._difficulty(flags, text)
        length = self._length(text)
        offload, execution, note, reason = self._offload_decision(flags, difficulty, length)
        return TaskAssessment(
            difficulty=difficulty,
            difficulty_label=_DIFFICULTY_LEVEL[difficulty],
            length=length,
            length_note=_LENGTH_NOTES[length],
            offload=offload,
            offload_note=note,
            execution=execution,
            risk_flags=sorted(flags, key=lambda f: f.value),
            reason=reason,
        )

    # -- internals --

    def _risk_flags_from_text(self, text: str) -> set[RiskFlag]:
        return {
            flag
            for flag, patterns in _RISK_PATTERNS.items()
            if any(p in text for p in patterns)
        }

    def _risk_flags_from_context(self, context: Optional[dict]) -> set[RiskFlag]:
        flags: set[RiskFlag] = set()
        if isinstance(context, dict):
            for key in ("files", "allowed_files", "read_only_files"):
                value = context.get(key)
                if isinstance(value, (list, tuple, set)) and len(value) > MAX_CONTEXT_FILES:
                    flags.add(RiskFlag.EXCESSIVE_CONTEXT)
                    break
        return flags

    def _difficulty(self, flags: set[RiskFlag], text: str) -> int:
        base = 1
        for flag in flags:
            base = max(base, _FLAG_BASE_DIFFICULTY.get(flag, 1))
        bumps = sum(1 for indicator in _COMPLEXITY_INDICATORS if indicator in text)
        return max(1, min(5, base + bumps))

    def _length(self, text: str) -> LengthLevel:
        for level, patterns in _LENGTH_PATTERNS:
            if any(p in text for p in patterns):
                return level
        return LengthLevel.SHORT

    def _offload_decision(self, flags: set[RiskFlag], difficulty: int, length: LengthLevel):
        """Map difficulty/length/flags to (offload, execution, offload_note, reason).

        High-risk flags override offload eligibility regardless of how small
        the task looks (handoff.md section 4).
        """
        if flags:
            flag_list = ", ".join(f.value for f in sorted(flags, key=lambda f: f.value))
            return (OffloadDecision.NO, Execution.KEEP_IN_CODEX, "local model not eligible",
                    f"Risk flags: {flag_list} - high-risk work stays in Codex.")
        if difficulty == 5:
            return (OffloadDecision.NO, Execution.KEEP_IN_CODEX, "local model not eligible",
                    f"Difficulty {difficulty} (Expert) - keep in Codex.")
        if difficulty == 4:
            return (OffloadDecision.PARTIAL, Execution.PARTIAL_HANDOFF, "local model (bounded subtasks)",
                    f"Difficulty {difficulty} (Hard) - partial handoff of bounded subtasks.")
        if difficulty == 3 and length in (LengthLevel.LONG, LengthLevel.EXTENDED):
            return (OffloadDecision.PARTIAL, Execution.PARTIAL_HANDOFF, "local model (bounded subtasks)",
                    f"Difficulty {difficulty} (Moderate), {length.name} scope - partial handoff.")
        if difficulty in (1, 2) and length is LengthLevel.EXTENDED:
            return (OffloadDecision.PARTIAL, Execution.PARTIAL_HANDOFF, "local model (bounded subtasks)",
                    f"Difficulty {difficulty} ({_DIFFICULTY_LEVEL[difficulty].name}), {length.name} scope - partial handoff.")
        return (OffloadDecision.YES, Execution.LOCAL_HANDOFF, "local model",
                f"Difficulty {difficulty} ({_DIFFICULTY_LEVEL[difficulty].name}), {length.name} scope, no risk flags - local handoff.")


__all__ = ["TaskClassifier", "MAX_CONTEXT_FILES"]
