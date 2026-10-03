"""Application settings loaded from ``INVIO_*`` environment variables and ``.env``.

Secrets are stored as :class:`pydantic.SecretStr`, so they never show up in ``repr``/``str``
output. Provider credentials are optional at startup; code that needs one calls
:meth:`Settings.require_secret`, which fails with a clear message only at that point.
"""

import logging
from functools import lru_cache
from typing import Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_PREFIX = "INVIO_"

SecretName = Literal[
    "database_url",
    "mistral_api_key",
    "openai_api_key",
    "anthropic_api_key",
    "google_api_key",
    "smtp_password",
]


class MissingSettingError(RuntimeError):
    """Raised when a setting that is required for the requested operation is not set."""

    def __init__(self, name: str) -> None:
        self.env_var = f"{ENV_PREFIX}{name.upper()}"
        super().__init__(f"{self.env_var} is not set")


class Settings(BaseSettings):
    """Invio configuration. Every field maps to an ``INVIO_<FIELD_NAME>`` variable."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Database (the URL embeds a password, hence SecretStr)
    database_url: SecretStr | None = None

    # LLM providers
    mistral_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    google_api_key: SecretStr | None = None
    ollama_base_url: str = "http://localhost:11434"

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

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.strip().upper()
        if level not in logging.getLevelNamesMapping():
            valid = ", ".join(sorted(logging.getLevelNamesMapping()))
            raise ValueError(f"invalid log level {value!r}; expected one of: {valid}")
        return level

    def require_secret(self, name: SecretName) -> str:
        """Return the plain value of secret ``name`` or raise :class:`MissingSettingError`."""
        secret: SecretStr | None = getattr(self, name)
        if secret is None or not secret.get_secret_value():
            raise MissingSettingError(name)
        return secret.get_secret_value()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached; call ``cache_clear()`` to reload)."""
    return Settings()
