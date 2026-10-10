"""``FallbackProvider``: an outage or rate limit of the primary hands the call to the fallback."""

import logging

import pytest

from invio.llm.base import (
    LLMAuthError,
    LLMError,
    LLMInvalidOutputError,
    LLMInvalidRequestError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
)
from invio.llm.fake import FakeProvider, FakeReply, FakeStep
from invio.llm.fallback import FallbackProvider
from invio.llm.retry import RetryingProvider
from invio.retry import RetrySettings
from tests.async_helpers import RecordingSleep
from tests.llm_helpers import VALID_SCORE_JSON, Score

SECRET = "secret prompt text"
FALLBACK_ERRORS = [
    LLMUnavailableError(f"down: {SECRET}"),
    LLMRateLimitError(f"slow down: {SECRET}"),
    LLMQuotaError(f"no credit: {SECRET}"),
]
PERMANENT_ERRORS = [
    LLMAuthError("bad key"),
    LLMInvalidRequestError("rejected", status=400),
]


def _fallback(
    primary: list[FakeStep], fallback: list[FakeStep]
) -> tuple[FallbackProvider, FakeProvider, FakeProvider]:
    first, second = FakeProvider(primary, name="primary"), FakeProvider(fallback, name="backup")
    provider = FallbackProvider(
        first, second, primary_name="primary", fallback_name="backup", fallback_model="b-model"
    )
    return provider, first, second


async def _complete(provider: FallbackProvider) -> tuple[str, Usage]:
    return await provider.complete(SECRET, SECRET, model="p-model", temperature=0.0, max_tokens=9)


async def _structured(provider: FallbackProvider) -> tuple[Score, Usage]:
    return await provider.complete_structured(
        SECRET, SECRET, Score, model="p-model", temperature=0.0
    )


async def test_a_primary_answer_is_stamped_with_the_primary() -> None:
    provider, _, backup = _fallback([FakeReply("ok", Usage(3, 4))], [])

    assert await _complete(provider) == ("ok", Usage(3, 4, 1, "primary", "p-model"))
    assert backup.requests == []


@pytest.mark.parametrize("error", FALLBACK_ERRORS, ids=lambda e: type(e).__name__)
async def test_complete_falls_back_with_the_fallback_model(error: LLMError) -> None:
    provider, primary, backup = _fallback([error], [FakeReply("ok", Usage(3, 4))])

    text, usage = await _complete(provider)

    assert (text, usage) == ("ok", Usage(3, 4, 1, "backup", "b-model"))
    assert [r.model for r in primary.requests] == ["p-model"]
    assert [(r.model, r.user, r.max_tokens) for r in backup.requests] == [("b-model", SECRET, 9)]


@pytest.mark.parametrize("error", FALLBACK_ERRORS, ids=lambda e: type(e).__name__)
async def test_complete_structured_falls_back(error: LLMError) -> None:
    provider, _, backup = _fallback([error], [FakeReply(VALID_SCORE_JSON, Usage(5, 6))])

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="relevant")
    assert usage == Usage(5, 6, 1, "backup", "b-model")
    assert [r.model for r in backup.requests] == ["b-model"]


@pytest.mark.parametrize("error", PERMANENT_ERRORS, ids=lambda e: type(e).__name__)
async def test_permanent_errors_do_not_fall_back(error: LLMError) -> None:
    provider, _, backup = _fallback([error, error], [FakeReply("ok"), FakeReply("ok")])

    with pytest.raises(type(error)) as plain:
        await _complete(provider)
    with pytest.raises(type(error)) as structured:
        await _structured(provider)

    assert plain.value is error and structured.value is error
    assert backup.requests == []


async def test_an_invalid_answer_of_the_primary_does_not_fall_back() -> None:
    provider, _, backup = _fallback([FakeReply("nope"), FakeReply("nope")], [FakeReply("ok")])

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.usage == Usage(20, 10, 2, "primary", "p-model")
    assert backup.requests == []


async def test_non_llm_errors_do_not_fall_back() -> None:
    class Broken(FakeProvider):
        async def complete(self, *args: object, **kwargs: object) -> tuple[str, Usage]:
            raise RuntimeError("bug")

    backup = FakeProvider([FakeReply("ok")])
    provider = FallbackProvider(
        Broken([]), backup, primary_name="p", fallback_name="b", fallback_model="b-model"
    )

    with pytest.raises(RuntimeError, match="bug"):
        await _complete(provider)
    assert backup.requests == []


async def test_an_invalid_answer_of_the_fallback_carries_the_fallback_usage() -> None:
    provider, _, _ = _fallback(
        [LLMUnavailableError("down")], [FakeReply("nope", Usage(1, 2)), FakeReply("x", Usage(3, 4))]
    )

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.usage == Usage(4, 6, 2, "backup", "b-model")


async def test_errors_of_the_fallback_propagate() -> None:
    second = LLMAuthError("backup key")
    provider, _, _ = _fallback([LLMUnavailableError("down")], [second])

    with pytest.raises(LLMAuthError) as info:
        await _complete(provider)

    assert info.value is second


async def test_a_fallback_logs_one_warning_without_message_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _, _ = _fallback([LLMQuotaError(f"no credit: {SECRET}")], [FakeReply("ok")])

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.fallback"]
    assert record.levelno == logging.WARNING
    fields = {
        key: record.__dict__[key]
        for key in ("provider", "model", "fallback_provider", "fallback_model", "error")
    }
    assert fields == {
        "provider": "primary",
        "model": "p-model",
        "fallback_provider": "backup",
        "fallback_model": "b-model",
        "error": "LLMQuotaError",
    }
    assert SECRET not in str([r.__dict__ for r in caplog.records])


async def test_no_warning_without_fallback(caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = _fallback([FakeReply("ok")], [])

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    assert "llm.fallback" not in [r.getMessage() for r in caplog.records]


async def test_the_primary_is_retried_before_falling_back() -> None:
    sleep = RecordingSleep()
    policy = RetrySettings(jitter=False, max_attempts=3)
    down = LLMUnavailableError("down")
    primary, backup = FakeProvider([down, down, down]), FakeProvider([FakeReply("ok")])
    provider = FallbackProvider(
        RetryingProvider(primary, policy=policy, sleep=sleep),
        RetryingProvider(backup, policy=policy, sleep=sleep),
        primary_name="primary",
        fallback_name="backup",
        fallback_model="b-model",
    )

    text, usage = await _complete(provider)

    assert (text, usage.provider) == ("ok", "backup")
    assert len(primary.requests) == 3
    assert len(backup.requests) == 1
    assert sleep.calls == [1.0, 2.0]


async def test_aclose_closes_both_providers_even_when_the_primary_fails() -> None:
    closed: list[str] = []

    class Failing(FakeProvider):
        async def aclose(self) -> None:
            closed.append("primary")
            raise OSError("socket")

    class Fine(FakeProvider):
        async def aclose(self) -> None:
            closed.append("fallback")

    provider = FallbackProvider(
        Failing([]), Fine([]), primary_name="p", fallback_name="b", fallback_model="m"
    )

    with pytest.raises(OSError, match="socket"):
        await provider.aclose()

    assert closed == ["primary", "fallback"]
