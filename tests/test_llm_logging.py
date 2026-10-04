"""Tests for the call logging and input validation done by the provider wrapper."""

import logging
from collections.abc import Callable
from typing import Any

import pytest

from invio.llm.base import (
    LLMAuthError,
    LLMError,
    LLMInvalidOutputError,
    LLMProvider,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
)
from invio.llm.factory import get_provider
from invio.llm.fake import FakeDelay, FakeProvider, FakeReply, FakeStep
from invio.llm.registry import load_registry
from tests.llm_helpers import FIXTURE_REGISTRY_DIR, Score, make_settings

PRICED = "fakeco-priced"
PROMPT = "PROMPT_TOKEN_42"
ANSWER = "ANSWER_TOKEN_42"
KEY = "sk-test-KEY_TOKEN_42"


def _provider(register: Callable[[str, Any], None], fake: FakeProvider) -> LLMProvider:
    register("fakeco", fake)
    return get_provider(
        "fakeco",
        make_settings(mistral_api_key=KEY),
        registry=load_registry([FIXTURE_REGISTRY_DIR]),
    )


def _records(caplog: pytest.LogCaptureFixture, message: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == message]


async def test_successful_call_logs_one_info_line(
    patched_providers: Callable[[str, Any], None], caplog: pytest.LogCaptureFixture
) -> None:
    provider = _provider(patched_providers, FakeProvider([FakeReply(ANSWER, Usage(1000, 500))]))

    with caplog.at_level(logging.INFO, logger="invio.llm"):
        await provider.complete(PROMPT, PROMPT, model=PRICED, temperature=0, max_tokens=10)

    (record,) = _records(caplog, "llm.call")
    fields = record.__dict__
    assert record.levelno == logging.INFO
    assert fields["provider"] == "fakeco"
    assert fields["model"] == PRICED
    assert fields["input_tokens"] == 1000
    assert fields["output_tokens"] == 500
    assert fields["cost_usd"] == "0.007500"
    assert fields["duration_ms"] >= 0
    assert fields["repaired"] is False


async def test_repaired_structured_call_logs_repair_and_call(
    patched_providers: Callable[[str, Any], None], caplog: pytest.LogCaptureFixture
) -> None:
    provider = _provider(
        patched_providers,
        FakeProvider(
            [
                FakeReply("garbage", Usage(10, 1)),
                FakeReply('{"score": 1, "reason": "r"}', Usage(5, 2)),
            ]
        ),
    )

    with caplog.at_level(logging.INFO, logger="invio.llm"):
        await provider.complete_structured(PROMPT, PROMPT, Score, model=PRICED, temperature=0)

    (call,) = _records(caplog, "llm.call")
    assert call.__dict__["repaired"] is True
    assert call.__dict__["input_tokens"] == 15
    assert call.__dict__["output_tokens"] == 3
    (repair,) = _records(caplog, "llm.repair")
    assert repair.levelno == logging.WARNING


async def test_unknown_model_logs_null_cost(
    patched_providers: Callable[[str, Any], None], caplog: pytest.LogCaptureFixture
) -> None:
    provider = _provider(patched_providers, FakeProvider([FakeReply(ANSWER)]))

    with caplog.at_level(logging.INFO, logger="invio.llm"):
        await provider.complete(PROMPT, PROMPT, model="not-registered", temperature=0, max_tokens=1)

    (record,) = _records(caplog, "llm.call")
    assert record.__dict__["cost_usd"] is None


async def test_llm_error_logs_one_warning_and_reraises(
    patched_providers: Callable[[str, Any], None], caplog: pytest.LogCaptureFixture
) -> None:
    provider = _provider(patched_providers, FakeProvider([LLMRateLimitError("slow")]))

    with (
        caplog.at_level(logging.INFO, logger="invio.llm"),
        pytest.raises(LLMRateLimitError, match="slow"),
    ):
        await provider.complete(PROMPT, PROMPT, model=PRICED, temperature=0, max_tokens=1)

    assert not _records(caplog, "llm.call")
    (record,) = _records(caplog, "llm.error")
    assert record.levelno == logging.WARNING
    fields = record.__dict__
    assert fields["error"] == "LLMRateLimitError"
    assert fields["provider"] == "fakeco"
    assert fields["model"] == PRICED
    assert fields["duration_ms"] >= 0


@pytest.mark.parametrize(
    "step",
    [LLMAuthError("auth"), LLMUnavailableError("down"), FakeDelay(1, FakeReply(ANSWER))],
    ids=["auth", "unavailable", "timeout"],
)
async def test_each_typed_error_logs_its_type(
    patched_providers: Callable[[str, Any], None],
    caplog: pytest.LogCaptureFixture,
    step: FakeStep,
) -> None:
    provider = _provider(patched_providers, FakeProvider([step], timeout_seconds=0.01))

    with caplog.at_level(logging.INFO, logger="invio.llm"), pytest.raises(LLMError) as info:
        await provider.complete(PROMPT, PROMPT, model=PRICED, temperature=0, max_tokens=1)

    (record,) = _records(caplog, "llm.error")
    assert record.__dict__["error"] == type(info.value).__name__
    assert not _records(caplog, "llm.call")


async def test_structured_error_is_logged_too(
    patched_providers: Callable[[str, Any], None], caplog: pytest.LogCaptureFixture
) -> None:
    provider = _provider(patched_providers, FakeProvider([LLMRateLimitError("slow")]))

    with (
        caplog.at_level(logging.INFO, logger="invio.llm"),
        pytest.raises(LLMRateLimitError),
    ):
        await provider.complete_structured(PROMPT, PROMPT, Score, model=PRICED, temperature=0)

    assert len(_records(caplog, "llm.error")) == 1


@pytest.mark.parametrize(
    ("temperature", "max_tokens"), [(-0.1, 10), (2.1, 10), (0.5, 0)], ids=["low", "high", "tokens"]
)
async def test_invalid_parameters_fail_before_any_request(
    patched_providers: Callable[[str, Any], None], temperature: float, max_tokens: int
) -> None:
    fake = FakeProvider([FakeReply(ANSWER)])
    provider = _provider(patched_providers, fake)

    with pytest.raises(ValueError, match=r"temperature|max_tokens"):
        await provider.complete(
            PROMPT, PROMPT, model=PRICED, temperature=temperature, max_tokens=max_tokens
        )

    assert fake.requests == []


async def test_structured_temperature_is_validated(
    patched_providers: Callable[[str, Any], None],
) -> None:
    fake = FakeProvider([FakeReply(ANSWER)])
    provider = _provider(patched_providers, fake)

    with pytest.raises(ValueError, match="temperature"):
        await provider.complete_structured(PROMPT, PROMPT, Score, model=PRICED, temperature=3)

    assert fake.requests == []


async def test_boundary_temperatures_are_accepted(
    patched_providers: Callable[[str, Any], None],
) -> None:
    provider = _provider(patched_providers, FakeProvider([FakeReply("a"), FakeReply("b")]))

    await provider.complete("s", "u", model=PRICED, temperature=0, max_tokens=1)
    await provider.complete("s", "u", model=PRICED, temperature=2, max_tokens=1)


async def test_logs_contain_no_prompt_answer_or_key(
    patched_providers: Callable[[str, Any], None], caplog: pytest.LogCaptureFixture
) -> None:
    provider = _provider(
        patched_providers,
        FakeProvider(
            [
                FakeReply(ANSWER),
                FakeReply(f'{{"score": 9, "reason": "{ANSWER}"}}'),
                FakeReply('{"score": 1, "reason": "ok"}'),
                LLMRateLimitError("slow"),
            ]
        ),
    )

    with caplog.at_level(logging.DEBUG, logger="invio.llm"):
        await provider.complete(PROMPT, PROMPT, model=PRICED, temperature=0, max_tokens=1)
        await provider.complete_structured(PROMPT, PROMPT, Score, model=PRICED, temperature=0)
        with pytest.raises(LLMRateLimitError):
            await provider.complete(PROMPT, PROMPT, model=PRICED, temperature=0, max_tokens=1)

    assert caplog.records
    for record in caplog.records:
        rendered = record.getMessage() + repr(record.__dict__)
        assert PROMPT not in rendered
        assert ANSWER not in rendered
        assert KEY not in rendered


async def test_invalid_output_error_log_carries_usage_and_cost(
    patched_providers: Callable[[str, Any], None], caplog: pytest.LogCaptureFixture
) -> None:
    provider = _provider(
        patched_providers,
        FakeProvider([FakeReply("nope", Usage(1000, 100)), FakeReply("nope", Usage(1000, 400))]),
    )

    with (
        caplog.at_level(logging.INFO, logger="invio.llm"),
        pytest.raises(LLMInvalidOutputError),
    ):
        await provider.complete_structured(PROMPT, PROMPT, Score, model=PRICED, temperature=0)

    (record,) = _records(caplog, "llm.error")
    fields = record.__dict__
    assert fields["error"] == "LLMInvalidOutputError"
    assert fields["input_tokens"] == 2000
    assert fields["output_tokens"] == 500
    assert fields["cost_usd"] == "0.010000"
    assert fields["repaired"] is True


async def test_plain_error_log_has_no_usage_fields(
    patched_providers: Callable[[str, Any], None], caplog: pytest.LogCaptureFixture
) -> None:
    provider = _provider(patched_providers, FakeProvider([LLMRateLimitError("slow")]))

    with caplog.at_level(logging.INFO, logger="invio.llm"), pytest.raises(LLMRateLimitError):
        await provider.complete(PROMPT, PROMPT, model=PRICED, temperature=0, max_tokens=1)

    (record,) = _records(caplog, "llm.error")
    assert "input_tokens" not in record.__dict__


async def test_aclose_is_forwarded_to_the_provider(patched_providers: Any) -> None:
    closed: list[bool] = []

    class _Closable(FakeProvider):
        async def aclose(self) -> None:
            closed.append(True)

    provider = _provider(patched_providers, _Closable([]))

    await provider.aclose()

    assert closed == [True]
