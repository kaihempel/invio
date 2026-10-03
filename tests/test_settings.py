"""Tests for ``invio.config.settings``: loading, secret masking, validation and caching."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from invio.config.settings import MissingSettingError, Settings, get_settings

SECRET = "sk-super-secret-value"
DB_PASSWORD = "db-pa55word"


def test_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("INVIO_SMTP_PORT", "2525")
    monkeypatch.setenv("INVIO_SMTP_STARTTLS", "false")
    monkeypatch.setenv("INVIO_OLLAMA_BASE_URL", "http://ollama:11434")

    settings = Settings()

    assert settings.require_secret("openai_api_key") == SECRET
    assert settings.smtp_port == 2525
    assert settings.smtp_starttls is False
    assert settings.ollama_base_url == "http://ollama:11434"


def test_loads_from_dotenv_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        f"INVIO_DATABASE_URL=mysql+pymysql://invio:{DB_PASSWORD}@localhost/invio\n"
        "INVIO_SMTP_HOST=smtp.example.com\n"
        "INVIO_ARCHIVE_DIR=/tmp/archive\n",  # unknown keys for later features are ignored
        encoding="utf-8",
    )

    settings = Settings()

    assert settings.smtp_host == "smtp.example.com"
    assert DB_PASSWORD in settings.require_secret("database_url")


def test_environment_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".env").write_text("INVIO_SMTP_HOST=from-file\n", encoding="utf-8")
    monkeypatch.setenv("INVIO_SMTP_HOST", "from-env")

    assert Settings().smtp_host == "from-env"


def test_defaults_without_any_configuration() -> None:
    settings = Settings()

    assert settings.database_url is None
    assert settings.openai_api_key is None
    assert settings.ollama_base_url == "http://localhost:11434"
    assert settings.smtp_port == 587
    assert settings.smtp_starttls is True
    assert settings.log_level == "INFO"


def test_secrets_are_not_leaked_in_repr_str_or_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MISTRAL", "OPENAI", "ANTHROPIC", "GOOGLE"):
        monkeypatch.setenv(f"INVIO_{name}_API_KEY", SECRET)
    monkeypatch.setenv("INVIO_SMTP_PASSWORD", SECRET)
    monkeypatch.setenv("INVIO_DATABASE_URL", f"mysql://invio:{DB_PASSWORD}@db/invio")

    settings = Settings()

    for rendered in (
        repr(settings),
        str(settings),
        str(settings.model_dump()),
        settings.model_dump_json(),
    ):
        assert SECRET not in rendered
        assert DB_PASSWORD not in rendered


def test_require_secret_raises_naming_env_var_when_missing() -> None:
    settings = Settings()

    with pytest.raises(MissingSettingError, match="INVIO_OPENAI_API_KEY is not set") as exc:
        settings.require_secret("openai_api_key")
    assert exc.value.env_var == "INVIO_OPENAI_API_KEY"


def test_require_secret_treats_empty_value_as_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_ANTHROPIC_API_KEY", "")

    with pytest.raises(MissingSettingError, match="INVIO_ANTHROPIC_API_KEY"):
        Settings().require_secret("anthropic_api_key")


@pytest.mark.parametrize(("raw", "expected"), [("debug", "DEBUG"), (" Warning ", "WARNING")])
def test_log_level_is_normalised(monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
    monkeypatch.setenv("INVIO_LOG_LEVEL", raw)

    assert Settings().log_level == expected


def test_invalid_log_level_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_LOG_LEVEL", "LOUD")

    with pytest.raises(ValidationError, match="invalid log level"):
        Settings()


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    monkeypatch.setenv("INVIO_SMTP_HOST", "changed")

    assert get_settings() is first
    assert get_settings().smtp_host is None

    get_settings.cache_clear()
    assert get_settings().smtp_host == "changed"
