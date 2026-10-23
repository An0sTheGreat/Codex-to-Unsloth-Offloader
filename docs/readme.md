# Unsloth ↔ ChatGPT Codex Automatic Handoff

> Project workflow template for building a local delegation layer that lets ChatGPT Codex hand bounded coding work to the LLM currently loaded in Unsloth Desktop.

## Project status

- **Phase 0 (Capability Discovery) complete (2026-10-21).** Transport surface confirmed: Unsloth OpenAI-compatible API on `http://127.0.0.1:8888` with loaded-model discovery via `GET /v1/models`. Single local model **Ternary Bonsai 2 27B PTQ1_0** installed and loaded. Chosen architecture: local bridge using the OpenAI-compatible API. See `docs/discovery.md`.
- **Phase 1 (Project Skeleton and Contracts) complete (2026-10-21).** The `bridge/` Python package (validated contracts, path-independent config, structured errors, transport + routing interfaces) and a `tests/` unittest suite are in place; 60 tests pass with no live Unsloth instance. The live transport (Phase 2) and automatic routing (Phase 3/4) are not yet implemented. See the completion log in `handoff.md`.
- **Phase 2 (Unsloth Transport) complete (2026-10-21).** `UnslothApiTransport` is implemented and unit-tested (90 tests pass). Live probing on the running server showed `POST /v1/responses` is unreliable (hangs / returns prose) while `POST /v1/chat/completions` is fast and returns valid `WorkerResult` JSON, so the transport uses `/v1/chat/completions` as the primary endpoint and `/v1/responses` as the fallback. The `--smoke` CLI entry point is complete and the live `--smoke` round-trip PASSED (`worker_result.state == completed`, exit 0; see `handoff.md`). See the completion log in `handoff.md`.
- **Phase 3 (Coding Task Classifier) complete (2026-10-22).** Deterministic `TaskClassifier` in `bridge/classifier.py` classifies free-text coding requests into a validated `TaskAssessment`: 1-5 difficulty, Short/Medium/Long/Extended length, six risk flags (security, secrets/credentials, destructive, deployment/release, architecture-heavy, excessive context — from task text and/or oversized context), and offload eligibility where any risk flag or difficulty 5 forces Keep in Codex. 23 new tests in `tests/test_classifier.py`; full suite now 113 tests, all pass. Routing policy (Phase 4) follows.
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
- `bridge/` - the offloader package (Phase 1): `schemas.py`, `config.py`, `config.json`, `errors.py`, `transport.py`, `routing.py`.
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
