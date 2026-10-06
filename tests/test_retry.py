"""Tests for ``invio.retry`` (backoff, jitter bound, ``retry_after``) and the retry predicates."""

import logging

import pytest

from invio.llm.base import (
    LLMAuthError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from invio.llm.retry import is_transient_llm, llm_retry_after
from invio.retry import RetrySettings, retrying
from invio.sources.errors import (
    BlockedError,
    BlockReason,
    FetchError,
    RenderUnavailableError,
    TooLargeError,
    is_transient_fetch,
)
from tests.async_helpers import RecordingSleep

URL = "https://example.com/feed"


class Flaky:
    """Callable that raises the queued errors in order, then returns ``"ok"``."""

    def __init__(self, *errors: BaseException) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> str:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return "ok"


def _transient(err: BaseException) -> bool:
    return isinstance(err, LLMUnavailableError | LLMRateLimitError)


async def test_two_failures_then_success_sleeps_with_exponential_backoff() -> None:
    sleep = RecordingSleep()
    flaky = Flaky(LLMUnavailableError("x"), LLMUnavailableError("x"))

    result = await retrying(
        flaky,
        policy=RetrySettings(jitter=False),
        retry_on=_transient,
        what="llm",
        sleep=sleep,
    )

    assert result == "ok"
    assert flaky.calls == 3
    assert sleep.calls == [1.0, 2.0]


async def test_exhausted_attempts_reraise_the_last_error_unchanged() -> None:
    sleep = RecordingSleep()
    last = LLMUnavailableError("third")
    flaky = Flaky(LLMUnavailableError("first"), LLMUnavailableError("second"), last)

    with pytest.raises(LLMUnavailableError) as raised:
        await retrying(
            flaky,
            policy=RetrySettings(max_attempts=3, jitter=False),
            retry_on=_transient,
            what="llm",
            sleep=sleep,
        )

    assert raised.value is last
    assert flaky.calls == 3
    assert sleep.calls == [1.0, 2.0]


async def test_retry_after_is_used_when_larger_than_the_backoff() -> None:
    sleep = RecordingSleep()
    flaky = Flaky(
        LLMRateLimitError("slow", retry_after=5), LLMRateLimitError("slow", retry_after=30)
    )

    await retrying(
        flaky,
        policy=RetrySettings(jitter=False, max_interval=30.0),
        retry_on=_transient,
        what="llm",
        retry_after=llm_retry_after,
        sleep=sleep,
    )

    assert sleep.calls == [5.0, 30.0]


async def test_retry_after_beyond_max_interval_gives_up_without_sleeping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    sleep = RecordingSleep()
    err = LLMRateLimitError("slow", retry_after=120)
    flaky = Flaky(err)

    with (
        caplog.at_level(logging.INFO, logger="invio.retry"),
        pytest.raises(LLMRateLimitError) as raised,
    ):
        await retrying(
            flaky,
            policy=RetrySettings(jitter=False, max_interval=30.0),
            retry_on=_transient,
            what="llm",
            retry_after=llm_retry_after,
            sleep=sleep,
        )

    assert raised.value is err
    assert flaky.calls == 1
    assert sleep.calls == []
    assert [r.getMessage() for r in caplog.records] == ["retry.gave_up"]


async def test_retry_after_is_ignored_without_a_hint_function() -> None:
    sleep = RecordingSleep()
    flaky = Flaky(LLMRateLimitError("slow", retry_after=120))

    await retrying(
        flaky,
        policy=RetrySettings(jitter=False),
        retry_on=_transient,
        what="llm",
        sleep=sleep,
    )

    assert sleep.calls == [1.0]


async def test_retry_after_smaller_than_backoff_is_ignored() -> None:
    sleep = RecordingSleep()
    flaky = Flaky(LLMRateLimitError("slow", retry_after=0.5), LLMRateLimitError("slow"))

    await retrying(
        flaky,
        policy=RetrySettings(jitter=False),
        retry_on=_transient,
        what="llm",
        retry_after=llm_retry_after,
        sleep=sleep,
    )

    assert sleep.calls == [1.0, 2.0]


async def test_jitter_adds_at_most_ten_percent_and_respects_max_interval() -> None:
    sleep = RecordingSleep()
    flaky = Flaky(LLMUnavailableError("x"), LLMUnavailableError("x"))

    await retrying(
        flaky,
        policy=RetrySettings(jitter=True, max_interval=2.05),
        retry_on=_transient,
        what="llm",
        sleep=sleep,
        rand=lambda: 1.0,
    )

    assert sleep.calls[0] == pytest.approx(1.1)
    assert sleep.calls[1] == pytest.approx(2.05)
    assert all(wait <= 2.05 for wait in sleep.calls)


async def test_jitter_with_zero_random_changes_nothing() -> None:
    sleep = RecordingSleep()
    flaky = Flaky(LLMUnavailableError("x"))

    await retrying(
        flaky,
        policy=RetrySettings(jitter=True),
        retry_on=_transient,
        what="llm",
        sleep=sleep,
        rand=lambda: 0.0,
    )

    assert sleep.calls == [1.0]


async def test_non_matching_error_is_attempted_once_without_sleeping() -> None:
    sleep = RecordingSleep()
    err = LLMAuthError("nope")
    flaky = Flaky(err)

    with pytest.raises(LLMAuthError) as raised:
        await retrying(flaky, policy=RetrySettings(), retry_on=_transient, what="llm", sleep=sleep)

    assert raised.value is err
    assert flaky.calls == 1
    assert sleep.calls == []


async def test_default_sleep_really_waits_briefly() -> None:
    flaky = Flaky(LLMUnavailableError("x"))

    result = await retrying(
        flaky,
        policy=RetrySettings(initial_interval=0.001, max_interval=0.001, jitter=False),
        retry_on=_transient,
        what="llm",
    )

    assert result == "ok"
    assert flaky.calls == 2


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (LLMRateLimitError("x"), True),
        (LLMUnavailableError("x"), True),
        (LLMAuthError("x"), False),
        (LLMInvalidRequestError("x"), False),
        (RuntimeError("x"), False),
    ],
)
def test_is_transient_llm(err: BaseException, expected: bool) -> None:
    assert is_transient_llm(err) is expected


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (LLMRateLimitError("x", retry_after=7), 7),
        (LLMRateLimitError("x"), None),
        (LLMUnavailableError("x"), None),
    ],
)
def test_llm_retry_after(err: BaseException, expected: float | None) -> None:
    assert llm_retry_after(err) == expected


@pytest.mark.parametrize(
    ("err", "expected"),
    [
        (FetchError("timeout", url=URL), True),
        (FetchError("http_status", url=URL, status=500), True),
        (BlockedError(BlockReason.BLOCKED_BY_ROBOTS, url=URL), False),
        (TooLargeError(url=URL, limit=1), False),
        (RenderUnavailableError(url=URL), False),
        (RuntimeError("x"), False),
    ],
)
def test_is_transient_fetch(err: BaseException, expected: bool) -> None:
    assert is_transient_fetch(err) is expected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"initial_interval": 0.0},
        {"backoff_factor": 0.5},
        {"initial_interval": 5.0, "max_interval": 4.0},
    ],
)
def test_retry_settings_rejects_invalid_values(kwargs: dict[str, float]) -> None:
    with pytest.raises(ValueError):
        RetrySettings(**kwargs)  # type: ignore[arg-type]


def test_retry_settings_defaults_match_the_contract() -> None:
    policy = RetrySettings()

    assert (
        policy.initial_interval,
        policy.backoff_factor,
        policy.max_interval,
        policy.max_attempts,
        policy.jitter,
    ) == (1.0, 2.0, 30.0, 3, True)


async def test_retry_log_records_carry_no_error_message(
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "secret-token-123"
    flaky = Flaky(LLMUnavailableError(secret))

    with caplog.at_level(logging.INFO, logger="invio.retry"):
        await retrying(
            flaky,
            policy=RetrySettings(jitter=False),
            retry_on=_transient,
            what="llm",
            sleep=RecordingSleep(),
        )

    records = [r for r in caplog.records if r.getMessage() == "retry.attempt"]
    assert len(records) == 1
    record = records[0]
    assert (record.what, record.attempt, record.wait_s, record.error) == (  # type: ignore[attr-defined]
        "llm",
        1,
        1.0,
        "LLMUnavailableError",
    )
    assert secret not in caplog.text


@pytest.mark.parametrize(
    ("reason", "status", "expected"),
    [
        ("timeout", None, True),
        ("connection_failed", None, True),
        ("dns_failed", None, True),
        ("invalid_response", None, True),
        ("render_failed", None, True),
        ("http_status", 408, True),
        ("http_status", 425, True),
        ("http_status", 429, True),
        ("http_status", 500, True),
        ("http_status", 503, True),
        ("http_status", 401, False),
        ("http_status", 403, False),
        ("http_status", 404, False),
        ("http_status", 410, False),
        ("http_status", 304, False),
        ("http_status", None, False),
        ("invalid_url", None, False),
        ("not_html", None, False),
        ("malformed_feed", None, False),
        ("selector_not_found", None, False),
        ("invalid_wait_for", None, False),
        ("too_many_redirects", None, False),
        ("missing_location", None, False),
        ("something_new", None, False),
    ],
)
def test_is_transient_fetch_is_an_allow_list(
    reason: str, status: int | None, expected: bool
) -> None:
    assert is_transient_fetch(FetchError(reason, url=URL, status=status)) is expected
