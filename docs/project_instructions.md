# Project Instructions Template for Codex

Use this file as the durable implementation contract for the Unsloth automatic coding-task handoff project. When the project is created, Codex may copy/adapt these rules into the repository's `AGENTS.md` or equivalent instruction mechanism.

## Mission

Build a local integration that allows ChatGPT Codex to automatically delegate appropriate coding tasks or subtasks to the model currently loaded in Unsloth Desktop, then independently validate the result before presenting it to the user.

## Baseline environment

The user's supplied screenshot showed:

- Unsloth Desktop `v0.1.909-beta`
- Package version `2026.10.7`

Treat these only as an initial compatibility baseline. Detect capabilities at runtime and avoid strict version coupling.

## Non-negotiable constraints

1. **No hard-coded project directory or absolute user path.** Resolve paths relative to the chosen project/repository root or configuration.
2. Prefer supported Unsloth interfaces such as its CLI and OpenAI-compatible API.
3. Verify whether a supported Unsloth plugin/extension interface exists before designing against one.
4. If no stable plugin API is available, implement a companion local bridge/adapter rather than modifying packaged Unsloth binaries.
5. Do not use GUI automation unless the user explicitly approves it after native/API approaches are exhausted.
6. Codex remains the final reviewer and is responsible for validation.
7. Treat all local-model output as untrusted input until parsed and validated.
8. Never expose secrets or unrestricted repository content to the worker model.
9. Prefer unified diffs/patches or structured results over unrestricted local-model filesystem writes.
10. All integration endpoints should default to local-only/loopback access.
11. Do not deploy, publish, push, or create releases without explicit user instruction.
12. Every coding-related final response must include the assessment block from `handoff.md`.

## Required first action

Before implementation, perform a read-only capability discovery and document the findings:

- Installed Unsloth CLI commands relevant to `start`, model serving, API access, status, model detection, and Codex integration.
- Whether `unsloth start codex` works in the target environment.
- Local OpenAI-compatible API base URL, authentication behavior (if any), model-list endpoint, chat/completions/responses compatibility, streaming behavior, and tool-call support.
- Whether Unsloth Desktop has a supported plugin/extension SDK or documented internal extension point.
- How to detect the currently loaded model without scraping the UI.
- How Codex Desktop can invoke the bridge in the selected project environment.

Do not assume undocumented endpoints. Prefer empirical local discovery plus official documentation.

## Architecture preference order

Use the first viable option:

1. **Supported native extension/plugin API**, if Unsloth provides one suitable for this behavior.
2. **Local bridge using the OpenAI-compatible Unsloth API**, invoked by Codex.
3. **Wrapper around supported `unsloth start` / CLI behavior** when it provides the needed control.
4. Another documented local IPC surface.
5. GUI automation only with explicit approval.

Document why the selected architecture was chosen.

## Separation of responsibilities

### Codex/orchestrator

Responsible for:

- understanding the user request,
- assigning difficulty/length,
- deciding full/partial/no offload,
- gathering minimal repository context,
- writing the handoff contract,
- invoking the local bridge,
- validating returned work,
- resolving integration conflicts,
- and producing the final answer.

### Local Unsloth worker

Responsible only for:

- the bounded coding/review task supplied,
- returning a patch or structured response,
- identifying missing context rather than guessing,
- and reporting assumptions/uncertainties.

### Bridge/plugin

Responsible for:

- capability detection,
- request serialization,
- local transport,
- timeouts/cancellation,
- response normalization,
- model metadata detection when available,
- and safe failure/fallback signaling.

It should not silently make architectural decisions.

## Configuration requirements

All meaningful settings must have sensible defaults and be overrideable without code edits. Expected configurable concepts include:

- local Unsloth API base URL,
- CLI executable override if needed,
- preferred local model alias,
- automatic handoff enable/disable,
- maximum difficulty eligible for full handoff,
- maximum task length,
- timeout,
- maximum retries,
- maximum context size/budget,
- excluded files/globs,
- logging verbosity,
- dry-run mode,
- and validation policy.

Do not require users to edit source files to change model routing.

## Model policy

Initial local model:

- `Ternary Bonsai 2 27B PTQ1_0`

Use it as a configurable routing profile, not an exact immutable model ID.

Default expectation:

- Ternary Bonsai 2 27B PTQ1_0: Difficulty 1-3, bounded low-risk work and bounded multi-file/debugging work.
- Codex: Difficulty 4-5, architecture/security/high-risk work, plus final review of delegated work.

## Coding assessment requirement

After every coding-related response, output:

```markdown
### Coding Task Assessment
- **Difficulty:** <1-5> — <label>
- **Length:** <Short | Medium | Long | Extended> — <scope estimate>
- **Smaller-LLM Offload:** <Yes | Partial | No> — <model/reason>
- **Execution:** <Local handoff | Partial handoff | Keep in Codex>
```

This footer is required even if offloading is disabled or unavailable.

## Implementation quality rules

- Make the smallest coherent change for each development phase.
- Add automated tests for routing and transport behavior.
- Mock the Unsloth endpoint for unit tests; do not require a live model for the main test suite.
- Add a separate opt-in live integration test.
- Use explicit timeouts and cancellation.
- Use typed/validated schemas where practical.
- Keep transport implementation separate from routing policy.
- Keep prompt construction separate from transport.
- Keep validation orchestration separate from model invocation.
- Preserve platform portability when reasonable; the user's immediate environment may be Windows, but avoid unnecessary OS-specific assumptions.

## Security rules

At minimum, deny or redact:

- `.env*`
- credentials/token files
- private keys/certificates
- common cloud credential locations
- package registry auth files
- Git credentials
- browser/session data
- files explicitly marked private by project configuration

Never include environment-variable values in a handoff unless explicitly allowlisted.

## Error handling

The bridge must distinguish:

- Unsloth not installed
- Unsloth not running
- API unavailable
- no model loaded
- model identifier unavailable
- request timeout
- cancellation
- malformed model output
- model context limit exceeded
- validation failure

Each state should produce a clear recoverable signal to Codex. Do not convert every failure into a generic exception.

## Required documentation before project completion

Codex must leave the completed repository with documentation for:

- installation/setup,
- architecture,
- configuration,
- handoff decision policy,
- model profiles,
- safety/privacy behavior,
- troubleshooting,
- testing,
- and how to disable automatic delegation.

## Definition of done

The project is done only when:

1. A coding task can be classified.
2. An eligible task can be sent to the loaded Unsloth model without hard-coded paths.
3. A non-eligible task remains in Codex.
4. A partial task can be delegated independently.
5. Local output is normalized into a defined response state.
6. Returned patches are scope-checked.
7. Validation failures cause safe fallback.
8. Unsloth unavailability causes safe fallback.
9. Secrets/excluded files are demonstrably blocked.
10. Unit tests and an opt-in live integration test exist.
11. The Coding Task Assessment footer is generated consistently.
12. Documentation is sufficient to configure the initial local model profile without changing source code.
