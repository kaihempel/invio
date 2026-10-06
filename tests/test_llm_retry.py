"""``RetryingProvider``: transient provider errors are retried at the request boundary."""

import pytest

from invio.llm.base import (
    LLMAuthError,
    LLMError,
    LLMInvalidOutputError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
)
from invio.llm.fake import FakeProvider, FakeReply, FakeStep
from invio.llm.retry import RetryingProvider
from invio.retry import RetrySettings
from tests.async_helpers import RecordingSleep
from tests.llm_helpers import VALID_SCORE_JSON, Score

POLICY = RetrySettings(jitter=False)


def _provider(
    script: list[FakeStep], sleep: RecordingSleep, policy: RetrySettings = POLICY
) -> tuple[RetryingProvider, FakeProvider]:
    inner = FakeProvider(script)
    return RetryingProvider(inner, policy=policy, sleep=sleep), inner


async def _complete(provider: RetryingProvider) -> tuple[str, Usage]:
    return await provider.complete("sys", "user", model="m", temperature=0.0, max_tokens=10)


async def _structured(provider: RetryingProvider) -> tuple[Score, Usage]:
    return await provider.complete_structured("sys", "user", Score, model="m", temperature=0.0)


@pytest.mark.parametrize(
    "error",
    [LLMRateLimitError("slow down"), LLMUnavailableError("down")],
    ids=["rate-limit", "unavailable"],
)
async def test_complete_is_retried_on_transient_errors(error: LLMError) -> None:
    sleep = RecordingSleep()
    provider, inner = _provider([error, error, FakeReply("ok", Usage(3, 4))], sleep)

    text, usage = await _complete(provider)

    assert (text, usage) == ("ok", Usage(3, 4))
    assert len(inner.requests) == 3
    assert sleep.calls == [1.0, 2.0]


@pytest.mark.parametrize(
    "error",
    [LLMRateLimitError("slow down"), LLMUnavailableError("down")],
    ids=["rate-limit", "unavailable"],
)
async def test_complete_structured_is_retried_on_transient_errors(error: LLMError) -> None:
    sleep = RecordingSleep()
    provider, inner = _provider([error, FakeReply(VALID_SCORE_JSON, Usage(5, 6))], sleep)

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="relevant")
    assert usage == Usage(5, 6)
    assert len(inner.requests) == 2
    assert sleep.calls == [1.0]


async def test_retry_after_of_a_rate_limit_sets_the_wait() -> None:
    sleep = RecordingSleep()
    error = LLMRateLimitError("slow", retry_after=4)
    provider, _ = _provider([error, FakeReply("ok")], sleep)

    await _complete(provider)

    assert sleep.calls == [4.0]


async def test_retry_after_beyond_max_interval_is_raised_after_one_attempt() -> None:
    sleep = RecordingSleep()
    error = LLMRateLimitError("slow", retry_after=120)
    provider, inner = _provider([error, FakeReply("ok")], sleep)

    with pytest.raises(LLMRateLimitError):
        await _complete(provider)

    assert len(inner.requests) == 1
    assert sleep.calls == []


async def test_attempts_are_bounded_and_the_last_error_is_raised() -> None:
    sleep = RecordingSleep()
    errors = [LLMUnavailableError(str(n)) for n in range(3)]
    provider, inner = _provider(list(errors), sleep, RetrySettings(max_attempts=3, jitter=False))

    with pytest.raises(LLMUnavailableError) as raised:
        await _complete(provider)

    assert raised.value is errors[-1]
    assert len(inner.requests) == 3


@pytest.mark.parametrize(
    "error",
    [
        LLMAuthError("no key"),
        LLMInvalidRequestError("bad request", status=400),
    ],
    ids=["auth", "invalid-request"],
)
async def test_permanent_errors_are_attempted_once(error: LLMError) -> None:
    sleep = RecordingSleep()
    provider, inner = _provider([error, FakeReply("never")], sleep)

    with pytest.raises(type(error)):
        await _complete(provider)

    assert len(inner.requests) == 1
    assert sleep.calls == []


async def test_an_invalid_structured_answer_is_not_retried() -> None:
    sleep = RecordingSleep()
    provider, inner = _provider([FakeReply("not json"), FakeReply("still not json")], sleep)

    with pytest.raises(LLMInvalidOutputError):
        await _structured(provider)

    assert len(inner.requests) == 2  # the one repair request of the inner provider, no retry
    assert sleep.calls == []


async def test_aclose_is_forwarded_to_the_inner_provider() -> None:
    closed: list[bool] = []

    class Closing(FakeProvider):
        async def aclose(self) -> None:
            closed.append(True)

    provider = RetryingProvider(Closing([]), policy=POLICY, sleep=RecordingSleep())

    await provider.aclose()

    assert closed == [True]
