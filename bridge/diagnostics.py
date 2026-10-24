"""Structured, log-safe diagnostics for offload handoffs (Phase 8).

Emits the non-secret metadata described in handoff.md section 12 (Logging
template) for each handoff: request id, timestamp, difficulty, length, selected
local model, reason for offload, files supplied / allowed to change, result
state, validation result, and fallback/escalation.

Only non-secret metadata is logged. Prompt bodies, source snippets, and secrets
are never logged (handoff.md section 12). The caller sets the level via
:func:`configure_logging`; the emitter itself only builds and returns the line.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from .schemas import HandoffRequest, ResultState, WorkerResult
from .validate import PatchValidationResult

_LOGGER_NAME = "unsloth.offloader"
_LOGGER = logging.getLogger(_LOGGER_NAME)

# Hard cap so an accidental long fragment can never bloat a log line.
_MAX_FIELD = 200


def configure_logging(level: str = "info") -> None:
    """Set the module logger level from the offloader ``log_level`` config."""
    _LOGGER.setLevel(getattr(logging, level.upper(), logging.INFO))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe(text: Optional[str]) -> str:
    """Log-safe version of ``text``: collapse whitespace and truncate.

    Defense-in-depth: the template only receives non-secret fields, but
    truncation keeps logs bounded and prevents accidental embedding of long
    source/prompt fragments.
    """
    if not text:
        return ""
    text = " ".join(str(text).split())
    if len(text) > _MAX_FIELD:
        return text[:_MAX_FIELD - 1] + "…"
    return text


def _fallback(result: WorkerResult,
              validation: Optional[PatchValidationResult]) -> str:
    """One-line next-step (fallback/escalation) for the log template."""
    if validation is not None and not validation.valid:
        return "escalate to Codex (patch failed scope validation)"
    if result.state is ResultState.COMPLETED:
        return "none (patch within scope; Codex runs final validation)"
    return "continue in Codex (state=%s)" % result.state.value


def log_handoff(request: HandoffRequest, result: WorkerResult,
                validation: Optional[PatchValidationResult] = None,
                model_alias: str = "", reason: str = "") -> str:
    """Build and emit the non-secret handoff metadata (handoff.md section 12).

    Returns the formatted log line so callers can assert on it in tests. Never
    logs prompt bodies, source snippets, or secrets.
    """
    supplied = "; ".join(
        request.repository_context.allowed_files +
        request.repository_context.read_only_files
    )
    lines = [
        "Request ID: " + _safe(request.request_id),
        "Timestamp: " + _utc_now(),
        "Difficulty: %s" % request.difficulty,
        "Length: " + request.length.value,
        "Selected local model: " + _safe(model_alias),
        "Reason for offload: " + _safe(reason),
        "Files supplied: " + _safe(supplied),
        "Files allowed to change: "
            + _safe("; ".join(request.repository_context.allowed_files)),
        "Result state: " + result.state.value,
    ]
    if validation is not None:
        lines.append("Validation result: " + _safe(validation.notes))
    lines.append("Fallback/escalation: " + _fallback(result, validation))
    log_line = "\n".join(lines)
    _LOGGER.info(log_line)
    return log_line


__all__ = ["log_handoff", "configure_logging"]
