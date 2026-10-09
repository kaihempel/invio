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
from invio.llm.anthropic import AnthropicProvider
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
from invio.llm.google import GoogleProvider
from invio.llm.mistral import MistralProvider
from invio.llm.ollama import OllamaProvider
from invio.llm.openai import OpenAIProvider
from tests import anthropic_helpers, google_helpers, ollama_helpers, openai_helpers
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
    def from_settings(cls, settings: Settings, *, registry: Any = None) -> Self:
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
        def from_settings(cls, settings: Settings, *, registry: Any = None) -> Self:
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


# --- OpenAI (issue #30, US3) --------------------------------------------------------------

GPT_CHEAP = "gpt-cheap"
GPT_PRICEY = "gpt-pricey"


@pytest.fixture
def openai_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry with two mistral and two openai models at different prices."""
    directory = tmp_path / "with-openai"
    write_registry(directory, "mistral", {PRICEY: _entry(5), CHEAP: _entry(1)})
    models_dir = write_registry(directory, "openai", {GPT_PRICEY: _entry(5), GPT_CHEAP: _entry(1)})
    registry = llm_registry.load_registry([models_dir])
    monkeypatch.setattr(llm_registry, "default_registry", lambda: registry)


def _recorded_openai(
    *replies: openai_helpers.Reply, built: list[OpenAIProvider] | None = None
) -> tuple[openai_helpers.Recorder, list[float]]:
    """Register a real ``OpenAIProvider`` on recorded HTTP replies as ``openai``.

    Needs ``patched_providers`` (isolated registry). The key comes from the settings as in
    production; only the HTTP transport, the base URL and the retry sleep are replaced. Each
    provider built by the command is appended to ``built``.
    """
    recorder = openai_helpers.Recorder(replies)
    options, waits = openai_helpers.recording_options(recorder)

    class _Recorded(OpenAIProvider):
        @classmethod
        def from_settings(cls, settings: Settings, *, registry: Any = None) -> Self:
            provider = cls(
                require_api_key(settings, "openai"),
                timeout_seconds=settings.llm_timeout_seconds,
                **options,
            )
            if built is not None:
                built.append(provider)
            return provider

    factory.register_provider("openai")(_Recorded)
    return recorder, waits


def test_recorded_openai_success(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, openai_registry: None
) -> None:
    monkeypatch.setenv("INVIO_OPENAI_API_KEY", openai_helpers.API_KEY)
    recorder, _ = _recorded_openai("response_ok")

    result = _invoke("openai")

    assert result.exit_code == 0, result.stderr
    assert re.fullmatch(
        rf"ok provider=openai model={GPT_CHEAP} input_tokens=12 output_tokens=3 "
        r"duration_ms=\d+(\.\d+)?\n",
        result.stdout,
    )
    (request,) = recorder.requests
    assert [client.is_closed for client in recorder.clients] == [True]
    assert (request.method, request.path) == ("POST", "/v1/responses")
    assert request.has_authorization
    assert (request.body["model"], request.body["temperature"]) == (GPT_CHEAP, 0)
    # The command asks for 5 tokens; the Responses API minimum is 16.
    assert request.body["max_output_tokens"] == 16
    assert request.body["store"] is False
    assert "Hello" not in result.stdout + result.stderr
    assert openai_helpers.API_KEY not in result.stdout + result.stderr


def test_recorded_openai_model_option_selects_that_model(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, openai_registry: None
) -> None:
    monkeypatch.setenv("INVIO_OPENAI_API_KEY", openai_helpers.API_KEY)
    recorder, _ = _recorded_openai("response_ok")

    result = _invoke("openai", "--model", GPT_PRICEY)

    assert result.exit_code == 0, result.stderr
    assert f"model={GPT_PRICEY}" in result.stdout
    assert recorder.requests[0].body["model"] == GPT_PRICEY


@pytest.mark.parametrize("model", ["gpt-4o-latest", CHEAP], ids=["unregistered", "mistral"])
def test_recorded_openai_model_must_be_registered_for_openai(
    patched_providers: Register,
    monkeypatch: pytest.MonkeyPatch,
    openai_registry: None,
    model: str,
) -> None:
    monkeypatch.setenv("INVIO_OPENAI_API_KEY", openai_helpers.API_KEY)
    recorder, _ = _recorded_openai("response_ok")

    result = _invoke("openai", "--model", model)

    assert result.exit_code == 2
    assert _last_message(result) == (
        f"Configuration error: model '{model}' is not registered for LLM provider 'openai'"
    )
    assert recorder.requests == []


@pytest.mark.parametrize("key", [None, "   "], ids=["missing", "blank"])
def test_recorded_openai_without_key_makes_no_request(
    patched_providers: Register,
    monkeypatch: pytest.MonkeyPatch,
    openai_registry: None,
    key: str | None,
) -> None:
    if key is not None:
        monkeypatch.setenv("INVIO_OPENAI_API_KEY", key)
    recorder, _ = _recorded_openai("response_ok")

    result = _invoke("openai")

    assert result.exit_code == 2
    assert _last_message(result) == (
        "Configuration error: LLM provider 'openai' needs an API key: set INVIO_OPENAI_API_KEY"
    )
    assert recorder.requests == []
    assert recorder.clients_created == 0


@pytest.mark.parametrize(
    ("replies", "error", "requests", "waits_expected"),
    [
        (("error_401",), "LLMAuthError", 1, []),
        (("error_403",), "LLMAuthError", 1, []),
        (("error_429",) * 4, "LLMRateLimitError", 4, [1.0, 2.0, 4.0]),
        (("error_429_retry_after_long",), "LLMRateLimitError", 1, []),
        (("error_429_insufficient_quota",), "LLMQuotaError", 1, []),
        (("error_503",) * 4, "LLMUnavailableError", 4, [1.0, 2.0, 4.0]),
        (("error_404_model",), "LLMInvalidRequestError", 1, []),
        (("malformed_200",), "LLMUnavailableError", 1, []),
        (("response_refusal",), "LLMUnavailableError", 1, []),
    ],
    ids=["401", "403", "429", "429-long", "quota", "503", "404", "malformed", "refusal"],
)
def test_recorded_openai_failures_exit_1(
    patched_providers: Register,
    monkeypatch: pytest.MonkeyPatch,
    openai_registry: None,
    replies: tuple[openai_helpers.Reply, ...],
    error: str,
    requests: int,
    waits_expected: list[float],
) -> None:
    monkeypatch.setenv("INVIO_OPENAI_API_KEY", openai_helpers.API_KEY)
    recorder, waits = _recorded_openai(*replies)

    result = _invoke("openai")

    assert result.exit_code == 1
    message = _last_message(result)
    assert message.startswith(f"Error: {error}: OpenAI ")
    assert f"model {GPT_CHEAP}" in message
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert openai_helpers.API_KEY not in result.stderr
    assert "Reply with OK." not in result.stderr
    assert len(recorder.requests) == requests
    assert waits == waits_expected
    assert [client.is_closed for client in recorder.clients] == [True]


def test_recorded_openai_auth_failure_names_the_setting(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, openai_registry: None
) -> None:
    monkeypatch.setenv("INVIO_OPENAI_API_KEY", openai_helpers.API_KEY)
    _recorded_openai("error_401")

    result = _invoke("openai")

    assert _last_message(result).startswith("Error: LLMAuthError: ")
    assert "INVIO_OPENAI_API_KEY" in _last_message(result)


def test_recorded_openai_timeout_is_capped(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, openai_registry: None
) -> None:
    # SC-004: the command's cap applies to the OpenAI provider like to any other.
    monkeypatch.setenv("INVIO_OPENAI_API_KEY", openai_helpers.API_KEY)
    monkeypatch.setenv("INVIO_LLM_TIMEOUT_SECONDS", "600")
    built: list[OpenAIProvider] = []
    _recorded_openai("response_ok", built=built)

    result = _invoke("openai")

    assert result.exit_code == 0, result.stderr
    assert [provider.timeout_seconds for provider in built] == [20.0]


def test_recorded_openai_hanging_request_is_bounded_by_the_timeout(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, openai_registry: None
) -> None:
    monkeypatch.setenv("INVIO_OPENAI_API_KEY", openai_helpers.API_KEY)
    monkeypatch.setenv("INVIO_LLM_TIMEOUT_SECONDS", "0.2")
    recorder, waits = _recorded_openai(openai_helpers.HANG)

    result = _invoke("openai")

    assert result.exit_code == 1
    assert _last_message(result).startswith("Error: LLMUnavailableError: ")
    assert "0.2 s" in _last_message(result)
    assert len(recorder.requests) == 1
    assert waits == []


# --- Anthropic (issue #31, US3) -----------------------------------------------------------

CLAUDE_CHEAP = "claude-haiku-4-5-20251001"
CLAUDE_SMART = "claude-sonnet-4-6"


def _claude_entry(price: int) -> dict[str, object]:
    return {**_entry(price), "max_output_tokens": 64000}


@pytest.fixture
def anthropic_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A registry with two mistral and two anthropic models at different prices."""
    directory = tmp_path / "with-anthropic"
    write_registry(directory, "mistral", {PRICEY: _entry(5), CHEAP: _entry(1)})
    models_dir = write_registry(
        directory,
        "anthropic",
        {CLAUDE_SMART: _claude_entry(5), CLAUDE_CHEAP: _claude_entry(1)},
    )
    registry = llm_registry.load_registry([models_dir])
    monkeypatch.setattr(llm_registry, "default_registry", lambda: registry)


def _recorded_anthropic(
    *replies: anthropic_helpers.Reply, built: list[AnthropicProvider] | None = None
) -> tuple[anthropic_helpers.Recorder, list[float]]:
    """Register a real ``AnthropicProvider`` on recorded HTTP replies as ``anthropic``.

    Needs ``patched_providers`` (isolated registry). The key comes from the settings as in
    production; only the HTTP transport, the base URL and the retry sleep are replaced. Each
    provider built by the command is appended to ``built``.
    """
    recorder = anthropic_helpers.Recorder(replies)
    options, waits = anthropic_helpers.recording_options(recorder)

    class _Recorded(AnthropicProvider):
        @classmethod
        def from_settings(cls, settings: Settings, *, registry: Any = None) -> Self:
            provider = cls(
                require_api_key(settings, "anthropic"),
                timeout_seconds=settings.llm_timeout_seconds,
                registry=registry,
                **options,
            )
            if built is not None:
                built.append(provider)
            return provider

    factory.register_provider("anthropic")(_Recorded)
    return recorder, waits


def test_recorded_anthropic_success(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, anthropic_registry: None
) -> None:
    monkeypatch.setenv("INVIO_ANTHROPIC_API_KEY", anthropic_helpers.API_KEY)
    recorder, _ = _recorded_anthropic("message_ok")

    result = _invoke("anthropic")

    assert result.exit_code == 0, result.stderr
    assert re.fullmatch(
        rf"ok provider=anthropic model={CLAUDE_CHEAP} input_tokens=12 output_tokens=3 "
        r"duration_ms=\d+(\.\d+)?\n",
        result.stdout,
    )
    (request,) = recorder.requests
    assert [client.is_closed for client in recorder.clients] == [True]
    assert (request.method, request.path) == ("POST", "/v1/messages")
    assert request.x_api_key == anthropic_helpers.API_KEY
    assert (request.body["model"], request.body["temperature"]) == (CLAUDE_CHEAP, 0)
    assert request.body["max_tokens"] == 5
    assert request.body["max_tokens"] == 5
    assert "Hello" not in result.stdout + result.stderr
    assert anthropic_helpers.API_KEY not in result.stdout + result.stderr


def test_recorded_anthropic_model_option_selects_that_model(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, anthropic_registry: None
) -> None:
    monkeypatch.setenv("INVIO_ANTHROPIC_API_KEY", anthropic_helpers.API_KEY)
    recorder, _ = _recorded_anthropic("message_ok")

    result = _invoke("anthropic", "--model", CLAUDE_SMART)

    assert result.exit_code == 0, result.stderr
    assert f"model={CLAUDE_SMART}" in result.stdout
    assert recorder.requests[0].body["model"] == CLAUDE_SMART


@pytest.mark.parametrize("model", ["claude-opus-9", CHEAP], ids=["unregistered", "mistral"])
def test_recorded_anthropic_model_must_be_registered_for_anthropic(
    patched_providers: Register,
    monkeypatch: pytest.MonkeyPatch,
    anthropic_registry: None,
    model: str,
) -> None:
    monkeypatch.setenv("INVIO_ANTHROPIC_API_KEY", anthropic_helpers.API_KEY)
    recorder, _ = _recorded_anthropic("message_ok")

    result = _invoke("anthropic", "--model", model)

    assert result.exit_code == 2
    assert _last_message(result) == (
        f"Configuration error: model '{model}' is not registered for LLM provider 'anthropic'"
    )
    assert recorder.requests == []


@pytest.mark.parametrize("key", [None, "   "], ids=["missing", "blank"])
def test_recorded_anthropic_without_key_makes_no_request(
    patched_providers: Register,
    monkeypatch: pytest.MonkeyPatch,
    anthropic_registry: None,
    key: str | None,
) -> None:
    if key is not None:
        monkeypatch.setenv("INVIO_ANTHROPIC_API_KEY", key)
    recorder, _ = _recorded_anthropic("message_ok")

    result = _invoke("anthropic")

    assert result.exit_code == 2
    assert _last_message(result) == (
        "Configuration error: LLM provider 'anthropic' needs an API key: "
        "set INVIO_ANTHROPIC_API_KEY"
    )
    assert recorder.requests == []
    assert recorder.clients_created == 0


@pytest.mark.parametrize(
    ("replies", "error", "requests", "waits_expected"),
    [
        (("error_401",), "LLMAuthError", 1, []),
        (("error_403",), "LLMAuthError", 1, []),
        (("error_429",) * 4, "LLMRateLimitError", 4, [1.0, 2.0, 4.0]),
        (("error_402",), "LLMQuotaError", 1, []),
        (("error_529",) * 4, "LLMUnavailableError", 4, [1.0, 2.0, 4.0]),
        (("error_404_model",), "LLMInvalidRequestError", 1, []),
        (("malformed_200",), "LLMUnavailableError", 1, []),
        (("message_refusal",), "LLMUnavailableError", 1, []),
    ],
    ids=["401", "403", "429", "402", "529", "404", "malformed", "refusal"],
)
def test_recorded_anthropic_failures_exit_1(
    patched_providers: Register,
    monkeypatch: pytest.MonkeyPatch,
    anthropic_registry: None,
    replies: tuple[anthropic_helpers.Reply, ...],
    error: str,
    requests: int,
    waits_expected: list[float],
) -> None:
    monkeypatch.setenv("INVIO_ANTHROPIC_API_KEY", anthropic_helpers.API_KEY)
    recorder, waits = _recorded_anthropic(*replies)

    result = _invoke("anthropic")

    assert result.exit_code == 1
    message = _last_message(result)
    assert message.startswith(f"Error: {error}: Anthropic ")
    assert f"model {CLAUDE_CHEAP}" in message
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert anthropic_helpers.API_KEY not in result.stderr
    assert "Reply with OK." not in result.stderr
    assert len(recorder.requests) == requests
    assert waits == waits_expected
    assert [client.is_closed for client in recorder.clients] == [True]


def test_recorded_anthropic_auth_failure_names_the_setting(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, anthropic_registry: None
) -> None:
    monkeypatch.setenv("INVIO_ANTHROPIC_API_KEY", anthropic_helpers.API_KEY)
    _recorded_anthropic("error_401")

    result = _invoke("anthropic")

    assert _last_message(result).startswith("Error: LLMAuthError: ")
    assert "INVIO_ANTHROPIC_API_KEY" in _last_message(result)


def test_recorded_anthropic_timeout_is_capped(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, anthropic_registry: None
) -> None:
    # SC-005: the command's cap applies to the Anthropic provider like to any other.
    monkeypatch.setenv("INVIO_ANTHROPIC_API_KEY", anthropic_helpers.API_KEY)
    monkeypatch.setenv("INVIO_LLM_TIMEOUT_SECONDS", "600")
    built: list[AnthropicProvider] = []
    _recorded_anthropic("message_ok", built=built)

    result = _invoke("anthropic")

    assert result.exit_code == 0, result.stderr
    assert [provider.timeout_seconds for provider in built] == [20.0]


def test_recorded_anthropic_hanging_request_is_bounded_by_the_timeout(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, anthropic_registry: None
) -> None:
    monkeypatch.setenv("INVIO_ANTHROPIC_API_KEY", anthropic_helpers.API_KEY)
    monkeypatch.setenv("INVIO_LLM_TIMEOUT_SECONDS", "0.2")
    recorder, waits = _recorded_anthropic(anthropic_helpers.HANG)

    result = _invoke("anthropic")

    assert result.exit_code == 1
    assert _last_message(result).startswith("Error: LLMUnavailableError: ")
    assert "0.2 s" in _last_message(result)
    assert len(recorder.requests) == 1
    assert waits == []


# --- Google (issue #32, US3) ----------------------------------------------------------------

GEMINI_CHEAP = "gemini-3.5-flash-lite"
GEMINI_SMART = "gemini-3.8-flash"


@pytest.fixture
def google_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The shipped registry, which lists the two Google models."""
    registry = llm_registry.load_registry()
    monkeypatch.setattr(llm_registry, "default_registry", lambda: registry)


def _recorded_google(
    *replies: google_helpers.Reply, built: list[GoogleProvider] | None = None
) -> tuple[google_helpers.Recorder, list[float]]:
    """Register a real ``GoogleProvider`` on recorded HTTP replies as ``google``.

    Needs ``patched_providers`` (isolated registry). The key comes from the settings as in
    production; only the HTTP transport, the base URL and the retry sleep are replaced.
    """
    recorder = google_helpers.Recorder(replies)
    options, waits = google_helpers.recording_options(recorder)

    class _Recorded(GoogleProvider):
        @classmethod
        def from_settings(cls, settings: Settings, *, registry: Any = None) -> Self:
            provider = cls(
                require_api_key(settings, "google"),
                timeout_seconds=settings.llm_timeout_seconds,
                registry=registry,
                **options,
            )
            if built is not None:
                built.append(provider)
            return provider

    factory.register_provider("google")(_Recorded)
    return recorder, waits


def test_recorded_google_success(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, google_registry: None
) -> None:
    monkeypatch.setenv("INVIO_GOOGLE_API_KEY", google_helpers.API_KEY)
    recorder, _ = _recorded_google("text_ok")

    result = _invoke("google")

    assert result.exit_code == 0, result.stderr
    assert re.fullmatch(
        rf"ok provider=google model={GEMINI_CHEAP} input_tokens=12 output_tokens=3 "
        r"duration_ms=\d+(\.\d+)?\n",
        result.stdout,
    )
    (request,) = recorder.requests
    assert [client.is_closed for client in recorder.clients] == [True]
    assert request.path == f"/v1beta/models/{GEMINI_CHEAP}:generateContent"
    assert request.headers["x-goog-api-key"] == google_helpers.API_KEY
    config = request.body["generationConfig"]
    assert config["maxOutputTokens"] == 5 + 1024
    assert "temperature" not in config
    assert "Hello" not in result.stdout + result.stderr
    assert google_helpers.API_KEY not in result.stdout + result.stderr


def test_recorded_google_model_option_selects_that_model(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, google_registry: None
) -> None:
    monkeypatch.setenv("INVIO_GOOGLE_API_KEY", google_helpers.API_KEY)
    recorder, _ = _recorded_google("text_ok")

    result = _invoke("google", "--model", GEMINI_SMART)

    assert result.exit_code == 0, result.stderr
    assert f"model={GEMINI_SMART}" in result.stdout
    (request,) = recorder.requests
    assert request.path == f"/v1beta/models/{GEMINI_SMART}:generateContent"
    assert request.body["generationConfig"]["maxOutputTokens"] == 5 + 4096


def test_recorded_google_unknown_model_exits_2(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, google_registry: None
) -> None:
    monkeypatch.setenv("INVIO_GOOGLE_API_KEY", google_helpers.API_KEY)
    recorder, _ = _recorded_google("text_ok")

    result = _invoke("google", "--model", "gpt-x")

    assert result.exit_code == 2
    assert _last_message(result) == (
        "Configuration error: model 'gpt-x' is not registered for LLM provider 'google'"
    )
    assert recorder.requests == []


def test_recorded_google_without_key_makes_no_request(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, google_registry: None
) -> None:
    recorder, _ = _recorded_google("text_ok")

    result = _invoke("google")

    assert result.exit_code == 2
    assert _last_message(result) == (
        "Configuration error: LLM provider 'google' needs an API key: set INVIO_GOOGLE_API_KEY"
    )
    assert recorder.requests == []
    assert recorder.clients_created == 0


@pytest.mark.parametrize(
    ("replies", "error", "requests"),
    [
        (("error_401",), "LLMAuthError", 1),
        (("error_503",) * 4, "LLMUnavailableError", 4),
        (("blocked_prompt",), "LLMInvalidOutputError", 1),
    ],
    ids=["401", "503", "blocked"],
)
def test_recorded_google_failures_exit_1(
    patched_providers: Register,
    monkeypatch: pytest.MonkeyPatch,
    google_registry: None,
    replies: tuple[google_helpers.Reply, ...],
    error: str,
    requests: int,
) -> None:
    monkeypatch.setenv("INVIO_GOOGLE_API_KEY", google_helpers.API_KEY)
    recorder, _ = _recorded_google(*replies)

    result = _invoke("google")

    assert result.exit_code == 1
    assert _last_message(result).startswith(f"Error: {error}: ")
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert google_helpers.API_KEY not in result.stderr
    assert "Reply with OK." not in result.stderr
    assert len(recorder.requests) == requests
    assert [client.is_closed for client in recorder.clients] == [True]


# --- Ollama (issue #33) --------------------------------------------------------------------

OLLAMA_FAST = "llama3.2:3b"


def _recorded_ollama(*replies: ollama_helpers.Reply) -> ollama_helpers.Recorder:
    """Register a real ``OllamaProvider`` on recorded HTTP replies as ``ollama``.

    Needs ``patched_providers`` (isolated registry). The base URL comes from the settings as in
    production; only the HTTP transport and the retry sleep are replaced.
    """
    recorder = ollama_helpers.Recorder(replies)
    options, _ = ollama_helpers.recording_options(recorder)

    class _Recorded(OllamaProvider):
        @classmethod
        def from_settings(cls, settings: Settings, *, registry: Any = None) -> Self:
            return cls(
                settings.ollama_base_url, timeout_seconds=settings.llm_timeout_seconds, **options
            )

    factory.register_provider("ollama")(_Recorded)
    return recorder


def test_recorded_ollama_success_needs_no_key(
    patched_providers: Register, monkeypatch: pytest.MonkeyPatch, google_registry: None
) -> None:
    monkeypatch.setenv("INVIO_OLLAMA_BASE_URL", ollama_helpers.BASE_URL)
    recorder = _recorded_ollama("chat_ok")

    result = _invoke("ollama")

    assert result.exit_code == 0, result.stderr
    assert re.fullmatch(
        rf"ok provider=ollama model={re.escape(OLLAMA_FAST)} input_tokens=12 output_tokens=3 "
        r"duration_ms=\d+(\.\d+)?\n",
        result.stdout,
    )
    (request,) = recorder.requests
    assert (request.host, request.path) == ("ollama.test", "/api/chat")
    assert request.body["options"] == {"temperature": 0, "num_predict": 5}
    assert [client.is_closed for client in recorder.clients] == [True]


def test_recorded_ollama_unreachable_server_exits_1_without_retry(
    patched_providers: Register, google_registry: None
) -> None:
    recorder = _recorded_ollama(httpx2.ConnectError("[Errno 61] Connection refused"))

    result = _invoke("ollama")

    assert result.exit_code == 1
    message = _last_message(result)
    assert message.startswith("Error: LLMUnavailableError: Ollama server unreachable")
    assert "INVIO_OLLAMA_BASE_URL" in message
    assert "Reply with OK." not in result.stderr
    assert len(recorder.requests) == 1


def test_recorded_ollama_model_not_pulled_exits_1(
    patched_providers: Register, google_registry: None
) -> None:
    recorder = _recorded_ollama("error_404_model")

    result = _invoke("ollama")

    assert result.exit_code == 1
    assert _last_message(result).startswith("Error: LLMInvalidRequestError: ")
    assert "try pulling it first" in _last_message(result)
    assert len(recorder.requests) == 1
