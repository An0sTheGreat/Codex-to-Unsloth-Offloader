# Handoff Protocol Template

How ChatGPT/Codex decides to delegate coding work to the LLM in Unsloth Desktop.

## 1. Role

Codex is the planner, context owner, validator, and final reviewer. The Unsloth model is a bounded worker.

A handoff is valid only if Codex can name:
- the exact subtask,
- the allowed files/scope,
- the expected output format,
- the acceptance criteria,
- the validation method.

If Codex cannot name these, keep the work in Codex.

## 2. Required assessment block

Append this at the end of every coding-related response, even if no handoff occurs:

```markdown
### Coding Task Assessment
- **Difficulty:** <1-5> — <Trivial | Easy | Moderate | Hard | Expert>
- **Length:** <Short | Medium | Long | Extended> — <brief scope estimate>
- **Smaller-LLM Offload:** <Yes | Partial | No> — <recommended model or reason>
- **Execution:** <Local handoff | Partial handoff | Keep in Codex>
```

### Difficulty scale
| Level | Label | Typical work |
|---|---|---|
| 1 | Trivial | Formatting, comments, tiny deterministic edits |
| 2 | Easy | Small functions, known bug fixes, simple tests |
| 3 | Moderate | Multi-step logic, a few files, bounded debugging |
| 4 | Hard | Broad refactors, ambiguous debugging, architecture |
| 5 | Expert | Security-critical, novel architecture, complex migrations |

### Length scale
| Length | Meaning |
|---|---|
| Short | 1 file or <=100 changed LOC; one bounded step |
| Medium | 2-5 files or ~100-500 changed LOC; several steps |
| Long | >5 files, >500 changed LOC, or multiple dependent stages |
| Extended | Repo-wide / multi-phase work needing repeated review |

These are routing hints, not time promises.

## 3. Default routing matrix

Model aliases are configurable.

| Task profile | Local model (Ternary Bonsai 2 27B PTQ1_0) | Codex |
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
| Destructive / deployment actions | No | Codex + user approval |

## 4. Routing rules

### Full local handoff
Default to a full handoff only when all are true:
- Difficulty 1-2, or low-risk 3 with precise acceptance criteria.
- Scope is Short or Medium.
- Repository context can be packaged compactly.
- No secrets or sensitive files are required.
- The local model has enough context capacity.
- The task returns a patch/diff or structured answer for review.
- Failure can safely fall back to Codex.

Start target: `Ternary Bonsai 2 27B PTQ1_0` for bounded Difficulty 1-3 work.

### Partial local handoff
Use partial handoff when:
- The task is Difficulty 3-4 but has well-bounded subtasks.
- Repetitive work can be separated from architecture/reasoning.
- Tests/docs/boilerplate can be delegated alone.
- Codex wants a second implementation or review candidate.

Codex must decompose the work first.

### Keep in Codex
Do not offload automatically when:
- Difficulty 5.
- Requirements are ambiguous.
- The task changes architecture or security boundaries.
- The work touches secrets or credentials.
- Repository context is too large.
- The task needs destructive actions.
- Local model capability is unknown.
- The local output failed validation twice for the same task.

## 5. Model selection

Selection is policy-driven, not hard-coded.

```yaml
models:
  local_coder:
    display_name: "Ternary Bonsai 2 27B PTQ1_0"
    use_for_difficulty: [1, 2, 3]
    max_default_length: "Long"
```

Use the model currently loaded in Unsloth. If it can be detected, use that. Otherwise use an explicit configurable alias.

## 6. Handoff task packet

The bridge sends a compact structured payload:

```json
{
  "request_id": "generated-locally",
  "task": "The bounded coding subtask.",
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
    "Do not add dependencies unless requested",
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

Do not force this exact JSON shape if the integration surface works better with another format. Keep the same meaning.

## 7. Worker prompt contract

The local worker prompt must say:
1. It is a bounded worker, not the final orchestrator.
2. It acts only on the supplied context/scope.
3. It must not invent repository facts.
4. If context is missing, it returns a structured `needs_context` result, not a guess.
5. It prefers the smallest correct diff.
6. It reports assumptions and validation performed.
7. It returns output in the requested machine-parseable format.

## 8. Response states

The bridge normalizes local output to one of these:
- `completed` — valid candidate result returned.
- `needs_context` — worker needs specific extra context.
- `declined` — task is outside the worker's scope/capability.
- `failed` — transport/model/runtime error.
- `invalid_output` — response violates the schema/patch contract.

Codex decides what happens next.

## 9. Validation gate

Before accepting an offloaded result, Codex must:
1. Parse and inspect the diff/result.
2. Confirm only allowed files changed.
3. Reject unexpected dependency or config changes.
4. Run the narrowest relevant tests/lint/type checks.
5. Review failures instead of hiding them.
6. Escalate to broader validation when impact may be wider.
7. Summarize what came from the local worker vs. what Codex changed.

Local-model claims like "tests pass" are not proof. Codex must verify independently when it can run.

## 10. Retry and fallback policy
- Retry a transport failure once when safe.
- For `needs_context`, add only the requested context and retry once.
- For malformed output, ask for one schema-correction retry without changing scope.
- If validation fails twice, stop automatic delegation for that task and finish in Codex.
- Never loop forever between Codex and the local model.

## 11. Context minimization
Prefer, in order:
1. Task statement + acceptance criteria.
2. Relevant function/class snippets.
3. Direct dependencies/interfaces.
4. Tests for the target behavior.
5. Only then, broader files.

Do not send the whole repository by default.

## 12. Logging template

Each handoff may record non-secret metadata:
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

Do not log prompt bodies or source snippets by default, unless explicitly enabled.

## Completion log

- **Phase 0 — Capability Discovery (2026-10-21): PASSED.**
  - Read-only discovery. Transport = Unsloth OpenAI API `http://127.0.0.1:8888`. Model discovery via `GET /v1/models`. Model **Ternary Bonsai 2 27B PTQ1_0**.
  - Files: `docs/discovery.md` (new), `docs/development_plan.md`, `docs/readme.md`, this log.
  - Verified live: auth enforced (401 without key), web UI 200, `unsloth start codex --no-launch` auto-mints key.
  - Note: the launch probe touched Unsloth's own auto-generated agent config, not user project config.
  - Next: build skeleton + typed contracts.

- **Phase 1 — Skeleton and Contracts (2026-10-21): DONE.**
  - Created `bridge/` package: machine-readable contracts, path-independent config, structured errors, transport/routing interfaces.
  - Files: `bridge/__init__.py`, `schemas.py`, `config.py`, `config.json`, `errors.py`, `transport.py`, `routing.py`; `tests/` (schemas, config, errors, transport, routing).
  - Key: pydantic 2.13.4; unittest (stdlib); no live-Unsloth dependency. `Transport` is an ABC, so routing/validation depend only on the interface.
  - Validation: 60 tests pass.
  - Next: implement the Unsloth transport.

- **Phase 2 — Unsloth Transport (2026-10-21): DONE.**
  - Implemented `UnslothApiTransport` in `bridge/transport.py`: capability check, model discovery, request submission, bounded retry, error mapping, normalization to all 5 result states.
  - Built `bridge/offload.py` CLI (task packet JSON in, normalized `WorkerResult` out; `--smoke` flag).
  - Key: `POST /v1/chat/completions` is primary (fast, valid JSON); `POST /v1/responses` is fallback. JSON-only worker prompt; parse failures -> `invalid_output`.
  - Validation: 90 tests pass; live `--smoke` PASSED (`state == completed`, exit 0).
  - Follow-up (2026-10-21): swapped endpoint order to fix live hangs; 90 tests pass; live smoke PASSED.

- **Phase 3 — Task Classifier (2026-10-22): DONE.**
  - Deterministic `TaskClassifier` in `bridge/classifier.py` (`assess()` -> `TaskAssessment`): 1-5 difficulty, Short/Medium/Long/Extended length, six risk flags, offload eligibility.
  - Files: `bridge/classifier.py` (new), `bridge/schemas.py` (RiskFlag + risk_flags + is_high_risk), `bridge/__init__.py`, `tests/test_classifier.py` (23 tests).
  - Validation: 113 tests pass.
  - Next: routing policy.

- **Phase 4 — Model Routing Policy (2026-10-22): DONE.**
  - `PolicyRouter` in `bridge/routing.py` implements `route()`: configurable `ModelProfile`, detected-model matching, outputs (`FULL_LOCAL` / `PARTIAL_LOCAL` / `KEEP_IN_CODEX`), rules for model availability, `auto_handoff_enabled`, validation failures, context budget, `is_high_risk`, difficulty.
  - Files: `bridge/routing.py`, `bridge/schemas.py`, `tests/test_routing_policy.py` (22 tests).
  - Validation: 146 tests pass; live smoke PASSED.
  - Next: wire routing into the Codex orchestration path.

- **Phase 5 — Context Packager + Secret Guard (2026-10-22): DONE.**
  - `ContextPackager` in `bridge/context.py`: allowlisted collection, excluded-file/glob denylist, secret redaction, context budget. Router runs it before the budget check; returns `KEEP_IN_CODEX` on `needs_scope_reduction` or overflow.
  - Files: `bridge/context.py` (new), `bridge/routing.py`, `tests/test_context.py` (25 tests), `docs/readme.md`.
  - Validation: 171 tests pass.
  - Next: wire full orchestration (assess -> route -> offload -> validate -> footer).

- **Docs reconciliation (2026-10-23): DONE.**
  - Made the three state docs agree on phase status. Added `Status: PASSED` markers to Phases 1, 2, 4, 5 in `docs/development_plan.md`; added the missing Phase 4 bullet to `docs/readme.md`; fixed stale notes.
  - Files: `docs/development_plan.md`, `docs/readme.md`, this log.
  - Validation: docs cross-checked; 171 tests still pass. Committed as `1b62d4d`.

- **Phase 6 — Worker Prompt + Output Contract (2026-10-23): DONE.**
  - Verified the bounded-worker prompt covers tasks 1-5 (scope, JSON-only, full `WorkerResult` schema, assumptions/uncertainties/tests_run, `needs_context` naming). Implemented task 6: one correction retry for schema/format violations. On parse/validation failure, `send()` re-prompts once via `_submit(request, correction_error)`, prepending a correction note with the previous error. Still invalid -> `INVALID_OUTPUT`. Bounded, no loop.
  - Files: `bridge/transport.py` (new `_submit()`; `send()` retry), `tests/test_transport.py` (3 tests).
  - Key: correction retry is a semantic re-prompt, distinct from transport-level bounded retry in `_send_with_retry`. A self-reported `state=invalid_output` on first try is respected (no retry).
  - Validation: 174 tests pass.

## Live smoke command (user shell)
```powershell
cd "Y:\Programs\Codex to Unsloth Offloader"
$env:UNSLOTH_API_KEY = (Get-Content api_key.txt -Raw).Trim()
python -m bridge.offload --smoke
```

## Coding Task Assessment examples

```
### Coding Task Assessment
- **Difficulty:** 2 — Easy
- **Length:** Short — one isolated file change
- **Smaller-LLM Offload:** Yes — Ternary Bonsai 2 27B PTQ1_0
- **Execution:** Local handoff
```

```
### Coding Task Assessment
- **Difficulty:** 3 — Moderate
- **Length:** Short — 3 files, routing policy + 22 policy tests
- **Smaller-LLM Offload:** Yes
- **Execution:** Code complete and unit-tested
```

## Next work
Phase 7 — Patch Scope Validation: parse returned diffs, verify every changed path is allowlisted, detect new dependencies / config / binary changes and path traversal, reject or escalate unexpected changes. Add tests for malicious/malformed patches.
