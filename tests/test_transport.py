"""Tests for the transport layer (bridge.transport).

Uses only the in-memory MockTransport, so no live Unsloth instance is required
(Phase 1 acceptance: "Tests run without a live Unsloth instance").
"""

import json
import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from bridge.schemas import HandoffRequest, ModelMetadata, RepositoryContext, ResultState, WorkerResult
from bridge.transport import MockTransport, UnslothApiTransport, Transport
from bridge.config import OffloaderConfig
from bridge.errors import ApiUnavailableError, AuthError, ModelNotLoadedError


def make_request(task="Add helper", request_id=None):
    kw = dict(
        task=task,
        objective="parse an int",
        difficulty=2,
        length="Short",
        repository_context=RepositoryContext(summary="single module"),
    )
    if request_id is not None:
        kw["request_id"] = request_id
    return HandoffRequest(**kw)


class TestMockTransport(unittest.TestCase):
    def test_capability_true(self):
        mt = MockTransport(capability=True)
        self.assertTrue(mt.check_capability())
        self.assertTrue(mt.can_offload())

    def test_capability_false_raises(self):
        mt = MockTransport(capability=False)
        with self.assertRaises(ApiUnavailableError):
            mt.check_capability()

    def test_model_not_loaded_when_not_running(self):
        mt = MockTransport(capability=False)
        with self.assertRaises(ModelNotLoadedError):
            mt.discover_loaded_model()

    def test_model_loaded_when_running(self):
        mt = MockTransport(capability=True, model_id="tb27b", model_display="Ternary Bonsai 2 27B")
        mm = mt.discover_loaded_model()
        self.assertIsInstance(mm, ModelMetadata)
        self.assertEqual(mm.id, "tb27b")
        self.assertEqual(mm.display_name, "Ternary Bonsai 2 27B")
        self.assertTrue(mm.loaded)

    def test_send_records_request(self):
        mt = MockTransport()
        req = make_request(request_id="r1")
        mt.send(req)
        self.assertEqual(len(mt.sent), 1)
        self.assertEqual(mt.sent[0].request_id, "r1")

    def test_send_default_returns_completed(self):
        mt = MockTransport()
        req = make_request(request_id="r2")
        wr = mt.send(req)
        self.assertEqual(wr.state, ResultState.COMPLETED)
        self.assertTrue(wr.summary)
        self.assertTrue(wr.diff)

    def test_send_with_canned_response(self):
        canned = {"state": "needs_context", "context_needed": "missing file A.py"}
        mt = MockTransport(responses={"*": canned})
        req = make_request(request_id="r3")
        wr = mt.send(req)
        self.assertEqual(wr.state, ResultState.NEEDS_CONTEXT)
        self.assertEqual(wr.context_needed, "missing file A.py")

    def test_send_canned_response_specific_id(self):
        canned = {"state": "declined", "decline_reason": "too large"}
        mt = MockTransport(responses={"r4": canned})
        req = make_request(request_id="r4")
        wr = mt.send(req)
        self.assertEqual(wr.state, ResultState.DECLINED)
        self.assertEqual(wr.decline_reason, "too large")

    def test_send_canned_response_invalid_raises(self):
        # A canned response that fails validation must raise (no silent bad state).
        mt = MockTransport(responses={"*": {"state": "needs_context"}})  # missing context_needed
        req = make_request(request_id="r5")
        with self.assertRaises(ValueError):
            mt.send(req)

    def test_close_is_noop(self):
        mt = MockTransport()
        self.assertIsNone(mt.close())

    def test_mock_transport_is_transport(self):
        self.assertIsInstance(MockTransport(), Transport)


class TestUnslothApiTransport(unittest.TestCase):
    """Tests for the real Unsloth HTTP transport.

    The HTTP session is replaced with a mock so no live server is required.
    """

    KEY = "sk-offloader-test-key"
    KEY_VAR = "_OFFLOADER_TEST_KEY_"

    def _cfg(self, **kw):
        return OffloaderConfig(
            api_base_url="http://127.0.0.1:8888",
            api_key_env_vars=[self.KEY_VAR],
            **kw,
        )

    def _t(self, **kw):
        return UnslothApiTransport(self._cfg(**kw))

    def _resp(self, status, body=None):
        class _Resp:
            def __init__(s, status, body):
                s.status_code = status
                s.text = json.dumps(body) if body is not None else ""
            def json(self):
                return json.loads(self.text) if self.text else {}
        return _Resp(status, body)

    def _install(self, t, get=None, post=None):
        s = MagicMock()
        if get is not None:
            s.get.return_value = get
        if post is not None:
            if callable(post):
                s.post = post
            else:
                s.post.return_value = post
        t._session = s
        return t

    def test_is_transport(self):
        t = UnslothApiTransport(None)
        self.assertIsInstance(t, Transport)

    def test_close_releases_session(self):
        t = self._t()
        t.close()
        self.assertIsNone(t._session)

    # -- capability / discovery -------------------------------------------------

    def test_discover_loaded_model(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            model = {"id": "tb-27b", "display_name": "Ternary Bonsai 2 27B",
                     "loaded": True, "backend": "llama-server", "context_length": 128000}
            self._install(t, get=self._resp(200, {"data": [model]}))
            mm = t.discover_loaded_model()
            self.assertEqual(mm.id, "tb-27b")
            self.assertTrue(mm.loaded)
            self.assertEqual(mm.backend, "llama-server")
            self.assertEqual(mm.context_length, 128000)

    def test_discover_prefers_loaded(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            self._install(t, get=self._resp(200, {"data": [
                {"id": "other", "loaded": False},
                {"id": "tb-27b", "loaded": True},
            ]}))
            self.assertEqual(t.discover_loaded_model().id, "tb-27b")

    def test_discover_model_not_loaded(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            self._install(t, get=self._resp(200, {"data": []}))
            with self.assertRaises(ModelNotLoadedError):
                t.discover_loaded_model()

    def test_discover_auth_error_without_key(self):
        t = self._t()
        self._install(t, get=self._resp(200, {"data": []}))
        with self.assertRaises(AuthError):
            t.discover_loaded_model()

    def test_check_capability_true(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            self._install(t, get=self._resp(200, {"data": [{"id": "tb", "loaded": True}]}))
            self.assertTrue(t.check_capability())
            self.assertTrue(t.can_offload())

    # -- request submission ------------------------------------------------------

    def _responses_body(self, worker_json, shape="text"):
        if shape == "text":
            return {"output": [{"type": "message", "text": json.dumps(worker_json)}]}
        return {"output": [{"type": "message",
                            "content": [{"type": "text",
                                        "text": json.dumps(worker_json)}]}]}

    def _chat_body(self, worker_json):
        return {"choices": [{"message": {"content": json.dumps(worker_json)}}]}

    def test_send_completed_responses(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            w = {"state": "completed", "summary": "added helper", "diff": "+x"}
            self._install(t, post=self._resp(200, self._responses_body(w, "text")))
            res = t.send(make_request())
            self.assertEqual(res.state, "completed")
            self.assertEqual(res.summary, "added helper")
            self.assertEqual(res.diff, "+x")

    def test_send_completed_content_shape(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            w = {"state": "completed", "summary": "ok", "diff": "+y"}
            self._install(t, post=self._resp(200, self._responses_body(w, "content")))
            res = t.send(make_request())
            self.assertEqual(res.state, "completed")

    def test_send_completed_chat_completions_shape(self):
        # The primary endpoint is now /v1/chat/completions; a chat-shaped
        # response (choices[0].message.content) must be extracted and validated.
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            w = {"state": "completed", "summary": "ok", "diff": "+z"}
            self._install(t, post=self._resp(200, self._chat_body(w)))
            res = t.send(make_request())
            self.assertEqual(res.state, "completed")
            self.assertEqual(res.summary, "ok")
            self.assertEqual(res.diff, "+z")

    def test_send_needs_context(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            w = {"state": "needs_context", "context_needed": "need file x.py"}
            self._install(t, post=self._resp(200, self._responses_body(w)))
            res = t.send(make_request())
            self.assertEqual(res.state, "needs_context")
            self.assertEqual(res.context_needed, "need file x.py")

    def test_send_declined(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            w = {"state": "declined", "decline_reason": "too hard"}
            self._install(t, post=self._resp(200, self._responses_body(w)))
            res = t.send(make_request())
            self.assertEqual(res.state, "declined")
            self.assertEqual(res.decline_reason, "too hard")

    def test_send_invalid_output_non_json(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            self._install(t, post=self._resp(200, {"output": [{"type": "message",
                                                             "text": "not json at all"}]}))
            res = t.send(make_request())
            self.assertEqual(res.state, "invalid_output")

    def test_send_invalid_schema(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            w = {"state": "done", "summary": "bad state"}
            self._install(t, post=self._resp(200, self._responses_body(w)))
            res = t.send(make_request())
            self.assertEqual(res.state, "invalid_output")

    def test_send_no_output(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            self._install(t, post=self._resp(200, {"output": []}))
            res = t.send(make_request())
            self.assertEqual(res.state, "invalid_output")

    def test_send_embedded_json_in_prose(self):
        # Prose around a valid JSON object must still be extracted and
        # schema-validated (robustness), not rejected for the surrounding text.
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            raw = ('Sure! Here is the result:\n'
                   '{"state": "completed", "summary": "ok", "diff": "+x"}\n'
                   'Hope that helps!')
            self._install(t, post=self._resp(200, {"output": [{"type": "message",
                                                              "text": raw}]}))
            res = t.send(make_request())
            self.assertEqual(res.state, "completed")
            self.assertEqual(res.summary, "ok")
            self.assertEqual(res.diff, "+x")

    def test_send_json_in_fenced_block(self):
        # A markdown-fenced JSON block must be accepted via fence stripping.
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            raw = '```json\n{"state": "completed", "summary": "ok", "diff": "+x"}\n```'
            self._install(t, post=self._resp(200, {"output": [{"type": "message",
                                                              "text": raw}]}))
            res = t.send(make_request())
            self.assertEqual(res.state, "completed")

    def test_send_json_list_is_invalid(self):
        # A valid JSON array is not a valid worker object -> invalid_output.
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            self._install(t, post=self._resp(200, {"output": [{"type": "message",
                                                              "text": "[1, 2, 3]"}]}))
            res = t.send(make_request())
            self.assertEqual(res.state, "invalid_output")

    def test_send_malformed_json_trailing_comma_is_invalid(self):
        # JSON with a trailing comma is malformed -> invalid_output (strict).
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t()
            self._install(t, post=self._resp(200, {"output": [{"type": "message",
                                                              "text": '{"state": "completed", "summary": "ok",}'}]}))
            res = t.send(make_request())
            self.assertEqual(res.state, "invalid_output")

    def test_send_falls_back_to_responses(self):
        # /v1/chat/completions is primary; it fails here, so the transport
        # falls back to /v1/responses.
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t(max_retries=0)
            w = {"state": "completed", "summary": "ok", "diff": "+y"}
            calls = []
            def post(url, **kw):
                calls.append(url)
                if "chat" in url:
                    return self._resp(500)
                return self._resp(200, self._responses_body(w, "text"))
            t._session = MagicMock()
            t._session.post = post
            res = t.send(make_request())
            self.assertEqual(res.state, "completed")
            self.assertEqual(len(calls), 2)
            self.assertTrue("chat/completions" in calls[0])
            self.assertTrue("responses" in calls[1])

    def test_send_auth_error(self):
        t = self._t()
        self._install(t, post=self._resp(200, {}))
        with self.assertRaises(AuthError):
            t.send(make_request())

    def test_send_connection_error(self):
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t(max_retries=0)
            t._session = MagicMock()
            t._session.post.side_effect = requests.exceptions.RequestException("refused")
            with self.assertRaises(ApiUnavailableError):
                t.send(make_request())

    # -- correction retry (Phase 6 task 6) -------------------------------------

    def test_send_correction_retry_recovers(self):
        # First response malformed (non-JSON); the single correction retry
        # re-prompts and the corrected response is valid -> completed.
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t(max_retries=0)
            good = {"state": "completed", "summary": "ok", "diff": "+x"}
            calls = []
            def post(url, **kw):
                calls.append(url)
                if len(calls) == 1:
                    return self._resp(200, {"output": [{"type": "message",
                                                       "text": "not json"}]})
                return self._resp(200, self._responses_body(good, "text"))
            t._session = MagicMock()
            t._session.post = post
            res = t.send(make_request())
            self.assertEqual(res.state, "completed")
            self.assertEqual(res.summary, "ok")
            self.assertEqual(len(calls), 2)

    def test_send_correction_retry_still_invalid(self):
        # First response invalid schema; the single correction retry is also
        # invalid -> invalid_output (bounded, no unbounded loop).
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t(max_retries=0)
            bad = {"state": "zzz_not_valid", "summary": "x"}
            calls = []
            def post(url, **kw):
                calls.append(url)
                return self._resp(200, self._responses_body(bad, "text"))
            t._session = MagicMock()
            t._session.post = post
            res = t.send(make_request())
            self.assertEqual(res.state, "invalid_output")
            self.assertEqual(len(calls), 2)

    def test_correction_prompt_includes_previous_error(self):
        # The correction re-prompt must carry the previous error so the model
        # can repair it (not a blind re-send).
        with patch.dict(os.environ, {self.KEY_VAR: self.KEY}):
            t = self._t(max_retries=0)
            bad = {"state": "zzz_not_valid", "summary": "x"}
            good = {"state": "completed", "summary": "ok", "diff": "+x"}
            payloads = []
            def post(url, **kw):
                payloads.append(kw.get("json"))
                if len(payloads) == 1:
                    return self._resp(200, self._responses_body(bad, "text"))
                return self._resp(200, self._responses_body(good, "text"))
            t._session = MagicMock()
            t._session.post = post
            t.send(make_request())
            second_user = payloads[1]["messages"][1]["content"]
            self.assertIn("CORRECTION:", second_user)
            self.assertIn("Retry now.", second_user)
            self.assertIn("zzz_not_valid", second_user)


if __name__ == "__main__":
    unittest.main()
