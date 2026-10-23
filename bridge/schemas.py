"""Machine-readable contract schemas for the Unsloth coding-handoff offloader.

Every value exchanged between the Codex orchestrator, the local bridge, and the
loaded Unsloth model is expressed as a pydantic model here. The field sets and
enumerations mirror the semantics defined in ``handoff.md`` (task packet, result
states, model metadata, validation outcomes) so that the bridge, the transport
adapter, and the routing policy share a single source of truth.

These schemas are the only thing the transport and routing layers understand; the
actual HTTP wire format (OpenAI Responses / Chat Completions) is a transport
detail handled in Phase 2 and never leaks into these structures.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enumerated fields (typed instead of free-form strings)
# ---------------------------------------------------------------------------

class DifficultyLevel(str, Enum):
    TRIVIAL = "Trivial"   # 1
    EASY = "Easy"         # 2
    MODERATE = "Moderate" # 3
    HARD = "Hard"         # 4
    EXPERT = "Expert"     # 5

class LengthLevel(str, Enum):
    SHORT = "Short"
    MEDIUM = "Medium"
    LONG = "Long"
    EXTENDED = "Extended"

class OffloadDecision(str, Enum):
    YES = "Yes"
    PARTIAL = "Partial"
    NO = "No"

class Execution(str, Enum):
    LOCAL_HANDOFF = "Local handoff"
    PARTIAL_HANDOFF = "Partial handoff"
    KEEP_IN_CODEX = "Keep in Codex"

class ResultState(str, Enum):
    """Normalized states the bridge reports for a local-model response."""
    COMPLETED = "completed"
    NEEDS_CONTEXT = "needs_context"
    DECLINED = "declined"
    FAILED = "failed"
    INVALID_OUTPUT = "invalid_output"
    NEEDS_SCOPE_REDUCTION = "needs_scope_reduction"

class OutputFormat(str, Enum):
    UNIFIED_DIFF = "unified_diff"
    STRUCTURED = "structured"

class RoutingDecisionKind(str, Enum):
    FULL_LOCAL = "full_local"
    PARTIAL_LOCAL = "partial_local"
    KEEP_IN_CODEX = "keep_in_codex"

class RiskFlag(str, Enum):
    """Risk flags that can override offload eligibility (handoff.md section 4).

    Any task carrying a risk flag stays in Codex by default; the Phase 4
    routing policy reads these from :class:`TaskAssessment`.
    """
    SECURITY_SENSITIVE = "security_sensitive"
    SECRETS_CREDENTIALS = "secrets_credentials"
    DESTRUCTIVE_ACTIONS = "destructive_actions"
    DEPLOYMENT_RELEASE = "deployment_release"
    ARCHITECTURE_HEAVY = "architecture_heavy"
    EXCESSIVE_CONTEXT = "excessive_context"

class ValidationOutcomeKind(str, Enum):
    ACCEPTED_UNCHANGED = "accepted unchanged"
    ACCEPTED_AFTER_EDITS = "accepted after Codex edits"
    REJECTED = "rejected and reimplemented by Codex"


# ---------------------------------------------------------------------------
# Coding Task Assessment
# ---------------------------------------------------------------------------

_DIFFICULTY_TO_LABEL = {
    1: DifficultyLevel.TRIVIAL,
    2: DifficultyLevel.EASY,
    3: DifficultyLevel.MODERATE,
    4: DifficultyLevel.HARD,
    5: DifficultyLevel.EXPERT,
}


class TaskAssessment(BaseModel):
    """The four-line 'Coding Task Assessment' block from handoff.md.

    ``execution`` is the authoritative decision (Local handoff / Partial handoff /
    Keep in Codex); ``offload`` is the smaller-LLM suitability line. They must be
    mutually consistent (enforced in :meth:`validate_consistency`).
    """
    difficulty: Literal[1, 2, 3, 4, 5] = Field(..., ge=1, le=5)
    difficulty_label: DifficultyLevel
    length: LengthLevel
    length_note: str = Field(..., description="Scope estimate, e.g. 'one isolated file change'.")
    offload: OffloadDecision
    offload_note: str = Field(..., description="Model name or reason for the offload decision.")
    execution: Execution
    risk_flags: list[RiskFlag] = Field(default_factory=list,
                                        description="Risk flags detected during classification (Phase 3).")
    reason: str = Field(default="", description="Short human-readable routing reason.")

    @property
    def is_high_risk(self) -> bool:
        """True when any risk flag is set; high-risk tasks stay in Codex."""
        return bool(self.risk_flags)

    @model_validator(mode="after")
    def validate_consistency(self) -> "TaskAssessment":
        # difficulty_label must agree with difficulty
        expected = _DIFFICULTY_TO_LABEL[self.difficulty]
        if self.difficulty_label is not expected:
            raise ValueError(f"difficulty_label {self.difficulty_label!r} must match difficulty {self.difficulty} (expected {expected.name!r})")
        # execution and offload must agree
        if self.offload is OffloadDecision.YES and self.execution != Execution.LOCAL_HANDOFF:
            raise ValueError("offload='Yes' requires execution='Local handoff'")
        if self.offload is OffloadDecision.PARTIAL and self.execution != Execution.PARTIAL_HANDOFF:
            raise ValueError("offload='Partial' requires execution='Partial handoff'")
        if self.offload is OffloadDecision.NO and self.execution != Execution.KEEP_IN_CODEX:
            raise ValueError("offload='No' requires execution='Keep in Codex'")
        return self


# ---------------------------------------------------------------------------
# Handoff task packet
# ---------------------------------------------------------------------------

class Snippet(BaseModel):
    """A small excerpt of a relevant file supplied to the local worker."""
    path: str = Field(..., description="Relative, project-scoped file path.")
    content: str
    note: str = Field(default="", description="Why this snippet is relevant.")

class RepositoryContext(BaseModel):
    """Minimum context packaged for a handoff (handoff.md section 11)."""
    summary: str = Field(..., description="Only relevant architectural facts.")
    allowed_files: list[str] = Field(default_factory=list, description="Paths the worker may change (relative).")
    read_only_files: list[str] = Field(default_factory=list, description="Paths the worker may read but not change (relative).")
    relevant_snippets: list[Snippet] = Field(default_factory=list)
    context_budget_tokens: int = Field(default=16384, ge=0, description="Token budget for the packaged context.")


# ---------------------------------------------------------------------------
# Phase 5 context packaging
# ---------------------------------------------------------------------------

class PackageStatus(str, Enum):
    """Outcome of context packaging (Phase 5)."""
    OK = "ok"
    REDUCED = "reduced"                  # optional context trimmed to fit budget
    NEEDS_SCOPE_REDUCTION = "needs_scope_reduction"


class BlockedFile(BaseModel):
    """A file blocked from the packaged context (secret/excluded file)."""
    path: str
    reason: str


class ContextPackage(BaseModel):
    """Result of packaging the minimum repository context for a handoff (Phase 5)."""
    status: PackageStatus
    repository_context: RepositoryContext
    acceptance_criteria: list[str] = Field(default_factory=list)
    blocked: list[BlockedFile] = Field(default_factory=list)
    tokens_used: int = Field(default=0, ge=0)
    budget_tokens: int = Field(default=0, ge=0)
    notes: list[str] = Field(default_factory=list)
    redacted_paths: list[str] = Field(default_factory=list)


class OutputContract(BaseModel):
    """What the local worker must return (handoff.md section 6)."""
    format: OutputFormat = OutputFormat.UNIFIED_DIFF
    include_summary: bool = True
    include_tests_run: bool = True
    include_uncertainties: bool = True

class HandoffRequest(BaseModel):
    """The compact structured payload sent to the local model (handoff.md section 6)."""
    request_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    task: str = Field(..., min_length=1, description="Imperative description of the bounded subtask.")
    objective: str = Field(..., min_length=1, description="Clear user-visible outcome.")
    difficulty: Literal[1, 2, 3, 4, 5] = Field(..., ge=1, le=5)
    length: LengthLevel
    repository_context: RepositoryContext
    constraints: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    output_contract: OutputContract = Field(default_factory=OutputContract)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# Model metadata (discovered from Unsloth /v1/models)
# ---------------------------------------------------------------------------

class ModelMetadata(BaseModel):
    """Metadata for the currently loaded model, as reported by Unsloth."""
    id: str = Field(..., description="The exact id string from GET /v1/models.")
    display_name: str = Field(..., description="Human-readable name.")
    loaded: bool = Field(default=False)
    backend: str = Field(default="", description="e.g. llama-server.")
    context_length: Optional[int] = Field(default=None, ge=0)


class ModelProfile(BaseModel):
    """Configurable capability profile for a local model (handoff.md section 5).

    The profile expresses what a model is *eligible* for: which difficulty
    levels may be offloaded and the longest scope eligible for a *full*
    handoff. The exact model identifier is a discovery concern
    (``OffloaderConfig.local_model_match``); ``name`` is the alias used in
    routing decisions and logs.
    """
    name: str = Field(
        default="Ternary Bonsai 2 27B PTQ1_0", min_length=1,
        description="Configurable alias for the local model.")
    use_for_difficulty: list[int] = Field(
        default_factory=lambda: [1, 2, 3],
        description="Difficulty levels (1-5) eligible for local handoff; hard "
        "(4) and expert (5) remain in Codex by default.")
    max_default_length: LengthLevel = Field(
        default=LengthLevel.MEDIUM,
        description="Longest scope eligible for a full local handoff; longer "
        "scope routes to partial handoff of bounded subtasks.")


# ---------------------------------------------------------------------------
# Worker result + validation outcome
# ---------------------------------------------------------------------------

class WorkerResult(BaseModel):
    """Normalized output from the local model. Exactly one ResultState drives it."""
    state: ResultState
    summary: str = Field(default="", description="Short human-readable summary of the work.")
    diff: str = Field(default="", description="Unified diff for code modifications (UNIFIED_DIFF).")
    structured: dict = Field(default_factory=dict, description="Non-diff structured payload (STRUCTURED).")
    # state-specific fields
    context_needed: str = Field(default="", description="Required for NEEDS_CONTEXT: what specific context is missing.")
    decline_reason: str = Field(default="", description="Required for DECLINED: why the task is out of scope.")
    error: str = Field(default="", description="Required for FAILED: transport/model/runtime error.")
    # fields required by the worker prompt contract (handoff.md section 7)
    assumptions: list[str] = Field(default_factory=list)
    uncertainties: list[str] = Field(default_factory=list)
    tests_run: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_state_fields(self) -> "WorkerResult":
        if self.state is ResultState.NEEDS_CONTEXT and not self.context_needed.strip():
            raise ValueError("state='needs_context' requires a non-empty context_needed")
        if self.state is ResultState.DECLINED and not self.decline_reason.strip():
            raise ValueError("state='declined' requires a non-empty decline_reason")
        if self.state is ResultState.FAILED and not self.error.strip():
            raise ValueError("state='failed' requires a non-empty error")
        return self

class ValidationOutcome(BaseModel):
    """Codex's independent verification of a WorkerResult (handoff.md section 9)."""
    outcome: ValidationOutcomeKind
    files_changed: list[str] = Field(default_factory=list, description="Paths the patch modified (relative).")
    unexpected_changes: list[str] = Field(default_factory=list, description="Paths outside the allowed scope.")
    tests_passed: bool = Field(default=False)
    lint_passed: bool = Field(default=True)
    type_checks_passed: bool = Field(default=True)
    applied: bool = Field(default=False, description="Whether the patch was applied to a working tree.")
    notes: str = Field(default="", description="Free-form validation notes.")

class OffloadResult(BaseModel):
    """End-to-end record: what was sent, what the worker returned, and how it validated."""
    request_id: str
    worker_result: WorkerResult
    validation: Optional[ValidationOutcome] = None
    decision: ValidationOutcomeKind = Field(default=ValidationOutcomeKind.REJECTED, description="Final disposition.")
    decision_note: str = Field(default="")


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------

class RoutingDecision(BaseModel):
    """The outcome of routing a task (handoff.md section 4) and the plan to execute it."""
    kind: RoutingDecisionKind
    model_alias: str = Field(default="", description="Configured alias of the local model to target.")
    reason: str = Field(default="")
    # For FULL_LOCAL: one handoff request. For PARTIAL_LOCAL: one per bounded subtask.
    subrequests: list[HandoffRequest] = Field(default_factory=list)
    assessment: Optional[TaskAssessment] = None
    # Lightweight task summary carried on the decision so explain() can
    # report difficulty/length even when the full assessment is not attached
    # (route() deliberately does not attach it).
    difficulty: Optional[int] = Field(
        default=None, description="Assessed difficulty (1-5) carried on the decision.")
    difficulty_label: Optional[str] = Field(
        default=None, description="Human-readable difficulty label.")
    length: Optional[LengthLevel] = Field(
        default=None, description="Assessed length level.")
    dry_run: bool = Field(default=False, description="True when this decision was "
                                                     "computed in dry-run mode; no "
                                                     "transport call should be made.")

    def explain(self) -> str:
        """One-line, log-safe routing explanation for logs/UI (handoff.md section 12)."""
        parts = [f"kind={self.kind.value}", f"model={self.model_alias or '-'}"]
        if self.assessment is not None:
            a = self.assessment
            difficulty, label, length = a.difficulty, a.difficulty_label.value, a.length.value
        elif self.difficulty is not None:
            difficulty, label = self.difficulty, (self.difficulty_label or "Unknown")
            length = self.length.value if self.length is not None else "-"
        else:
            difficulty = label = length = None
        if difficulty is not None:
            parts.append(f"difficulty={difficulty}/{label}")
            parts.append(f"length={length}")
        if self.subrequests:
            parts.append(f"subrequests={len(self.subrequests)}")
        parts.append(f"reason={self.reason}")
        if self.dry_run:
            parts.append("dry-run")
        return " | ".join(parts)
