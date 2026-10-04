"""CLI tests for ``invio llm test`` (contract: specs/005-gh-issue-8/contracts/cli.md)."""

import re
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any, ClassVar, Self

import httpx2
import pytest
from typer.testing import CliRunner, Result

from invio.cli.main import app
from invio.config.settings import Settings
from invio.llm import factory
from invio.llm import registry as llm_registry
from invio.llm.base import (
    LLMAuthError,
    LLMError,
    LLMInvalidOutputError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMUnavailableError,
    ModelRegistryError,
    Usage,
    require_api_key,
)
from invio.llm.fake import FakeProvider, FakeReply
from invio.llm.mistral import MistralProvider
from tests.llm_helpers import write_registry
from tests.mistral_helpers import API_KEY, Recorder, Reply, recording_options

runner = CliRunner()
Register = Callable[[str, Any], None]

CHEAP = "model-cheap"
PRICEY = "model-pricey"


def _entry(price: int) -> dict[str, object]:
    return {"input_price_per_mtok": price, "output_price_per_mtok": price, "context_window": 1000}


@pytest.fixture(autouse=True)
def temp_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replace the shipped registry with two mistral models at different prices."""
    directory = write_registry(
        tmp_path / "registry", "mistral", {PRICEY: _entry(5), CHEAP: _entry(1)}
    )
    registry = llm_registry.load_registry([directory])
    monkeypatch.setattr(llm_registry, "default_registry", lambda: registry)
    yield


def _invoke(*args: str) -> Result:
    return runner.invoke(app, ["llm", "test", *args])


def _last_message(result: Result) -> str:
    """The last stderr line that is not a JSON log record."""
    lines = [line for line in result.stderr.splitlines() if not line.startswith("{")]
    return lines[-1] if lines else ""


def _fake(register: Register, *script: Any) -> FakeProvider:
    fake = FakeProvider(list(script))
    register("mistral", fake)
    return fake


def test_success_prints_one_metadata_line(patched_providers: Register) -> None:
    fake = _fake(patched_providers, FakeReply("OK", Usage(12, 2)))

    result = _invoke("mistral")

    assert result.exit_code == 0, result.stderr
    assert re.fullmatch(
        rf"ok provider=mistral model={CHEAP} input_tokens=12 output_tokens=2 "
        r"duration_ms=\d+(\.\d+)?\n",
        result.stdout,
    )
    (request,) = fake.requests
    assert (request.model, request.temperature, request.max_tokens) == (CHEAP, 0, 5)
    assert request.system == "Connectivity check."
    assert request.user == "Reply with OK."


def test_model_option_selects_that_model(patched_providers: Register) -> None:
    fake = _fake(patched_providers, FakeReply("OK", Usage(1, 1)))

    result = _invoke("mistral", "--model", PRICEY)

    assert result.exit_code == 0, result.stderr
    assert f"model={PRICEY}" in result.stdout
    assert fake.requests[0].model == PRICEY


def test_price_tie_picks_the_lowest_model_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_providers: Register
) -> None:
    directory = write_registry(
        tmp_path / "tie", "mistral", {"b-model": _entry(1), "a-model": _entry(1)}
    )
    registry = llm_registry.load_registry([directory])
    monkeypatch.setattr(llm_registry, "default_registry", lambda: registry)
    _fake(patched_providers, FakeReply("OK", Usage(1, 1)))

    result = _invoke("mistral")

    assert "model=a-model" in result.stdout


def test_unregistered_model_is_a_configuration_error(patched_providers: Register) -> None:
    fake = _fake(patched_providers)

    result = _invoke("mistral", "--model", "mistral-small-latest")

    assert result.exit_code == 2
    assert _last_message(result) == (
        "Configuration error: model 'mistral-small-latest' is not registered "
        "for LLM provider 'mistral'"
    )
    assert fake.requests == []


def test_unknown_provider_lists_registered_ones(patched_providers: Register) -> None:
    _fake(patched_providers)

    result = _invoke("nope")

    assert result.exit_code == 2
    assert _last_message(result) == (
        "Configuration error: unknown LLM provider 'nope'; registered: mistral"
    )


def test_missing_api_key_is_a_configuration_error() -> None:
    result = _invoke("mistral")

    assert result.exit_code == 2
    assert "INVIO_MISTRAL_API_KEY" in _last_message(result)
    assert _last_message(result).startswith("Configuration error: ")


@pytest.mark.parametrize(
    "error",
    [
        LLMAuthError("bad key", provider="mistral"),
        LLMRateLimitError("slow down", provider="mistral"),
        LLMUnavailableError("down", provider="mistral"),
        LLMInvalidRequestError("rejected", provider="mistral", status=400),
    ],
    ids=lambda e: type(e).__name__,
)
def test_llm_errors_exit_1_without_traceback(patched_providers: Register, error: LLMError) -> None:
    _fake(patched_providers, error)

    result = _invoke("mistral")

    assert result.exit_code == 1
    assert _last_message(result) == f"Error: {type(error).__name__}: {error}"
    assert "Traceback" not in result.stderr + result.stdout
    assert result.stdout == ""


def test_broken_registry_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> llm_registry.ModelRegistry:
        raise ModelRegistryError("models.d/mistral.yaml: invalid registry file: models: required")

    monkeypatch.setattr(llm_registry, "default_registry", broken)

    result = _invoke("mistral")

    assert result.exit_code == 2
    assert _last_message(result) == (
        "Configuration error: models.d/mistral.yaml: invalid registry file: models: required"
    )
    assert "Traceback" not in result.stderr + result.stdout


def test_provider_without_registry_models_is_a_configuration_error(
    patched_providers: Register,
) -> None:
    patched_providers("other", FakeProvider([]))

    result = _invoke("other")

    assert result.exit_code == 2
    assert "no models registered for LLM provider 'other'" in _last_message(result)


def test_answer_text_is_never_printed(patched_providers: Register) -> None:
    _fake(patched_providers, FakeReply("ANSWER-SENTINEL", Usage(3, 4)))

    result = _invoke("mistral")

    assert "ANSWER-SENTINEL" not in result.stdout + result.stderr


class _Capture:
    captured: ClassVar[list[Settings]] = []

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        cls.captured.append(settings)
        return cls()


@pytest.mark.parametrize(("configured", "expected"), [("60", 20.0), ("5", 5.0)])
def test_timeout_is_capped_at_20_seconds(
    patched_providers: Register,
    monkeypatch: pytest.MonkeyPatch,
    configured: str,
    expected: float,
) -> None:
    patched_providers("x", FakeProvider([]))  # isolates the registry
    _Capture.captured = []
    factory.register_provider("capture")(_Capture)  # type: ignore[type-var]
    monkeypatch.setenv("INVIO_LLM_TIMEOUT_SECONDS", configured)

    _invoke("capture")

    assert [s.llm_timeout_seconds for s in _Capture.captured] == [expected]


def test_help_lists_the_test_command() -> None:
    result = runner.invoke(app, ["llm", "--help"])

    assert result.exit_code == 0
    assert "test" in result.stdout


# --- traceability gap tests ---------------------------------------------------------------


def _recorded_mistral(*replies: Reply) -> tuple[Recorder, list[float]]:
    """Register a real ``MistralProvider`` on recorded HTTP replies as ``mistral``.

    Needs ``patched_providers`` (isolated registry). The key comes from the settings as in
    production; only the HTTP transport and the retry sleep are replaced.
    """
    recorder = Recorder(replies)
    options, waits = recording_options(recorder)

    class _Recorded(MistralProvider):
        @classmethod
        def from_settings(cls, settings: Settings) -> Self:
            return cls(
                require_api_key(settings, "mistral"),
                timeout_seconds=settings.llm_timeout_seconds,
                **options,
            )

    factory.register_provider("mistral")(_Recorded)
    return recorder, waits


def test_recorded_mistral_success(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch
) -> None:
    # US4-1 against the real provider and recorded HTTP.
    monkeypatch.setenv("INVIO_MISTRAL_API_KEY", API_KEY)
    recorder, _ = _recorded_mistral("chat_ok")

    result = _invoke("mistral")

    assert result.exit_code == 0, result.stderr
    assert re.fullmatch(
        rf"ok provider=mistral model={CHEAP} input_tokens=12 output_tokens=3 "
        r"duration_ms=\d+(\.\d+)?\n",
        result.stdout,
    )
    (request,) = recorder.requests
    assert [client.is_closed for client in recorder.clients] == [True]
    assert request.has_authorization
    assert (request.body["model"], request.body["temperature"]) == (CHEAP, 0)
    assert request.body["max_tokens"] == 5
    assert "Hello" not in result.stdout + result.stderr
    assert API_KEY not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("replies", "error", "requests", "waits_expected"),
    [
        (("error_401",), "LLMAuthError", 1, []),
        (("error_403",), "LLMAuthError", 1, []),
        (("error_429",) * 4, "LLMRateLimitError", 4, [1.0, 2.0, 4.0]),
        (("error_429_retry_after_long",), "LLMRateLimitError", 1, []),
        (("error_503",) * 4, "LLMUnavailableError", 4, [1.0, 2.0, 4.0]),
        (("error_404_model",), "LLMInvalidRequestError", 1, []),
        (("malformed_200",), "LLMUnavailableError", 1, []),
        ((httpx2.DecodingError("bad gzip"),), "LLMUnavailableError", 1, []),
        ((httpx2.TooManyRedirects("loop"),), "LLMUnavailableError", 1, []),
    ],
    ids=["401", "403", "429", "429-long", "503", "404", "malformed", "decoding", "redirects"],
)
def test_recorded_mistral_failures_exit_1(
    patched_providers: Register,
    monkeypatch: pytest.MonkeyPatch,
    replies: tuple[Reply, ...],
    error: str,
    requests: int,
    waits_expected: list[float],
) -> None:
    # US4-3 / FR-022 against the real provider and recorded HTTP.
    monkeypatch.setenv("INVIO_MISTRAL_API_KEY", API_KEY)
    recorder, waits = _recorded_mistral(*replies)

    result = _invoke("mistral")

    assert result.exit_code == 1
    message = _last_message(result)
    assert message.startswith(f"Error: {error}: Mistral ")
    assert f"model {CHEAP}" in message
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert API_KEY not in result.stderr
    assert "Reply with OK." not in result.stderr
    assert len(recorder.requests) == requests
    assert waits == waits_expected
    assert [client.is_closed for client in recorder.clients] == [True]


def test_recorded_mistral_auth_failure_names_the_setting(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVIO_MISTRAL_API_KEY", API_KEY)
    _recorded_mistral("error_401")

    result = _invoke("mistral")

    assert "INVIO_MISTRAL_API_KEY" in _last_message(result)


@pytest.mark.parametrize("key", [None, "   "], ids=["missing", "blank"])
def test_recorded_mistral_without_key_makes_no_request(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, key: str | None
) -> None:
    # US4-2 / FR-022: exit 2 naming the setting, before any HTTP request.
    if key is not None:
        monkeypatch.setenv("INVIO_MISTRAL_API_KEY", key)
    recorder, _ = _recorded_mistral("chat_ok")

    result = _invoke("mistral")

    assert result.exit_code == 2
    assert _last_message(result) == (
        "Configuration error: LLM provider 'mistral' needs an API key: set INVIO_MISTRAL_API_KEY"
    )
    assert recorder.requests == []
    assert recorder.clients_created == 0


def test_model_of_another_provider_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_providers: Register
) -> None:
    # FR-021 / contract: a registered model of a different provider is not accepted.
    directory = tmp_path / "mixed"
    write_registry(directory, "mistral", {CHEAP: _entry(1)})
    models_dir = write_registry(directory, "openai", {"gpt-other": _entry(1)})
    registry = llm_registry.load_registry([models_dir])
    monkeypatch.setattr(llm_registry, "default_registry", lambda: registry)
    fake = _fake(patched_providers)

    result = _invoke("mistral", "--model", "gpt-other")

    assert result.exit_code == 2
    assert "model 'gpt-other' is not registered for LLM provider 'mistral'" in _last_message(result)
    assert fake.requests == []


def test_default_model_ignores_cheaper_models_of_other_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, patched_providers: Register
) -> None:
    directory = tmp_path / "mixed"
    write_registry(directory, "mistral", {PRICEY: _entry(5)})
    models_dir = write_registry(directory, "openai", {"gpt-free": _entry(0)})
    registry = llm_registry.load_registry([models_dir])
    monkeypatch.setattr(llm_registry, "default_registry", lambda: registry)
    fake = _fake(patched_providers, FakeReply("OK", Usage(1, 1)))

    result = _invoke("mistral")

    assert result.exit_code == 0, result.stderr
    assert fake.requests[0].model == PRICEY


def test_invalid_output_error_exits_1(patched_providers: Register) -> None:
    # FR-022 lists invalid output among the provider errors.
    error = LLMInvalidOutputError(
        "still invalid", errors="x", usage=Usage(1, 1), provider="mistral"
    )
    _fake(patched_providers, error)

    result = _invoke("mistral")

    assert result.exit_code == 1
    assert _last_message(result) == "Error: LLMInvalidOutputError: still invalid"


def test_unknown_provider_is_rejected_before_reading_the_key(patched_providers: Register) -> None:
    # US4-5: exit 2 for an unregistered provider even without any key configured.
    _recorded_mistral()

    result = _invoke("openai")

    assert result.exit_code == 2
    assert _last_message(result) == (
        "Configuration error: unknown LLM provider 'openai'; registered: mistral"
    )


def test_root_help_lists_the_llm_command() -> None:
    # FR-023: auto-discovered without editing other CLI files.
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "llm" in result.stdout
