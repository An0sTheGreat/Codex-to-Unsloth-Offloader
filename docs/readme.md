# Unsloth ↔ ChatGPT Codex Automatic Handoff

> Project workflow template for building a local delegation layer that lets ChatGPT Codex hand bounded coding work to the LLM currently loaded in Unsloth Desktop.

## Project status

- **Phase 0 (Capability Discovery) complete (2026-10-21).** Transport surface confirmed: Unsloth OpenAI-compatible API on `http://127.0.0.1:8888` with loaded-model discovery via `GET /v1/models`. Single local model **Ternary Bonsai 2 27B PTQ1_0** installed and loaded. Chosen architecture: local bridge using the OpenAI-compatible API. See `docs/discovery.md`.
- **Phase 1 (Project Skeleton and Contracts) complete (2026-10-21).** The `bridge/` Python package (validated contracts, path-independent config, structured errors, transport + routing interfaces) and a `tests/` unittest suite are in place; 60 tests pass with no live Unsloth instance. The live transport (Phase 2) and automatic routing (Phase 3/4) are not yet implemented. See the completion log in `handoff.md`.
- **Phase 2 (Unsloth Transport) complete (2026-10-21).** `UnslothApiTransport` is implemented and unit-tested (90 tests pass). Live probing on the running server showed `POST /v1/responses` is unreliable (hangs / returns prose) while `POST /v1/chat/completions` is fast and returns valid `WorkerResult` JSON, so the transport uses `/v1/chat/completions` as the primary endpoint and `/v1/responses` as the fallback. The `--smoke` CLI entry point is complete and the live `--smoke` round-trip PASSED (`worker_result.state == completed`, exit 0; see `handoff.md`). See the completion log in `handoff.md`.
- **Phase 3 (Coding Task Classifier) complete (2026-10-22).** Deterministic `TaskClassifier` in `bridge/classifier.py` classifies free-text coding requests into a validated `TaskAssessment`: 1-5 difficulty, Short/Medium/Long/Extended length, six risk flags (security, secrets/credentials, destructive, deployment/release, architecture-heavy, excessive context — from task text and/or oversized context), and offload eligibility where any risk flag or difficulty 5 forces Keep in Codex. 23 new tests in `tests/test_classifier.py`; full suite now 113 tests, all pass. Routing policy (Phase 4) is complete (see the entry below).
- **Phase 4 (Model Routing Policy) complete (2026-10-22).** `PolicyRouter` in `bridge/routing.py` implements the deterministic `route()` policy on the Phase 3 `TaskAssessment`: configurable `ModelProfile` (Ternary Bonsai 2 27B PTQ1_0, difficulty 1-3, full handoff up to `Medium`), detected-model matching, routing outputs (`FULL_LOCAL` / `PARTIAL_LOCAL` / `KEEP_IN_CODEX`), and rules for model availability, `auto_handoff_enabled`, validation failures, context budget, `is_high_risk`, and difficulty eligibility. Concise log-safe `RoutingDecision.explain()`. 22 policy tests added in `tests/test_routing_policy.py`; full suite now 146 tests, all pass. Live `--smoke` round-trip PASSED (`worker_result.state == completed`).
- **Phase 5 (Context Packager and Secret Guard) complete (2026-10-22).** `ContextPackager` in `bridge/context.py` sends only the minimum context required for the delegated task: allowlisted collection (only explicitly named `allowed_files`/`read_only_files` are read; unrelated repository files are never included by default), an excluded-file/glob denylist (`config.excluded_files`) that blocks secret/credential files outright, inline secret redaction (API keys, tokens, passwords, private-key blocks, Bearer tokens) applied to file contents and user summaries/snippets, and a configurable `context_budget_tokens` budget. Required (allowed) context always fits or the packager returns `needs_scope_reduction`; optional context (read-only files, user snippets) is trimmed to fit (status `reduced`). All paths in the packet are relative/project-scoped — absolute paths under the root are converted, and `..` traversal or out-of-root absolute paths are blocked. The `PolicyRouter` now accepts an optional `scope` dict and `project_root`, runs the packager *before* the budget check, and returns `KEEP_IN_CODEX` on `needs_scope_reduction` or budget overflow (overflow handled before any model invocation); packaged context + acceptance criteria are threaded into each subrequest. 25 new tests in `tests/test_context.py`; full suite now 171 tests, all pass.
- **Phase 6 (Worker Prompt and Output Contract) complete (2026-10-23).** The bounded-worker prompt contract in `bridge/transport.py` is complete: the worker acts only on the supplied context/file scope (no silent scope expansion), returns ONLY a single valid JSON object, and fills the full `WorkerResult` schema — `state` (5 states), `summary`, `diff` (unified diff by default), `structured` (for review/explanation tasks), `context_needed`/`decline_reason`/`error`, and the required `assumptions`/`uncertainties`/`tests_run` fields. Responses are parsed deterministically into the normalized `ResultState` values. New: a single **correction retry** for schema/format violations — on a parse or validation failure, the transport re-prompts the model once (via `_submit(request, correction_error)`) with a correction note carrying the previous error; if the corrected output is still invalid (or the correction request fails) it returns `INVALID_OUTPUT`, so malformed output never reaches the validation gate unchecked. Bounded: exactly one re-prompt, never an unbounded loop. Implemented in `UnslothApiTransport._submit()`/`send()`; 3 new tests in `tests/test_transport.py`; full suite now **174 tests, all OK**.
- **Phase 7 (Patch Scope Validation) complete (2026-10-23).** `bridge/validate.py` (`PatchValidator`) parses a returned unified diff and validates it against the request's `allowed_files`/`read_only_files`: an allowlisted-scope check plus detection of dependency/lock, configuration, binary, deletion, and path-traversal changes; the `PatchValidationResult.valid` gate is the single accept/reject/escalate decision (hard violations reject; dependency/config/deletion flags escalate to Codex review). It is wired into the offload flow (`bridge/offload.py`, `run_handoff`): on a `completed` result the gate runs before acceptance and a failed patch returns exit code 4 (`EXIT_VALIDATION_FAILED`) with a `validation` key in the JSON output, never reported as success; a `needs_context` result retries once with the requested context appended (bounded, handoff.md section 10). `PatchValidationResult`/`PatchValidator` exported from `bridge/__init__.py`; 26 tests in `tests/test_validate.py`, offload-flow gate + bounded retry in `tests/test_offload.py`; full suite now 212 tests, all OK.
- **Phase 8 (Codex Validation Workflow) complete (2026-10-23).** Candidate patches are independently applied to an isolated review worktree and verified before acceptance (`bridge/verification.py`): the repo root is resolved via Git (no hard-coded paths), the worktree is created from the current state, `git apply --check` preflights the candidate, the patch is applied inside the worktree only, the project's own validation (`python -m unittest discover -s tests`, plus lint/type-check only when configured) runs against it, and unexpected tracked changes are detected. A scope-invalid patch never reaches verification; failed independent verification returns exit code 4 (`EXIT_VALIDATION_FAILED`) with a `verification` key in the JSON output, never reported as success. The local worker's self-reported `tests_run` is never trusted. 7 new tests in `tests/test_verification.py`; full suite now 219 tests, all OK.
- No project directory is assumed or hard-coded.
- Baseline observed:
  - Unsloth Desktop: `v0.1.909-beta`
  - Unsloth package: `2026.10.7`
- The integration must perform runtime capability/version detection instead of depending on those exact versions.

## Important architecture note

Current Unsloth Desktop already provides two useful native integration surfaces:

1. `unsloth start codex ...` for running Codex against a local Unsloth model.
2. An OpenAI-compatible local API for apps/scripts that need direct model access.

This project should **not** recreate model serving. Its purpose is to add an orchestration layer that decides *when* a coding task is appropriate for local offload, packages a bounded handoff, submits it to the currently loaded model, validates the returned work, and returns control to Codex.

If Unsloth exposes a supported plugin/extension API, Codex may implement the integration through it. If no stable plugin API exists, build a companion local bridge/adapter instead of patching Unsloth binaries or automating the GUI.

## Primary goal

When a coding-related request is handled by ChatGPT/Codex:

1. Classify the task by difficulty and length.
2. Decide whether a local model can safely perform all or part of it.
3. If appropriate, automatically hand off a tightly scoped subtask to the model loaded in Unsloth.
4. Receive the result as a patch/diff or structured response.
5. Validate the result with Codex using repository context, tests, linters, and diff review.
6. Preserve Codex as the final orchestrator and reviewer.
7. End every coding-related user response with the standard **Coding Task Assessment** block defined in `handoff.md`.

## Initial model routing targets

Treat the model name as a configurable alias rather than a hard-coded identifier.

### Ternary Bonsai 2 27B PTQ1_0

Best-fit starting tasks:

- Boilerplate and repetitive implementation
- Small single-file changes
- Straightforward unit tests
- Documentation/code comments
- Simple bug fixes with a known cause
- Mechanical refactors
- Data transformations and parsers with clear acceptance criteria
- Isolated helper functions
- Medium multi-file implementation
- More involved debugging
- Bounded refactors
- Test generation requiring cross-file context
- API/client integration with clear specifications
- Code review of a defined diff

Avoid autonomous use for:

- Architectural decisions
- Ambiguous multi-module debugging
- Security-sensitive code
- Dependency migrations with uncertain compatibility
- Large refactors requiring broad repository understanding

Codex should still retain final review for high-risk, security-sensitive, production-critical, or architecture-heavy work.

## Recommended execution model

```text
User coding request
      │
      ▼
ChatGPT/Codex task classifier
      │
      ├── Keep in Codex ───────────────► Codex executes/reviews
      │
      ├── Partial offload ─► Local task packet ─► Unsloth LLM
      │                                      │
      │                                      ▼
      │                              patch/structured result
      │                                      │
      └──────────────────────────────────────▼
                                  Codex validation gate
                                          │
                                          ▼
                                  final user response
                                          │
                                          ▼
                              Coding Task Assessment
```

## Files in this workflow

- `readme.md` — project overview and usage template.
- `handoff.md` — delegation policy, task packet, routing matrix, and response footer contract.
- `project_instructions.md` — durable instructions for Codex while implementing the integration.
- `development_plan.md` — phased scope and acceptance criteria for Codex to execute.
- `bridge/` - the offloader package: `schemas.py` (contracts + enums), `config.py` + `config.json` (path-independent config), `errors.py` (structured errors), `transport.py` (Unsloth transport), `classifier.py` (Phase 3 task classifier), `routing.py` (Phase 4 routing policy), `context.py` (Phase 5 context packager + secret guard), `validate.py` (Phase 7 patch scope validation), `verification.py` (Phase 8 independent apply + verify), `diagnostics.py` (Phase 8 log-safe diagnostics), `offload.py` (Phase 2 CLI).
- `tests/` - unit-test suite (no live Unsloth required); run with `python -m unittest discover -s tests`.

All files are intentionally stored at the ZIP root. Move them into whichever project directory you choose later.

## How to use with Codex Desktop

1. Choose/create the project directory yourself.
2. Extract these four Markdown files into that project.
3. Open the project in the ChatGPT Codex desktop app.
4. Ask Codex to read all four files before making changes.
5. Optionally use `/init` to create an `AGENTS.md` for the repository, then instruct Codex to incorporate the durable rules from `project_instructions.md` without deleting useful generated guidance.
6. Begin with Phase 0 in `development_plan.md`.
7. Do not allow later phases to begin until the previous phase's acceptance criteria pass.

## Success criteria

A successful implementation should:

- Work without hard-coded absolute paths.
- Detect whether Unsloth is available and a model is loaded.
- Prefer supported Unsloth CLI/API surfaces.
- Avoid GUI automation unless explicitly approved as a last resort.
- Route tasks according to configurable policy.
- Support full-task and subtask delegation.
- Send only the minimum repository context required.
- Prefer patch/diff output over unrestricted file writes by the local model.
- Validate every offloaded result before accepting it.
- Fall back cleanly to Codex if Unsloth is unavailable, times out, returns malformed output, or fails validation.
- Never silently discard local-model failures.
- Append the standard coding assessment footer to every coding-related response.

## Security and privacy requirements

- Bind local services to loopback by default.
- Never transmit secrets, API keys, `.env` values, credentials, or unrelated private files to the local model.
- Respect repository ignore rules and an explicit denylist.
- Log metadata and decisions, but do not log secret-bearing prompt contents by default.
- Require explicit approval before any destructive action, deployment, release, remote push, or credential operation.
- Treat local-model output as untrusted until validated.

## Official references used when drafting this workflow

- Unsloth Desktop: https://www.unsloth.ai/
- Example Unsloth Codex launch command: https://unsloth.ai/models/qwen3.8-27b
- OpenAI Codex with ChatGPT plans / project instructions: https://help.openai.com/en/articles/11369540-using-codex-with-your-chatgpt-plan
- OpenAI Codex development workflow guidance: https://developers.openai.com/cookbook/examples/codex/iterating-development-workflows-with-codex
