"""Codex-to-Unsloth offloader: local delegation bridge.

Phase 1 of the project defines the machine-readable contracts (schemas),
path-independent configuration loading, structured error types, and the
transport/routing interfaces. No live Unsloth instance is required to import
or run this package.

Public API is intentionally small and stable; everything else is internal.
"""

from __future__ import annotations

from .schemas import (
    DifficultyLevel,
    Execution,
    HandoffRequest,
    LengthLevel,
    ModelMetadata,
    ModelProfile,
    OffloadDecision,
    OffloadResult,
    OutputContract,
    OutputFormat,
    RepositoryContext,
    ResultState,
    RiskFlag,
    Snippet,
    TaskAssessment,
    ValidationOutcome,
    ValidationOutcomeKind,
    BlockedFile,
    ContextPackage,
    PackageStatus,
)
from .config import OffloaderConfig, get_project_root, load_config
from .errors import (
    ApiUnavailableError,
    CancellationError,
    ConfigurationError,
    ContextLimitExceededError,
    ModelIdentifierUnavailableError,
    ModelNotLoadedError,
    MalformedOutputError,
    UnslothError,
    UnslothNotInstalledError,
    UnslothNotRunningError,
    ValidationFailureError,
)
from .classifier import TaskClassifier
from .routing import Router, RoutingDecision, PolicyRouter
from .transport import MockTransport, Transport, UnslothApiTransport
from .context import ContextPackager, estimate_tokens

__version__ = "0.1.0"

__all__ = [
    # schemas
    "DifficultyLevel",
    "Execution",
    "HandoffRequest",
    "LengthLevel",
    "ModelMetadata",
    "OffloadDecision",
    "OffloadResult",
    "OutputContract",
    "OutputFormat",
    "RepositoryContext",
    "ResultState",
    "Snippet",
    "TaskAssessment",
    "ValidationOutcome",
    "ValidationOutcomeKind",
    "BlockedFile",
    "ContextPackage",
    "PackageStatus",
    # context
    "ContextPackager",
    "estimate_tokens",
    # config
    "OffloaderConfig",
    "get_project_root",
    "load_config",
    # errors
    "ApiUnavailableError",
    "CancellationError",
    "ConfigurationError",
    "ContextLimitExceededError",
    "ModelIdentifierUnavailableError",
    "ModelNotLoadedError",
    "MalformedOutputError",
    "UnslothError",
    "UnslothNotInstalledError",
    "UnslothNotRunningError",
    "ValidationFailureError",
    # routing
    "Router",
    "RoutingDecision",
    "PolicyRouter",
    "RiskFlag",
    "TaskClassifier",
    # transport
    "MockTransport",
    "Transport",
    "UnslothApiTransport",
    "__version__",
]
