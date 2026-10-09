"""Offline tests of the Anthropic provider (recorded HTTP fixtures, no network, no real key)."""

import asyncio
import json
import logging
import threading
import traceback
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import anthropic
import httpx2
import pytest
from pydantic import BaseModel

import invio.llm
from invio.config.job import JobConfig
from invio.llm import factory
from invio.llm.anthropic import AnthropicProvider, _classify
from invio.llm.base import (
    LLMAuthError,
    LLMConfigError,
    LLMError,
    LLMInvalidOutputError,
    LLMInvalidRequestError,
    LLMQuotaError,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
    with_timeout,
)
from invio.llm.factory import _LoggedProvider
from invio.llm.http_retry import RetryPolicy
from invio.llm.registry import default_registry, load_registry
from invio.llm.retry import is_transient_llm
from tests.anthropic_helpers import API_KEY, BASE_URL, HANG, make_provider
from tests.llm_helpers import Score, make_settings, write_registry

SYSTEM = "You rate things."
USER = "PROMPT-TEXT-SENTINEL rate this"
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
MODEL = HAIKU
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


async def _complete(provider: AnthropicProvider) -> tuple[str, Usage]:
    return await provider.complete(SYSTEM, USER, model=MODEL, temperature=0.2, max_tokens=50)


async def _structured(provider: AnthropicProvider, model: str = MODEL) -> tuple[Score, Usage]:
    return await provider.complete_structured(SYSTEM, USER, Score, model=model, temperature=0.2)


def _text_reply(text: str, **overrides: Any) -> httpx2.Response:
    body = {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": MODEL,
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 12, "output_tokens": 3},
        **overrides,
    }
    return httpx2.Response(200, json=body)


def _tool_reply(tool_input: dict[str, Any], name: str = "Score") -> httpx2.Response:
    block = {"type": "tool_use", "id": "toolu_1", "name": name, "input": tool_input}
    return _text_reply("", content=[block], stop_reason="tool_use")


# --- registration, settings -----------------------------------------------------------------


def test_from_settings_without_key_names_the_env_var() -> None:
    with pytest.raises(LLMAuthError, match="INVIO_ANTHROPIC_API_KEY"):
        AnthropicProvider.from_settings(make_settings())


def test_from_settings_with_blank_key_is_missing() -> None:
    with pytest.raises(LLMAuthError, match="INVIO_ANTHROPIC_API_KEY"):
        AnthropicProvider.from_settings(make_settings(anthropic_api_key="   "))


def test_from_settings_builds_provider_whose_repr_hides_the_key() -> None:
    provider = AnthropicProvider.from_settings(
        make_settings(anthropic_api_key=API_KEY, llm_timeout_seconds=5)
    )

    assert provider.timeout_seconds == 5
    assert API_KEY not in repr(provider)
    assert "AnthropicProvider" in repr(provider)


def test_from_settings_uses_the_default_registry_at_call_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    lacking = load_registry(
        [
            write_registry(
                tmp_path,
                "anthropic",
                {
                    "m": {
                        "input_price_per_mtok": 1,
                        "output_price_per_mtok": 1,
                        "context_window": 10,
                    }
                },
            )
        ]
    )
    monkeypatch.setattr("invio.llm.registry.default_registry", lambda: lacking)

    with pytest.raises(LLMConfigError, match="max_output_tokens"):
        AnthropicProvider.from_settings(make_settings(anthropic_api_key=API_KEY))


def test_provider_is_registered_by_real_discovery() -> None:
    factory._discover()

    assert factory._REGISTRY["anthropic"] is AnthropicProvider
    assert factory.get_provider("anthropic", make_settings(anthropic_api_key=API_KEY)) is not None


def test_constructing_the_provider_creates_no_http_client() -> None:
    _, recorder, _ = make_provider()

    assert recorder.clients_created == 0


def test_provider_without_max_output_tokens_in_registry_is_a_config_error(
    tmp_path: Path,
) -> None:
    entry = {"input_price_per_mtok": 1, "output_price_per_mtok": 1, "context_window": 10}
    registry = load_registry([write_registry(tmp_path, "anthropic", {"m": entry})])

    with pytest.raises(LLMConfigError, match="max_output_tokens"):
        make_provider(registry=registry)


def test_factory_built_provider_reads_limits_from_the_default_registry(tmp_path: Path) -> None:
    """Research R5: ``get_provider(registry=...)`` does not forward the registry."""
    entry = {"input_price_per_mtok": 1, "output_price_per_mtok": 1, "context_window": 10}
    custom = load_registry([write_registry(tmp_path, "anthropic", {"m": entry})])

    provider = factory.get_provider(
        "anthropic", make_settings(anthropic_api_key=API_KEY), registry=custom
    )

    assert provider is not None  # a directly constructed one would raise LLMConfigError


async def test_directly_built_provider_uses_its_own_registry(tmp_path: Path) -> None:
    entry = {
        "input_price_per_mtok": 1,
        "output_price_per_mtok": 1,
        "context_window": 10,
        "max_output_tokens": 1234,
    }
    registry = load_registry([write_registry(tmp_path, "anthropic", {MODEL: entry})])
    provider, recorder, _ = make_provider(
        _tool_reply({"score": 0.5, "reason": "r"}), registry=registry
    )

    await _structured(provider)

    assert recorder.requests[0].body["max_tokens"] == 1234


# --- complete() -----------------------------------------------------------------------------


async def test_complete_returns_text_and_usage() -> None:
    provider, _, _ = make_provider("message_ok")

    text, usage = await _complete(provider)

    assert text == "Hello"
    assert usage == Usage(12, 3)
    assert usage.requests == 1


async def test_complete_sends_the_expected_request() -> None:
    provider, recorder, _ = make_provider("message_ok")

    await _complete(provider)

    (request,) = recorder.requests
    assert (request.method, request.path) == ("POST", "/v1/messages")
    assert request.has_api_key_header
    assert request.body["model"] == MODEL
    assert request.body["max_tokens"] == 50
    assert request.body["system"] == SYSTEM
    assert request.body["messages"] == [{"role": "user", "content": USER}]
    assert request.body["temperature"] == 0.2
    assert "tools" not in request.body
    assert "tool_choice" not in request.body


async def test_temperature_zero_is_sent_explicitly() -> None:
    provider, recorder, _ = make_provider("message_ok")

    await provider.complete(SYSTEM, USER, model=MODEL, temperature=0, max_tokens=5)

    assert recorder.requests[0].body["temperature"] == 0
    assert recorder.requests[0].body["max_tokens"] == 5


async def test_complete_without_usage_reports_zero_tokens() -> None:
    provider, _, _ = make_provider("message_no_usage")

    text, usage = await _complete(provider)

    assert (text, usage) == ("Hello", Usage(0, 0))


async def test_complete_concatenates_text_blocks() -> None:
    blocks = [{"type": "text", "text": "Hel"}, {"type": "text", "text": "lo"}]
    provider, _, _ = make_provider(_text_reply("", content=blocks))

    text, _ = await _complete(provider)

    assert text == "Hello"


@pytest.mark.parametrize(
    ("fixture", "leak"),
    [
        ("message_empty", ""),
        ("message_max_tokens", "PARTIAL-ANSWER-SENTINEL"),
        ("message_refusal", "REFUSAL-TEXT-SENTINEL"),
        ("malformed_200", ""),
    ],
)
async def test_unusable_answers_are_unavailable_and_not_leaked(fixture: str, leak: str) -> None:
    provider, recorder, waits = make_provider(fixture)

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("anthropic", MODEL)
    assert MODEL in str(info.value)
    if leak:
        assert leak not in str(info.value)
        assert leak not in repr(info.value)
    assert len(recorder.requests) == 1
    assert waits == []


# --- structured output ----------------------------------------------------------------------


async def test_structured_returns_validated_value() -> None:
    provider, _, _ = make_provider("tool_use_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert usage == Usage(12, 3, requests=1)


@pytest.mark.parametrize(("model", "limit"), [(HAIKU, 64000), (SONNET, 128000)])
async def test_structured_request_forces_one_tool(model: str, limit: int) -> None:
    provider, recorder, _ = make_provider("tool_use_ok")

    await _structured(provider, model)

    (request,) = recorder.requests
    body = request.body
    assert body["tools"] == [
        {
            "name": "Score",
            "description": "Return the answer as the tool input.",
            "input_schema": Score.model_json_schema(),
        }
    ]
    assert body["tool_choice"] == {
        "type": "tool",
        "name": "Score",
        "disable_parallel_tool_use": True,
    }
    assert body["max_tokens"] == limit
    assert body["temperature"] == 0.2
    assert body["system"].startswith(SYSTEM)


class _Inner(BaseModel):
    label: str
    optional: str | None = None


class _Outer(BaseModel):
    inner: _Inner
    items: list[_Inner]
    note: str | None = None


async def test_structured_nested_models_and_lists_round_trip() -> None:
    provider, recorder, _ = make_provider("tool_use_nested")

    value, _ = await provider.complete_structured(SYSTEM, USER, _Outer, model=MODEL, temperature=0)

    assert value == _Outer(
        inner=_Inner(label="a"),
        items=[_Inner(label="b"), _Inner(label="c", optional="x")],
        note="n",
    )
    assert recorder.requests[0].body["tools"][0]["input_schema"] == _Outer.model_json_schema()


class _Color(BaseModel):
    kind: str


class _Node(BaseModel):
    value: int
    children: list["_Node"] = []


class _Rich(BaseModel):
    maybe: int | None = None
    mode: str
    choice: int | str
    tree: _Node


async def test_structured_schema_constructs_are_sent_unchanged() -> None:
    answer = {"mode": "a", "choice": "x", "tree": {"value": 1, "children": [{"value": 2}]}}
    provider, recorder, _ = make_provider(_tool_reply(answer, "_Rich"))

    value, _ = await provider.complete_structured(SYSTEM, USER, _Rich, model=MODEL, temperature=0)

    assert value.tree.children[0].value == 2
    sent = recorder.requests[0].body["tools"][0]["input_schema"]
    assert sent == _Rich.model_json_schema()
    assert "$defs" in sent
    assert "$ref" in str(sent)
    assert "additionalProperties" not in sent


class Page[Item](BaseModel):
    items: list[Item]


async def test_tool_name_is_sanitized_for_generic_models() -> None:
    provider, recorder, _ = make_provider(_tool_reply({"items": [1]}, "Page_int_"))

    await provider.complete_structured(SYSTEM, USER, Page[int], model=MODEL, temperature=0)

    body = recorder.requests[0].body
    assert body["tools"][0]["name"] == "Page_int_"
    assert body["tool_choice"]["name"] == "Page_int_"


async def test_tool_name_is_limited_to_64_characters() -> None:
    long_model = type("N" * 100, (BaseModel,), {"__annotations__": {"a": int}})
    provider, recorder, _ = make_provider(_tool_reply({"a": 1}, "N" * 64))

    await provider.complete_structured(SYSTEM, USER, long_model, model=MODEL, temperature=0)

    assert recorder.requests[0].body["tools"][0]["name"] == "N" * 64


async def test_structured_repairs_once_after_invalid_input() -> None:
    provider, recorder, _ = make_provider("tool_use_invalid", "tool_use_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert len(recorder.requests) == 2
    repair_user = recorder.requests[1].body["messages"][0]["content"]
    assert "score" in repair_user
    assert "invalid" in repair_user.lower()
    assert usage == Usage(24, 6, 2)
    for request in recorder.requests:
        assert request.body["tool_choice"]["type"] == "tool"


async def test_structured_text_instead_of_tool_is_repaired_once() -> None:
    provider, recorder, _ = make_provider("text_instead_of_tool", "tool_use_ok")

    value, usage = await _structured(provider)

    assert value.score == 0.8
    assert len(recorder.requests) == 2
    assert usage.requests == 2


async def test_structured_invalid_twice_raises_invalid_output_with_summed_usage() -> None:
    provider, recorder, _ = make_provider("tool_use_invalid", "text_instead_of_tool")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.usage == Usage(24, 6, 2)
    assert (info.value.provider, info.value.model) == ("anthropic", MODEL)
    assert len(recorder.requests) == 2


async def test_invalid_output_error_contains_no_answer_text() -> None:
    provider, _, _ = make_provider("tool_use_invalid", "tool_use_invalid")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    for text in (str(info.value), info.value.errors, repr(info.value)):
        assert "ANSWER-SENTINEL" not in text
        assert "PROMPT-TEXT-SENTINEL" not in text


@pytest.mark.parametrize("fixture", ["tool_use_max_tokens", "message_refusal"])
async def test_structured_unusable_answer_is_unavailable_without_repair(fixture: str) -> None:
    provider, recorder, _ = make_provider(fixture)

    with pytest.raises(LLMUnavailableError):
        await _structured(provider)

    assert len(recorder.requests) == 1


async def test_structured_empty_answer_is_repaired_once() -> None:
    provider, recorder, _ = make_provider("message_empty", "tool_use_ok")

    value, usage = await _structured(provider)

    assert value.score == 0.8
    assert (len(recorder.requests), usage.requests) == (2, 2)


async def test_structured_for_unregistered_model_is_a_config_error() -> None:
    provider, recorder, _ = make_provider("tool_use_ok")

    with pytest.raises(LLMConfigError, match="claude-unknown"):
        await _structured(provider, "claude-unknown")

    assert recorder.requests == []


# --- configuration --------------------------------------------------------------------------


def test_shipped_registry_has_two_models_with_output_limits() -> None:
    models = default_registry().models_for("anthropic")

    assert [info.model_id for info in models] == [HAIKU, SONNET]
    assert [info.max_output_tokens for info in models] == [64000, 128000]


def test_shipped_models_resolve_for_both_roles(job_data: dict[str, Any]) -> None:
    job_data["llm"] = {"provider": "anthropic", "models": {"fast": HAIKU, "smart": SONNET}}
    llm = JobConfig.model_validate(job_data).llm
    factory._discover()
    settings = make_settings(anthropic_api_key=API_KEY)

    provider, fast = factory.resolve(llm, "fast", settings)
    _, smart = factory.resolve(llm, "smart", settings)

    assert provider is not None
    assert (fast, smart) == (HAIKU, SONNET)


# --- client lifecycle and credentials -------------------------------------------------------


def test_provider_survives_successive_event_loops() -> None:
    provider, recorder, _ = make_provider("message_ok", "message_ok")

    first = asyncio.run(_complete(provider))
    second = asyncio.run(_complete(provider))

    assert first[0] == second[0] == "Hello"
    assert recorder.clients_created == 2


async def test_provider_reuses_the_client_within_one_loop() -> None:
    provider, recorder, _ = make_provider("message_ok", "message_ok")

    await _complete(provider)
    await _complete(provider)

    assert recorder.clients_created == 1


async def test_aclose_closes_the_client_of_the_running_loop() -> None:
    provider, recorder, _ = make_provider("message_ok", "message_ok")
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
    provider, recorder, _ = make_provider(*["message_ok"] * 4)
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
    assert recorder.clients_created == 2


async def test_only_the_api_key_header_is_sent_to_the_injected_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "token-from-env")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://elsewhere.invalid")
    provider, recorder, _ = make_provider("message_ok")

    await _complete(provider)

    (request,) = recorder.requests
    assert request.host == httpx2.URL(BASE_URL).host
    assert request.has_api_key_header
    assert not request.has_authorization


# --- logging --------------------------------------------------------------------------------


async def test_successful_call_logs_one_llm_call_record(caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = make_provider("message_ok")
    logged = _LoggedProvider(provider, name="anthropic", registry=default_registry())

    with caplog.at_level(logging.INFO, logger="invio.llm"):
        await logged.complete(SYSTEM, USER, model=MODEL, temperature=0.2, max_tokens=50)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.call"]
    assert record.provider == "anthropic"  # type: ignore[attr-defined]
    assert record.model == MODEL  # type: ignore[attr-defined]
    assert (record.input_tokens, record.output_tokens) == (12, 3)  # type: ignore[attr-defined]
    assert record.cost_usd is not None  # type: ignore[attr-defined]
    dump = str([r.__dict__ for r in caplog.records])
    assert API_KEY not in dump
    assert "PROMPT-TEXT-SENTINEL" not in dump


def test_llm_package_docstring_lists_anthropic() -> None:
    assert "anthropic" in (invio.llm.__doc__ or "")


# --- error mapping --------------------------------------------------------------------------


@pytest.mark.parametrize("fixture", ["error_401", "error_403"])
async def test_auth_failures_are_not_retried(fixture: str) -> None:
    provider, recorder, waits = make_provider(fixture, "message_ok")

    with pytest.raises(LLMAuthError, match="INVIO_ANTHROPIC_API_KEY") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("anthropic", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize("fixture", ["error_402", "error_400_credit_balance"])
async def test_exhausted_credit_is_a_non_retryable_quota_error(fixture: str) -> None:
    provider, recorder, waits = make_provider(fixture, "message_ok")

    with pytest.raises(LLMQuotaError) as info:
        await _complete(provider)

    assert info.value.retry_after is None
    assert isinstance(info.value, LLMRateLimitError)
    assert not is_transient_llm(info.value)
    assert (info.value.provider, info.value.model) == ("anthropic", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    ("fixture", "status", "text"),
    [
        ("error_400", 400, "max_tokens: 99999 > 64000"),
        ("error_404_model", 404, "claude-nope"),
        ("error_413", 413, "maximum allowed number of bytes"),
    ],
)
async def test_rejected_requests_are_invalid_request_errors(
    fixture: str, status: int, text: str
) -> None:
    provider, recorder, waits = make_provider(fixture, "message_ok")

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    message = str(info.value)
    assert info.value.status == status
    assert (info.value.provider, info.value.model) == ("anthropic", MODEL)
    assert f"HTTP {status}" in message
    assert text in message
    assert len(recorder.requests) == 1
    assert waits == []


async def test_free_text_limit_above_the_model_maximum_is_an_invalid_request() -> None:
    provider, recorder, _ = make_provider("error_400")

    with pytest.raises(LLMInvalidRequestError, match="HTTP 400"):
        await provider.complete(SYSTEM, USER, model=MODEL, temperature=0, max_tokens=99999)

    assert recorder.requests[0].body["max_tokens"] == 99999


async def test_bad_request_with_non_json_body_still_names_the_status() -> None:
    provider, _, _ = make_provider(httpx2.Response(400, content=b"<html>BODY-LEAK</html>"))

    with pytest.raises(LLMInvalidRequestError, match="HTTP 400") as info:
        await _complete(provider)

    assert "BODY-LEAK" not in str(info.value)


async def test_error_message_is_bounded_and_free_of_the_response_body() -> None:
    huge = httpx2.Response(
        400,
        json={
            "type": "error",
            "error": {"type": "invalid_request_error", "message": "x" * 10_000},
            "extra": "BODY-LEAK",
        },
    )
    provider, _, _ = make_provider(huge)

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert len(str(info.value)) <= 400
    assert "BODY-LEAK" not in str(info.value)


async def test_error_message_collapses_whitespace_and_control_characters() -> None:
    body = {"type": "error", "error": {"message": "bad\n\n  thing\x00here"}}
    provider, _, _ = make_provider(httpx2.Response(400, json=body))

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
        httpx2.ConnectTimeout("x"),
        httpx2.ReadTimeout("x"),
        httpx2.WriteTimeout("x"),
        httpx2.PoolTimeout("x"),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_every_transport_timeout_is_not_retried(failure: Exception) -> None:
    provider, recorder, waits = make_provider(failure, "message_ok")

    with pytest.raises(LLMUnavailableError, match="timed out") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("anthropic", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


async def test_timeout_after_a_retry_is_not_retried_again() -> None:
    provider, recorder, waits = make_provider("error_529", httpx2.ReadTimeout("x"), "message_ok")

    with pytest.raises(LLMUnavailableError, match="timed out"):
        await _complete(provider)

    assert len(recorder.requests) == 2
    assert waits == [1.0]


async def test_timeout_bounds_each_attempt_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    bounds: list[float] = []

    async def recording_with_timeout(awaitable: Any, *, seconds: float, **kwargs: Any) -> Any:
        bounds.append(seconds)
        return await with_timeout(awaitable, seconds=seconds, **kwargs)

    monkeypatch.setattr("invio.llm.anthropic.with_timeout", recording_with_timeout)
    provider, _, _ = make_provider("error_429", "error_529", "message_ok", timeout_seconds=7.0)

    await _complete(provider)

    assert bounds == [7.0, 7.0, 7.0]


@pytest.mark.parametrize(
    "reply",
    [
        httpx2.Response(200, content=b"<html>BODY-LEAK</html>"),
        httpx2.Response(200, headers={"content-type": "application/json"}, content=b"{not json"),
        httpx2.Response(200, headers={"content-type": "application/json"}, content=b"[1, 2]"),
    ],
    ids=["html", "broken-json", "json-array"],
)
async def test_non_object_success_body_is_unavailable_without_retry(
    reply: httpx2.Response,
) -> None:
    provider, recorder, waits = make_provider(reply, "message_ok")

    with pytest.raises(LLMUnavailableError) as info:
        await _complete(provider)

    assert "BODY-LEAK" not in str(info.value)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    "reply",
    [
        "error_401",
        "error_402",
        "error_413",
        "error_429",
        "error_529",
        "malformed_200",
        "message_refusal",
        httpx2.ConnectError("boom SECRET-IN-EXC"),
    ],
    ids=["401", "402", "413", "429", "529", "malformed", "refusal", "connect"],
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
    provider, _, _ = make_provider("tool_use_invalid", "tool_use_invalid")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.__cause__ is None
    assert info.value.__context__ is None


# --- rate limits and retries ----------------------------------------------------------------


async def test_rate_limit_is_retried_with_exponential_backoff() -> None:
    provider, recorder, waits = make_provider(*["error_429"] * 4)

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert len(recorder.requests) == 4
    assert waits == [1.0, 2.0, 4.0]
    assert info.value.retry_after is None
    assert (info.value.provider, info.value.model) == ("anthropic", MODEL)
    assert "per-minute rate limit" in str(info.value)


@pytest.mark.parametrize("fixture", ["error_500", "error_529"])
async def test_server_and_overloaded_errors_exhaust_retries_naming_the_status(
    fixture: str,
) -> None:
    provider, recorder, waits = make_provider(*[fixture] * 4)

    with pytest.raises(LLMUnavailableError, match=r"HTTP 5\d\d"):
        await _complete(provider)

    assert len(recorder.requests) == 4
    assert waits == [1.0, 2.0, 4.0]


@pytest.mark.parametrize("status", [500, 502, 503, 504, 529])
async def test_every_5xx_status_exhausts_retries(status: int) -> None:
    provider, recorder, _ = make_provider(*[httpx2.Response(status, text="<html>oops</html>")] * 4)

    with pytest.raises(LLMUnavailableError, match=str(status)):
        await _complete(provider)

    assert len(recorder.requests) == 4


async def test_overloaded_then_success() -> None:
    provider, recorder, waits = make_provider("error_529", "message_ok")

    text, usage = await _complete(provider)

    assert (text, usage.requests) == ("Hello", 1)
    assert waits == [1.0]
    assert len(recorder.requests) == 2


async def test_connection_failure_exhausts_retries() -> None:
    provider, recorder, waits = make_provider(*[httpx2.ConnectError("x")] * 4)

    with pytest.raises(LLMUnavailableError, match="connection failed"):
        await _complete(provider)

    assert len(recorder.requests) == 4
    assert waits == [1.0, 2.0, 4.0]


@pytest.mark.parametrize(
    "failure",
    [httpx2.ConnectError("x"), httpx2.ReadError("x"), httpx2.RemoteProtocolError("x")],
    ids=lambda e: type(e).__name__,
)
async def test_connection_failure_once_then_success(failure: Exception) -> None:
    provider, _, waits = make_provider(failure, "message_ok")

    text, _ = await _complete(provider)

    assert text == "Hello"
    assert waits == [1.0]


async def test_unsendable_request_is_not_retried() -> None:
    provider, recorder, waits = make_provider(httpx2.UnsupportedProtocol("x"), "message_ok")

    with pytest.raises(LLMUnavailableError, match="could not be sent"):
        await _complete(provider)

    assert (len(recorder.requests), waits) == (1, [])


async def test_retry_after_header_sets_the_wait() -> None:
    provider, _, waits = make_provider("error_429_retry_after", "message_ok")

    await _complete(provider)

    assert waits == [2.0]


async def test_retry_after_above_the_cap_fails_immediately() -> None:
    provider, recorder, waits = make_provider("error_429_retry_after_long", "message_ok")

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after == 3600
    assert len(recorder.requests) == 1
    assert waits == []


async def test_final_rate_limit_error_keeps_retry_after() -> None:
    provider, recorder, waits = make_provider(*["error_429_retry_after"] * 4)

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after == 2
    assert len(recorder.requests) == 4
    assert waits == [2.0, 2.0, 2.0]


async def test_retry_after_at_and_above_the_cap() -> None:
    body = {"type": "error", "error": {"type": "rate_limit_error", "message": "x"}}
    at_cap = httpx2.Response(429, headers={"retry-after": "5"}, json=body)
    provider, _, waits = make_provider(at_cap, "message_ok", retry=RetryPolicy(max_retry_after=5))
    await _complete(provider)
    assert waits == [5.0]

    over = httpx2.Response(429, headers={"retry-after": "6"}, json=body)
    provider, recorder, waits = make_provider(
        over, "message_ok", retry=RetryPolicy(max_retry_after=5)
    )
    with pytest.raises(LLMRateLimitError):
        await _complete(provider)
    assert (len(recorder.requests), waits) == (1, [])


async def test_retry_after_http_date_is_relative_to_now() -> None:
    reply = httpx2.Response(
        429,
        headers={"Retry-After": format_datetime(NOW + timedelta(seconds=10), usegmt=True)},
        json={"type": "error", "error": {"message": "slow down"}},
    )
    provider, _, waits = make_provider(reply, "message_ok", now=lambda: NOW)

    await _complete(provider)

    assert waits == [10.0]


async def test_unparseable_retry_after_falls_back_to_backoff() -> None:
    reply = httpx2.Response(429, headers={"Retry-After": "soon"}, content=b"{}")
    provider, _, waits = make_provider(reply, "message_ok")

    await _complete(provider)

    assert waits == [1.0]


async def test_overflowing_retry_after_fails_immediately() -> None:
    reply = httpx2.Response(429, headers={"Retry-After": "9" * 400}, content=b"{}")
    provider, recorder, waits = make_provider(reply, "message_ok")

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after is None
    assert len(recorder.requests) == 1
    assert waits == []


async def test_retry_after_on_a_server_error_is_ignored() -> None:
    reply = httpx2.Response(529, headers={"Retry-After": "30"}, content=b"{}")
    provider, _, waits = make_provider(reply, "message_ok")

    await _complete(provider)

    assert waits == [1.0]


async def test_jitter_scales_the_wait_and_uses_the_policy_range() -> None:
    bounds: list[tuple[float, float]] = []

    def uniform(a: float, b: float) -> float:
        bounds.append((a, b))
        return b

    provider, _, waits = make_provider(
        "error_529", "message_ok", retry=RetryPolicy(jitter=0.5), uniform=uniform
    )

    await _complete(provider)

    assert bounds == [(-0.5, 0.5)]
    assert waits == [1.5]


async def test_zero_retries_means_exactly_one_http_request() -> None:
    provider, recorder, waits = make_provider("error_529", retry=RetryPolicy(max_retries=0))

    with pytest.raises(LLMUnavailableError):
        await _complete(provider)

    assert len(recorder.requests) == 1
    assert waits == []


async def test_repair_request_has_its_own_retry_budget() -> None:
    provider, recorder, waits = make_provider("tool_use_invalid", "error_529", "tool_use_ok")

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

    provider, recorder, _ = make_provider("error_529", "message_ok", sleep=blocking_sleep)
    task = asyncio.ensure_future(_complete(provider))
    await entered.wait()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(recorder.requests) == 1


async def test_each_retry_logs_one_warning_without_prompt_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _, _ = make_provider("error_429", "error_529", "message_ok")

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    records = [r for r in caplog.records if r.getMessage() == "llm.retry"]
    assert len(records) == 2
    first, second = records
    assert (first.provider, first.model, first.attempt) == ("anthropic", MODEL, 1)  # type: ignore[attr-defined]
    assert (first.status, first.failure, first.wait_s) == (429, "rate_limit", 1.0)  # type: ignore[attr-defined]
    assert (second.attempt, second.status, second.failure) == (2, 529, "server")  # type: ignore[attr-defined]
    dump = str([r.__dict__ for r in caplog.records])
    assert "PROMPT-TEXT-SENTINEL" not in dump
    assert API_KEY not in dump
    assert all(r.exc_info is None for r in records)


@pytest.mark.parametrize(
    "reply",
    ["error_401", "error_400", "error_402", httpx2.ReadTimeout("x"), "error_429_retry_after_long"],
    ids=["auth", "invalid-request", "billing", "timeout", "retry-after-above-cap"],
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
        (("error_402",), 60.0),
        (("error_400",), 60.0),
        (("error_404_model",), 60.0),
        (("error_413",), 60.0),
        (("error_429",) * 4, 60.0),
        (("error_500",) * 4, 60.0),
        (("error_529",) * 4, 60.0),
        ((httpx2.ConnectError("x"),) * 4, 60.0),
        ((httpx2.ReadTimeout("x"),), 60.0),
        ((HANG,), 0.01),
        (("malformed_200",), 60.0),
        (("message_empty",), 60.0),
        (("message_refusal",), 60.0),
        (("message_max_tokens",), 60.0),
    ],
)
async def test_every_error_names_provider_and_model(
    replies: tuple[Any, ...], timeout: float
) -> None:
    provider, _, _ = make_provider(*replies, timeout_seconds=timeout)

    with pytest.raises(LLMError) as info:
        await _complete(provider)

    message = str(info.value)
    assert (info.value.provider, info.value.model) == ("anthropic", MODEL)
    assert MODEL in message
    assert API_KEY not in message
    assert "PROMPT-TEXT-SENTINEL" not in message


# --- client configuration -------------------------------------------------------------------


async def test_sdk_retries_are_disabled_and_timeout_has_a_margin() -> None:
    provider, _, _ = make_provider("message_ok", timeout_seconds=10.0)
    await _complete(provider)

    sdk_client, _ = next(iter(provider._clients.values()))

    assert sdk_client.max_retries == 0
    assert sdk_client.timeout == 15.0
    assert str(sdk_client.base_url).rstrip("/") == BASE_URL


def test_default_base_url_is_the_anthropic_api() -> None:
    provider = AnthropicProvider(API_KEY, timeout_seconds=5, registry=default_registry())

    assert provider._base_url == "https://api.anthropic.com"


async def test_large_max_tokens_does_not_trip_the_sdk_streaming_guard() -> None:
    provider, recorder, _ = make_provider("tool_use_ok")

    await _structured(provider, SONNET)

    assert recorder.requests[0].body["max_tokens"] == 128000


# --- helpers: _classify ---------------------------------------------------------------------


def _request() -> httpx2.Request:
    return httpx2.Request("POST", "https://api.anthropic.test/v1/messages")


def _status_error(status: int, body: object, *, headers: dict[str, str] | None = None) -> Any:
    response = httpx2.Response(status, request=_request(), headers=headers or {})
    return anthropic.APIStatusError("SDK-MESSAGE-LEAK raw body", response=response, body=body)


def _wrapped(kind: str, message: str = "m") -> dict[str, Any]:
    return {"type": "error", "error": {"type": kind, "message": message}}


def test_classify_other_exception_returns_none() -> None:
    assert _classify(RuntimeError("x"), MODEL, lambda: NOW) is None


def test_classify_timeout_is_checked_before_connection_error() -> None:
    failure = _classify(anthropic.APITimeoutError(request=_request()), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("timeout", False)


def test_classify_connection_error_without_cause_is_retryable() -> None:
    failure = _classify(anthropic.APIConnectionError(request=_request()), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == ("connection", True)


@pytest.mark.parametrize(
    ("cause", "kind", "retryable"),
    [
        (httpx2.InvalidURL("x"), "unsendable", False),
        (httpx2.UnsupportedProtocol("x"), "unsendable", False),
        (httpx2.LocalProtocolError("x"), "unsendable", False),
        (httpx2.DecodingError("x"), "bad_response", False),
        (httpx2.TooManyRedirects("x"), "bad_response", False),
        (httpx2.StreamConsumed(), "bad_response", False),
        (httpx2.ConnectError("x"), "connection", True),
        (httpx2.RemoteProtocolError("x"), "connection", True),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else "",
)
def test_classify_connection_error_by_cause(cause: Exception, kind: str, retryable: bool) -> None:
    exc = anthropic.APIConnectionError(request=_request())
    exc.__cause__ = cause

    failure = _classify(exc, MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable) == (kind, retryable)
    assert type(cause).__name__ in str(failure.error)


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        (httpx2.InvalidURL("x"), "unsendable"),
        (httpx2.ReadTimeout("x"), "timeout"),
        (httpx2.ConnectError("x"), "connection"),
        (httpx2.DecodingError("x"), "bad_response"),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else "",
)
def test_classify_raw_transport_errors(raw: Exception, kind: str) -> None:
    failure = _classify(raw, MODEL, lambda: NOW)

    assert failure is not None
    assert failure.kind == kind


def test_classify_response_validation_and_json_errors_are_not_retryable() -> None:
    response = httpx2.Response(200, request=_request())
    for exc in (
        anthropic.APIResponseValidationError(response, None),
        json.JSONDecodeError("x", "doc", 0),
    ):
        failure = _classify(exc, MODEL, lambda: NOW)
        assert failure is not None
        assert (failure.kind, failure.retryable) == ("bad_response", False)


@pytest.mark.parametrize("status", [100, 301, 304])
def test_classify_non_error_status_is_bad_response(status: int) -> None:
    failure = _classify(_status_error(status, _wrapped("x")), MODEL, lambda: NOW)

    assert failure is not None
    assert (failure.kind, failure.retryable, failure.status) == ("bad_response", False, status)


def test_classify_other_4xx_never_uses_the_sdk_message() -> None:
    failure = _classify(_status_error(409, _wrapped("x", "conflict")), MODEL, lambda: NOW)

    assert failure is not None
    assert isinstance(failure.error, LLMInvalidRequestError)
    assert failure.error.status == 409
    assert "conflict" in str(failure.error)
    assert "SDK-MESSAGE-LEAK" not in str(failure.error)


def test_classify_accepts_a_flat_message_body() -> None:
    failure = _classify(_status_error(400, {"message": "flat"}), MODEL, lambda: NOW)

    assert failure is not None
    assert "flat" in str(failure.error)


@pytest.mark.parametrize(
    "body", [None, "plain text", ["a"], {"message": 5}, {"error": {"message": 5}}, {"other": "x"}]
)
def test_classify_status_with_unusable_body_has_no_detail(body: object) -> None:
    failure = _classify(_status_error(400, body), MODEL, lambda: NOW)

    assert failure is not None
    assert str(failure.error) == f"Anthropic rejected the request (HTTP 400, model {MODEL})"


def test_classify_credit_exhaustion_by_status_type_or_message() -> None:
    errors = [
        _status_error(402, None),
        _status_error(400, _wrapped("billing_error")),
        _status_error(400, _wrapped("invalid_request_error", "Your Credit Balance is low")),
        _status_error(400, _wrapped("invalid_request_error", "bad")),
        _status_error(429, _wrapped("rate_limit_error")),
    ]

    failures = [_classify(e, MODEL, lambda: NOW) for e in errors]

    assert [f.kind for f in failures if f is not None] == [
        "quota",
        "quota",
        "quota",
        "invalid_request",
        "rate_limit",
    ]


def test_classify_billing_type_attribute_of_a_typed_sdk_error() -> None:
    exc = _status_error(400, None)
    exc.type = "billing_error"

    failure = _classify(exc, MODEL, lambda: NOW)

    assert failure is not None
    assert failure.kind == "quota"


def test_retry_after_is_read_from_lowercase_sdk_headers() -> None:
    exc = _status_error(429, _wrapped("rate_limit_error"), headers={"retry-after": "4"})

    failure = _classify(exc, MODEL, lambda: NOW)

    assert failure is not None
    assert failure.retry_after == 4.0


def test_graph_layer_does_not_retry_quota_errors() -> None:
    failure = _classify(_status_error(402, _wrapped("billing_error")), MODEL, lambda: NOW)

    assert failure is not None
    assert isinstance(failure.error, LLMQuotaError)
    assert not is_transient_llm(failure.error)
