"""Tests for configuration loading (bridge.config).

No live Unsloth instance is required. Config files are written to a temp dir
so the tests are path-independent and don't touch the real project config.
"""

import json
import os
import tempfile
from pathlib import Path
import unittest

from bridge.config import (
    OffloaderConfig,
    default_excluded_files,
    get_project_root,
    load_config,
)
from bridge.errors import ConfigurationError
from bridge.schemas import LengthLevel, ModelProfile


class TestDefaultConfig(unittest.TestCase):
    def test_defaults_are_path_independent(self):
        cfg = load_config()
        self.assertIsInstance(cfg, OffloaderConfig)
        # No absolute user paths in the defaults.
        self.assertEqual(cfg.api_base_url, "http://127.0.0.1:8888")
        self.assertTrue(cfg.auto_handoff_enabled)
        self.assertFalse(cfg.dry_run)
        self.assertEqual(cfg.timeout_seconds, 120.0)
        self.assertGreaterEqual(cfg.max_retries, 0)
        self.assertGreaterEqual(cfg.context_budget_tokens, 0)
    def test_default_model_profile_and_failure_limit(self):
        cfg = load_config()
        self.assertEqual(cfg.model_profile.name, "Ternary Bonsai 2 27B PTQ1_0")
        self.assertEqual(cfg.model_profile.use_for_difficulty, [1, 2, 3])
        self.assertEqual(cfg.model_profile.max_default_length, LengthLevel.MEDIUM)
        self.assertEqual(cfg.max_validation_failures, 2)
    def test_default_model_alias_is_ternary_bonsai(self):
        cfg = load_config()
        self.assertEqual(cfg.local_model_alias, "Ternary Bonsai 2 27B PTQ1_0")

    def test_default_excluded_files_present(self):
        cfg = load_config()
        self.assertIn(".env", cfg.excluded_files)
        self.assertIn(".env.*", cfg.excluded_files)
        self.assertIn("**/id_rsa", cfg.excluded_files)
        self.assertEqual(cfg.excluded_files, default_excluded_files())


class TestConfigFileLoading(unittest.TestCase):
    def _load_from(self, path, content):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "myconf.json"
            with open(p, "w", encoding="utf-8") as f:
                f.write(content)
            return load_config(path=p)

    def test_config_file_overrides_defaults(self):
        cfg = self._load_from(
            None,
            json.dumps({"timeout_seconds": 30.0, "local_model_alias": "Other Model",
                        "dry_run": True}),
        )
        self.assertEqual(cfg.timeout_seconds, 30.0)
        self.assertEqual(cfg.local_model_alias, "Other Model")
        self.assertTrue(cfg.dry_run)
    def test_model_profile_and_failure_limit_from_config_file(self):
        content = json.dumps({
            "model_profile": {
                "name": "Other Model",
                "use_for_difficulty": [1, 2],
                "max_default_length": "Long",
            },
            "max_validation_failures": 3,
        })
        cfg = self._load_from(None, content)
        self.assertEqual(cfg.model_profile.name, "Other Model")
        self.assertEqual(cfg.model_profile.use_for_difficulty, [1, 2])
        self.assertEqual(cfg.model_profile.max_default_length, LengthLevel.LONG)
        self.assertEqual(cfg.max_validation_failures, 3)
    def test_config_file_non_dict_raises(self):
        with self.assertRaises(ConfigurationError):
            self._load_from(None, "[1, 2, 3]")

    def test_config_file_bad_json_raises(self):
        with self.assertRaises(ConfigurationError):
            self._load_from(None, "{ not json")

    def test_missing_config_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "does_not_exist.json"
            cfg = load_config(path=p)
            self.assertEqual(cfg.timeout_seconds, 120.0)


class TestEnvOverrides(unittest.TestCase):
    def test_env_overrides_file_and_defaults(self):
        # Save state
        saved = {
            "UNSLOTH_API_BASE_URL": "http://127.0.0.1:9999",
            "UNSLOTH_LOCAL_MODEL_ALIAS": "Env Model",
            "UNSLOTH_DRY_RUN": "yes",
            "UNSLOTH_API_KEY_ENV_VARS": "MY_KEY,OTHER_KEY",
        }
        orig = {}
        for k, v in saved.items():
            orig[k] = os.environ.pop(k, None)
        try:
            os.environ.update(saved)
            cfg = load_config()
            self.assertEqual(cfg.api_base_url, "http://127.0.0.1:9999")
            self.assertEqual(cfg.local_model_alias, "Env Model")
            self.assertTrue(cfg.dry_run)
            self.assertEqual(cfg.api_key_env_vars, ["MY_KEY", "OTHER_KEY"])
        finally:
            for k, v in orig.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_env_dry_run_no(self):
        orig = os.environ.pop("UNSLOTH_DRY_RUN", None)
        try:
            os.environ["UNSLOTH_DRY_RUN"] = "no"
            cfg = load_config()
            self.assertFalse(cfg.dry_run)
        finally:
            if orig is None:
                os.environ.pop("UNSLOTH_DRY_RUN", None)
            else:
                os.environ["UNSLOTH_DRY_RUN"] = orig


class TestApiKeyLookup(unittest.TestCase):
    def test_api_key_first_non_empty_wins(self):
        orig = {
            "A": os.environ.pop("UNSLOTH_STUDIO_AUTH_TOKEN", None),
            "B": os.environ.pop("UNSLOTH_API_KEY", None),
        }
        try:
            os.environ["UNSLOTH_STUDIO_AUTH_TOKEN"] = "token-a"
            os.environ["UNSLOTH_API_KEY"] = "token-b"
            cfg = OffloaderConfig()
            self.assertEqual(cfg.api_key(), "token-a")
        finally:
            for k, v in orig.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_api_key_none_when_no_env(self):
        orig = {
            "A": os.environ.pop("UNSLOTH_STUDIO_AUTH_TOKEN", None),
            "B": os.environ.pop("UNSLOTH_API_KEY", None),
        }
        try:
            cfg = OffloaderConfig()
            self.assertIsNone(cfg.api_key())
        finally:
            for k, v in orig.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class TestGetProjectRoot(unittest.TestCase):
    def test_get_project_root_is_relative_to_package(self):
        root = get_project_root()
        # The root must contain the bridge package dir.
        (root / "bridge").exists()
        self.assertTrue((root / "bridge" / "__init__.py").exists())
        # It is derived from the package location, not a hardcoded absolute path.
        self.assertTrue(root.is_absolute(), "project root should be resolved")


if __name__ == "__main__":
    unittest.main()
