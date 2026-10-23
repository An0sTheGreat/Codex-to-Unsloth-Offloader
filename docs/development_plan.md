# Development Plan — Unsloth Automatic Coding Handoff

This plan is intentionally path-agnostic. Codex should execute it from whatever project root the user selects later.

## Scope

Create an integration layer that lets ChatGPT Codex automatically delegate appropriate coding tasks/subtasks to the LLM loaded in Unsloth Desktop, then validate the result before accepting it.

### In scope

- Capability detection
- Unsloth local transport integration
- Loaded-model discovery when supported
- Task classification
- Model routing policy
- Handoff packet generation
- Full/partial delegation
- Structured result parsing
- Patch/diff safety checks
- Retry/fallback logic
- Logging/diagnostics
- Coding Task Assessment footer generation
- Unit/integration tests
- User documentation

### Out of scope unless explicitly approved later

- Modifying Unsloth packaged binaries
- Reverse-engineering private protocols when a supported interface is available
- Unattended deployment/release actions
- Secret handling beyond exclusion/redaction
- General-purpose autonomous agents unrelated to coding handoff
- Cloud model routing
- GUI automation

---

# Phase 0 — Capability Discovery

## Objective

Determine the supported integration surface before writing production code.

## Tasks

1. Inspect official/local Unsloth help/documentation for:
   - `unsloth start codex`
   - local API serving
   - model listing/status
   - currently loaded model discovery
   - plugin/extension mechanisms
2. Verify locally available CLI commands without mutating user configuration.
3. Determine how Codex Desktop can invoke a local helper/bridge within the chosen project.
4. Probe the local OpenAI-compatible API only if running and safe to do so.
5. Record:
   - endpoint shape,
   - model identifier behavior,
   - response format,
   - streaming support,
   - tool-call support relevant to this project,
   - timeout/error behavior.
6. Choose the architecture preference described in `project_instructions.md`.

## Deliverable

A short discovery note in the repository documentation explaining the selected integration surface and rejected alternatives.

## Acceptance criteria

- No undocumented endpoint is assumed as fact.
- No absolute user path is introduced.
- Architecture choice is evidence-based.
- Codex can describe a viable local invocation path.

## Gate

Do not begin Phase 1 until the transport surface is known.

> **Status: PASSED (2026-10-21).** Transport surface confirmed — Unsloth OpenAI-compatible API on `http://127.0.0.1:8888`, with `/v1/chat/completions` (primary) and `/v1/responses` (fallback), plus loaded-model discovery via `GET /v1/models`. Single local model **Ternary Bonsai 2 27B PTQ1_0** is installed and loaded. Architecture decision: **local bridge using the OpenAI-compatible API** (project_instructions.md Option 2). Full evidence in `docs/discovery.md`. Phase 1 may proceed.

---

# Phase 1 — Project Skeleton and Contracts

## Objective

Create the minimal code structure and machine-readable contracts without implementing automatic routing yet.

## Tasks

1. Choose the implementation language/tooling based on Phase 0 and the target environment.
2. Define typed/validated structures for:
   - task assessment,
   - handoff request,
   - allowed/read-only file scope,
   - result states,
   - model metadata,
   - validation outcome.
3. Define configuration loading with path-independent defaults.
4. Define transport and routing interfaces separately.
5. Add structured error types/states.
6. Add unit-test scaffolding.

## Acceptance criteria

- Configuration can be loaded without editing source.
- Core contracts match the semantics in `handoff.md`.
- Transport can be mocked.
- Tests run without a live Unsloth instance.

## Gate

Contracts must be stable enough to support transport implementation.

---

# Phase 2 — Unsloth Transport Adapter

## Objective

Implement reliable communication with the selected supported Unsloth interface.

## Tasks

1. Implement connection/capability checks.
2. Implement loaded-model discovery if supported.
3. Implement a request method with:
   - explicit timeout,
   - cancellation,
   - bounded retry,
   - streaming handling if needed.
4. Normalize responses to:
   - `completed`
   - `needs_context`
   - `declined`
   - `failed`
   - `invalid_output`
5. Add mock-based tests for all states.
6. Add an opt-in live smoke test.

## Acceptance criteria

- Unsloth unavailable produces a recoverable state, not a crash.
- A loaded local model can receive a simple bounded prompt in the live smoke test.
- Malformed responses are detected.
- No secret values are logged.

---

# Phase 3 — Coding Task Classifier

## Objective

Implement deterministic, inspectable routing inputs: difficulty, length, risk, and offload eligibility.

## Tasks

1. Implement the 1-5 difficulty scale from `handoff.md`.
2. Implement Short/Medium/Long/Extended length classification.
3. Add risk flags for:
   - security-sensitive work,
   - secrets/credentials,
   - destructive actions,
   - deployment/release,
   - architecture-heavy changes,
   - excessive context requirements.
4. Produce a normalized assessment object.
5. Add tests covering representative tasks.

## Acceptance criteria

- Same input yields stable classification when using deterministic rules.
- High-risk flags can override offload eligibility.
- Assessment contains a short human-readable reason.

> **Status: PASSED (2026-10-22).** Deterministic classifier implemented in `bridge/classifier.py` (`TaskClassifier.assess()`): 1-5 difficulty scale (base from risk flags + complexity-indicator bumps, clamped 1-5), Short/Medium/Long/Extended length (keyword indicators, checked Extended > Long > Medium > Short), six risk flags (security-sensitive, secrets/credentials, destructive actions, deployment/release, architecture-heavy, excessive context — from task text and/or oversized structured context, >20 files), and offload eligibility where any risk flag or difficulty 5 forces Keep in Codex. `RiskFlag` enum + `TaskAssessment.risk_flags` / `is_high_risk` added to `bridge/schemas.py`; exported from `bridge/__init__.py`. 23 new unit tests in `tests/test_classifier.py`; full suite now 113/113 pass. Phase 4 (routing policy) can now consume `risk_flags`/`is_high_risk` from the assessment.

---

# Phase 4 — Model Routing Policy

## Objective

Map eligible tasks to the local model profile or keep them in Codex.

## Tasks

1. Implement a configurable profile for:
   - `Ternary Bonsai 2 27B PTQ1_0`
2. Support the actual currently loaded model when detectable.
3. Implement routing outputs:
   - full local handoff,
   - partial handoff,
   - keep in Codex.
4. Add rules for context budget and previous validation failures.
5. Add a global automatic-handoff toggle and dry-run mode.
6. Provide a concise routing explanation for logs/UI.

## Acceptance criteria

- Bounded Difficulty 1-3 tasks route to the local model profile by default.
- Hard/expert/high-risk tasks remain in Codex by default.
- Unknown model capability degrades conservatively.

---

# Phase 5 — Context Packager and Secret Guard

## Objective

Send only the minimum context required for the delegated task.

## Tasks

1. Implement allowlisted context collection based on task scope.
2. Add excluded-file/glob policy.
3. Redact or block common secret-bearing files and environment values.
4. Support:
   - relevant snippets,
   - full target files when justified,
   - read-only context files,
   - acceptance criteria,
   - repository summary.
5. Enforce a configurable context budget.
6. Return `needs_scope_reduction` or equivalent when the task is too large.

## Acceptance criteria

- Secret fixtures are blocked in tests.
- Unrelated repository files are not included by default.
- Context overflow is handled before model invocation.
- All paths in packets are relative/project-scoped.

---

# Phase 6 — Worker Prompt and Output Contract

## Objective

Make local model output predictable enough for safe automation.

## Tasks

1. Implement the bounded-worker prompt contract in `handoff.md`.
2. Request a unified diff for code modifications by default.
3. Allow structured non-diff output for review/explanation tasks.
4. Require assumptions, uncertainties, and reported tests.
5. Parse responses into the normalized result states.
6. Add one correction retry for schema/format violations.

## Acceptance criteria

- The worker cannot silently expand file scope.
- `needs_context` can name specific missing information.
- Diff output can be parsed deterministically.
- Malformed output does not reach the application stage unchecked.

---

# Phase 7 — Patch Scope Validation

## Objective

Prevent delegated output from modifying unexpected files or violating request boundaries.

## Tasks

1. Parse returned patch/diff.
2. Verify every changed path is allowlisted.
3. Detect:
   - new dependencies,
   - configuration changes,
   - binary changes,
   - deletions,
   - suspicious path traversal.
4. Reject or escalate unexpected changes.
5. Add tests for malicious/malformed patches.

## Acceptance criteria

- A patch touching a non-allowed file is rejected.
- Path traversal attempts are rejected.
- Unexpected dependency changes require Codex review rather than automatic application.

---

# Phase 8 — Codex Validation Workflow

## Objective

Independently verify local-model work before accepting it.

## Tasks

1. Apply candidate patches safely to a reviewable working tree/workspace state.
2. Run targeted tests/lint/type checks based on project configuration.
3. Compare failures before/after where useful.
4. Allow Codex to repair small local-model defects.
5. Record whether the final solution was:
   - accepted unchanged,
   - accepted after Codex edits,
   - rejected and reimplemented by Codex.
6. Limit automatic local retries to prevent loops.

## Acceptance criteria

- Local claims are not treated as proof.
- Validation failure can never be reported as success.
- Two failed attempts on the same task force Codex fallback by default.

---

# Phase 9 — Automatic Handoff Orchestration

## Objective

Connect classification, routing, packaging, transport, and validation into one controlled workflow.

## Tasks

1. Implement end-to-end flow:
   - assess,
   - route,
   - package,
   - send,
   - parse,
   - validate,
   - fallback.
2. Add partial-handoff support for decomposed subtasks.
3. Add dry-run output showing what *would* be offloaded.
4. Add user-configurable disable/override controls.
5. Ensure interruption/cancellation propagates cleanly.

## Acceptance criteria

- Eligible task: local handoff succeeds and Codex validates.
- Ineligible task: no local call occurs.
- Unsloth down: task continues in Codex without data loss.
- Invalid worker output: safe fallback occurs.
- Partial task: only the bounded subtask is delegated.

---

# Phase 10 — Coding Task Assessment Footer

## Objective

Guarantee the required assessment appears at the end of every coding-related response.

## Tasks

1. Implement/render the exact four-line assessment schema from `handoff.md`.
2. Derive it from the actual routing decision when available.
3. When no handoff system is available, still render the assessment and state `Keep in Codex`.
4. Ensure non-coding responses do not receive the footer unless intentionally configured.
5. Add unit/snapshot tests.

## Acceptance criteria

Representative coding responses end with:

```markdown
### Coding Task Assessment
- **Difficulty:** 2 — Easy
- **Length:** Short — one isolated file change
- **Smaller-LLM Offload:** Yes — Ternary Bonsai 2 27B PTQ1_0
- **Execution:** Local handoff
```

The footer must reflect the actual route selected.

---

# Phase 11 — Observability and Troubleshooting

## Objective

Make routing decisions and failures understandable without leaking sensitive context.

## Tasks

1. Add structured local diagnostics for:
   - routing decision,
   - selected model metadata,
   - latency,
   - result state,
   - validation outcome,
   - fallback reason.
2. Exclude prompt/source bodies from logs by default.
3. Add a diagnostic command/view for current Unsloth connectivity.
4. Add a dry-run routing diagnostic.

## Acceptance criteria

- A user can tell why a task was or was not offloaded.
- Logs contain no secret test fixtures.
- Connectivity failures have actionable messages.

---

# Phase 12 — End-to-End Test Matrix

## Objective

Verify behavior across normal and failure scenarios.

## Required scenarios

1. Trivial single-file task → local model candidate → pass.
2. Moderate bounded multi-file task → local model → pass.
3. Hard architecture task → remain in Codex.
4. Security-sensitive task → remain in Codex.
5. Unsloth unavailable → fallback.
6. No model loaded → fallback.
7. Model times out → retry/fallback.
8. Model returns malformed schema → one correction retry → fallback if still invalid.
9. Model requests specific context → bounded context retry.
10. Model patch touches unauthorized file → reject.
11. Model patch fails tests → retry or Codex fallback.
12. Secret-bearing file requested → block.
13. Dry-run → no local invocation.
14. Auto-handoff disabled → no local invocation.
15. Coding footer accurately reports decision in all coding scenarios.

## Acceptance criteria

All deterministic tests pass. Live tests are opt-in and clearly separated.

---

# Phase 13 — Documentation and Release Readiness

## Objective

Make the project usable without reading the implementation source.

## Tasks

1. Finalize README/setup instructions.
2. Document configuration and the initial local model profile.
3. Document how to enable/disable automatic handoff.
4. Document privacy/security behavior.
5. Document failure modes and troubleshooting.
6. Document how to run unit tests and live integration tests.
7. Document architecture and extension points for future local models.
8. Run final lint/test/type checks.
9. Review the full diff for accidental hard-coded paths or credentials.

## Final acceptance criteria

- Project works from an arbitrary user-selected project root.
- No absolute project location is required.
- Unsloth integration uses a supported local interface.
- Offloading is conservative and explainable.
- The currently loaded Unsloth model can be used when supported by discovery.
- The initial local model (Ternary Bonsai 2 27B PTQ1_0) can be represented by a configuration profile.
- Codex independently validates delegated code.
- The user can disable automatic handoff immediately.
- Coding-related responses consistently include difficulty, length, and smaller-LLM offload suitability.

---

# Suggested first Codex prompt

Paste or adapt this after extracting the workflow into the project directory:

```text
Read readme.md, handoff.md, project_instructions.md, and development_plan.md completely.

Begin only Phase 0 (Capability Discovery). Do not implement production code yet.
Inspect the local environment and official/documented Unsloth integration surfaces needed for Codex delegation. Verify whether a stable plugin/extension API exists; otherwise prefer the supported OpenAI-compatible API or `unsloth start codex` path. Do not hard-code any absolute project directory.

At the end, report:
1. What you discovered.
2. Which integration architecture you recommend and why.
3. Any unresolved limitations.
4. The exact Phase 0 acceptance criteria that passed or failed.
5. The Coding Task Assessment block from handoff.md.
```
