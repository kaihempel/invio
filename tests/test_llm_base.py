"""Tests for the LLM base module: Usage, errors, timeouts and credentials."""

import asyncio

import pytest

from invio.llm.base import (
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMInvalidOutputError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMUnavailableError,
    ModelRegistryError,
    Usage,
    require_api_key,
    with_timeout,
)
from tests.llm_helpers import make_settings

SECRET = "sk-test-SECRET123"


def test_usage_defaults_to_one_request() -> None:
    assert Usage(1, 2).requests == 1


def test_usage_addition_sums_fields() -> None:
    assert Usage(1, 2) + Usage(3, 4) == Usage(4, 6, 2)


def test_usage_addition_keeps_provider_and_model_of_the_left_operand() -> None:
    assert Usage(1, 2).provider is None and Usage(1, 2).model is None
    assert Usage(1, 2, 1, "p", "m") + Usage(3, 4, 1, "q", "n") == Usage(4, 6, 2, "p", "m")
    assert Usage(1, 2) + Usage(3, 4, 1, "q", "n") == Usage(4, 6, 2)


@pytest.mark.parametrize(
    "args", [(-1, 0, 1), (0, -1, 1), (0, 0, 0)], ids=["input", "output", "requests"]
)
def test_usage_rejects_invalid_values(args: tuple[int, int, int]) -> None:
    with pytest.raises(ValueError, match=">="):
        Usage(*args)


def test_usage_accepts_zero_tokens() -> None:
    assert Usage(0, 0).input_tokens == 0


@pytest.mark.parametrize(
    "cls", [LLMRateLimitError, LLMAuthError, LLMUnavailableError], ids=lambda c: c.__name__
)
def test_provider_errors_are_llm_errors(cls: type[LLMError]) -> None:
    err = cls("boom", provider="p", model="m")

    assert isinstance(err, LLMError)
    assert (err.provider, err.model) == ("p", "m")
    assert str(err) == "boom"


def test_rate_limit_error_carries_retry_after() -> None:
    err = LLMRateLimitError("m", provider="p", model="x", retry_after=2.5)

    assert err.retry_after == 2.5
    assert (err.provider, err.model) == ("p", "x")


def test_rate_limit_error_retry_after_defaults_to_none() -> None:
    assert LLMRateLimitError("m").retry_after is None
    assert LLMRateLimitError("m", provider="p", model="x").retry_after is None


@pytest.mark.parametrize("bad", [-1.0, float("nan"), float("inf")])
def test_rate_limit_error_rejects_invalid_retry_after(bad: float) -> None:
    with pytest.raises(ValueError, match="retry_after"):
        LLMRateLimitError("m", retry_after=bad)


def test_invalid_request_error_is_an_llm_error_but_not_unavailable() -> None:
    err = LLMInvalidRequestError("m", provider="p", model="x", status=422)

    assert isinstance(err, LLMError)
    assert not isinstance(err, LLMUnavailableError)
    assert err.status == 422
    assert (err.provider, err.model) == ("p", "x")


def test_invalid_request_error_status_defaults_to_none() -> None:
    assert LLMInvalidRequestError("m").status is None


def test_llm_error_attributes_default_to_none() -> None:
    err = LLMError("boom")

    assert err.provider is None
    assert err.model is None


def test_invalid_output_error_carries_errors_and_usage() -> None:
    err = LLMInvalidOutputError("bad", errors="score: nope", usage=Usage(1, 2, 2), model="m")

    assert isinstance(err, LLMError)
    assert err.errors == "score: nope"
    assert err.usage == Usage(1, 2, 2)
    assert err.model == "m"


def test_config_errors_are_value_errors() -> None:
    assert issubclass(LLMConfigError, ValueError)
    assert issubclass(ModelRegistryError, LLMConfigError)
    assert not issubclass(LLMConfigError, LLMError)


async def test_with_timeout_returns_fast_result() -> None:
    async def fast() -> int:
        return 7

    assert await with_timeout(fast(), seconds=1, provider="p", model="m") == 7


async def test_with_timeout_converts_timeout_to_unavailable() -> None:
    with pytest.raises(LLMUnavailableError) as info:
        await with_timeout(asyncio.sleep(1), seconds=0.01, provider="prov", model="mod")

    assert "prov" in str(info.value)
    assert "mod" in str(info.value)
    assert "0.01" in str(info.value)
    assert (info.value.provider, info.value.model) == ("prov", "mod")


async def test_with_timeout_propagates_other_exceptions() -> None:
    async def failing() -> None:
        raise LLMRateLimitError("slow down")

    with pytest.raises(LLMRateLimitError, match="slow down"):
        await with_timeout(failing(), seconds=1, provider="p", model="m")


def test_require_api_key_missing_names_env_var() -> None:
    with pytest.raises(LLMAuthError) as info:
        require_api_key(make_settings(), "mistral")

    assert "mistral" in str(info.value)
    assert "INVIO_MISTRAL_API_KEY" in str(info.value)
    assert info.value.provider == "mistral"


@pytest.mark.parametrize("blank", ["", "   "])
def test_require_api_key_blank_is_missing(blank: str) -> None:
    with pytest.raises(LLMAuthError, match="INVIO_MISTRAL_API_KEY"):
        require_api_key(make_settings(mistral_api_key=blank), "mistral")


def test_require_api_key_returns_key_without_leaking_it() -> None:
    settings = make_settings(mistral_api_key=SECRET)

    assert require_api_key(settings, "mistral") == SECRET
    assert SECRET not in repr(settings)


def test_require_api_key_unknown_provider_is_config_error() -> None:
    with pytest.raises(LLMConfigError, match="no API key setting"):
        require_api_key(make_settings(), "ollama")


def test_error_messages_never_contain_the_key() -> None:
    settings = make_settings(mistral_api_key=SECRET)
    with pytest.raises(LLMAuthError) as info:
        require_api_key(make_settings(), "mistral")

    assert require_api_key(settings, "mistral") == SECRET
    assert SECRET not in str(info.value)


async def test_layer_errors_never_expose_configured_keys() -> None:
    """US3 AS4 / FR-004: str, repr and attributes of produced errors hold no credential."""
    settings = make_settings(openai_api_key=SECRET, anthropic_api_key=SECRET, google_api_key=SECRET)
    errors: list[LLMError] = []
    with pytest.raises(LLMAuthError) as auth:
        require_api_key(settings, "mistral")
    errors.append(auth.value)
    with pytest.raises(LLMUnavailableError) as unavailable:
        await with_timeout(asyncio.sleep(1), seconds=0.01, provider="openai", model="m")
    errors.append(unavailable.value)

    for error in errors:
        rendered = str(error) + repr(error) + repr(vars(error)) + repr(error.args)
        assert SECRET not in rendered


async def test_with_timeout_propagates_inner_timeout_error_unchanged() -> None:
    async def inner() -> None:
        raise TimeoutError("inner deadline")

    with pytest.raises(TimeoutError, match="inner deadline") as info:
        await with_timeout(inner(), seconds=5, provider="p", model="m")

    assert not isinstance(info.value, LLMUnavailableError)
