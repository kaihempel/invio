"""Application settings loaded from ``INVIO_*`` environment variables and ``.env``.

Secrets are stored as :class:`pydantic.SecretStr`, so they never show up in ``repr``/``str``
output. Provider credentials are optional at startup; code that needs one calls
:meth:`Settings.require_secret`, which fails with a clear message only at that point.

The ``.env`` file is read from the working directory unless ``INVIO_ENV_FILE`` points
elsewhere (useful for cron/systemd, where the working directory is not the project).
"""

import logging
import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "INVIO_"
ENV_FILE_VAR = f"{ENV_PREFIX}ENV_FILE"
DEFAULT_ENV_FILE = ".env"

SecretName = Literal[
    "database_url",
    "test_database_url",
    "mistral_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "google_api_key",
    "smtp_password",
    "youtube_proxy",
]

WhisperModelSize = Literal["tiny", "base", "small", "medium", "large-v3"]


class MissingSettingError(RuntimeError):
    """Raised when a setting that is required for the requested operation is not set."""

    def __init__(self, name: str) -> None:
        self.env_var = f"{ENV_PREFIX}{name.upper()}"
        super().__init__(f"{self.env_var} is not set")


def parse_log_level(value: str) -> str:
    """Return ``value`` as a canonical upper-case level name or raise :class:`ValueError`."""
    level = value.strip().upper()
    if level not in logging.getLevelNamesMapping():
        valid = ", ".join(sorted(logging.getLevelNamesMapping()))
        raise ValueError(f"invalid log level {value!r}; expected one of: {valid}")
    return level


class Settings(BaseSettings):
    """Invio configuration. Every field maps to an ``INVIO_<FIELD_NAME>`` variable."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database (the URLs may embed a password, hence SecretStr)
    database_url: SecretStr | None = None
    test_database_url: SecretStr | None = None

    # LLM providers
    mistral_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    ollama_base_url: str = "http://localhost:11434"
    llm_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)

    # E-mail notifications
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from: str | None = None
    smtp_starttls: bool = True

    # Operations
    log_level: str = "INFO"
    healthcheck_url: str | None = None
    archive_dir: Path | None = None

    # YouTube source (the proxy URL may embed credentials, hence SecretStr)
    youtube_cookies_file: Path | None = None
    youtube_proxy: SecretStr | None = None

    # Transcription
    whisper_model_size: WhisperModelSize = "small"

    # HTTP client for sources
    http_contact: str = Field(default="admin@example.invalid", min_length=1)
    http_max_response_bytes: int = Field(default=10_485_760, gt=0)
    http_max_redirects: int = Field(default=5, ge=0)
    http_connect_timeout_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    http_read_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    http_total_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    http_host_interval_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    http_respect_robots: bool = True

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        return parse_log_level(value)

    @field_validator("http_contact")
    @classmethod
    def _validate_http_contact(cls, value: str) -> str:
        # The contact ends up in the User-Agent header; a line break would allow header injection.
        if "\r" in value or "\n" in value:
            raise ValueError("must not contain line breaks")
        return value

    def require_secret(self, name: SecretName) -> str:
        """Return the plain value of secret ``name`` or raise :class:`MissingSettingError`."""
        secret: SecretStr | None = getattr(self, name)
        if secret is None or not secret.get_secret_value():
            raise MissingSettingError(name)
        return secret.get_secret_value()


def env_file_path() -> Path:
    """Return the ``.env`` path: ``$INVIO_ENV_FILE`` if set, else ``.env`` in the CWD.

    Raises :class:`FileNotFoundError` if ``INVIO_ENV_FILE`` is set but does not exist, so a
    misconfigured deployment fails loudly instead of silently running without its secrets.
    """
    override = os.environ.get(ENV_FILE_VAR)
    if not override:
        return Path(DEFAULT_ENV_FILE)
    path = Path(override)
    if not path.is_file():
        raise FileNotFoundError(f"{ENV_FILE_VAR} points to a missing file: {path}")
    return path


def unknown_env_keys(env_file: Path | None = None) -> list[str]:
    """Return ``INVIO_*`` names from the environment and ``env_file`` that no setting uses.

    Such keys are ignored by :class:`Settings`, so they usually indicate a typo.
    """
    env_file = env_file if env_file is not None else env_file_path()
    known = {f"{ENV_PREFIX}{name.upper()}" for name in Settings.model_fields} | {ENV_FILE_VAR}
    names = set(os.environ)
    if env_file.is_file():
        names |= set(dotenv_values(env_file, encoding="utf-8"))
    return sorted(
        name for name in names if name.upper().startswith(ENV_PREFIX) and name.upper() not in known
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached; call ``cache_clear()`` to reload)."""
    return Settings(_env_file=env_file_path())
