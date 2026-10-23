"""Configuration loading for the offloader.

The config is a small JSON document with path-independent defaults: no absolute
user paths are hard-coded. The project root is resolved relative to this package
(``bridge``), so the same code runs from any working directory or installation
location. Environment variables override individual fields (handoff.md section 12
and project_instructions section 2), and a JSON file (default ``bridge/config.json``)
may override the defaults without editing source.

Precedence (highest wins):
    1. Environment variables
    2. JSON config file (default ``bridge/config.json`` or ``UNSLOTH_OFFLOADER_CONFIG``)
    3. Built-in defaults
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .schemas import LengthLevel, ModelProfile


def get_project_root() -> Path:
    """Return the project root as a path relative to this package.

    ``bridge`` is a top-level package inside the project, so the project root is
    the parent of the package directory. This avoids any absolute user path.
    """
    return Path(__file__).resolve().parent.parent


def default_excluded_files() -> list:
    """Default secret/credential exclusion globs (project_instructions section 2.3).

    Relative, glob-form patterns; matched against working-tree paths."""
    return [
        ".env",
        ".env.*",
        "credentials",
        "credentials/*",
        "**/.aws",
        "**/.aws/*",
        "**/*.pem",
        "**/*.p12",
        "**/*.pfx",
        "**/.gpg",
        "**/id_rsa",
        "**/id_ed25519",
        "**/.ssh",
        "**/.ssh/*",
        "**/.netrc",
        "**/token",
        "**/secret",
        "**/secrets",
        "**/secret/*",
        "package.json.auth",
        "npmrc",
        "**/private_key",
        "**/private-key",
        "**/private_key.txt",
        "**/private-key.txt",
    ]


def _env_list(env, name) -> Optional[list]:
    value = env.get(name, "").strip()
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def load_config(path=None, *, env=None):
    """Load and validate the offloader configuration.

    :param path: Path to a JSON config file. When None, the default is
        ``<project_root>/bridge/config.json`` (or ``UNSLOTH_OFFLOADER_CONFIG``).
        The file is read only if it exists; otherwise defaults are used.
    :param env: Optional mapping of environment variables (for tests); defaults to
        ``os.environ``.
    :return: A validated :class:`OffloaderConfig`.
    :raises: :class:`ConfigurationError` if the JSON is malformed or fields are invalid.
    """
    env = env or os.environ

    # Be lenient about the path type (accepts str, bytes, Path, or None).
    if path is not None and not isinstance(path, Path):
        path = Path(path)

    # Resolve the config file path (path-independent).
    if path is None:
        env_path = env.get("UNSLOTH_OFFLOADER_CONFIG", "").strip()
        if env_path:
            path = Path(env_path).expanduser()
        else:
            path = get_project_root() / "bridge" / "config.json"

    data = {}
    if path is not None and path.exists():
        import json

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: S101 - intentionally broad
            from .errors import ConfigurationError

            raise ConfigurationError(
                f"invalid JSON config at {path}: {exc!r}", details={"path": str(path)}
            ) from exc
        if not isinstance(data, dict):
            from .errors import ConfigurationError

            raise ConfigurationError(
                "config file root must be a JSON object", details={"path": str(path)}
            )

    # Environment overrides (highest precedence).
    config = OffloaderConfig(**data)

    base_url = env.get("UNSLOTH_API_BASE_URL", "").strip()
    if base_url:
        config.api_base_url = base_url

    key_vars = _env_list(env, "UNSLOTH_API_KEY_ENV_VARS")
    if key_vars:
        config.api_key_env_vars = key_vars

    model_alias = env.get("UNSLOTH_LOCAL_MODEL_ALIAS", "").strip()
    if model_alias:
        config.local_model_alias = model_alias

    model_match = env.get("UNSLOTH_LOCAL_MODEL_MATCH", "").strip()
    if model_match:
        config.local_model_match = model_match

    dry_run = env.get("UNSLOTH_DRY_RUN", "").strip().lower()
    if dry_run in {"1", "true", "yes", "on"}:
        config.dry_run = True
    elif dry_run in {"0", "false", "no", "off"}:
        config.dry_run = False

    return config


class OffloaderConfig(BaseModel):
    """Offloader configuration. All defaults are path-independent."""
    # Unsloth server
    api_base_url: str = Field(
        default="http://127.0.0.1:8888",
        description="Base URL of the local OpenAI-compatible API.",
    )
    api_key_env_vars: list = Field(
        default_factory=lambda: ["UNSLOTH_STUDIO_AUTH_TOKEN", "UNSLOTH_API_KEY"],
        description="Env vars to read the API key from (first non-empty wins).",
    )
    # model
    local_model_alias: str = Field(
        default="Ternary Bonsai 2 27B PTQ1_0",
        description="Configurable alias for the local model.",
    )
    local_model_match: str = Field(
        default="Ternary Bonsai 2 27B",
        description="Case-insensitive substring matched against /v1/models id/name "
        "to identify the loaded model.",
    )
    # routing
    auto_handoff_enabled: bool = Field(
        default=True, description="Whether to attempt automatic offload decisions."
    )
    max_difficulty_full_handoff: int = Field(
        default=3, ge=1, le=5, description="Max difficulty for full local handoff."
    )
    max_default_length: LengthLevel = Field(
        default=LengthLevel.MEDIUM, description="Max length for default offload."
    )
    model_profile: ModelProfile = Field(
        default_factory=ModelProfile,
        description="Configurable local model profile (handoff.md section 5).")
    max_validation_failures: int = Field(
        default=2, ge=1,
        description="Automatic local delegation stops after this many validation "
        "failures for the same task (handoff.md section 10).")
    # transport
    timeout_seconds: float = Field(default=120.0, gt=0)
    max_retries: int = Field(default=2, ge=0)
    context_budget_tokens: int = Field(default=16384, ge=0)
    # secrets
    excluded_files: list = Field(default_factory=default_excluded_files)
    # general
    dry_run: bool = Field(default=False)
    log_level: str = Field(default="info")

    def api_key(self):
        """Return the first non-empty API key from the configured env vars, or None."""
        for name in self.api_key_env_vars:
            value = os.environ.get(name, "").strip()
            if value:
                return value
        return None


__all__ = ["OffloaderConfig", "load_config", "get_project_root", "default_excluded_files"]
