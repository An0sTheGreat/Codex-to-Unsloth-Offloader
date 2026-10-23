"""Structured error types and states for the offloader.

Every error carries a stable machine-readable ``code`` so that Codex (and the
Coding Task Assessment) can react deterministically without parsing prose. The
hierarchy mirrors the recovery states described in handoff.md (recoverable state,
not a crash) and the error-handling section of project_instructions.md.

``details`` is an optional structured dict (never user-facing prose) that can
carry machine-readable context such as ``status_code`` or ``model_id``."""

from __future__ import annotations


class UnslothError(Exception):
    """Base for all offloader errors.

    :attr:`code` is the stable machine-readable identifier.
    :attr:`details` is an optional structured dict for diagnostics.
    """
    code: str = "unsloth_error"

    def __init__(self, message: str, code: Optional[str] = None, *, details: dict = None) -> None:
        super().__init__(message)
        self.code = code or self.code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "code": self.code,
            "message": str(self),
            "details": self.details,
        }

class ConfigurationError(UnslothError):
    """Configuration could not be loaded or is invalid."""
    code = "configuration_error"

class UnslothNotInstalledError(UnslothError):
    """Unsloth CLI / Studio is not present or not on PATH."""
    code = "unsloth_not_installed"

class UnslothNotRunningError(UnslothError):
    """Unsloth is installed but the local server is not reachable."""
    code = "unsloth_not_running"

class ApiUnavailableError(UnslothError):
    """The OpenAI-compatible endpoint is reachable but not serving (5xx, closed, etc.)."""
    code = "api_unavailable"

class AuthError(UnslothError):
    """Authentication/authorization was rejected (401/403)."""
    code = "auth_error"

class ModelNotLoadedError(UnslothError):
    """No model is currently loaded in Unsloth."""
    code = "model_not_loaded"

class ModelIdentifierUnavailableError(UnslothError):
    """The loaded model id/name could not be discovered (non-OpenAI transport)."""
    code = "model_identifier_unavailable"

class RequestTimeoutError(UnslothError):
    """A request exceeded its explicit timeout."""
    code = "request_timeout"

class CancellationError(UnslothError):
    """A request was cancelled (cooperative cancellation)."""
    code = "cancellation"

class MalformedOutputError(UnslothError):
    """The local model's output could not be parsed into a valid WorkerResult."""
    code = "malformed_output"

class ContextLimitExceededError(UnslothError):
    """The packaged context exceeded the model's usable context budget."""
    code = "context_limit_exceeded"

class ValidationFailureError(UnslothError):
    """Codex validation rejected a worker result (tests/lint/type checks failed)."""
    code = "validation_failure"

class ScopeViolationError(UnslothError):
    """A patch touched a path outside the allowed/read-only file scope."""
    code = "scope_violation"


__all__ = [
    "UnslothError",
    "ConfigurationError",
    "UnslothNotInstalledError",
    "UnslothNotRunningError",
    "ApiUnavailableError",
    "AuthError",
    "ModelNotLoadedError",
    "ModelIdentifierUnavailableError",
    "RequestTimeoutError",
    "CancellationError",
    "MalformedOutputError",
    "ContextLimitExceededError",
    "ValidationFailureError",
    "ScopeViolationError",
]
