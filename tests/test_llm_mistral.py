"""Tests for the Mistral provider, driven through recorded HTTP fixtures (no network)."""

import asyncio
import json
import logging
import math
import re
import threading
import traceback
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml
from mistralai.client import errors
from pydantic import BaseModel

import invio.llm
from invio.config.job import JobConfig
from invio.llm import factory
from invio.llm.base import (
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMInvalidOutputError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
    with_timeout,
)
from invio.llm.mistral import (
    MistralProvider,
    RetryPolicy,
    _classify,
    _retry_after,
    _safe_detail,
    _strict_schema,
)
from invio.llm.registry import default_registry
from tests.llm_helpers import Score, make_settings
from tests.mistral_helpers import API_KEY, HANG, Recorder, load_fixture, make_provider

SYSTEM = "You rate things."
USER = "PROMPT-TEXT-SENTINEL rate this"
MODEL = "mistral-small-2603"
NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)


async def _complete(provider: MistralProvider) -> tuple[str, Usage]:
    return await provider.complete(SYSTEM, USER, model=MODEL, temperature=0.2, max_tokens=50)


async def _structured(provider: MistralProvider) -> tuple[Score, Usage]:
    return await provider.complete_structured(SYSTEM, USER, Score, model=MODEL, temperature=0.2)


# --- skeleton: RetryPolicy, settings, registration (T008) ---------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_retries", -1),
        ("base_delay", 0),
        ("base_delay", -1.0),
        ("jitter", -0.1),
        ("jitter", 1.0),
        ("max_retry_after", 0),
        ("max_retry_after", -5.0),
    ],
)
def test_retry_policy_rejects_invalid_values(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        RetryPolicy(**{field: value})


def test_retry_policy_defaults() -> None:
    policy = RetryPolicy()

    assert (policy.max_retries, policy.base_delay, policy.jitter, policy.max_retry_after) == (
        3,
        1.0,
        0.25,
        60.0,
    )


def test_retry_policy_allows_zero_retries_and_zero_jitter() -> None:
    assert RetryPolicy(max_retries=0, jitter=0).max_retries == 0


def test_from_settings_without_key_names_the_env_var() -> None:
    with pytest.raises(LLMAuthError, match="INVIO_MISTRAL_API_KEY"):
        MistralProvider.from_settings(make_settings())


def test_from_settings_with_blank_key_is_missing() -> None:
    with pytest.raises(LLMAuthError, match="INVIO_MISTRAL_API_KEY"):
        MistralProvider.from_settings(make_settings(mistral_api_key="   "))


def test_from_settings_builds_provider_whose_repr_hides_the_key() -> None:
    provider = MistralProvider.from_settings(
        make_settings(mistral_api_key=API_KEY, llm_timeout_seconds=5)
    )

    assert provider.timeout_seconds == 5
    assert API_KEY not in repr(provider)
    assert "MistralProvider" in repr(provider)


def test_provider_is_registered_and_discoverable() -> None:
    factory._discover()

    provider = factory.get_provider("mistral", make_settings(mistral_api_key=API_KEY))

    assert provider is not None
    assert factory._REGISTRY["mistral"] is MistralProvider


def test_constructing_the_provider_creates_no_http_client() -> None:
    _, recorder, _ = make_provider()

    assert recorder.clients_created == 0


# --- US1: complete() ----------------------------------------------------------------------


async def test_complete_returns_text_and_usage() -> None:
    provider, _, _ = make_provider("chat_ok")

    text, usage = await _complete(provider)

    assert text == "Hello"
    assert usage == Usage(12, 3)
    assert usage.requests == 1


async def test_complete_sends_the_expected_request() -> None:
    provider, recorder, _ = make_provider("chat_ok")

    await _complete(provider)

    (request,) = recorder.requests
    assert (request.method, request.path) == ("POST", "/v1/chat/completions")
    assert request.has_authorization
    assert request.body["model"] == MODEL
    assert request.body["temperature"] == 0.2
    assert request.body["max_tokens"] == 50
    assert request.body["messages"] == [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER},
    ]
    assert "response_format" not in request.body


async def test_complete_concatenates_text_chunks() -> None:
    provider, _, _ = make_provider("chat_ok_chunks")

    text, _ = await _complete(provider)

    assert text == "Hello"


async def test_complete_without_usage_reports_zero_tokens() -> None:
    provider, _, _ = make_provider("chat_no_usage")

    text, usage = await _complete(provider)

    assert text == "Hello"
    assert usage == Usage(0, 0)


async def test_complete_with_usage_object_missing_is_unavailable() -> None:
    # The SDK requires the ``usage`` object, so a response without it is a bad response.
    body = load_fixture("chat_ok").json()
    del body["usage"]
    provider, recorder, _ = make_provider(httpx2.Response(200, json=body))

    with pytest.raises(LLMUnavailableError, match="unexpected response"):
        await _complete(provider)

    assert len(recorder.requests) == 1


async def test_complete_with_no_choices_is_unavailable() -> None:
    provider, recorder, waits = make_provider("chat_empty_choices")

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


async def test_complete_with_empty_content_is_unavailable() -> None:
    response = load_fixture("chat_ok")
    body = response.json()
    body["choices"][0]["message"]["content"] = ""
    provider, recorder, _ = make_provider(httpx2.Response(200, json=body))

    with pytest.raises(LLMUnavailableError, match="no answer text"):
        await _complete(provider)

    assert len(recorder.requests) == 1


async def test_complete_with_non_text_chunks_only_is_unavailable() -> None:
    body = load_fixture("chat_ok").json()
    body["choices"][0]["message"]["content"] = [
        {"type": "image_url", "image_url": "https://example.com/x.png"}
    ]
    provider, _, _ = make_provider(httpx2.Response(200, json=body))

    with pytest.raises(LLMUnavailableError, match="no answer text"):
        await _complete(provider)


async def test_complete_with_malformed_body_is_unavailable_without_retry() -> None:
    provider, recorder, waits = make_provider("malformed_200")

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


def test_provider_survives_successive_event_loops() -> None:
    provider, recorder, _ = make_provider("chat_ok", "chat_ok")

    first = asyncio.run(_complete(provider))
    second = asyncio.run(_complete(provider))

    assert first[0] == second[0] == "Hello"
    assert recorder.clients_created == 2


async def test_provider_reuses_the_client_within_one_loop() -> None:
    provider, recorder, _ = make_provider("chat_ok", "chat_ok")

    await _complete(provider)
    await _complete(provider)

    assert recorder.clients_created == 1


def test_previous_client_of_a_closed_loop_is_dropped_without_error() -> None:
    provider, recorder, _ = make_provider("chat_ok", "chat_ok", "chat_ok")

    asyncio.run(_complete(provider))
    asyncio.run(_complete(provider))
    asyncio.run(_complete(provider))

    assert recorder.clients_created == 3
    # Only the client of the last loop is still referenced; the others were released.
    assert [client for _, client in provider._clients.values()] == [recorder.clients[-1]]


async def test_aclose_closes_the_client_of_the_running_loop() -> None:
    provider, recorder, _ = make_provider("chat_ok", "chat_ok")
    await _complete(provider)

    await provider.aclose()
    await provider.aclose()  # a second close is a no-op

    assert recorder.clients[0].is_closed
    assert provider._clients == {}
    await _complete(provider)
    assert recorder.clients_created == 2
    await provider.aclose()


async def test_aclose_before_any_call_is_a_no_op() -> None:
    provider, recorder, _ = make_provider()

    await provider.aclose()

    assert recorder.clients_created == 0


def test_threads_with_their_own_loops_keep_their_own_clients() -> None:
    # Two live loops in two threads: neither evicts the other's client.
    provider, recorder, _ = make_provider(*["chat_ok"] * 4)
    barrier = threading.Barrier(2, timeout=5)
    failures: list[BaseException] = []

    async def two_calls() -> None:
        await _complete(provider)
        await asyncio.to_thread(barrier.wait)
        await _complete(provider)

    def run() -> None:
        try:
            asyncio.run(two_calls())
        except BaseException as exc:  # reported below, threads swallow exceptions
            failures.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert failures == []
    assert len(recorder.requests) == 4
    assert recorder.clients_created == 2


# --- US2: complete_structured() -----------------------------------------------------------


async def test_structured_returns_validated_value() -> None:
    provider, _, _ = make_provider("structured_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert usage.requests == 1


async def test_structured_request_carries_strict_json_schema() -> None:
    provider, recorder, _ = make_provider("structured_ok")

    await _structured(provider)

    (request,) = recorder.requests
    strict = {**Score.model_json_schema(), "additionalProperties": False}
    assert request.body["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Score", "schema": strict, "strict": True},
    }
    assert "max_tokens" not in request.body


class _Inner(BaseModel):
    label: str


class _Outer(BaseModel):
    inner: _Inner
    items: list[_Inner]
    note: str | None = None


def test_strict_schema_closes_every_object_and_leaves_the_input_untouched() -> None:
    original = _Outer.model_json_schema()
    pristine = json.loads(json.dumps(original))

    strict = _strict_schema(original)

    assert original == pristine
    assert isinstance(strict, dict)
    assert strict["additionalProperties"] is False
    assert strict["$defs"]["_Inner"]["additionalProperties"] is False
    assert strict["properties"] == original["properties"]


async def test_structured_repairs_once() -> None:
    provider, recorder, _ = make_provider("structured_invalid", "structured_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert len(recorder.requests) == 2
    repair_user = recorder.requests[1].body["messages"][1]["content"]
    assert "score" in repair_user
    assert "invalid" in repair_user.lower()
    assert usage == Usage(24, 6, 2)


async def test_structured_invalid_twice_raises_invalid_output() -> None:
    provider, recorder, _ = make_provider("structured_invalid", "structured_invalid")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.usage == Usage(24, 6, 2)
    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert len(recorder.requests) == 2


# --- US3: error mapping -------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["error_401", "error_403"])
async def test_auth_failures_are_not_retried(fixture: str) -> None:
    provider, recorder, waits = make_provider(fixture)

    with pytest.raises(LLMAuthError, match="INVIO_MISTRAL_API_KEY") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    ("fixture", "status", "text"),
    [
        ("error_400", 400, "Invalid model: x"),
        ("error_404_model", 404, "Invalid model: x"),
        ("error_422", 422, "field required"),
    ],
)
async def test_rejected_requests_are_invalid_request_errors(
    fixture: str, status: int, text: str
) -> None:
    provider, recorder, waits = make_provider(fixture)

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    message = str(info.value)
    assert info.value.status == status
    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert str(status) in message
    assert text in message
    assert "PROMPT-ECHO-SENTINEL" not in message
    assert len(recorder.requests) == 1
    assert waits == []


async def test_422_message_names_the_field_location() -> None:
    provider, _, _ = make_provider("error_422")

    with pytest.raises(LLMInvalidRequestError, match=r"body\.messages: field required"):
        await _complete(provider)


async def test_timeout_deadline_is_unavailable_and_not_retried() -> None:
    provider, recorder, waits = make_provider(HANG, timeout_seconds=0.01)

    with pytest.raises(LLMUnavailableError, match=r"did not answer within 0\.01 s"):
        await _complete(provider)

    assert len(recorder.requests) == 1
    assert waits == []


async def test_transport_timeout_is_unavailable_and_not_retried() -> None:
    provider, recorder, waits = make_provider(httpx2.ReadTimeout("x"))

    with pytest.raises(LLMUnavailableError, match="timed out") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


async def test_error_message_is_bounded_and_free_of_the_response_body() -> None:
    huge = httpx2.Response(400, json={"message": "x" * 10_000, "extra": "BODY-LEAK"})
    provider, _, _ = make_provider(huge)

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert len(str(info.value)) <= 400
    assert "x" * 301 not in str(info.value)
    assert "BODY-LEAK" not in str(info.value)


@pytest.mark.parametrize(
    "reply",
    [
        "error_401",
        "error_422",
        "error_429",
        "error_503",
        "malformed_200",
        httpx2.ConnectError("boom SECRET-IN-EXC"),
    ],
    ids=["401", "422", "429", "503", "malformed", "connect"],
)
async def test_errors_never_expose_key_or_prompt(reply: Any) -> None:
    provider, _, _ = make_provider(*([reply] * 4), retry=RetryPolicy(max_retries=3))

    with pytest.raises(LLMError) as info:
        await _complete(provider)

    # Reporters that walk __context__ regardless of __suppress_context__ see nothing either.
    chain = "".join(traceback.format_exception(info.value, chain=True))
    for text in (str(info.value), repr(info.value), repr(provider), chain):
        assert API_KEY not in text
        assert "PROMPT-TEXT-SENTINEL" not in text
        assert "SECRET-IN-EXC" not in text
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


@pytest.mark.parametrize(
    "fixture", ["error_401", "error_422", "error_503", "error_429_retry_after_long"]
)
async def test_errors_do_not_keep_the_response_body_as_context(fixture: str) -> None:
    provider, _, _ = make_provider(*([fixture] * 4))

    with pytest.raises(LLMError) as info:
        await _complete(provider)

    assert info.value.__context__ is None
    assert info.value.__cause__ is None


async def test_structured_invalid_output_has_no_context() -> None:
    provider, _, _ = make_provider("structured_invalid", "structured_invalid")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.__context__ is None


# --- US3: retries -------------------------------------------------------------------------


async def test_rate_limit_is_retried_with_exponential_backoff() -> None:
    provider, recorder, waits = make_provider(*["error_429"] * 4)

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert len(recorder.requests) == 4
    assert waits == [1.0, 2.0, 4.0]
    assert info.value.retry_after is None
    assert (info.value.provider, info.value.model) == ("mistral", MODEL)


async def test_server_error_exhausts_retries_naming_the_status() -> None:
    provider, recorder, _ = make_provider(*["error_503"] * 4)

    with pytest.raises(LLMUnavailableError, match="503"):
        await _complete(provider)

    assert len(recorder.requests) == 4


async def test_connection_failure_exhausts_retries() -> None:
    provider, recorder, waits = make_provider(*[httpx2.ConnectError("x")] * 4)

    with pytest.raises(LLMUnavailableError, match="connection"):
        await _complete(provider)

    assert len(recorder.requests) == 4
    assert waits == [1.0, 2.0, 4.0]


async def test_rate_limit_then_success() -> None:
    provider, recorder, waits = make_provider("error_429", "chat_ok")

    text, usage = await _complete(provider)

    assert text == "Hello"
    assert usage.requests == 1
    assert waits == [1.0]
    assert len(recorder.requests) == 2


async def test_server_error_then_success() -> None:
    provider, _, waits = make_provider("error_500", "chat_ok")

    text, _ = await _complete(provider)

    assert text == "Hello"
    assert waits == [1.0]


async def test_retry_after_header_sets_the_wait() -> None:
    provider, _, waits = make_provider("error_429_retry_after", "chat_ok")

    await _complete(provider)

    assert waits == [7.0]


async def test_retry_after_above_the_cap_fails_immediately() -> None:
    provider, recorder, waits = make_provider("error_429_retry_after_long", "chat_ok")

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after == 120
    assert len(recorder.requests) == 1
    assert waits == []


async def test_final_rate_limit_error_keeps_retry_after() -> None:
    provider, recorder, waits = make_provider(*["error_429_retry_after"] * 4)

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after == 7
    assert len(recorder.requests) == 4
    assert waits == [7.0, 7.0, 7.0]


async def test_retry_after_http_date_is_relative_to_now() -> None:
    response = httpx2.Response(
        429,
        headers={"Retry-After": format_datetime(NOW + timedelta(seconds=10), usegmt=True)},
        json={"message": "slow down"},
    )
    provider, _, waits = make_provider(response, "chat_ok", now=lambda: NOW)

    await _complete(provider)

    assert waits == [10.0]


async def test_unparseable_retry_after_falls_back_to_backoff() -> None:
    response = httpx2.Response(429, headers={"Retry-After": "soon"}, json={"message": "x"})
    provider, _, waits = make_provider(response, "chat_ok")

    await _complete(provider)

    assert waits == [1.0]


async def test_jitter_scales_the_wait() -> None:
    provider, _, waits = make_provider("error_503", "chat_ok", uniform=lambda a, b: b)

    await _complete(provider)

    assert waits == [1.25]


async def test_jitter_range_uses_policy_jitter() -> None:
    bounds: list[tuple[float, float]] = []

    def uniform(a: float, b: float) -> float:
        bounds.append((a, b))
        return 0.0

    provider, _, _ = make_provider(
        "error_503", "chat_ok", retry=RetryPolicy(jitter=0.5), uniform=uniform
    )

    await _complete(provider)

    assert bounds == [(-0.5, 0.5)]


async def test_zero_retries_means_exactly_one_http_request() -> None:
    provider, recorder, waits = make_provider("error_503", retry=RetryPolicy(max_retries=0))

    with pytest.raises(LLMUnavailableError):
        await _complete(provider)

    assert len(recorder.requests) == 1
    assert waits == []


async def test_repair_request_has_its_own_retry_budget() -> None:
    provider, recorder, waits = make_provider("structured_invalid", "error_503", "structured_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert len(recorder.requests) == 3
    assert waits == [1.0]
    assert usage.requests == 2


async def test_each_retry_logs_one_warning_without_prompt_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _, _ = make_provider("error_429", "error_503", "chat_ok")

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    records = [r for r in caplog.records if r.getMessage() == "llm.retry"]
    assert len(records) == 2
    first, second = records
    assert (first.provider, first.model, first.attempt) == ("mistral", MODEL, 1)  # type: ignore[attr-defined]
    assert (first.status, first.failure, first.wait_s) == (429, "rate_limit", 1.0)  # type: ignore[attr-defined]
    assert (second.attempt, second.status, second.failure) == (2, 503, "server")  # type: ignore[attr-defined]
    assert second.wait_s == 2.0  # type: ignore[attr-defined]
    assert "PROMPT-TEXT-SENTINEL" not in str([r.__dict__ for r in caplog.records])


async def test_connection_retry_logs_failure_kind(caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = make_provider(httpx2.ConnectError("x"), "chat_ok")

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.retry"]
    assert record.status is None  # type: ignore[attr-defined]
    assert record.failure == "connection"  # type: ignore[attr-defined]


async def test_cancellation_during_the_retry_wait_stops_the_request() -> None:
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocking_sleep(seconds: float) -> None:
        entered.set()
        await never.wait()

    provider, recorder, _ = make_provider("error_503", "chat_ok", sleep=blocking_sleep)
    task = asyncio.ensure_future(_complete(provider))
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(recorder.requests) == 1


async def test_unknown_exceptions_propagate_unchanged() -> None:
    class Odd(Exception):
        pass

    provider, recorder, _ = make_provider(Odd("odd"))

    with pytest.raises(Odd):
        await _complete(provider)

    assert len(recorder.requests) == 1


async def test_overflowing_retry_after_fails_immediately() -> None:
    reply = httpx2.Response(429, headers={"Retry-After": "9" * 400}, content=b"{}")
    provider, recorder, waits = make_provider(reply, "chat_ok")

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after is None
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (httpx2.DecodingError("bad gzip SECRET-IN-EXC"), "unexpected response (DecodingError)"),
        (httpx2.TooManyRedirects("loop SECRET-IN-EXC"), "unexpected response (TooManyRedirects)"),
        (httpx2.StreamConsumed(), "unexpected response (StreamConsumed)"),
        (httpx2.InvalidURL("bad SECRET-IN-EXC"), "could not be sent (InvalidURL)"),
        (httpx2.UnsupportedProtocol("ftp SECRET-IN-EXC"), "not be sent (UnsupportedProtocol)"),
        (httpx2.LocalProtocolError("h11 SECRET-IN-EXC"), "could not be sent (LocalProtocolError)"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else "",
)
async def test_other_httpx_errors_are_typed_and_not_retried(failure: Exception, kind: str) -> None:
    provider, recorder, waits = make_provider(failure, "chat_ok")

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert kind in str(info.value)
    assert "SECRET-IN-EXC" not in str(info.value)
    assert info.value.__context__ is None
    assert len(recorder.requests) == 1
    assert waits == []


# --- helpers: _retry_after, _safe_detail, _classify ----------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("7", 7.0),
        ("0", 0.0),
        ("1.5", 1.5),
        (" 3 ", 3.0),
        ("-5", None),
        ("nan", None),
        ("inf", None),
        ("1e400", None),
        ("1_0", None),
        ("+5", None),
        ("9" * 400, math.inf),
        ("soon", None),
        ("", None),
        (None, None),
        ("Sun, 04 Oct 2026 11:00:00 GMT", 0.0),
        ("Sun, 04 Oct 2026 12:00:30 GMT", 30.0),
        ("Sun, 04 Oct 2026 12:00:30 -0000", 30.0),
    ],
)
def test_retry_after_parsing(value: str | None, expected: float | None) -> None:
    headers = {} if value is None else {"Retry-After": value}

    assert _retry_after(httpx2.Headers(headers), lambda: NOW) == expected


def _sdk_error(status: int, body: Any, *, raw: str | None = None) -> errors.SDKError:
    content = raw.encode() if raw is not None else json.dumps(body).encode()
    response = httpx2.Response(status, content=content)
    return errors.SDKError("API error occurred", response)


@pytest.mark.parametrize(
    ("body", "raw", "expected"),
    [
        ({"message": "Bad  thing\n happened"}, None, "Bad thing happened"),
        (
            {"detail": [{"loc": ["body", 0, "x"], "msg": "bad", "input": "SECRET"}]},
            None,
            "body.0.x: bad",
        ),
        ({"detail": [{"msg": "no loc"}, "junk", {"loc": ["a"], "msg": 5}]}, None, "no loc"),
        ({"detail": "plain detail"}, None, "plain detail"),
        ({"detail": []}, None, ""),
        ({"message": 5}, None, ""),
        ([1, 2], None, ""),
        (None, "not json at all", ""),
        (None, "", ""),
    ],
)
def test_safe_detail(body: Any, raw: str | None, expected: str) -> None:
    assert _safe_detail(_sdk_error(400, body, raw=raw)) == expected


def test_safe_detail_truncates_to_300_characters() -> None:
    assert len(_safe_detail(_sdk_error(400, {"message": "y" * 1000}))) == 300


def test_classify_other_exception_returns_none() -> None:
    assert _classify(RuntimeError("x"), MODEL, lambda: NOW) is None


def test_classify_no_response_error_is_retryable_connection() -> None:
    failure = _classify(errors.NoResponseError(), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("connection", True)
    assert isinstance(failure.error, LLMUnavailableError)


def test_classify_response_validation_error_is_not_retryable() -> None:
    response = httpx2.Response(200, content=b"{}")
    exc = errors.ResponseValidationError("bad", response, ValueError("x"))

    failure = _classify(exc, MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("bad_response", False)


@pytest.mark.parametrize("status", [200, 302, 399])
def test_classify_non_error_status_is_bad_response(status: int) -> None:
    failure = _classify(_sdk_error(status, {"message": "odd"}), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("bad_response", False)
    assert isinstance(failure.error, LLMUnavailableError)


def test_classify_other_4xx_is_invalid_request() -> None:
    failure = _classify(_sdk_error(409, {"message": "conflict"}), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("invalid_request", False)
    assert isinstance(failure.error, LLMInvalidRequestError)
    assert failure.error.status == 409


class Page[Item](BaseModel):
    items: list[Item]


async def test_structured_schema_name_is_sanitized_for_generic_models() -> None:
    body = load_fixture("chat_ok").json()
    body["choices"][0]["message"]["content"] = '{"items": [1]}'
    provider, recorder, _ = make_provider(httpx2.Response(200, json=body))

    await provider.complete_structured(SYSTEM, USER, Page[int], model=MODEL, temperature=0)

    name = recorder.requests[0].body["response_format"]["json_schema"]["name"]
    assert name == "Page_int_"


def test_real_discovery_registers_mistral() -> None:
    # The session fixture ran real discovery; other providers may be registered too.
    assert factory._REGISTRY.get("mistral") is MistralProvider


# --- US5: shipped registry ----------------------------------------------------------------


def _shipped_ids() -> list[str]:
    path = Path(invio.llm.__path__[0]) / "models.d" / "mistral.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    return list(document["models"])


def test_shipped_registry_has_priced_mistral_models() -> None:
    models = default_registry().models_for("mistral")

    assert len(models) >= 2
    for info in models:
        assert info.input_price_per_mtok >= 0
        assert info.output_price_per_mtok >= 0
        assert info.context_window > 0


def test_shipped_mistral_ids_are_pinned() -> None:
    assert _shipped_ids()
    assert not [model_id for model_id in _shipped_ids() if model_id.endswith("-latest")]


def test_shipped_models_resolve_for_both_roles(job_data: dict[str, Any]) -> None:
    fast, smart = _shipped_ids()[:2]
    job_data["llm"] = {"provider": "mistral", "models": {"fast": fast, "smart": smart}}
    llm = JobConfig.model_validate(job_data).llm
    factory._discover()

    provider, model = factory.resolve(llm, "fast", make_settings(mistral_api_key="sk-test"))
    _, smart_model = factory.resolve(llm, "smart", make_settings(mistral_api_key="sk-test"))

    assert provider is not None
    assert (model, smart_model) == (fast, smart)


def test_latest_alias_is_not_registered(job_data: dict[str, Any]) -> None:
    job_data["llm"] = {
        "provider": "mistral",
        "models": {"fast": "mistral-small-latest", "smart": "mistral-small-latest"},
    }
    llm = JobConfig.model_validate(job_data).llm

    with pytest.raises(LLMConfigError, match="not registered"):
        factory.resolve(llm, "fast", make_settings(mistral_api_key="sk-test"))


def test_cost_of_a_million_tokens_each_is_the_sum_of_the_prices() -> None:
    fast = _shipped_ids()[0]
    info = default_registry().get(fast)
    assert info is not None

    cost = default_registry().cost(fast, Usage(1_000_000, 1_000_000))

    assert cost == info.input_price_per_mtok + info.output_price_per_mtok


def test_recorder_fails_clearly_when_exhausted() -> None:
    recorder = Recorder(())

    with pytest.raises(AssertionError, match="queue exhausted"):
        asyncio.run(recorder(httpx2.Request("POST", "https://x.test/")))


# --- traceability gap tests ---------------------------------------------------------------


def _with_content(content: Any) -> httpx2.Response:
    body = load_fixture("chat_ok").json()
    body["choices"][0]["message"]["content"] = content
    return httpx2.Response(200, json=body)


def _with_usage(usage: dict[str, int]) -> httpx2.Response:
    body = load_fixture("chat_ok").json()
    body["usage"] = usage
    return httpx2.Response(200, json=body)


def _html(status: int, text: str) -> httpx2.Response:
    return httpx2.Response(status, content=text.encode(), headers={"content-type": "text/html"})


async def test_concurrent_first_calls_share_one_client() -> None:
    # FR-027: one HTTP client per loop, also when the first calls run concurrently.
    provider, recorder, _ = make_provider("chat_ok", "chat_ok", "chat_ok")

    results = await asyncio.gather(*(_complete(provider) for _ in range(3)))

    assert [text for text, _ in results] == ["Hello"] * 3
    assert recorder.clients_created == 1


async def test_temperature_zero_is_sent_explicitly() -> None:
    # FR-005: the CLI check uses temperature 0, a falsy value that must not be dropped.
    provider, recorder, _ = make_provider("chat_ok")

    await provider.complete(SYSTEM, USER, model=MODEL, temperature=0, max_tokens=5)

    assert recorder.requests[0].body["temperature"] == 0
    assert recorder.requests[0].body["max_tokens"] == 5


@pytest.mark.parametrize(
    ("usage", "expected"),
    [({"prompt_tokens": 9}, Usage(9, 0)), ({"completion_tokens": 4}, Usage(0, 4))],
    ids=["no-completion-tokens", "no-prompt-tokens"],
)
async def test_one_missing_token_count_is_zero(usage: dict[str, int], expected: Usage) -> None:
    # US1-3 / FR-006: "one or both" counts missing.
    provider, _, _ = make_provider(_with_usage(usage))

    text, result = await _complete(provider)

    assert text == "Hello"
    assert result == expected


async def test_null_content_is_unavailable() -> None:
    # US1-4 / FR-007
    provider, recorder, waits = make_provider(_with_content(None))

    with pytest.raises(LLMUnavailableError, match="no answer text") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    "response",
    [
        _html(200, "<html>oops</html>"),
        httpx2.Response(200, content=b"oops", headers={"content-type": "application/json"}),
        httpx2.Response(204),
    ],
    ids=["html-200", "invalid-json-200", "empty-204"],
)
async def test_non_json_success_body_is_unavailable_without_retry(
    response: httpx2.Response,
) -> None:
    # Edge case / FR-013: a body that is not valid JSON is provider-unavailable, not retried.
    provider, recorder, waits = make_provider(response, "chat_ok")

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert "oops" not in str(info.value)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_every_5xx_status_exhausts_retries(status: int) -> None:
    # US3-5 / FR-012 / SC-003
    replies = [httpx2.Response(status, json={"message": "server trouble"})] * 4
    provider, recorder, waits = make_provider(*replies)

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert f"HTTP {status}" in str(info.value)
    assert len(recorder.requests) == 4
    assert waits == [1.0, 2.0, 4.0]


async def test_exhausted_server_errors_name_the_last_status() -> None:
    # US3-5: "naming the provider, model and last status".
    provider, _, _ = make_provider("error_500", "error_500", "error_500", "error_503")

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert "HTTP 503" in str(info.value)
    assert "HTTP 500" not in str(info.value)


async def test_html_gateway_error_is_retried() -> None:
    provider, recorder, waits = make_provider(_html(502, "<html>Bad Gateway</html>"), "chat_ok")

    text, _ = await _complete(provider)

    assert text == "Hello"
    assert len(recorder.requests) == 2
    assert waits == [1.0]


async def test_exhausted_html_gateway_error_message_has_no_markup() -> None:
    provider, _, _ = make_provider(*[_html(502, "<html>BODY-LEAK</html>")] * 4)

    with pytest.raises(LLMUnavailableError, match="HTTP 502") as info:
        await _complete(provider)

    assert "BODY-LEAK" not in str(info.value)
    assert "<html>" not in str(info.value)


@pytest.mark.parametrize("status", [402, 405, 409, 413, 418])
async def test_any_other_4xx_is_an_invalid_request_without_retry(status: int) -> None:
    # FR-013: "any other 4xx response not covered by FR-010/FR-011", end to end over HTTP.
    provider, recorder, waits = make_provider(
        httpx2.Response(status, json={"message": "rejected here"}), "chat_ok"
    )

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert info.value.status == status
    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert f"HTTP {status}" in str(info.value)
    assert "rejected here" in str(info.value)
    assert len(recorder.requests) == 1
    assert waits == []


async def test_invalid_request_with_non_json_body_still_names_the_status() -> None:
    provider, _, _ = make_provider(_html(400, "<html>BODY-LEAK</html>"))

    with pytest.raises(LLMInvalidRequestError, match="HTTP 400") as info:
        await _complete(provider)

    assert "BODY-LEAK" not in str(info.value)


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectError("connection refused"),
        httpx2.ConnectError("[Errno 8] nodename nor servname provided"),
        httpx2.ReadError("connection reset by peer"),
        httpx2.RemoteProtocolError("server disconnected without sending a response"),
    ],
    ids=["refused", "dns", "reset", "disconnected"],
)
async def test_connection_failure_once_then_success(failure: Exception) -> None:
    # US3-7 second half / FR-016
    provider, recorder, waits = make_provider(failure, "chat_ok")

    text, usage = await _complete(provider)

    assert text == "Hello"
    assert usage == Usage(12, 3)
    assert len(recorder.requests) == 2
    assert waits == [1.0]


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectTimeout("x"),
        httpx2.ReadTimeout("x"),
        httpx2.WriteTimeout("x"),
        httpx2.PoolTimeout("x"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_every_transport_timeout_is_not_retried(failure: Exception) -> None:
    # US3-6 / FR-015
    provider, recorder, waits = make_provider(failure, "chat_ok")

    with pytest.raises(LLMUnavailableError, match="timed out"):
        await _complete(provider)

    assert len(recorder.requests) == 1
    assert waits == []


async def test_timeout_after_a_retry_is_not_retried_again() -> None:
    provider, recorder, waits = make_provider("error_503", httpx2.ReadTimeout("x"), "chat_ok")

    with pytest.raises(LLMUnavailableError, match="timed out"):
        await _complete(provider)

    assert len(recorder.requests) == 2
    assert waits == [1.0]


async def test_timeout_bounds_each_attempt_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    # Edge case / FR-015: the timeout applies per attempt; retries and waits are not counted.
    bounds: list[float] = []

    async def recording_with_timeout(awaitable: Any, *, seconds: float, **kwargs: Any) -> Any:
        bounds.append(seconds)
        return await with_timeout(awaitable, seconds=seconds, **kwargs)

    monkeypatch.setattr("invio.llm.mistral.with_timeout", recording_with_timeout)
    provider, _, _ = make_provider("error_429", "error_503", "chat_ok", timeout_seconds=7.0)

    await _complete(provider)

    assert bounds == [7.0, 7.0, 7.0]


async def test_backoff_base_is_configurable() -> None:
    # FR-018
    provider, recorder, waits = make_provider(*["error_429"] * 4, retry=RetryPolicy(base_delay=0.5))

    with pytest.raises(LLMRateLimitError):
        await _complete(provider)

    assert len(recorder.requests) == 4
    assert waits == [0.5, 1.0, 2.0]


async def test_retry_count_is_configurable() -> None:
    # FR-018
    provider, recorder, waits = make_provider(*["error_503"] * 2, retry=RetryPolicy(max_retries=1))

    with pytest.raises(LLMUnavailableError):
        await _complete(provider)

    assert len(recorder.requests) == 2
    assert waits == [1.0]


async def test_lower_max_retry_after_fails_immediately() -> None:
    # FR-018: the maximum honoured wait is configurable (Retry-After 7 > 5).
    provider, recorder, waits = make_provider(
        "error_429_retry_after", "chat_ok", retry=RetryPolicy(max_retry_after=5)
    )

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after == 7
    assert len(recorder.requests) == 1
    assert waits == []


async def test_higher_max_retry_after_honours_long_waits() -> None:
    provider, _, waits = make_provider(
        "error_429_retry_after_long", "chat_ok", retry=RetryPolicy(max_retry_after=300)
    )

    await _complete(provider)

    assert waits == [120.0]


async def test_retry_after_at_the_cap_is_honoured() -> None:
    # FR-017: "at most 60 s" is inclusive.
    response = httpx2.Response(429, headers={"Retry-After": "60"}, json={"message": "x"})
    provider, recorder, waits = make_provider(response, "chat_ok")

    await _complete(provider)

    assert waits == [60.0]
    assert len(recorder.requests) == 2


async def test_retry_after_just_above_the_cap_fails_immediately() -> None:
    response = httpx2.Response(429, headers={"Retry-After": "60.5"}, json={"message": "x"})
    provider, recorder, waits = make_provider(response, "chat_ok")

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after == 60.5
    assert len(recorder.requests) == 1
    assert waits == []


async def test_retry_after_http_date_above_the_cap_fails_immediately() -> None:
    response = httpx2.Response(
        429,
        headers={"Retry-After": format_datetime(NOW + timedelta(seconds=120), usegmt=True)},
        json={"message": "slow down"},
    )
    provider, recorder, waits = make_provider(response, "chat_ok", now=lambda: NOW)

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after == 120.0
    assert len(recorder.requests) == 1
    assert waits == []


async def test_retry_after_on_a_server_error_is_ignored() -> None:
    # FR-017 scopes Retry-After to 429; 5xx keeps the computed backoff.
    response = httpx2.Response(503, headers={"Retry-After": "120"}, json={"message": "busy"})
    provider, _, waits = make_provider(response, "chat_ok")

    await _complete(provider)

    assert waits == [1.0]


async def test_backoff_resumes_when_a_later_429_has_no_retry_after() -> None:
    provider, _, waits = make_provider("error_429_retry_after", "error_429", "chat_ok")

    await _complete(provider)

    assert waits == [7.0, 2.0]


async def test_lower_jitter_bound_shortens_the_wait() -> None:
    provider, _, waits = make_provider("error_503", "chat_ok", uniform=lambda a, b: a)

    await _complete(provider)

    assert waits == [0.75]


async def test_default_jitter_stays_within_25_percent() -> None:
    # FR-016 with the real random source (only the sleep is faked).
    recorder = Recorder(tuple(["error_503"] * 4))
    waits: list[float] = []

    async def record_sleep(seconds: float) -> None:
        waits.append(seconds)

    provider = MistralProvider(
        API_KEY, timeout_seconds=60, client_factory=recorder.client_factory, sleep=record_sleep
    )

    with pytest.raises(LLMUnavailableError):
        await _complete(provider)

    assert len(waits) == 3
    for wait, base in zip(waits, [1.0, 2.0, 4.0], strict=True):
        assert base * 0.75 <= wait <= base * 1.25


@pytest.mark.parametrize(
    ("repair_replies", "error"),
    [
        (["error_429"] * 4, LLMRateLimitError),
        (["error_503"] * 4, LLMUnavailableError),
        (["error_401"], LLMAuthError),
        (["error_400"], LLMInvalidRequestError),
        ([httpx2.ReadTimeout("x")], LLMUnavailableError),
    ],
    ids=["429", "503", "401", "400", "timeout"],
)
async def test_failing_repair_request_raises_the_typed_provider_error(
    repair_replies: list[Any], error: type[LLMError]
) -> None:
    # Edge case: a provider failure on the repair request is not an invalid-output error.
    provider, recorder, _ = make_provider("structured_invalid", *repair_replies)

    with pytest.raises(LLMError) as info:
        await _structured(provider)

    assert type(info.value) is error
    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert len(recorder.requests) == 1 + len(repair_replies)


@pytest.mark.parametrize(
    ("first", "error"),
    [("error_401", LLMAuthError), ("error_422", LLMInvalidRequestError)],
)
async def test_structured_first_request_failure_is_not_repaired(
    first: str, error: type[LLMError]
) -> None:
    provider, recorder, _ = make_provider(first, "structured_ok")

    with pytest.raises(error):
        await _structured(provider)

    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    "content",
    [
        '```json\n{"score": 0.5, "reason": "r"}\n```',
        '  \n```\n{"score": 0.5, "reason": "r"}\n```\n ',
        '\n  {"score": 0.5, "reason": "r"}  \n',
    ],
    ids=["json-fence", "bare-fence-whitespace", "whitespace"],
)
async def test_structured_answer_in_code_fence_or_whitespace_is_valid(content: str) -> None:
    # Edge case: handled by the shared repair parsing, no repair request.
    provider, recorder, _ = make_provider(_with_content(content))

    value, usage = await _structured(provider)

    assert value == Score(score=0.5, reason="r")
    assert usage.requests == 1
    assert len(recorder.requests) == 1


async def test_structured_requests_carry_model_temperature_and_schema() -> None:
    # US2-2 / FR-008: both the first and the repair request are schema-constrained.
    provider, recorder, _ = make_provider("structured_invalid", "structured_ok")

    await provider.complete_structured(SYSTEM, USER, Score, model=MODEL, temperature=0.7)

    first, repair = (r.body for r in recorder.requests)
    for body in (first, repair):
        assert body["model"] == MODEL
        assert body["temperature"] == 0.7
        assert body["response_format"]["type"] == "json_schema"
        assert body["response_format"]["json_schema"]["schema"] == _strict_schema(
            Score.model_json_schema()
        )
        assert body["messages"][0]["role"] == "system"
        assert body["messages"][0]["content"].startswith(SYSTEM)
    assert first["messages"][1]["content"] == USER
    assert repair["messages"][1]["content"].startswith(USER)


@pytest.mark.parametrize(
    "content",
    ['{"score": 7, "reason": "ANSWER-SENTINEL"}', "ANSWER-SENTINEL is not JSON"],
    ids=["out-of-range", "not-json"],
)
async def test_invalid_output_error_contains_no_answer_text(content: str) -> None:
    # FR-014 / SC-007 for the invalid-output error.
    provider, _, _ = make_provider(_with_content(content), _with_content(content))

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.errors
    for text in (str(info.value), info.value.errors, repr(info.value)):
        assert "ANSWER-SENTINEL" not in text
        assert "PROMPT-TEXT-SENTINEL" not in text


@pytest.mark.parametrize(
    ("replies", "timeout"),
    [
        (("error_401",), 60.0),
        (("error_403",), 60.0),
        (("error_400",), 60.0),
        (("error_404_model",), 60.0),
        (("error_422",), 60.0),
        (("error_429",) * 4, 60.0),
        (("error_429_retry_after_long",), 60.0),
        (("error_500",) * 4, 60.0),
        ((httpx2.ConnectError("x"),) * 4, 60.0),
        ((httpx2.ReadTimeout("x"),), 60.0),
        ((HANG,), 0.01),
        (("malformed_200",), 60.0),
        (("chat_empty_choices",), 60.0),
    ],
    ids=[
        "401",
        "403",
        "400",
        "404",
        "422",
        "429",
        "429-long",
        "500",
        "connect",
        "read-timeout",
        "deadline",
        "malformed",
        "empty-choices",
    ],
)
async def test_every_error_names_provider_and_model(
    replies: tuple[Any, ...], timeout: float
) -> None:
    # FR-014: "Every raised error MUST name the provider and model" (attributes and message).
    provider, _, _ = make_provider(*replies, timeout_seconds=timeout)

    with pytest.raises(LLMError) as info:
        await _complete(provider)

    message = str(info.value)
    assert (info.value.provider, info.value.model) == ("mistral", MODEL)
    assert MODEL in message
    assert API_KEY not in message
    assert "PROMPT-TEXT-SENTINEL" not in message
    assert "Hello" not in message


@pytest.mark.parametrize(
    "reply",
    ["error_401", "error_400", httpx2.ReadTimeout("x"), "error_429_retry_after_long"],
    ids=["auth", "invalid-request", "timeout", "retry-after-above-cap"],
)
async def test_non_retried_failures_log_no_retry_line(
    reply: Any, caplog: pytest.LogCaptureFixture
) -> None:
    # FR-019: a retry line only for an actual retry.
    provider, _, _ = make_provider(reply)

    with caplog.at_level(logging.WARNING, logger="invio.llm"), pytest.raises(LLMError):
        await _complete(provider)

    assert [r for r in caplog.records if r.getMessage() == "llm.retry"] == []


async def test_retry_log_reports_the_retry_after_wait_and_no_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _, _ = make_provider("error_429_retry_after", "chat_ok")

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.retry"]
    assert (record.status, record.wait_s) == (429, 7.0)  # type: ignore[attr-defined]
    dump = str([r.__dict__ for r in caplog.records])
    assert API_KEY not in dump
    assert "Hello" not in dump


@pytest.mark.parametrize("key", [None, " \t"], ids=["missing", "blank"])
def test_get_provider_without_usable_key_raises_auth_error(key: str | None) -> None:
    # FR-003 through the factory with the real registration.
    with pytest.raises(LLMAuthError, match="INVIO_MISTRAL_API_KEY"):
        factory.get_provider("mistral", make_settings(mistral_api_key=key))


def test_shipped_mistral_ids_carry_a_version_suffix() -> None:
    # FR-004: pinned (dated/versioned) ids only, not merely "no -latest".
    for model_id in _shipped_ids():
        assert re.fullmatch(r"[a-z0-9-]+-\d{4}", model_id), model_id


@pytest.mark.parametrize("module", ["base", "factory", "registry", "fake"])
def test_shared_llm_modules_do_not_reference_mistral(module: str) -> None:
    # US5-3 / FR-002: registration needs no edit of shared LLM code.
    source = (Path(invio.llm.__path__[0]) / f"{module}.py").read_text(encoding="utf-8")

    assert "mistral" not in source.lower()


def test_live_tests_carry_the_live_marker() -> None:
    # FR-025 / SC-005: conftest skips "live" tests unless "-m" selects them.
    from tests import test_llm_mistral_live

    for test in (
        test_llm_mistral_live.test_live_connectivity_check,
        test_llm_mistral_live.test_live_structured_output,
    ):
        assert [mark.name for mark in getattr(test, "pytestmark", [])] == ["live"]


async def test_default_clock_measures_an_http_date_from_now() -> None:
    # FR-017 with the real clock (no ``now`` injected); HTTP dates have 1 s resolution.
    when = format_datetime(datetime.now(UTC) + timedelta(seconds=30), usegmt=True)
    recorder = Recorder((httpx2.Response(429, headers={"Retry-After": when}, json={}), "chat_ok"))
    waits: list[float] = []

    async def record_sleep(seconds: float) -> None:
        waits.append(seconds)

    provider = MistralProvider(
        API_KEY, timeout_seconds=60, client_factory=recorder.client_factory, sleep=record_sleep
    )

    await _complete(provider)

    (wait,) = waits
    assert 25 < wait <= 30


@pytest.mark.parametrize(
    "header", ["0", "Sun, 04 Oct 2026 11:00:00 GMT"], ids=["zero", "past-date"]
)
async def test_retry_after_zero_or_past_date_retries_without_waiting(header: str) -> None:
    response = httpx2.Response(429, headers={"Retry-After": header}, json={"message": "x"})
    provider, _, waits = make_provider(response, "chat_ok", now=lambda: NOW)

    await _complete(provider)

    assert waits == [0.0]


@pytest.mark.parametrize("status", [600, 999])
def test_classify_status_above_5xx_is_bad_response(status: int) -> None:
    failure = _classify(_sdk_error(status, {"message": "odd"}), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("bad_response", False)
    assert isinstance(failure.error, LLMUnavailableError)


def test_safe_detail_replaces_control_characters() -> None:
    detail = _safe_detail(_sdk_error(400, {"message": "bad\x1b[31m\x00value\u2028here"}))

    assert detail == "bad [31m value here"
