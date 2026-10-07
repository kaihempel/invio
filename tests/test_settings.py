"""Tests for ``invio.config.settings``: loading, secret masking, validation and caching."""

from pathlib import Path
from typing import get_args

import pytest
from pydantic import SecretStr, ValidationError

from invio.config.settings import (
    MissingSettingError,
    SecretName,
    Settings,
    get_settings,
    unknown_env_keys,
)

SECRET = "sk-super-secret-value"
DB_PASSWORD = "db-pa55word"


def test_loads_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("INVIO_SMTP_PORT", "2525")
    monkeypatch.setenv("INVIO_SMTP_SECURITY", "ssl")
    monkeypatch.setenv("INVIO_OLLAMA_BASE_URL", "http://ollama:11434")

    settings = Settings()

    assert settings.require_secret("openai_api_key") == SECRET
    assert settings.smtp_port == 2525
    assert settings.smtp_security == "ssl"
    assert settings.ollama_base_url == "http://ollama:11434"


def test_loads_from_dotenv_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(
        f"INVIO_DATABASE_URL=mysql+pymysql://invio:{DB_PASSWORD}@localhost/invio\n"
        "INVIO_SMTP_HOST=smtp.example.com\n"
        "INVIO_NOT_A_SETTING=whatever\n",  # unknown keys are ignored (but reported)
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
    assert settings.smtp_security == "starttls"
    assert settings.smtp_timeout_seconds == 30.0
    assert settings.log_level == "INFO"
    assert settings.archive_dir is None
    assert settings.youtube_cookies_file is None
    assert settings.youtube_proxy is None
    assert settings.whisper_model_size == "small"


def test_secrets_are_not_leaked_in_repr_str_or_dump(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("MISTRAL", "OPENAI", "ANTHROPIC", "GOOGLE"):
        monkeypatch.setenv(f"INVIO_{name}_API_KEY", SECRET)
    monkeypatch.setenv("INVIO_SMTP_PASSWORD", SECRET)
    monkeypatch.setenv("INVIO_DATABASE_URL", f"mysql://invio:{DB_PASSWORD}@db/invio")
    monkeypatch.setenv("INVIO_TEST_DATABASE_URL", f"mysql://invio:{DB_PASSWORD}@db/test")
    monkeypatch.setenv("INVIO_YOUTUBE_PROXY", f"http://user:{DB_PASSWORD}@proxy:8080")

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


def test_llm_timeout_defaults_to_sixty_seconds() -> None:
    assert Settings().llm_timeout_seconds == 60.0


def test_llm_timeout_is_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_LLM_TIMEOUT_SECONDS", "2.5")

    assert Settings().llm_timeout_seconds == 2.5


@pytest.mark.parametrize("raw", ["0", "-1", "abc", "inf", "nan"])
def test_invalid_llm_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("INVIO_LLM_TIMEOUT_SECONDS", raw)

    with pytest.raises(ValidationError, match="llm_timeout_seconds"):
        Settings()


@pytest.mark.parametrize("value", ["starttls", "ssl", "none"])
def test_smtp_security_accepts_known_modes(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("INVIO_SMTP_SECURITY", value)

    assert Settings().smtp_security == value


def test_smtp_security_rejects_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_SMTP_SECURITY", "tls")

    with pytest.raises(ValidationError) as exc_info:
        Settings()

    message = str(exc_info.value)
    assert "smtp_security" in message
    assert "'starttls', 'ssl' or 'none'" in message


def test_smtp_timeout_defaults_to_thirty_seconds() -> None:
    assert Settings().smtp_timeout_seconds == 30.0


@pytest.mark.parametrize("raw", ["0", "-1", "inf", "nan"])
def test_invalid_smtp_timeout_is_rejected(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    monkeypatch.setenv("INVIO_SMTP_TIMEOUT_SECONDS", raw)

    with pytest.raises(ValidationError, match="smtp_timeout_seconds"):
        Settings()


def test_leftover_smtp_starttls_is_reported_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_SMTP_STARTTLS", "true")

    assert unknown_env_keys() == ["INVIO_SMTP_STARTTLS"]


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    first = get_settings()
    monkeypatch.setenv("INVIO_SMTP_HOST", "changed")

    assert get_settings() is first
    assert get_settings().smtp_host is None

    get_settings.cache_clear()
    assert get_settings().smtp_host == "changed"


def test_secret_name_matches_secret_fields() -> None:
    secret_fields = {
        name
        for name, field in Settings.model_fields.items()
        if field.annotation in (SecretStr, SecretStr | None)
    }

    assert set(get_args(SecretName)) == secret_fields


def test_env_example_keys_are_all_known_settings() -> None:
    env_example = Path(__file__).resolve().parents[1] / ".env.example"

    assert unknown_env_keys(env_example) == []


def test_env_file_override_is_used_by_get_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text("INVIO_SMTP_HOST=from-cwd\n", encoding="utf-8")
    deploy_env = tmp_path / "deploy" / "invio.env"
    deploy_env.parent.mkdir()
    deploy_env.write_text("INVIO_SMTP_HOST=from-override\n", encoding="utf-8")
    monkeypatch.setenv("INVIO_ENV_FILE", str(deploy_env))

    assert get_settings().smtp_host == "from-override"


def test_missing_env_file_override_fails_loudly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_ENV_FILE", "/does/not/exist.env")

    with pytest.raises(FileNotFoundError, match="INVIO_ENV_FILE"):
        get_settings()


def test_unknown_env_keys_reports_typos_from_env_and_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / ".env").write_text(
        "INVIO_SMTP_HOST=ok\ninvio_smpt_port=25\nOTHER_VAR=1\n", encoding="utf-8"
    )
    monkeypatch.setenv("INVIO_OPENAI_APIKEY", "typo")
    monkeypatch.setenv("INVIO_ENV_FILE", str(tmp_path / ".env"))

    assert unknown_env_keys() == ["INVIO_OPENAI_APIKEY", "invio_smpt_port"]


def test_http_client_defaults() -> None:
    settings = Settings()

    assert settings.http_contact == "admin@example.invalid"
    assert settings.http_max_response_bytes == 10_485_760
    assert settings.http_max_redirects == 5
    assert settings.http_connect_timeout_seconds == 10.0
    assert settings.http_read_timeout_seconds == 30.0
    assert settings.http_total_timeout_seconds == 60.0
    assert settings.http_host_interval_seconds == 1.0
    assert settings.http_respect_robots is True


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("HTTP_HOST_INTERVAL_SECONDS", "0"),
        ("HTTP_MAX_RESPONSE_BYTES", "0"),
        ("HTTP_MAX_REDIRECTS", "-1"),
        ("HTTP_READ_TIMEOUT_SECONDS", "nan"),
        ("HTTP_CONNECT_TIMEOUT_SECONDS", "inf"),
        ("HTTP_TOTAL_TIMEOUT_SECONDS", "-5"),
        ("HTTP_CONTACT", "ops@example.org\nX-Injected: 1"),
        ("HTTP_CONTACT", "a\rb"),
        ("HTTP_CONTACT", ""),
        ("HTTP_CONTACT", "a\x07b"),
        ("HTTP_CONTACT", "a\tb"),
    ],
)
def test_invalid_http_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, raw: str
) -> None:
    monkeypatch.setenv(f"INVIO_{name}", raw)

    with pytest.raises(ValidationError, match=name.lower()):
        Settings()


def test_http_total_timeout_must_cover_read_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_HTTP_READ_TIMEOUT_SECONDS", "20")
    monkeypatch.setenv("INVIO_HTTP_TOTAL_TIMEOUT_SECONDS", "10")

    with pytest.raises(ValidationError, match="http_total_timeout_seconds"):
        Settings()


def test_run_orchestration_defaults() -> None:
    settings = Settings()

    assert settings.max_parallel_items == 4
    assert settings.run_lock_seconds == 7200


def test_run_orchestration_settings_are_read_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INVIO_MAX_PARALLEL_ITEMS", "9")
    monkeypatch.setenv("INVIO_RUN_LOCK_SECONDS", "60")

    settings = Settings()

    assert settings.max_parallel_items == 9
    assert settings.run_lock_seconds == 60


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("INVIO_MAX_PARALLEL_ITEMS", "0"),
        ("INVIO_MAX_PARALLEL_ITEMS", "-3"),
        ("INVIO_RUN_LOCK_SECONDS", "59"),
        ("INVIO_RUN_LOCK_SECONDS", "0"),
    ],
)
def test_invalid_run_orchestration_settings_are_rejected(
    monkeypatch: pytest.MonkeyPatch, name: str, raw: str
) -> None:
    monkeypatch.setenv(name, raw)

    with pytest.raises(ValidationError, match=name.removeprefix("INVIO_").lower()):
        Settings()


def test_healthcheck_url_is_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_HEALTHCHECK_URL", "https://hc-ping.com/uuid-123")

    url = get_settings().healthcheck_url

    assert isinstance(url, SecretStr)
    assert url.get_secret_value() == "https://hc-ping.com/uuid-123"
    assert "uuid-123" not in repr(get_settings())


def test_healthcheck_url_is_unset_by_default() -> None:
    assert get_settings().healthcheck_url is None


@pytest.mark.parametrize("value", ["ftp://x", "not a url", "https://", "http:///path", "//host"])
def test_an_invalid_healthcheck_url_fails_validation_without_echoing_it(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setenv("INVIO_HEALTHCHECK_URL", value)

    with pytest.raises(ValidationError) as excinfo:
        get_settings()

    assert "healthcheck_url" in str(excinfo.value)
    assert "absolute http(s) URL" in str(excinfo.value)
