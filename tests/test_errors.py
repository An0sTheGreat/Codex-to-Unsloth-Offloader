"""Tests for the structured error hierarchy (bridge.errors).

The contract: every error carries a stable machine-readable ``code`` and an
optional ``details`` dict, and every public error is a subclass of
:class:`UnslothError`. Codex reacts on ``code`` (not prose), so these codes
must be stable.
"""

import unittest

from bridge.errors import (
    ApiUnavailableError,
    AuthError,
    CancellationError,
    ConfigurationError,
    ContextLimitExceededError,
    MalformedOutputError,
    ModelIdentifierUnavailableError,
    ModelNotLoadedError,
    RequestTimeoutError,
    ScopeViolationError,
    ValidationFailureError,
    UnslothError,
    UnslothNotInstalledError,
    UnslothNotRunningError,
)


class TestErrorBase(unittest.TestCase):
    def test_every_error_is_unsloth_error(self):
        errors = [
            ConfigurationError,
            UnslothNotInstalledError,
            UnslothNotRunningError,
            ApiUnavailableError,
            AuthError,
            ModelNotLoadedError,
            ModelIdentifierUnavailableError,
            RequestTimeoutError,
            CancellationError,
            MalformedOutputError,
            ContextLimitExceededError,
            ValidationFailureError,
            ScopeViolationError,
        ]
        for cls in errors:
            self.assertTrue(
                issubclass(cls, UnslothError),
                "error %s is not a subclass of UnslothError" % cls.__name__,
            )

    def test_base_code(self):
        e = UnslothError("boom")
        self.assertEqual(e.code, "unsloth_error")

    def test_code_is_stable_and_lowercase(self):
        e = ModelNotLoadedError("no model")
        self.assertEqual(e.code, "model_not_loaded")
        self.assertTrue(e.code == e.code.lower(), "codes must be lowercase")

    def test_details_dict(self):
        e = ApiUnavailableError("down", details={"status_code": 503})
        self.assertEqual(e.details, {"status_code": 503})

    def test_to_dict(self):
        e = ConfigurationError("bad json", details={"path": "x.json"})
        d = e.to_dict()
        self.assertEqual(d["code"], "configuration_error")
        self.assertEqual(d["message"], "bad json")
        self.assertEqual(d["details"], {"path": "x.json"})


class TestErrorMessages(unittest.TestCase):
    def test_message_preserved(self):
        e = ModelNotLoadedError("no model loaded")
        self.assertEqual(str(e), "no model loaded")


if __name__ == "__main__":
    unittest.main()
