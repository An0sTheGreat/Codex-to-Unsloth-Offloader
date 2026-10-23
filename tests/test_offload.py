"""Tests for the offload CLI (bridge.offload).

Uses the in-memory MockTransport, so no live Unsloth instance is required
(consistent with the rest of the suite).
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import contextmanager, redirect_stdout
from unittest.mock import patch

from bridge.schemas import ResultState
from bridge.transport import MockTransport
from bridge.offload import (
    EXIT_COMPLETED,
    EXIT_INVALID_PACKET,
    EXIT_UNSLOTH,
    EXIT_WORKER_STATE,
    main,
    run_handoff,
    run_smoke,
)


class TestRunHandoff(unittest.TestCase):
    def _packet(self, **kw):
        return json.dumps({
            "task": "Add helper",
            "objective": "parse an int",
            "difficulty": 2,
            "length": "Short",
            "repository_context": {"summary": "single module"},
            **kw,
        })

    def test_completed_exit_zero(self):
        mt = MockTransport()
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet())
        self.assertEqual(code, EXIT_COMPLETED)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["state"], "completed")

    def test_needs_context_exit_one(self):
        mt = MockTransport(responses={"*": {"state": "needs_context",
                                           "context_needed": "file A.py"}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet())
        self.assertEqual(code, EXIT_WORKER_STATE)
        self.assertEqual(json.loads(buf.getvalue())["state"], "needs_context")

    def test_declined_exit_one(self):
        mt = MockTransport(responses={"*": {"state": "declined",
                                            "decline_reason": "too large"}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_handoff(mt, self._packet())
        self.assertEqual(code, EXIT_WORKER_STATE)

    def test_invalid_packet_raises_value_error(self):
        mt = MockTransport()
        with self.assertRaises(ValueError):
            run_handoff(mt, "{ not json")

    def test_schema_mismatch_raises_value_error(self):
        mt = MockTransport()
        with self.assertRaises(ValueError):
            run_handoff(mt, '{"task": "x"}')  # missing required fields


class TestRunSmoke(unittest.TestCase):
    def test_completed_exit_zero(self):
        mt = MockTransport(capability=True,
                           model_id="tb-27b",
                           model_display="Ternary Bonsai 2 27B")
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_smoke(mt)
        self.assertEqual(code, EXIT_COMPLETED)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["model"]["id"], "tb-27b")
        self.assertEqual(data["worker_result"]["state"], "completed")
        self.assertEqual(len(mt.sent), 1)

    def test_declined_exit_one(self):
        mt = MockTransport(responses={"*": {"state": "declined",
                                            "decline_reason": "too large"}})
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = run_smoke(mt)
        self.assertEqual(code, EXIT_WORKER_STATE)

    def test_capability_error_propagates(self):
        mt = MockTransport(capability=False)
        with self.assertRaises(Exception):
            run_smoke(mt)


class TestMain(unittest.TestCase):
    @contextmanager
    def _patch_transport(self, mt):
        with patch("bridge.offload.UnslothApiTransport",
                   side_effect=lambda cfg: mt):
            yield mt

    def test_main_smoke_success(self):
        mt = MockTransport(capability=True)
        buf = io.StringIO()
        with self._patch_transport(mt):
            with redirect_stdout(buf):
                code = main(["--smoke"])
        self.assertEqual(code, EXIT_COMPLETED)
        json.loads(buf.getvalue())

    def test_main_smoke_server_down(self):
        mt = MockTransport(capability=False)
        buf = io.StringIO()
        with self._patch_transport(mt):
            with redirect_stdout(buf):
                code = main(["--smoke"])
        self.assertEqual(code, EXIT_UNSLOTH)

    def test_main_packet_file(self):
        mt = MockTransport()
        packet = json.dumps({
            "task": "Add helper",
            "objective": "parse an int",
            "difficulty": 2,
            "length": "Short",
            "repository_context": {"summary": "single module"},
        })
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write(packet)
            path = f.name
        try:
            buf = io.StringIO()
            with self._patch_transport(mt):
                with redirect_stdout(buf):
                    code = main([path])
            self.assertEqual(code, EXIT_COMPLETED)
            self.assertEqual(json.loads(buf.getvalue())["state"], "completed")
        finally:
            os.unlink(path)

    def test_main_no_args_usage_error(self):
        with self._patch_transport(MockTransport()):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main([])

    def test_main_smoke_and_packet_conflict(self):
        with self._patch_transport(MockTransport()):
            with redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    main(["--smoke", "{}"])


