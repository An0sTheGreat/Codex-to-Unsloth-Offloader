# Handoff Protocol Template

This document defines how ChatGPT/Codex should decide whether to delegate coding work to the LLM currently loaded in Unsloth Desktop.

## 1. Orchestration principle

Codex remains the **planner, context owner, validator, and final reviewer**. The Unsloth model is a bounded worker.

A handoff is successful only when Codex can describe:

- the exact subtask,
- the allowed files/scope,
- the expected output format,
- the acceptance criteria,
- and the validation method.

If those cannot be stated clearly, keep the work in Codex.

## 2. Required assessment for every coding-related response

Append this block at the **end** of every response related to coding, even when no handoff occurs:

```markdown
### Coding Task Assessment
- **Difficulty:** <1-5> — <Trivial | Easy | Moderate | Hard | Expert>
- **Length:** <Short | Medium | Long | Extended> — <brief scope estimate>
- **Smaller-LLM Offload:** <Yes | Partial | No> — <recommended model or reason>
- **Execution:** <Local handoff | Partial handoff | Keep in Codex>
```

### Difficulty scale

| Level | Label | Typical characteristics |
|---|---|---|
| 1 | Trivial | Formatting, comments, tiny deterministic edits |
| 2 | Easy | Small functions, known bug fixes, simple tests |
| 3 | Moderate | Multi-step logic, a few files, bounded debugging |
| 4 | Hard | Broad refactors, ambiguous debugging, architecture interaction |
| 5 | Expert | Security-critical, novel architecture, complex migrations, high-risk systems |

### Length scale

| Length | Working definition |
|---|---|
| Short | Usually 1 file or ≤100 changed LOC; one bounded step |
| Medium | Usually 2-5 files or roughly 100-500 changed LOC; several bounded steps |
| Long | Usually >5 files, >500 changed LOC, or multiple dependent implementation stages |
| Extended | Repository-wide/multi-phase work requiring repeated review and validation |

These are routing heuristics, not promises of execution time.

## 3. Default model routing matrix

Model aliases must be configurable.

| Task profile | Local model — Ternary Bonsai 2 27B PTQ1_0 | Codex |
|---|---:|---:|
| Formatting / comments / docs | Preferred | Suitable |
| Boilerplate | Preferred | Suitable |
| Small isolated function | Preferred | Suitable |
| Straightforward tests | Preferred | Suitable |
| Known-cause bug fix | Preferred | Review |
| Bounded 2-5 file change | Preferred | Review |
| Cross-file debugging | Conditional/Preferred | Review |
| Large refactor | Partial only | Preferred |
| Architecture decisions | Advisory only | Preferred |
| Security-sensitive code | Advisory only | Preferred |
| Destructive/deployment actions | No | Codex + user approval |

## 4. Automatic routing rules

### Full local handoff

Default to a full handoff only when **all** are true:

- Difficulty is 1-2, or low-risk difficulty 3 with precise acceptance criteria.
- Scope is Short or Medium.
- Required repository context can be packaged compactly.
- No sensitive files/secrets are required.
- The local model has sufficient context capacity.
- The task can return a patch/diff or structured answer for independent review.
- Failure can safely fall back to Codex.

Recommended starting target: `Ternary Bonsai 2 27B PTQ1_0` for bounded Difficulty 1-3 work.

### Partial local handoff

Use partial handoff when:

- the overall task is Difficulty 3-4 but contains well-bounded subtasks,
- repetitive implementation can be separated from architecture/reasoning,
- tests/docs/boilerplate can be delegated independently,
- or Codex wants a second implementation/review candidate.

Codex must decompose the work before sending it.

### Keep in Codex

Do not automatically offload when:

- Difficulty is 5,
- requirements are materially ambiguous,
- the task changes architecture or security boundaries,
- the work touches secrets/credentials,
- repository context would be excessively large,
- the task requires destructive operations,
- local model capability is unknown,
- or previous local output failed validation twice for the same task.

## 5. Local model selection

Selection order should be policy-driven rather than hard-coded.

Example logical alias:

```yaml
models:
  local_coder:
    display_name: "Ternary Bonsai 2 27B PTQ1_0"
    use_for_difficulty: [1, 2, 3]
    max_default_length: "Long"
```

The implementation should be able to use **the model currently loaded in Unsloth** without requiring the user to duplicate the exact model identifier in multiple places. If the loaded model can be detected, use that information. Otherwise expose an explicit configurable alias.

## 6. Handoff task packet

The bridge should send a compact structured payload equivalent to:

```json
{
  "request_id": "generated-locally",
  "task": "Implement the bounded coding subtask described here.",
  "objective": "Clear user-visible outcome",
  "difficulty": 2,
  "length": "Short",
  "repository_context": {
    "summary": "Only relevant architectural facts",
    "allowed_files": ["relative/path/example.py"],
    "read_only_files": ["relative/path/context.py"],
    "relevant_snippets": []
  },
  "constraints": [
    "Do not modify files outside allowed scope",
    "Do not add dependencies unless explicitly requested",
    "Do not access secrets",
    "Return a unified diff or structured patch"
  ],
  "acceptance_criteria": [
    "Behavior X is implemented",
    "Existing tests still pass",
    "New test Y passes"
  ],
  "output_contract": {
    "format": "unified_diff",
    "include_summary": true,
    "include_tests_run": true,
    "include_uncertainties": true
  }
}
```

Do not require this literal JSON shape if the selected integration surface works better with another format. Preserve the same semantics.

## 7. Prompt contract for the Unsloth worker

The local worker prompt should explicitly state:

1. It is a bounded worker, not the final orchestrator.
2. It may only act on the supplied context/scope.
3. It must not invent repository facts.
4. It should ask for missing context through a structured `needs_context` result rather than guessing.
5. It should prefer the smallest correct diff.
6. It must report assumptions and validation performed.
7. It must return output in the requested machine-parseable format.

## 8. Response states

The bridge should normalize local output into one of these states:

- `completed` — valid candidate result returned.
- `needs_context` — worker cannot proceed safely without specific additional context.
- `declined` — worker determines task is outside its assigned scope/capability.
- `failed` — transport/model/runtime error.
- `invalid_output` — response violates the expected schema/patch contract.

Codex decides what happens next.

## 9. Validation gate

Before accepting an offloaded result, Codex must:

1. Parse and inspect the diff/result.
2. Confirm only allowed files changed.
3. Reject unexpected dependency or configuration changes.
4. Run the narrowest relevant tests/lint/type checks.
5. Review failures rather than automatically hiding them.
6. Escalate to broader validation when the change could have wider impact.
7. Summarize what came from the local worker vs. what Codex changed afterward.

Local-model claims such as "tests pass" are not proof unless Codex independently verifies them when execution is available.

## 10. Retry and fallback policy

- Retry a transport failure once when safe.
- For `needs_context`, add only the requested relevant context and retry once.
- For malformed output, request one schema-correction retry without changing task scope.
- If validation fails twice, stop automatic local delegation for that task and finish/rework in Codex.
- Never loop indefinitely between Codex and the local model.

## 11. Context minimization

Prefer, in order:

1. Task statement + acceptance criteria.
2. Relevant function/class snippets.
3. Direct dependencies/interfaces.
4. Tests for the target behavior.
5. Only then, broader files.

Do not send the entire repository by default.

## 12. Logging template

Each handoff may record non-secret metadata such as:

```text
Request ID:
Timestamp:
Difficulty:
Length:
Selected local model:
Reason for offload:
Files supplied:
Files allowed to change:
Result state:
Validation commands:
Validation result:
Fallback/escalation:
```

Prompt bodies and source snippets should be excluded from persistent logs by default unless explicitly enabled.

---

## Completion log

- **Phase 0 — Capability Discovery (2026-10-21): DONE.**
  - **What was changed:** no protocol/routing changes. Read-only discovery performed; results recorded in `docs/discovery.md`; Phase 0 gate marked PASSED in `docs/development_plan.md`; status updated in `docs/readme.md`.
  - **Files modified:** `docs/discovery.md` (new), `docs/development_plan.md`, `docs/readme.md`, `handoff.md` (this log).
  - **Key decisions:** transport = Unsloth OpenAI-compatible API (`http://127.0.0.1:8888`), `/v1/responses` primary / `/v1/chat/completions` fallback; loaded-model discovery via `GET /v1/models` (never hard-code the model ID); auth key from env `UNSLOTH_STUDIO_AUTH_TOKEN`/`UNSLOTH_API_KEY` (never logged); single local model **Ternary Bonsai 2 27B PTQ1_0**.
  - **Validation:** verified live — Studio + llama-server running; `GET /v1/models` & `/v1/props` return 401 without key (auth enforced); `GET /` returns 200 (web UI); `unsloth start codex --no-launch` confirmed loaded model + auto-minted key mechanism. No secrets read/logged.
  - **Known issues / remaining:** exact `/v1/models` response shape and canonical model ID need a live authenticated check in Phase 2 (key not accessible to tools); the `unsloth start codex --no-launch` probe touched Unsloth's own internal agent config under `Y:\Programs\Unsloth\studio\auth\agents\codex` (auto-generated, not user project config).
  - **Next agent (Phase 1):** build project skeleton + typed contracts (task assessment, handoff request, file scope, result states, model metadata, validation outcome) matching this discovery; transport interface mockable; no live-Unsloth dependency in unit tests.

- **Phase 1 - Project Skeleton and Contracts (2026-10-21): DONE.**
  - **What was changed:** Created the `bridge/` Python package with machine-readable contracts, path-independent config loading, structured error types, and the transport/routing interfaces. No automatic routing implemented (per Phase 1 scope).
  - **Files created:**
    - `bridge/__init__.py` - package exports.
    - `bridge/schemas.py` - validated contracts: `TaskAssessment`, `HandoffRequest`, `RepositoryContext`, `OutputContract`, `WorkerResult`, `ModelMetadata`, `ValidationOutcome`, `OffloadResult`, plus all enums (`DifficultyLevel`, `LengthLevel`, `OffloadDecision`, `Execution`, `ResultState`, `OutputFormat`, `RoutingDecisionKind`, `ValidationOutcomeKind`).
    - `bridge/config.py` - `OffloaderConfig` + `load_config()` + `get_project_root()`; JSON file + env overrides + built-in defaults (precedence: env > JSON > defaults).
    - `bridge/errors.py` - structured `UnslothError` hierarchy (16 subtypes, each with a machine-readable `code`).
    - `bridge/transport.py` - `Transport` ABC, `MockTransport` (full), `UnslothApiTransport` (Phase-2 stub).
    - `bridge/routing.py` - `Router` ABC + `RoutingDecision`.
    - `bridge/config.json` - default config (loads without editing source).
    - `tests/` - unittest suite: `test_schemas.py`, `test_config.py`, `test_errors.py`, `test_transport.py`, `test_routing.py`.
  - **Key decisions:**
    - Language/tooling: Python 3.13.16 + pydantic 2.13.4 for validated schemas; unittest (stdlib) for tests - no live-Unsloth dependency.
    - `Transport` is an ABC; routing/validation depend only on the interface, so `MockTransport` fully satisfies "transport can be mocked."
    - `UnslothApiTransport` is a deliberate Phase-1 stub (raises `NotImplementedError`); its HTTP core is Phase 2's.
    - `Router`/`RoutingDecision` are interface-only; Phase 3 (classifier) and Phase 4 (routing policy) implement `assess()`/`route()`.
    - `WorkerResult` enforces state-specific required fields (needs_context->context_needed, declined->decline_reason, failed->error).
    - `TaskAssessment` enforces mutual consistency (difficulty<->difficulty_label, offload<->execution).
    - Config: JSON file (`bridge/config.json`) + env overrides + built-in defaults; project root derived from package location (no absolute user paths).
  - **Validation:** 60 unittest tests pass (`python -m unittest discover -s tests`); no live Unsloth instance required. Sanity checks confirm contracts validate/reject correctly.
  - **Known issues / remaining:**
    - `UnslothApiTransport` is a stub - Phase 2 must implement capability check, loaded-model discovery, request submission (timeout/cancel/bounded retry/streaming), and response normalization.
    - Router classifier (Phase 3) and routing policy (Phase 4) not implemented - only the interface.
    - Exact `/v1/models` response shape still needs a live authenticated check (key not accessible to tools) - carried from Phase 0.
  - **Next agent (Phase 2):** implement the Unsloth transport (HTTP core) against the OpenAI-compatible API, wiring the existing config + error mapping; verify loaded-model discovery and request submission.

- **Phase 2 - Unsloth Transport Adapter (2026-10-21): COMPLETE.**
  - **What was done:**
    - Implemented `UnslothApiTransport` in `bridge/transport.py`: capability check, loaded-model discovery via `GET /v1/models`, request submission via `POST /v1/chat/completions` (primary, reliable on current Unsloth build) with fallback to `POST /v1/responses`, explicit timeout, bounded retry (configurable), error mapping to structured `UnslothError` subclasses, and response normalization to all 5 `ResultState` values (`completed`, `needs_context`, `declined`, `failed`, `invalid_output`). Worker prompt enforces the handoff.md prompt contract (bounded worker, JSON-only output, state-specific fields). Key is read from env vars only, never logged.
    - Built `bridge/offload.py` CLI: loads config, builds `UnslothApiTransport`, accepts a task packet JSON (arg or file), calls `send()`, prints normalized `WorkerResult` as JSON to stdout; opt-in `--smoke` flag sends a trivial bounded prompt, prints model metadata + worker result.
    - Rewrote `TestUnslothApiTransport` in `tests/test_transport.py` with mock-based tests covering: capability check, model discovery (loaded/unloaded), auth error, all 5 result states, non-JSON output, schema validation failure, no-output response, chat/completions-shaped response (primary path), embedded/fenced JSON robustness, Chat→Responses fallback, connection error. All use a mocked `requests.Session` — no live server required.
  - **Files modified:** `bridge/transport.py` (replaced stub with full implementation; endpoint order swapped in `send()`), `bridge/offload.py` (new CLI), `tests/test_transport.py` (replaced stub tests with real transport tests + JSON-output tests).
  - **Validation:** 90 unittest tests pass (`python -m unittest discover -s tests`). No live Unsloth instance required. **Live smoke test PASSED** (user run): `worker_result.state == completed`, non-empty summary, model `0. GOAT - Ternary Bonsai 2 27B｜PTQ1_0` loaded, exit code 0, ~15–20s.
  - **Key decisions:**
    - Primary wire format: `POST /v1/chat/completions` (reliable on Unsloth Studio v0.1.909-beta — returns valid JSON in ~14s), fallback `POST /v1/responses` (unreliable there — hangs / returns prose). Both handled in `_send_with_retry`.
    - Output extraction handles both Responses `output[].text` / `output[].content[].text` shapes and Chat Completions `choices[0].message.content`.
    - Model output is parsed as JSON; fenced-code-block wrapping is stripped. Parse failures / schema mismatches → `invalid_output` state (recoverable, not a crash). Transport-level failures (auth, connection, timeout, server 5xx) raise `UnslothError` subclasses.
    - `check_capability()` = successful `discover_loaded_model()`.
    - Retry: up to `max_retries` attempts (default 2) on 429/5xx and transport exceptions; 401/403 fail immediately.
  - **Known issues / remaining:**
    - Live smoke test added and PASSED; `bridge/offload.py` CLI complete.
    - Cancellation is handled via the request timeout (bounded); full mid-stream cancellation via `requests`' `cancel` mechanism is not implemented (acceptable for bounded tasks).
    - Exact `/v1/models` `id` string confirmed via live run: `0. GOAT - Ternary Bonsai 2 27B｜PTQ1_0` (note the `｜` character, not `|`; `local_model_alias` in config is a separate alias string and matches the model display, so discovery works).

- **Phase 2 follow-up — worker JSON-output fix (2026-10-21): DONE — live `--smoke` PASSED.**
  - **Real root cause (found by live-probing the running server, 2026-10-21):** the transport tried `POST /v1/responses` *first*. On this Unsloth build that endpoint is unreliable — it hangs (ReadTimeout even at 300s) or returns prose, so the worker result surfaced as `state: invalid_output` / `model response not valid JSON`. Meanwhile `POST /v1/chat/completions` is fast (~14s for the full smoke prompt) and returns a valid `WorkerResult` JSON object. The prompt was already correctly demanding JSON-only (previous fix); the remaining failure was **endpoint selection**, not the prompt.
  - **What was changed:** `bridge/transport.py` — swapped the primary/fallback endpoints in `send()`: `/v1/chat/completions` is now primary (system message = `_worker_instructions()`, user message = `_worker_prompt()`), with `/v1/responses` demoted to the fallback. The JSON-only prompt, `_parse_worker_text`, and `WorkerResult` schema validation are unchanged and still strict.
  - **Files modified:** `bridge/transport.py` (endpoint order in `send()`), `tests/test_transport.py` (updated the order-dependent fallback test + added chat-completions response-shape test).
  - **Test changes:** `test_send_falls_back_to_chat` renamed to `test_send_falls_back_to_responses` (chat/completions tried first, responses fallback); added `test_send_completed_chat_completions_shape` (primary path extracts + validates a chat-shaped body). The 4 earlier JSON-output tests and all existing transport tests unchanged and still pass.
  - **Validation (verified):** full suite `python -m unittest discover -s tests -p test_*.py` → **90 tests, all OK**. **Live `--smoke` PASSED** (user shell run, 2026-10-21): `worker_result.state == completed`, non-empty summary, model `0. GOAT - Ternary Bonsai 2 27B｜PTQ1_0` loaded, exit code 0, ~15–20s (no more 6-min hang).
  - **Known issues / blockers:** none in the code. Cancellation is timeout-bounded only (acceptable for bounded tasks). Leftover diagnostic probes `probe_smoke.py` / `probe_fallback.py` were deleted (no longer present in the project root).
  - **Success criteria (task):** all met — unit tests verified (valid JSON → `completed`; malformed/non-JSON → `invalid_output`; validation stays strict) and live `state: completed` confirmed.

- **Phase 3 — Coding Task Classifier (2026-10-22): COMPLETE.**
  - **What was done:**
    - Implemented deterministic `TaskClassifier` in `bridge/classifier.py` (`assess(task_description, context=None)` → `TaskAssessment`): 1-5 difficulty (base from risk flags — destructive/deployment/architecture-heavy = 4, security/secrets = 3 — plus +1 per matched complexity indicator (multi-file, debug/investigate, refactor, complex/tricky, integration, end-to-end/e2e, rewrite, large/big, api/endpoint, expert); clamped 1..5), Short/Medium/Long/Extended length (keyword indicators checked Extended > Long > Medium > Short, default Short), and the six `handoff.md` §4 risk flags (security-sensitive, secrets/credentials, destructive actions, deployment/release, architecture-heavy, excessive context) via inspectable lowercase substring patterns, plus excessive-context detection from oversized structured `context` (>20 files). Offload eligibility in the assessment: any risk flag or difficulty 5 → No/Keep in Codex (high-risk flags override offload eligibility); difficulty 4 → Partial/Partial handoff; difficulty 3 → Local handoff for Short/Medium, Partial for Long/Extended; difficulty 1-2 → Local handoff (Partial for Extended). Non-empty human-readable `reason` always set.
    - Contract extension: `RiskFlag` enum (6 values) + `TaskAssessment.risk_flags: list[RiskFlag]` (default empty) + `TaskAssessment.is_high_risk` property added to `bridge/schemas.py`; `RiskFlag` and `TaskClassifier` exported from `bridge/__init__.py`. Backwards compatible (defaulted field).
  - **Files modified:** `bridge/schemas.py` (RiskFlag + risk_flags + is_high_risk), `bridge/classifier.py` (new), `bridge/__init__.py` (exports), `tests/test_classifier.py` (new, 23 tests).
  - **Validation:** full suite `python -m unittest discover -s tests -p "test_*.py"` → **113 tests, all OK** (90 prior + 23 new); no live Unsloth instance required. Manual spot-check of 10 representative tasks confirmed expected difficulty/length/flags/offload combos (trivial typo → d1/Short/Yes; debug+integration across codebase → d3/Long/Partial; d4 no-flag → Partial; architecture flag → No/Keep in Codex; password → No even at low difficulty; deploy → No; entire-repo refactor → excessive-context No; migrate codebase → architecture+excessive No; module update → d1/Medium/Yes).
  - **Key decisions:**
    - Classifier is a standalone class, not a `Router` subclass yet; Phase 4 wires it into a concrete `Router` (assess separate from route, per the `Router` docstring design).
    - Risk flags live on the assessment (schema), so the Phase 4 router and the Phase 10 footer can both consume them.
    - `EXCESSIVE_CONTEXT` does not raise difficulty (routing concern, not complexity); it overrides eligibility via the flag rule.
    - `refactor` is a complexity bump (difficulty +1), not an architecture-heavy flag — bounded refactors stay eligible for local offload per `handoff.md`.
    - No config knobs in Phase 3 (deterministic rules per spec); routing thresholds/model alias are Phase 4's.
  - **Known issues / remaining:** none in the classifier. Phase 4 (routing policy — `Router.route()` implementation, model alias from config, dry-run toggle, context budget) is next.
  - **Next agent (Phase 4):** implement `route()` in a concrete `Router` consuming `TaskAssessment` (risk flags, difficulty, length, `is_high_risk`), produce `RoutingDecision` with `HandoffRequest` subrequests, wire in `TaskClassifier`, add policy tests (incl. dry-run + config toggle).

- **Phase 4 — Model Routing Policy (2026-10-22): COMPLETE.**
  - **What was done:**
    - Concrete `PolicyRouter` in `bridge/routing.py` implementing the deterministic `route()` policy on the Phase 3 `TaskAssessment`, plus the global `auto_handoff_enabled` toggle and `dry_run` mode.
    - Configurable `ModelProfile` (name, `use_for_difficulty`, `max_default_length`); default targets **Ternary Bonsai 2 27B PTQ1_0** (difficulty 1-3, full handoff up to `MEDIUM`). A *detected* matching model uses the actual loaded display name as the alias; mismatch/unknown capability degrades conservatively to Keep in Codex.
    - Routing outputs: `FULL_LOCAL` (1 `HandoffRequest`), `PARTIAL_LOCAL` (1 per bounded subtask, difficulty capped at 3, `Short`), `KEEP_IN_CODEX`.
    - Rules: model availability (detected model must match `config.local_model_match`), `auto_handoff_enabled`, validation failures (`max_validation_failures`, default 2), context budget (`context_budget_tokens`, default 16384), `is_high_risk`, difficulty eligibility.
    - Concise log-safe `RoutingDecision.explain()` for logs/UI.
  - **Files modified:** `bridge/schemas.py` (summary fields on `RoutingDecision`; `explain()`), `bridge/routing.py` (`route()`, `_keep()`, `_difficulty_eligible()`), `tests/test_routing_policy.py` (22 tests).
  - **Validation:** full suite `python -m unittest discover -s tests -p "test_*.py"` → **146 tests, all OK**; no live Unsloth required. Live `python -m bridge.offload --smoke` round-trip **PASSED** in the user's shell: `model.id = 0. GOAT - Ternary Bonsai 2 27B｜PTQ1_0`, `loaded: true`, `worker_result.state == completed` (see `docs/readme.md` and `docs/discovery.md`).
  - **Key decisions:** profile `use_for_difficulty` is the difficulty source of truth; `max_difficulty_full_handoff` is a ceiling that applies only to the built-in default profile (a custom profile is fully authoritative and may enable difficulties above the default ceiling). `route()` does not attach the full assessment; it carries a lightweight summary (`difficulty`, `difficulty_label`, `length`) so `explain()` reports those fields even when the assessment is not attached.
  - **Known issues / remaining:** none for the routing policy itself. Next: wire `route()` into the Codex orchestration path and the Coding Task Assessment footer, and add an end-to-end live round-trip test (transport → route → validate).
  - **Next agent (Phase 5):** integrate the `PolicyRouter` into the Codex orchestration path (assess → route → offload → validate → footer), surfacing `RoutingDecision.explain()` in logs/UI.

- **Phase 5 (Context Packager and Secret Guard) complete (2026-10-22): COMPLETE.**
  - **What was done:** New `bridge/context.py` — `ContextPackager` (allowlisted collection, excluded-file/glob policy, secret content redaction, context-budget enforcement, project-scoped paths) plus `estimate_tokens()` and `redact_content()` helpers. The `PolicyRouter.route()`/`plan()` now accept optional `scope` + `project_root`, run the packager *before* the budget check, and return `KEEP_IN_CODEX` on `needs_scope_reduction` or budget overflow; packaged context and acceptance criteria are threaded into each `HandoffRequest` subrequest.
  - **Files modified:** `bridge/context.py` (new; added missing `ContextPackage` import), `bridge/routing.py` (new `scope`/`project_root` params on `route()`/`plan()`; `packaged` threaded through `_build_request`/`_partial_subrequests`; base `Router.plan` forwards `scope`/`project_root` only when provided so legacy routers stay compatible), `tests/test_context.py` (new, 25 tests), `docs/readme.md` (Phase 5 status entry + expanded `bridge/` file list).
  - **Validation:** full suite (Python310, `python -m unittest discover -s tests -p "test_*.py"`) → **171 tests, all OK** (146 prior + 25 new). Phase 5 acceptance criteria verified: secret fixtures blocked; unrelated repo files excluded by default; required-context overflow → `needs_scope_reduction` / KEEP_IN_CODEX (handled before model invocation); all packet paths project-relative. End-to-end routing smoke confirmed: FULL_LOCAL with packaged scope (allowed/read-only/snippets/acceptance criteria carried into subrequest), `.env` blocked and absent from `allowed_files`, overflow → KEEP_IN_CODEX with `needs scope reduction` reason.
  - **Key decisions:** deterministic ~4-chars-per-token estimator; gitignore-style globs (`**` spans components, `*` within a segment); `ContextPackage` is the unit the router consumes; a missing (non-excluded) file is noted ("file not found") but not blocked and still kept in `allowed_files` (worker may create it), contributing no snippet; base `Router.plan` forwards `scope`/`project_root` only when non-None for legacy compatibility.
  - **Known issues / remaining:** no live-Unsloth dependency needed (packager is pure aside from reading project files). The end-to-end live round-trip (transport → route → validate) and the Codex orchestration-path wiring (assess → route → offload → validate → footer, surfacing `RoutingDecision.explain()`) remain — carried from Phase 4.
  - **Next agent:** wire the full `route()` → offload → validate → footer orchestration path for real Codex use, and add an end-to-end live round-trip test.

**Live smoke-test command (user shell):**
```powershell
cd "Y:\\Programs\\Codex to Unsloth Offloader"
$env:UNSLOTH_API_KEY = (Get-Content api_key.txt -Raw).Trim()
python -m bridge.offload --smoke
```

### Coding Task Assessment
- **Difficulty:** 2 — Easy — endpoint-order swap in `send()` + 1 test update
- **Length:** Short — 2 files, transport dispatch only; prompt/parse/validation logic unchanged
- **Smaller-LLM Offload:** Yes — bounded change
- **Execution:** Code complete and unit-tested; user to run `python -m bridge.offload --smoke` to confirm live `state: completed`.

### Coding Task Assessment
- **Difficulty:** 3 — Moderate — routing policy implementation + 22 policy tests
- **Length:** Short — 3 files (schemas, routing, new test module); no transport/parse behavior changed
- **Smaller-LLM Offload:** Yes — bounded routing policy + tests
- **Execution:** Code complete and unit-tested (146/146 pass); live smoke test PASSED in the user's shell (`state: completed`)
