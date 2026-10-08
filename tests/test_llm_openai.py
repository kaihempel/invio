"""Offline tests of the OpenAI provider (recorded HTTP fixtures, no network, no real key)."""

import asyncio
import json
import logging
import threading
import traceback
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import httpx
import openai
import pytest
import yaml
from pydantic import BaseModel

import invio.llm
from invio.config.job import JobConfig
from invio.llm import factory
from invio.llm.base import (
    LLMAuthError,
    LLMError,
    LLMInvalidOutputError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
    with_timeout,
)
from invio.llm.factory import _LoggedProvider
from invio.llm.http_retry import RetryPolicy
from invio.llm.openai import OpenAIProvider, _classify
from invio.llm.registry import default_registry
from invio.llm.retry import is_transient_llm
from tests.llm_helpers import Score, make_settings
from tests.openai_helpers import API_KEY, HANG, load_fixture, make_provider

SYSTEM = "You rate things."
USER = "PROMPT-TEXT-SENTINEL rate this"
MODEL = "gpt-4.1-mini-2025-04-14"
NOW = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


async def _complete(provider: OpenAIProvider) -> tuple[str, Usage]:
    return await provider.complete(SYSTEM, USER, model=MODEL, temperature=0.2, max_tokens=50)


async def _structured(provider: OpenAIProvider) -> tuple[Score, Usage]:
    return await provider.complete_structured(SYSTEM, USER, Score, model=MODEL, temperature=0.2)


def _with_text(text: str) -> httpx.Response:
    body = load_fixture("response_ok").json()
    body["output"][0]["content"][0]["text"] = text
    return httpx.Response(200, json=body)


# --- registration, settings -----------------------------------------------------------------


def test_from_settings_without_key_names_the_env_var() -> None:
    with pytest.raises(LLMAuthError, match="INVIO_OPENAI_API_KEY"):
        OpenAIProvider.from_settings(make_settings())


def test_from_settings_with_blank_key_is_missing() -> None:
    with pytest.raises(LLMAuthError, match="INVIO_OPENAI_API_KEY"):
        OpenAIProvider.from_settings(make_settings(openai_api_key="   "))


def test_from_settings_builds_provider_whose_repr_hides_the_key() -> None:
    provider = OpenAIProvider.from_settings(
        make_settings(openai_api_key=API_KEY, llm_timeout_seconds=5)
    )

    assert provider.timeout_seconds == 5
    assert API_KEY not in repr(provider)
    assert "OpenAIProvider" in repr(provider)


def test_provider_is_registered_by_real_discovery() -> None:
    factory._discover()

    assert factory._REGISTRY["openai"] is OpenAIProvider
    assert factory.get_provider("openai", make_settings(openai_api_key=API_KEY)) is not None


def test_constructing_the_provider_creates_no_http_client() -> None:
    _, recorder, _ = make_provider()

    assert recorder.clients_created == 0


# --- complete() -----------------------------------------------------------------------------


async def test_complete_returns_text_and_usage() -> None:
    provider, _, _ = make_provider("response_ok")

    text, usage = await _complete(provider)

    assert text == "Hello"
    assert usage == Usage(12, 3)
    assert usage.requests == 1


async def test_complete_sends_the_expected_stateless_request() -> None:
    provider, recorder, _ = make_provider("response_ok")

    await _complete(provider)

    (request,) = recorder.requests
    assert (request.method, request.path) == ("POST", "/v1/responses")
    assert request.has_authorization
    assert request.body["model"] == MODEL
    assert request.body["temperature"] == 0.2
    assert request.body["max_output_tokens"] == 50
    assert request.body["store"] is False
    assert request.body["input"] == [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": USER},
    ]
    assert "text" not in request.body


async def test_temperature_zero_is_sent_explicitly() -> None:
    provider, recorder, _ = make_provider("response_ok")

    await provider.complete(SYSTEM, USER, model=MODEL, temperature=0, max_tokens=5)

    assert recorder.requests[0].body["temperature"] == 0


@pytest.mark.parametrize(("requested", "sent"), [(1, 16), (5, 16), (16, 16), (17, 17)])
async def test_output_limit_is_raised_to_the_api_minimum(requested: int, sent: int) -> None:
    provider, recorder, _ = make_provider("response_ok")

    await provider.complete(SYSTEM, USER, model=MODEL, temperature=0, max_tokens=requested)

    assert recorder.requests[0].body["max_output_tokens"] == sent


async def test_complete_without_usage_reports_zero_tokens() -> None:
    provider, _, _ = make_provider("response_no_usage")

    text, usage = await _complete(provider)

    assert text == "Hello"
    assert usage == Usage(0, 0)


async def test_complete_concatenates_text_parts() -> None:
    body = load_fixture("response_ok").json()
    body["output"][0]["content"] = [
        {"type": "output_text", "text": "Hel", "annotations": []},
        {"type": "output_text", "text": "lo", "annotations": []},
    ]
    provider, _, _ = make_provider(httpx.Response(200, json=body))

    text, _ = await _complete(provider)

    assert text == "Hello"


@pytest.mark.parametrize(
    ("fixture", "leak"),
    [
        ("response_empty", ""),
        ("response_refusal", "REFUSAL-TEXT-SENTINEL"),
        ("response_incomplete", "PARTIAL-ANSWER-SENTINEL"),
        ("malformed_200", ""),
    ],
)
async def test_unusable_answers_are_unavailable_and_not_leaked(fixture: str, leak: str) -> None:
    provider, recorder, waits = make_provider(fixture)

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("openai", MODEL)
    assert MODEL in str(info.value)
    if leak:
        assert leak not in str(info.value)
        assert leak not in repr(info.value)
    assert len(recorder.requests) == 1
    assert waits == []


async def test_output_without_any_message_is_unavailable() -> None:
    body = load_fixture("response_ok").json()
    body["output"] = []
    provider, _, _ = make_provider(httpx.Response(200, json=body))

    with pytest.raises(LLMUnavailableError):
        await _complete(provider)


async def test_failed_status_is_unavailable() -> None:
    body = load_fixture("response_ok").json()
    body["status"] = "failed"
    provider, _, _ = make_provider(httpx.Response(200, json=body))

    with pytest.raises(LLMUnavailableError, match="failed"):
        await _complete(provider)


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        ({"input_tokens": 5}, Usage(5, 0)),
        ({"output_tokens": 4}, Usage(0, 4)),
    ],
    ids=["no-output-count", "no-input-count"],
)
async def test_one_missing_token_count_is_zero(usage: dict[str, int], expected: Usage) -> None:
    body = load_fixture("response_ok").json()
    body["usage"] = usage
    provider, _, _ = make_provider(httpx.Response(200, json=body))

    _, got = await _complete(provider)

    assert got == expected


# --- structured output ----------------------------------------------------------------------


async def test_structured_returns_validated_value() -> None:
    provider, _, _ = make_provider("structured_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert usage == Usage(12, 3)


async def test_structured_request_carries_strict_json_schema() -> None:
    provider, recorder, _ = make_provider("structured_ok")

    await _structured(provider)

    (request,) = recorder.requests
    schema = {**Score.model_json_schema(), "additionalProperties": False}
    assert request.body["text"] == {
        "format": {"type": "json_schema", "name": "Score", "schema": schema, "strict": True}
    }
    assert request.body["store"] is False
    assert "max_output_tokens" not in request.body


class _Inner(BaseModel):
    label: str
    optional: str | None = None


class _Outer(BaseModel):
    inner: _Inner
    items: list[_Inner]
    note: str | None = None


async def test_structured_schema_is_closed_and_requires_every_property() -> None:
    provider, recorder, _ = make_provider(_with_text('{"inner": {"label": "a"}, "items": []}'))

    value, _ = await provider.complete_structured(SYSTEM, USER, _Outer, model=MODEL, temperature=0)

    assert value.inner.label == "a"
    schema = recorder.requests[0].body["text"]["format"]["schema"]
    for node in (schema, schema["$defs"]["_Inner"]):
        assert node["additionalProperties"] is False
        assert node["required"] == list(node["properties"])
    assert schema["required"] == ["inner", "items", "note"]
    assert schema["$defs"]["_Inner"]["required"] == ["label", "optional"]


class Page[Item](BaseModel):
    items: list[Item]


async def test_structured_schema_name_is_sanitized_for_generic_models() -> None:
    provider, recorder, _ = make_provider(_with_text('{"items": [1]}'))

    await provider.complete_structured(SYSTEM, USER, Page[int], model=MODEL, temperature=0)

    name = recorder.requests[0].body["text"]["format"]["name"]
    assert name == "Page_int_"


async def test_structured_schema_name_is_limited_to_64_characters() -> None:
    long_model = type("N" * 100, (BaseModel,), {"__annotations__": {"a": int}})
    provider, recorder, _ = make_provider(_with_text('{"a": 1}'))

    await provider.complete_structured(SYSTEM, USER, long_model, model=MODEL, temperature=0)

    assert recorder.requests[0].body["text"]["format"]["name"] == "N" * 64


async def test_structured_repairs_once() -> None:
    provider, recorder, _ = make_provider("structured_invalid", "structured_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert len(recorder.requests) == 2
    repair_user = recorder.requests[1].body["input"][1]["content"]
    assert "score" in repair_user
    assert "invalid" in repair_user.lower()
    assert usage == Usage(24, 6, 2)
    for request in recorder.requests:
        assert request.body["text"]["format"]["strict"] is True


async def test_structured_invalid_twice_raises_invalid_output_with_summed_usage() -> None:
    provider, recorder, _ = make_provider("structured_invalid", "structured_invalid")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.usage == Usage(24, 6, 2)
    assert (info.value.provider, info.value.model) == ("openai", MODEL)
    assert len(recorder.requests) == 2


@pytest.mark.parametrize("fixture", ["response_empty", "response_refusal", "response_incomplete"])
async def test_structured_unusable_answer_is_unavailable_without_repair(fixture: str) -> None:
    provider, recorder, _ = make_provider(fixture)

    with pytest.raises(LLMUnavailableError):
        await _structured(provider)

    assert len(recorder.requests) == 1


async def test_invalid_output_error_contains_no_answer_text() -> None:
    answer = '{"score": 7, "reason": "ANSWER-SENTINEL"}'
    provider, _, _ = make_provider(_with_text(answer), _with_text(answer))

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    for text in (str(info.value), info.value.errors, repr(info.value)):
        assert "ANSWER-SENTINEL" not in text
        assert "PROMPT-TEXT-SENTINEL" not in text


# --- client lifecycle -----------------------------------------------------------------------


def test_provider_survives_successive_event_loops() -> None:
    provider, recorder, _ = make_provider("response_ok", "response_ok")

    first = asyncio.run(_complete(provider))
    second = asyncio.run(_complete(provider))

    assert first[0] == second[0] == "Hello"
    assert recorder.clients_created == 2


async def test_provider_reuses_the_client_within_one_loop() -> None:
    provider, recorder, _ = make_provider("response_ok", "response_ok")

    await _complete(provider)
    await _complete(provider)

    assert recorder.clients_created == 1


async def test_concurrent_first_calls_share_one_client() -> None:
    provider, recorder, _ = make_provider("response_ok", "response_ok", "response_ok")

    await asyncio.gather(*(_complete(provider) for _ in range(3)))

    assert recorder.clients_created == 1


async def test_aclose_closes_the_client_of_the_running_loop() -> None:
    provider, recorder, _ = make_provider("response_ok", "response_ok")
    await _complete(provider)

    await provider.aclose()
    await provider.aclose()  # a second close is a no-op

    assert recorder.clients[0].is_closed
    await _complete(provider)
    assert recorder.clients_created == 2
    await provider.aclose()


async def test_aclose_before_any_call_is_a_no_op() -> None:
    provider, recorder, _ = make_provider()

    await provider.aclose()

    assert recorder.clients_created == 0


def test_threads_with_their_own_loops_keep_their_own_clients() -> None:
    provider, recorder, _ = make_provider(*["response_ok"] * 4)
    barrier = threading.Barrier(2, timeout=5)
    failures: list[BaseException] = []
    results: list[str] = []

    async def two_calls() -> None:
        results.append((await _complete(provider))[0])
        await asyncio.to_thread(barrier.wait)
        results.append((await _complete(provider))[0])

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
    assert results == ["Hello"] * 4
    assert len(recorder.requests) == 4
    assert recorder.clients_created == 2


# --- registry and logging -------------------------------------------------------------------


def _shipped_ids() -> list[str]:
    path = Path(invio.llm.__path__[0]) / "models.d" / "openai.yaml"
    return list(yaml.safe_load(path.read_text(encoding="utf-8"))["models"])


def test_shipped_registry_has_two_priced_pinned_models() -> None:
    models = default_registry().models_for("openai")

    assert len(models) >= 2
    for info in models:
        assert info.input_price_per_mtok >= 0
        assert info.output_price_per_mtok >= 0
        assert info.context_window > 0
        assert not info.model_id.endswith("-latest")


def test_shipped_models_resolve_for_both_roles(job_data: dict[str, Any]) -> None:
    fast, smart = _shipped_ids()[:2]
    job_data["llm"] = {"provider": "openai", "models": {"fast": fast, "smart": smart}}
    llm = JobConfig.model_validate(job_data).llm
    factory._discover()

    provider, model = factory.resolve(llm, "fast", make_settings(openai_api_key="sk-test"))
    _, smart_model = factory.resolve(llm, "smart", make_settings(openai_api_key="sk-test"))

    assert provider is not None
    assert (model, smart_model) == (fast, smart)


async def test_successful_call_logs_one_llm_call_record(caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = make_provider("response_ok")
    logged = _LoggedProvider(provider, name="openai", registry=default_registry())

    with caplog.at_level(logging.INFO, logger="invio.llm"):
        await logged.complete(SYSTEM, USER, model=MODEL, temperature=0.2, max_tokens=50)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.call"]
    assert record.provider == "openai"  # type: ignore[attr-defined]
    assert record.model == MODEL  # type: ignore[attr-defined]
    assert (record.input_tokens, record.output_tokens) == (12, 3)  # type: ignore[attr-defined]
    assert record.cost_usd is not None  # type: ignore[attr-defined]
    dump = str([r.__dict__ for r in caplog.records])
    assert API_KEY not in dump
    assert "PROMPT-TEXT-SENTINEL" not in dump


def test_llm_package_docstring_lists_openai() -> None:
    assert "openai" in (invio.llm.__doc__ or "")


# --- error mapping --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["error_401", "error_403"])
async def test_auth_failures_are_not_retried(fixture: str) -> None:
    provider, recorder, waits = make_provider(fixture)

    with pytest.raises(LLMAuthError, match="INVIO_OPENAI_API_KEY") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("openai", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    ("fixture", "status", "text"),
    [
        ("error_400", 400, "Invalid value for 'temperature'."),
        ("error_404_model", 404, "does not exist or you do not have access"),
        ("error_422", 422, "Unprocessable entity"),
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
    assert (info.value.provider, info.value.model) == ("openai", MODEL)
    assert str(status) in message
    assert text in message
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize("status", [405, 409, 413, 418, 451])
async def test_any_other_4xx_is_an_invalid_request_without_retry(status: int) -> None:
    reply = httpx.Response(status, json={"error": {"message": "nope"}})
    provider, recorder, _ = make_provider(reply, "response_ok")

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert info.value.status == status
    assert len(recorder.requests) == 1


async def test_invalid_request_with_non_json_body_still_names_the_status() -> None:
    reply = httpx.Response(400, content=b"<html>BODY-LEAK</html>")
    provider, _, _ = make_provider(reply)

    with pytest.raises(LLMInvalidRequestError, match="HTTP 400") as info:
        await _complete(provider)

    assert "BODY-LEAK" not in str(info.value)


async def test_error_message_is_bounded_and_free_of_the_response_body() -> None:
    huge = httpx.Response(
        400, json={"error": {"message": "x" * 10_000, "param": "BODY-LEAK"}, "extra": "BODY-LEAK"}
    )
    provider, _, _ = make_provider(huge)

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert len(str(info.value)) <= 400
    assert "x" * 301 not in str(info.value)
    assert "BODY-LEAK" not in str(info.value)


async def test_error_message_collapses_whitespace_and_control_characters() -> None:
    reply = httpx.Response(400, json={"error": {"message": "bad\n\n  thing\x00here"}})
    provider, _, _ = make_provider(reply)

    with pytest.raises(LLMInvalidRequestError, match=r"bad thing here"):
        await _complete(provider)


async def test_deadline_is_unavailable_and_not_retried() -> None:
    provider, recorder, waits = make_provider(HANG, timeout_seconds=0.5)

    with pytest.raises(LLMUnavailableError, match=r"did not answer within 0\.5 s"):
        await _complete(provider)

    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectTimeout("x"),
        httpx.ReadTimeout("x"),
        httpx.WriteTimeout("x"),
        httpx.PoolTimeout("x"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_every_transport_timeout_is_not_retried(failure: Exception) -> None:
    provider, recorder, waits = make_provider(failure, "response_ok")

    with pytest.raises(LLMUnavailableError, match="timed out") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("openai", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


async def test_timeout_after_a_retry_is_not_retried_again() -> None:
    provider, recorder, waits = make_provider("error_503", httpx.ReadTimeout("x"), "response_ok")

    with pytest.raises(LLMUnavailableError, match="timed out"):
        await _complete(provider)

    assert len(recorder.requests) == 2
    assert waits == [1.0]


async def test_timeout_bounds_each_attempt_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    bounds: list[float] = []

    async def recording_with_timeout(awaitable: Any, *, seconds: float, **kwargs: Any) -> Any:
        bounds.append(seconds)
        return await with_timeout(awaitable, seconds=seconds, **kwargs)

    monkeypatch.setattr("invio.llm.openai.with_timeout", recording_with_timeout)
    provider, _, _ = make_provider("error_429", "error_503", "response_ok", timeout_seconds=7.0)

    await _complete(provider)

    assert bounds == [7.0, 7.0, 7.0]


async def test_malformed_200_is_unavailable_without_retry() -> None:
    provider, recorder, waits = make_provider("malformed_200", "response_ok")

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("openai", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, content=b"<html>BODY-LEAK</html>"),
        httpx.Response(200, headers={"content-type": "application/json"}, content=b"{not json"),
        httpx.Response(200, headers={"content-type": "application/json"}, content=b"[1, 2]"),
    ],
    ids=["html", "broken-json", "json-array"],
)
async def test_non_object_success_body_is_unavailable_without_retry(reply: httpx.Response) -> None:
    provider, recorder, waits = make_provider(reply, "response_ok")

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert "BODY-LEAK" not in str(info.value)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    "reply",
    [
        "error_401",
        "error_422",
        "error_429",
        "error_429_insufficient_quota",
        "error_503",
        "malformed_200",
        "response_refusal",
        httpx.ConnectError("boom SECRET-IN-EXC"),
    ],
    ids=["401", "422", "429", "quota", "503", "malformed", "refusal", "connect"],
)
async def test_errors_never_expose_key_or_prompt_and_are_not_chained(reply: Any) -> None:
    provider, _, _ = make_provider(*([reply] * 4), retry=RetryPolicy(max_retries=3))

    with pytest.raises(LLMError) as info:
        await _complete(provider)

    chain = "".join(traceback.format_exception(info.value, chain=True))
    for text in (str(info.value), repr(info.value), repr(provider), chain):
        assert API_KEY not in text
        assert "PROMPT-TEXT-SENTINEL" not in text
        assert "SECRET-IN-EXC" not in text
        assert "REFUSAL-TEXT-SENTINEL" not in text
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


async def test_structured_invalid_output_has_no_context() -> None:
    provider, _, _ = make_provider("structured_invalid", "structured_invalid")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.__context__ is None


# --- rate limits and retries ----------------------------------------------------------------


async def test_rate_limit_is_retried_with_exponential_backoff() -> None:
    provider, recorder, waits = make_provider(*["error_429"] * 4)

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert len(recorder.requests) == 4
    assert waits == [1.0, 2.0, 4.0]
    assert info.value.retry_after is None
    assert (info.value.provider, info.value.model) == ("openai", MODEL)
    assert "Rate limit reached" in str(info.value)


async def test_server_error_exhausts_retries_naming_the_status() -> None:
    provider, recorder, _ = make_provider(*["error_503"] * 4)

    with pytest.raises(LLMUnavailableError, match="503"):
        await _complete(provider)

    assert len(recorder.requests) == 4


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
async def test_every_5xx_status_exhausts_retries(status: int) -> None:
    provider, recorder, _ = make_provider(*[httpx.Response(status, text="<html>oops</html>")] * 4)

    with pytest.raises(LLMUnavailableError, match=str(status)):
        await _complete(provider)

    assert len(recorder.requests) == 4


async def test_connection_failure_exhausts_retries() -> None:
    provider, recorder, waits = make_provider(*[httpx.ConnectError("x")] * 4)

    with pytest.raises(LLMUnavailableError, match="connection failed"):
        await _complete(provider)

    assert len(recorder.requests) == 4
    assert waits == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("x"), httpx.ReadError("x"), httpx.RemoteProtocolError("x")],
    ids=lambda e: type(e).__name__,
)
async def test_connection_failure_once_then_success(failure: Exception) -> None:
    provider, _, waits = make_provider(failure, "response_ok")

    text, _ = await _complete(provider)

    assert text == "Hello"
    assert waits == [1.0]


async def test_rate_limit_then_success() -> None:
    provider, recorder, waits = make_provider("error_429", "response_ok")

    text, usage = await _complete(provider)

    assert (text, usage.requests) == ("Hello", 1)
    assert waits == [1.0]
    assert len(recorder.requests) == 2


async def test_server_error_then_success() -> None:
    provider, _, waits = make_provider("error_500", "response_ok")

    await _complete(provider)

    assert waits == [1.0]


async def test_retry_after_header_sets_the_wait() -> None:
    provider, _, waits = make_provider("error_429_retry_after", "response_ok")

    await _complete(provider)

    assert waits == [7.0]


async def test_retry_after_above_the_cap_fails_immediately() -> None:
    provider, recorder, waits = make_provider("error_429_retry_after_long", "response_ok")

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after == 3600
    assert len(recorder.requests) == 1
    assert waits == []


async def test_final_rate_limit_error_keeps_retry_after() -> None:
    provider, recorder, waits = make_provider(*["error_429_retry_after"] * 4)

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after == 7
    assert len(recorder.requests) == 4
    assert waits == [7.0, 7.0, 7.0]


async def test_retry_after_at_and_above_the_cap() -> None:
    at_cap = httpx.Response(429, headers={"retry-after": "5"}, json={"error": {"message": "x"}})
    provider, _, waits = make_provider(at_cap, "response_ok", retry=RetryPolicy(max_retry_after=5))
    await _complete(provider)
    assert waits == [5.0]

    over = httpx.Response(429, headers={"retry-after": "6"}, json={"error": {"message": "x"}})
    provider, recorder, waits = make_provider(
        over, "response_ok", retry=RetryPolicy(max_retry_after=5)
    )
    with pytest.raises(LLMRateLimitError):
        await _complete(provider)
    assert (len(recorder.requests), waits) == (1, [])


async def test_retry_after_http_date_is_relative_to_now() -> None:
    reply = httpx.Response(
        429,
        headers={"Retry-After": format_datetime(NOW + timedelta(seconds=10), usegmt=True)},
        json={"error": {"message": "slow down"}},
    )
    provider, _, waits = make_provider(reply, "response_ok", now=lambda: NOW)

    await _complete(provider)

    assert waits == [10.0]


async def test_unparseable_retry_after_falls_back_to_backoff() -> None:
    reply = httpx.Response(429, headers={"Retry-After": "soon"}, json={"error": {"message": "x"}})
    provider, _, waits = make_provider(reply, "response_ok")

    await _complete(provider)

    assert waits == [1.0]


async def test_overflowing_retry_after_fails_immediately() -> None:
    reply = httpx.Response(429, headers={"Retry-After": "9" * 400}, content=b"{}")
    provider, recorder, waits = make_provider(reply, "response_ok")

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after is None
    assert len(recorder.requests) == 1
    assert waits == []


async def test_retry_after_on_a_server_error_is_ignored() -> None:
    reply = httpx.Response(
        503, headers={"Retry-After": "30"}, json={"error": {"message": "overloaded"}}
    )
    provider, _, waits = make_provider(reply, "response_ok")

    await _complete(provider)

    assert waits == [1.0]


async def test_jitter_scales_the_wait_and_uses_the_policy_range() -> None:
    bounds: list[tuple[float, float]] = []

    def uniform(a: float, b: float) -> float:
        bounds.append((a, b))
        return b

    provider, _, waits = make_provider(
        "error_503", "response_ok", retry=RetryPolicy(jitter=0.5), uniform=uniform
    )

    await _complete(provider)

    assert bounds == [(-0.5, 0.5)]
    assert waits == [1.5]


async def test_retry_policy_is_configurable() -> None:
    provider, recorder, waits = make_provider(
        *["error_429"] * 2, retry=RetryPolicy(max_retries=1, base_delay=0.5)
    )

    with pytest.raises(LLMRateLimitError):
        await _complete(provider)

    assert len(recorder.requests) == 2
    assert waits == [0.5]


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


async def test_cancellation_during_the_retry_wait_stops_the_request() -> None:
    entered = asyncio.Event()
    never = asyncio.Event()

    async def blocking_sleep(seconds: float) -> None:
        entered.set()
        await never.wait()

    provider, recorder, _ = make_provider("error_503", "response_ok", sleep=blocking_sleep)
    task = asyncio.ensure_future(_complete(provider))
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(recorder.requests) == 1


# --- insufficient_quota ---------------------------------------------------------------------


async def test_insufficient_quota_is_a_non_retryable_rate_limit_error() -> None:
    provider, recorder, waits = make_provider("error_429_insufficient_quota", "response_ok")

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after is None
    assert (info.value.provider, info.value.model) == ("openai", MODEL)
    assert "quota" in str(info.value)
    assert len(recorder.requests) == 1
    assert waits == []


async def test_insufficient_quota_ignores_a_retry_after_header() -> None:
    reply = httpx.Response(
        429,
        headers={"Retry-After": "2"},
        json={"error": {"message": "m", "type": "insufficient_quota", "code": None}},
    )
    provider, recorder, waits = make_provider(reply, "response_ok")

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after is None
    assert (len(recorder.requests), waits) == (1, [])


# --- logging --------------------------------------------------------------------------------


async def test_each_retry_logs_one_warning_without_prompt_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _, _ = make_provider("error_429", "error_503", "response_ok")

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    records = [r for r in caplog.records if r.getMessage() == "llm.retry"]
    assert len(records) == 2
    first, second = records
    assert (first.provider, first.model, first.attempt) == ("openai", MODEL, 1)  # type: ignore[attr-defined]
    assert (first.status, first.failure, first.wait_s) == (429, "rate_limit", 1.0)  # type: ignore[attr-defined]
    assert (second.attempt, second.status, second.failure) == (2, 503, "server")  # type: ignore[attr-defined]
    assert second.wait_s == 2.0  # type: ignore[attr-defined]
    dump = str([r.__dict__ for r in caplog.records])
    assert "PROMPT-TEXT-SENTINEL" not in dump
    assert API_KEY not in dump
    assert all(r.exc_info is None for r in records)


async def test_connection_retry_logs_failure_kind(caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = make_provider(httpx.ConnectError("x"), "response_ok")

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.retry"]
    assert record.status is None  # type: ignore[attr-defined]
    assert record.failure == "connection"  # type: ignore[attr-defined]


async def test_retry_log_reports_the_retry_after_wait(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _, _ = make_provider("error_429_retry_after", "response_ok")

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.retry"]
    assert (record.status, record.wait_s) == (429, 7.0)  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "reply",
    ["error_401", "error_400", httpx.ReadTimeout("x"), "error_429_retry_after_long"],
    ids=["auth", "invalid-request", "timeout", "retry-after-above-cap"],
)
async def test_non_retried_failures_log_no_retry_line(
    reply: Any, caplog: pytest.LogCaptureFixture
) -> None:
    provider, _, _ = make_provider(reply)

    with caplog.at_level(logging.WARNING, logger="invio.llm"), pytest.raises(LLMError):
        await _complete(provider)

    assert [r for r in caplog.records if r.getMessage() == "llm.retry"] == []


@pytest.mark.parametrize(
    ("replies", "timeout"),
    [
        (("error_401",), 60.0),
        (("error_403",), 60.0),
        (("error_400",), 60.0),
        (("error_404_model",), 60.0),
        (("error_422",), 60.0),
        (("error_429",) * 4, 60.0),
        (("error_429_insufficient_quota",), 60.0),
        (("error_500",) * 4, 60.0),
        ((httpx.ConnectError("x"),) * 4, 60.0),
        ((httpx.ReadTimeout("x"),), 60.0),
        ((HANG,), 0.01),
        (("malformed_200",), 60.0),
        (("response_empty",), 60.0),
        (("response_refusal",), 60.0),
        (("response_incomplete",), 60.0),
    ],
    ids=[
        "401",
        "403",
        "400",
        "404",
        "422",
        "429",
        "quota",
        "500",
        "connect",
        "read-timeout",
        "deadline",
        "malformed",
        "empty",
        "refusal",
        "incomplete",
    ],
)
async def test_every_error_names_provider_and_model(
    replies: tuple[Any, ...], timeout: float
) -> None:
    provider, _, _ = make_provider(*replies, timeout_seconds=timeout)

    with pytest.raises(LLMError) as info:
        await _complete(provider)

    message = str(info.value)
    assert (info.value.provider, info.value.model) == ("openai", MODEL)
    assert MODEL in message
    assert API_KEY not in message
    assert "PROMPT-TEXT-SENTINEL" not in message


# --- client configuration -------------------------------------------------------------------


async def test_base_url_env_variable_cannot_redirect_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_BASE_URL", "https://evil.example/v1")
    provider, recorder, _ = make_provider("response_ok")

    await _complete(provider)

    assert recorder.requests[0].host == "api.openai.test"


async def test_sdk_retries_are_disabled_and_timeout_has_a_margin() -> None:
    provider, _, _ = make_provider("response_ok", timeout_seconds=10.0)
    await _complete(provider)

    sdk_client, _ = next(iter(provider._clients.values()))

    assert sdk_client.max_retries == 0
    assert sdk_client.timeout == 15.0
    assert str(sdk_client.base_url).rstrip("/") == "https://api.openai.test/v1"


def test_default_base_url_is_the_openai_api() -> None:
    provider = OpenAIProvider(API_KEY, timeout_seconds=5)

    assert provider._base_url == "https://api.openai.com/v1"


# --- helpers: _classify ---------------------------------------------------------------------


def _status_error(status: int, body: object, *, headers: dict[str, str] | None = None) -> Any:
    request = httpx.Request("POST", "https://api.openai.test/v1/responses")
    response = httpx.Response(status, request=request, headers=headers or {})
    return openai.APIStatusError("SDK-MESSAGE-LEAK raw body", response=response, body=body)


def _request() -> httpx.Request:
    return httpx.Request("POST", "https://api.openai.test/v1/responses")


def test_classify_other_exception_returns_none() -> None:
    assert _classify(RuntimeError("x"), MODEL, lambda: NOW) is None


def test_classify_timeout_is_checked_before_connection_error() -> None:
    failure = _classify(openai.APITimeoutError(_request()), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("timeout", False)


def test_classify_connection_error_without_cause_is_retryable() -> None:
    failure = _classify(openai.APIConnectionError(request=_request()), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("connection", True)


@pytest.mark.parametrize(
    ("cause", "kind", "retryable"),
    [
        (httpx.InvalidURL("x"), "unsendable", False),
        (httpx.UnsupportedProtocol("x"), "unsendable", False),
        (httpx.LocalProtocolError("x"), "unsendable", False),
        (httpx.DecodingError("x"), "bad_response", False),
        (httpx.TooManyRedirects("x"), "bad_response", False),
        (httpx.StreamConsumed(), "bad_response", False),
        (httpx.ConnectError("x"), "connection", True),
        (httpx.RemoteProtocolError("x"), "connection", True),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else "",
)
def test_classify_connection_error_by_cause(cause: Exception, kind: str, retryable: bool) -> None:
    exc = openai.APIConnectionError(request=_request())
    exc.__cause__ = cause

    failure = _classify(exc, MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == (kind, retryable)
    assert type(cause).__name__ in str(failure.error)


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (httpx.InvalidURL("x"), "unsendable"),
        (httpx.ReadTimeout("x"), "timeout"),
        (httpx.ConnectError("x"), "connection"),
        (httpx.DecodingError("x"), "bad_response"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else "",
)
def test_classify_raw_httpx_errors(raw: Exception, kind: str) -> None:
    failure = _classify(raw, MODEL, lambda: NOW)

    assert failure is not None
    assert failure.kind == kind


def test_classify_response_validation_and_json_errors_are_not_retryable() -> None:
    response = httpx.Response(200, request=_request())
    for exc in (
        openai.APIResponseValidationError(response, None),
        json.JSONDecodeError("x", "doc", 0),
    ):
        failure = _classify(exc, MODEL, lambda: NOW)
        assert failure is not None
        assert (failure.kind, failure.retryable) == ("bad_response", False)


@pytest.mark.parametrize("status", [100, 301, 304])
def test_classify_non_error_status_is_bad_response(status: int) -> None:
    failure = _classify(_status_error(status, {"message": "odd"}), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable, failure.status) == ("bad_response", False, status)


def test_classify_status_below_600_above_5xx_is_server_error() -> None:
    failure = _classify(_status_error(599, None), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("server", True)


def test_classify_other_4xx_is_invalid_request_and_never_uses_the_sdk_message() -> None:
    failure = _classify(_status_error(409, {"message": "conflict"}), MODEL, lambda: NOW)

    assert failure is not None
    assert isinstance(failure.error, LLMInvalidRequestError)
    assert failure.error.status == 409
    assert "conflict" in str(failure.error)
    assert "SDK-MESSAGE-LEAK" not in str(failure.error)


@pytest.mark.parametrize("body", [None, "plain text", ["a"], {"message": 5}, {"other": "x"}])
def test_classify_status_with_unusable_body_has_no_detail(body: object) -> None:
    failure = _classify(_status_error(400, body), MODEL, lambda: NOW)

    assert failure is not None
    assert str(failure.error) == f"OpenAI rejected the request (HTTP 400, model {MODEL})"


def test_classify_quota_is_detected_by_type_or_code() -> None:
    by_type = _status_error(429, {"message": "m", "type": "insufficient_quota"})
    by_code = _status_error(429, {"message": "m", "code": "insufficient_quota"})
    plain = _status_error(429, {"message": "m", "code": "rate_limit_exceeded"})

    kinds = [_classify(e, MODEL, lambda: NOW) for e in (by_type, by_code, plain)]

    assert [f.kind for f in kinds if f is not None] == ["quota", "quota", "rate_limit"]


def test_retry_after_is_read_from_lowercase_sdk_headers() -> None:
    exc = _status_error(429, {"message": "m"}, headers={"retry-after": "4"})

    failure = _classify(exc, MODEL, lambda: NOW)

    assert failure is not None
    assert failure.retry_after == 4.0


def test_graph_layer_still_retries_quota_errors() -> None:
    # Known limitation (see the module docstring): the outer, graph-level retry treats every
    # LLMRateLimitError as transient, including insufficient_quota.
    failure = _classify(_status_error(429, {"type": "insufficient_quota"}), MODEL, lambda: NOW)

    assert failure is not None
    assert is_transient_llm(failure.error)
