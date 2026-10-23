"""Transport layer for the Unsloth offloader.

The Transport ABC is the only thing the routing and validation layers care about.
It abstracts the wire format so the same bridge can talk to the Unsloth
OpenAI-compatible API (Phase 2) or a mock in tests (Phase 1 acceptance:
"Transport can be mocked"; "Tests run without a live Unsloth instance").

Responsibilities of a concrete transport:

* capability check (is the server up and a model loaded);
* loaded-model discovery (GET /v1/models, normalized to ModelMetadata);
* request submission with explicit timeout, cancellation, and bounded retry;
* streaming handling if the API streams;
* response normalization into one of the ResultState values (WorkerResult).

Every failure is expressed as a structured UnslothError subclass, never a bare
Exception, so Codex can react deterministically.
"""

from __future__ import annotations

import json
import re

from abc import ABC, abstractmethod
from typing import Optional

import requests

from .schemas import HandoffRequest, ModelMetadata, WorkerResult, ResultState
from .errors import (ApiUnavailableError, AuthError, MalformedOutputError,
                     ModelIdentifierUnavailableError, ModelNotLoadedError,
                     RequestTimeoutError, UnslothError)


class Transport(ABC):
    """Abstract interface to a local model server.

    Implementations must be safe to instantiate without a live server (they may
    perform capability checks lazily, not in __init__).
    """

    @abstractmethod
    def check_capability(self) -> bool:
        """Return True if the server is reachable AND a model is loaded.

        Raises ApiUnavailableError if the server cannot be reached at all
        (connection refused, DNS error, etc.).
        Raises ModelNotLoadedError if the server is up but no model is loaded.
        """

    @abstractmethod
    def discover_loaded_model(self) -> ModelMetadata:
        """Return metadata for the currently loaded model.

        Raises ModelNotLoadedError / ModelIdentifierUnavailableError.
        """

    @abstractmethod
    def send(self, request: HandoffRequest) -> WorkerResult:
        """Submit a handoff request and return a normalized WorkerResult.

        Must honor the transport budget (timeout, bounded retry). The returned
        state drives the validation gate and the Coding Task Assessment.
        """

    @abstractmethod
    def close(self) -> None:
        """Release any held resources (e.g. an HTTP session). No-op is fine."""

    # -- convenience --

    def can_offload(self) -> bool:
        """High-level readiness: reachable + a model loaded. Used by the router."""
        return self.check_capability()


# ---------------------------------------------------------------------------
# Mock transport (Phase 1: makes "transport can be mocked" a unit-test fact)
# ---------------------------------------------------------------------------

class MockTransport(Transport):
    """In-memory transport used by unit tests and dry runs.

    responses maps request_id (or the wildcard *) to a dict that is validated
    as a WorkerResult. If no canned response matches, a completed result with a
    trivial diff is returned so callers always get a well-formed object.
    """

    def __init__(self, *, capability=True, model_id="ternary-bonsai-2-27b",
                 model_display="Ternary Bonsai 2 27B PTQ1_0", backend="llama-server",
                 responses=None):
        self._capability = capability
        self._model_id = model_id
        self._model_display = model_display
        self._backend = backend
        self._responses = responses or {}
        self.sent = []  # HandoffRequest objects, in order

    def check_capability(self) -> bool:
        if not self._capability:
            raise ApiUnavailableError("mock server not reachable")
        return True

    def discover_loaded_model(self) -> ModelMetadata:
        if not self._capability:
            raise ModelNotLoadedError("mock server not running")
        return ModelMetadata(
            id=self._model_id,
            display_name=self._model_display,
            loaded=True,
            backend=self._backend,
        )

    def send(self, request: HandoffRequest) -> WorkerResult:
        self.sent.append(request)
        canned = self._responses.get(request.request_id)
        if canned is None:
            canned = self._responses.get("*")
        if canned is None:
            nl = chr(10)
            return WorkerResult(
                state="completed",
                summary="mock completed: " + request.task[:40],
                diff=nl.join([
                    "--- a/" + request.task[:20],
                    "+++ b/" + request.task[:20],
                    "@@ -0,0 +0,1 @@",
                    "+ok",
                ]) + nl,
            )
        return WorkerResult.model_validate(canned)

    def close(self) -> None:
        pass

    def __repr__(self):
        return "MockTransport(capability=%s, sent=%d)" % (self._capability, len(self.sent))


# ---------------------------------------------------------------------------
# Unsloth API transport (Phase 2 target)
# ---------------------------------------------------------------------------

class UnslothApiTransport(Transport):
    """Reference implementation for the Unsloth OpenAI-compatible API.

    Wire format (docs/discovery.md): primary POST /v1/responses, fallback
    POST /v1/chat/completions. Loaded model via GET /v1/models (loaded:true,
    matched by config.local_model_match). Bearer key from an env var
    (config.api_key) or local config file; never logged.
    """

    def __init__(self, config):
        self._config = config
        self._session = requests.Session()

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
        self._session = None

    def _auth_headers(self) -> dict:
        key = self._config.api_key()
        if not key:
            raise AuthError("No API key in env vars %s" % ", ".join(self._config.api_key_env_vars))
        return {"Authorization": "Bearer %s" % key}

    def discover_loaded_model(self) -> ModelMetadata:
        resp = self._session.get(self._config.api_base_url + "/v1/models",
                                 headers=self._auth_headers(),
                                 timeout=self._config.timeout_seconds)
        try:
            entries = resp.json().get("data") or []
        except ValueError:
            raise MalformedOutputError("GET /v1/models returned non-JSON")
        if not entries:
            raise ModelNotLoadedError("GET /v1/models returned no models")
        loaded = [e for e in entries if e.get("loaded")]
        entry = loaded[0] if loaded else entries[0]
        model_id = entry.get("id") or entry.get("name") or ""
        if not model_id:
            raise ModelIdentifierUnavailableError("model entry has no id/name")
        return ModelMetadata(
            id=model_id,
            display_name=entry.get("display_name") or entry.get("name") or model_id,
            loaded=entry.get("loaded", bool(loaded)),
            backend=entry.get("backend") or "",
            context_length=entry.get("context_length"),
        )

    def check_capability(self) -> bool:
        self.discover_loaded_model()
        return True

    def _worker_instructions(self) -> str:
        return ("""You are a bounded coding worker for a local LLM offload, not the final
orchestrator. Work only on the supplied context and file scope; never invent
repository facts. Prefer the smallest correct diff.

OUTPUT RULES (strict, non-negotiable):
- Respond with ONLY a single valid JSON object. Your entire reply, from the
  first character to the last, must be parseable as JSON.
- No prose, commentary, apologies, explanations, or "here is the result" text
  before or after the JSON.
- No markdown fences, no code blocks, no surrounding characters. The first
  character of your reply must be '{' and the last character must be '}'.
- The JSON must be valid: double-quoted strings, no trailing commas.

Return exactly this schema (every field present; use an empty string or empty
list where a field does not apply):
{
  "state": "completed | needs_context | declined | failed | invalid_output",
  "summary": "short human-readable summary of the work",
  "diff": "unified diff for code modifications, or empty string",
  "structured": {},
  "context_needed": "specific missing context, or empty string",
  "decline_reason": "why the task is out of scope, or empty string",
  "error": "error description, or empty string",
  "assumptions": [],
  "uncertainties": [],
  "tests_run": []
}

State semantics:
- completed: work done; provide a summary and a diff when code changes were made.
- needs_context: you cannot proceed; put the specific missing context in
  context_needed.
- declined: the task is out of your scope; put the reason in decline_reason.
- failed: a runtime/model error occurred; put the description in error.

Report assumptions and validation performed in the assumptions field.
""")

    def _worker_prompt(self, request: HandoffRequest) -> str:
        return ("TASK: " + request.task + "\n"
                "OBJECTIVE: " + request.objective + "\n"
                "DIFFICULTY: " + str(request.difficulty) + "\n"
                "LENGTH: " + str(request.length) + "\n"
                "SUMMARY: " + request.repository_context.summary + "\n"
                "ALLOWED FILES: " + "; ".join(request.repository_context.allowed_files) + "\n"
                "READ-ONLY: " + "; ".join(request.repository_context.read_only_files) + "\n"
                "CONSTRAINTS:\n" + "\n".join(request.constraints) + "\n"
                "ACCEPTANCE CRITERIA:\n" + "\n".join(request.acceptance_criteria) + "\n"
                "OUTPUT CONTRACT: " + request.output_contract.model_dump_json() + "\n"
                "Respond with ONLY a single valid JSON object that matches the schema "
                "exactly. No prose, no markdown fences, no commentary before or after the JSON.")

    def _send_with_retry(self, endpoint: str, payload: dict) -> dict:
        last_exc = None
        for attempt in range(self._config.max_retries + 1):
            try:
                resp = self._session.post(self._config.api_base_url + endpoint, json=payload,
                                          headers=self._auth_headers(),
                                          timeout=self._config.timeout_seconds)
            except requests.exceptions.Timeout:
                last_exc = RequestTimeoutError("request timed out")
                continue
            except requests.exceptions.RequestException as exc:
                last_exc = ApiUnavailableError("connection failed: %s" % exc)
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < self._config.max_retries:
                continue
            if resp.status_code in (401, 403):
                raise AuthError("authentication rejected (HTTP %d)" % resp.status_code)
            if resp.status_code >= 500:
                raise ApiUnavailableError("server error (HTTP %d)" % resp.status_code)
            last_exc = None
            try:
                return resp.json()
            except ValueError:
                raise MalformedOutputError("non-JSON response (HTTP %d)" % resp.status_code)
        if last_exc is not None:
            raise last_exc
        raise ApiUnavailableError("server unavailable after retries")

    def _extract_output(self, data: dict) -> str:
        parts = []
        for o in data.get("output") or []:
            text = (o.get("text") or "").strip()
            if text:
                parts.append(text)
            for c in o.get("content") or []:
                if isinstance(c, dict):
                    t = (c.get("text") or "").strip()
                    if t:
                        parts.append(t)
        for o in data.get("choices") or []:
            content = (o.get("message") or {}).get("content", "")
            if isinstance(content, str) and content.strip():
                parts.append(content.strip())
        for c in (data.get("messages") or {}).get("content") or []:
            if isinstance(c, str) and c.strip():
                parts.append(c)
        if parts:
            return "\n".join(parts)
        raise MalformedOutputError("no model output found in response")

    def _parse_worker_text(self, text: str) -> WorkerResult:
        text = (text or "").strip()
        if not text:
            raise MalformedOutputError("empty model response")
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z0-9]*\n", "", text)
            text = text.rsplit("\n```", 1)[0].strip()
        try:
            obj = json.loads(text)
        except ValueError:
            start, end = text.find("{"), text.rfind("}")
            if start >= 0 and end > start:
                try:
                    obj = json.loads(text[start:end + 1])
                except ValueError:
                    raise MalformedOutputError("model response not valid JSON")
            else:
                raise MalformedOutputError("model response not valid JSON")
        if not isinstance(obj, dict):
            raise MalformedOutputError("model response JSON is not an object")
        return WorkerResult.model_validate({
            "state": obj.get("state"),
            "summary": obj.get("summary", ""),
            "diff": obj.get("diff", ""),
            "structured": obj.get("structured") or {},
            "context_needed": obj.get("context_needed", ""),
            "decline_reason": obj.get("decline_reason", ""),
            "error": obj.get("error", ""),
            "assumptions": obj.get("assumptions") or [],
            "uncertainties": obj.get("uncertainties") or [],
            "tests_run": obj.get("tests_run") or [],
        })

    def send(self, request: HandoffRequest) -> WorkerResult:
        # /v1/chat/completions is the reliable endpoint on the current Unsloth
        # build (returns valid JSON); /v1/responses is kept as the fallback
        # since it hangs / returns prose on Unsloth Studio v0.1.909-beta.
        try:
            data = self._send_with_retry("/v1/chat/completions", {
                "model": self._config.local_model_alias,
                "messages": [{"role": "system", "content": self._worker_instructions()},
                             {"role": "user", "content": self._worker_prompt(request)}],
                "temperature": 0.2,
                "max_tokens": 4096,
            })
        except UnslothError:
            try:
                data = self._send_with_retry("/v1/responses", {
                    "model": self._config.local_model_alias,
                    "instructions": self._worker_instructions(),
                    "input": [{"type": "message", "role": "user", "content": self._worker_prompt(request)}],
                    "output_format": "json_object",
                    "temperature": 0.2,
                    "max_output_tokens": 4096,
                })
            except UnslothError:
                raise
        try:
            return self._parse_worker_text(self._extract_output(data))
        except ValueError:
            return WorkerResult(state=ResultState.INVALID_OUTPUT,
                                summary="model output failed validation",
                                error="model output did not match expected schema")
        except MalformedOutputError as exc:
            return WorkerResult(state=ResultState.INVALID_OUTPUT,
                                summary="model output could not be parsed",
                                error=str(exc))


__all__ = ["Transport", "MockTransport", "UnslothApiTransport"]
