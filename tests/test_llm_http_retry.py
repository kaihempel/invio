"""Unit tests of the provider-neutral helpers in ``invio.llm.http_retry``."""

from datetime import UTC, datetime

import pytest

from invio.llm.base import LLMUnavailableError
from invio.llm.http_retry import (
    Failure,
    RetryPolicy,
    describe,
    retry_after,
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
