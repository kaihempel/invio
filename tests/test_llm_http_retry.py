"""Unit tests of the provider-neutral helpers in ``invio.llm.http_retry``."""

import logging
from datetime import UTC, datetime

import pytest

from invio.llm.base import LLMAuthError, LLMError, LLMRateLimitError, LLMUnavailableError
from invio.llm.http_retry import (
    Failure,
    RetryPolicy,
    classify_status,
    classify_transport,
    describe,
    retry_after,
    run_with_retries,
    sanitize_detail,
    strict_schema,
    wait_before_retry,
)

NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def _failure(*, retryable: bool = True, after: float | None = None) -> Failure:
    error = LLMUnavailableError("x", provider="p", model="m")
    return Failure("server", retryable, error, 503, after)


@pytest.mark.parametrize("name", ["Retry-After", "retry-after", "RETRY-AFTER"])
def test_retry_after_matches_header_names_case_insensitively(name: str) -> None:
    assert retry_after({name: "7"}, lambda: NOW) == 7.0


def test_retry_after_without_header_is_none() -> None:
    assert retry_after({"content-type": "x"}, lambda: NOW) is None


@pytest.mark.parametrize(("hint", "expected"), [(None, 7.0), (2.5, 2.5)])
def test_classify_status_rate_limit_prefers_the_wait_hint(
    hint: float | None, expected: float
) -> None:
    failure = classify_status(
        429,
        label="X",
        env_var="X_KEY",
        provider="p",
        model="m",
        detail="",
        headers={"Retry-After": "7"},
        now=lambda: NOW,
        wait_hint=hint,
    )

    assert failure.retry_after == expected
    assert isinstance(failure.error, LLMRateLimitError)
    assert failure.error.retry_after == expected


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        (None, "X rejected the API key; check X_KEY (HTTP 401, model m)"),
        ("X refused access", "X refused access (HTTP 401, model m)"),
    ],
)
def test_classify_status_auth_summary_replaces_the_default(
    summary: str | None, expected: str
) -> None:
    failure = classify_status(
        401,
        label="X",
        env_var="X_KEY",
        provider="p",
        model="m",
        detail="",
        headers={},
        now=lambda: NOW,
        auth_summary=summary,
    )

    assert isinstance(failure.error, LLMAuthError)
    assert str(failure.error) == expected


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("", "X request could not be sent (ValueError) (model m)"),
        ("check X_URL", "X request could not be sent (ValueError); check X_URL (model m)"),
    ],
)
def test_classify_transport_appends_the_unsendable_hint(hint: str, expected: str) -> None:
    failure = classify_transport(
        ValueError("x"),
        label="X",
        provider="p",
        model="m",
        unsendable=(ValueError,),
        bad_response=(),
        unsendable_hint=hint,
    )

    assert (failure.kind, failure.retryable) == ("unsendable", False)
    assert str(failure.error) == expected


def test_sanitize_detail_collapses_whitespace_and_truncates() -> None:
    assert sanitize_detail("a \n\t b\x00c") == "a b c"
    assert len(sanitize_detail("y" * 1000)) == 300


def test_describe_with_and_without_status_and_detail() -> None:
    assert describe("Oops", "m", 500, "why") == "Oops (HTTP 500, model m): why"
    assert describe("Oops", "m", None) == "Oops (model m)"


def test_strict_schema_closes_nested_objects_without_mutating() -> None:
    original = {"type": "object", "properties": {"a": {"type": "object", "properties": {}}}}
    closed = strict_schema(original)
    assert closed == {
        "type": "object",
        "additionalProperties": False,
        "properties": {"a": {"type": "object", "additionalProperties": False, "properties": {}}},
    }
    assert "additionalProperties" not in original


def test_wait_before_retry_backs_off_exponentially_with_jitter() -> None:
    policy = RetryPolicy(base_delay=2.0, jitter=0.5)
    wait = wait_before_retry(_failure(), 3, policy, lambda low, high: high)
    assert wait == 2.0 * 4 * 1.5


def test_wait_before_retry_gives_up_when_not_retryable_or_exhausted() -> None:
    policy = RetryPolicy(max_retries=2)
    assert wait_before_retry(_failure(retryable=False), 1, policy, lambda a, b: 0.0) is None
    assert wait_before_retry(_failure(), 3, policy, lambda a, b: 0.0) is None


def test_wait_before_retry_honours_retry_after_up_to_the_cap() -> None:
    policy = RetryPolicy(max_retry_after=10)
    assert wait_before_retry(_failure(after=10), 1, policy, lambda a, b: 0.0) == 10
    assert wait_before_retry(_failure(after=10.5), 1, policy, lambda a, b: 0.0) is None


def test_strict_schema_leaves_free_form_maps_open() -> None:
    schema = {"type": "object", "additionalProperties": {"type": "integer"}}

    assert strict_schema(schema) == schema


class _Boom(Exception):
    pass


def _classify(exc: Exception, model: str, now: object) -> Failure | None:
    return _failure() if isinstance(exc, _Boom) else None


async def _run(attempt: object, slept: list[float]) -> object:
    async def sleep(seconds: float) -> None:
        slept.append(seconds)

    return await run_with_retries(
        attempt,  # type: ignore[arg-type]
        classify=_classify,  # type: ignore[arg-type]
        policy=RetryPolicy(max_retries=2, jitter=0.0),
        provider="p",
        model="m",
        sleep=sleep,
        uniform=lambda low, high: 0.0,
        now=lambda: NOW,
    )


async def test_run_with_retries_succeeds_after_transient_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        if calls < 3:
            raise _Boom
        return "ok"

    slept: list[float] = []
    with caplog.at_level(logging.WARNING):
        assert await _run(attempt, slept) == "ok"

    assert len(slept) == 2
    assert [r.message for r in caplog.records].count("llm.retry") == 2


async def test_run_with_retries_gives_up_without_exception_context() -> None:
    async def attempt() -> str:
        raise _Boom

    with pytest.raises(LLMUnavailableError) as info:
        await _run(attempt, [])

    assert info.value.__context__ is None


async def test_run_with_retries_does_not_retry_typed_errors() -> None:
    calls = 0

    async def attempt() -> str:
        nonlocal calls
        calls += 1
        raise LLMError("typed")

    with pytest.raises(LLMError):
        await _run(attempt, [])

    assert calls == 1


async def test_run_with_retries_propagates_unrecognised_exceptions() -> None:
    async def attempt() -> str:
        raise KeyError("x")

    with pytest.raises(KeyError):
        await _run(attempt, [])
