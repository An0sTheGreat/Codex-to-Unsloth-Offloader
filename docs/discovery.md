# Phase 0 — Capability Discovery (Completed)

Date: 2026-10-21

## Objective

Determine the supported Unsloth integration surface before writing production code, and select the architecture from `project_instructions.md` (prefer: native plugin API → OpenAI-compatible API bridge → `unsloth start` wrapper → other IPC → GUI automation, in that order).

## Environment (observed, read-only)

- **Unsloth version:** `2026.10.7` (matches the baseline in `readme.md` / `project_instructions.md`). CLI at `Y:\Programs\Unsloth\studio\unsloth_studio\Scripts\unsloth.exe`.
- **Running processes (confirmed via `tasklist`):** `unsloth-studio.exe` and `llama-server.exe` are both live. The local API is **up**.
- **API endpoint:** `http://127.0.0.1:8888` (default host `127.0.0.1`, port `8888`). Confirmed: `GET /` returns the Studio web UI (HTTP 200); `GET /v1/models` and `GET /v1/props` return **HTTP 401** without auth → the API requires a Bearer key.
- **Loaded model (observed via `unsloth start codex --no-launch`):** `0. GOAT - Ternary Bonsai 2 27B｜PTQ1_0`. So the project's sole local model, **Ternary Bonsai 2 27B PTQ1_0**, is installed and loaded. (The exact machine ID returned by `GET /v1/models` should be confirmed in Phase 2; the display label contains a full-width `｜` and a leading `0. GOAT - ` prefix, so the bridge must match by substring, not exact string.)

## Supported integration surfaces (evidence)

### 1. OpenAI-compatible local API (the chosen transport)

From official docs (`unsloth.ai/docs/basics/api`) + local probe, the server speaks two dialects on the same port, both with streaming, tool-calling, and vision:

- `POST /v1/chat/completions` — OpenAI chat completions (streaming + tools).
- `POST /v1/responses` — OpenAI **Responses API** (the wire format Codex uses exclusively; listed for "Codex and other compatible clients").
- `POST /v1/messages` — Anthropic Messages API (for Claude Code etc.).
- `POST /v1/completions` — plain completions (GGUF/MLX only).
- `GET /v1/models` — lists models; **the currently loaded model is added first**, each entry has a `loaded` boolean. Shape: `{"data": [{"id": "<model_id>", "loaded": true, ...}]}`. **This is the loaded-model discovery mechanism.**
- `GET /v1/props` — llama-server-compatible server properties.
- `POST /v1/embeddings`, `POST /v1/chat/count_tokens`, `POST /v1/messages/count_tokens`.
- Interactive reference: `http://127.0.0.1:8888/docs`; machine-readable schema: `http://127.0.0.1:8888/openapi.json`.

**Auth:** every API request needs `Authorization: Bearer sk-unsloth-...`. No key in the probe → 401. (See "API key" below.)

### 2. `unsloth run` (CLI model load + server)

`unsloth run` (= `unsloth studio run`) loads a model, starts the server + UI on the default port, and **prints the endpoint URL and API key to the console**. Key flags: `--model`/`-hf`, `--gguf-variant`, `--max-seq-length`, `--port`/`-p`, `--host`/`-H`, `--api-only` (API only, no web UI, headless), `--disable-tools`/`--enable-tools` (server-side tool policy), `--api-key-name`.

### 3. `unsloth start codex` (reference only, not the chosen surface)

`unsloth start codex` points an **entire Codex instance** at the running Unsloth server and creates an **isolated Codex home** + a Unsloth-backed Responses provider. It does **not** keep the user's normal Codex — so it is the wrong shape for *subtask offload* (which is what this project needs). However it confirmed two facts used here:
- The local API key is **auto-minted per server and remembered**, exposed as env var **`UNSLOTH_STUDIO_AUTH_TOKEN`** (for Codex) / **`UNSLOTH_API_KEY`** (for `unsloth start`), and **never printed** (CLI shows `[redacted]`). It is stored under `Y:\Programs\Unsloth\studio\auth\agents\codex\` (config.toml, unsloth_api.config.toml, model-catalog.json) — a protected dir the sandbox correctly refuses to let tools read.
- `--no-launch` prints the env/command without launching (safe read-only probe).
- `--model` defaults to the currently loaded model.

### 4. MCP extension point

`unsloth mcp add` — "Connect coding agents to the Unsloth Studio MCP server. The API key stays in an env var." This is a documented extension point for *attaching agents*, not a general plugin SDK for building a custom delegation bridge.

### 5. Plugin / extension API

No stable, documented **public** plugin/extension SDK exists for this use case. The repo has an internal `studio/backend/plugins` directory, but it is not a public extension surface. (Third-party extensions exist, e.g. an Ollama-compatible API proxy on a separate port, but those are external projects, not an unsloth-provided hook.) ⇒ **Option 1 is not viable.**

## Endpoint / behavior summary (recorded per Phase 0)

- **Endpoint shape:** `http://127.0.0.1:8888/v1/...` (port configurable via `--port`; default 8888; loopback by default).
- **Model identifier behavior:** `GET /v1/models` returns exact IDs; the loaded model is first and flagged `loaded: true`. The displayed label (`0. GOAT - Ternary Bonsai 2 27B｜PTQ1_0`) is not guaranteed to equal the API `id` — resolve via the endpoint, match by substring.
- **Response format:** JSON. Model list: `data[].id` / `data[].loaded`. Full schema at `/openapi.json`.
- **Streaming:** supported on `/v1/chat/completions` and `/v1/responses` (SSE; for `/v1/messages` ensure the client consumes SSE, not a buffered blob).
- **Tool calling:** client-side (`tools`/`tool_choice` in OpenAI or Anthropic shape) **and** server-side (python / bash / web_search, on by default on loopback). For this project the local worker should return a patch/diff, so server-side tools are unnecessary; use passthrough (`--disable-tools`) if tools are involved so the agent's own tools are used, not Unsloth's.
- **Timeout / error behavior:** `401 Unauthorized` (missing/wrong key), `Connection refused` (server not running / wrong port), `Lost connection to the model server` (model crashed/closed), `429`/context OOM (reduce context). No streaming still → wrong path or buffered client.

## API key handling (bridge design input)

- The local key is auto-minted and remembered per server; it is **not** viewable in the UI after creation and **not** printed by the CLI.
- It is surfaced as env vars **`UNSLOTH_STUDIO_AUTH_TOKEN`** and/or **`UNSLOTH_API_KEY`**.
- The bridge must read the key from an **environment variable** (prefer `UNSLOTH_STUDIO_AUTH_TOKEN`, fall back to `UNSLOTH_API_KEY`) or a user-provided local config file, and **never log or echo it**. (The protected `studio/auth/` dir is not a readable source for tools.)

## How Codex (the orchestrator) can invoke the bridge

The bridge is a **local helper script** living in this project (e.g. `bridge/offload.py`). Codex, when its classification says "offload", runs the script as a local command, passing the task packet (via args or a temp JSON file) and reading back the result (normalized state + patch/diff as a file or stdout). This is a plain local-process invocation — no Unsloth GUI, no plugin. The bridge then talks to `127.0.0.1:8888` directly over HTTP. Codex validates the returned patch and owns the final response + the Coding Task Assessment footer.

## Architecture decision

**Chosen: Option 2 — a local bridge using Unsloth's OpenAI-compatible API.**

Rationale (evidence-based):
- Option 1 (native plugin/extension API): **not viable** — no stable public plugin SDK (only internal `studio/backend/plugins` + the MCP attach point).
- Option 2 (OpenAI-compatible API bridge): **viable and documented** — the API is live on loopback, supports `/v1/responses` + `/v1/chat/completions` with streaming and tools, and exposes loaded-model discovery via `/v1/models`. This is the supported surface Codex itself uses.
- Option 3 (`unsloth start` / CLI wrapper): `unsloth start codex` replaces the whole Codex provider (wrong shape for subtask offload); kept only as reference for the key mechanism and Responses wire format. The underlying API of Option 2 is what we drive directly.
- Option 4 (other local IPC): none better documented.
- Option 5 (GUI automation): unnecessary; a supported API exists.

**Wire format recommendation:** primary `POST /v1/responses` (the Codex wire format, explicitly listed for Codex-compatible clients); fallback `POST /v1/chat/completions`. Final choice is confirmed in Phase 2 (transport adapter) against a mock.

## Side effects of this discovery (transparency)

Running `unsloth start codex --no-launch` (a documented, non-launching, read-only probe) caused Unsloth to **auto-generate/update its own internal agent config** under `Y:\Programs\Unsloth\studio\auth\agents\codex\` (config.toml, model-catalog.json, unsloth_api.config.toml). This is Unsloth's own per-server state, **not** the user's project settings; the user's `~/.codex` configuration was left untouched. No model, data, or project file was modified. The key was never read or printed.

## Acceptance criteria (Phase 0)

- [x] No undocumented endpoint is assumed as fact. (All endpoints sourced from official docs + live probe.)
- [x] No absolute user path is introduced **into the implementation**. (Paths in this note are evidence only; the bridge must resolve the API base URL, key, and project root via config/env, never hard-coded.)
- [x] Architecture choice is evidence-based. (Option 2, per the preference order.)
- [x] Codex can describe a viable local invocation path. (Local bridge script → HTTP to `127.0.0.1:8888` → patch/diff back → Codex validates.)

## Gate

Transport surface is known. **Phase 1 (Project Skeleton and Contracts) may proceed.**

## Open items for Phase 1/2

1. Confirm the exact `id` from `GET /v1/models` (requires the API key; done in the live smoke test, Phase 2).
2. Decide final wire format (`/v1/responses` vs `/v1/chat/completions`) and implement the transport adapter with mocks.
3. Define how the user supplies the API key (env var vs local config file) and confirm the loopback-only default.

