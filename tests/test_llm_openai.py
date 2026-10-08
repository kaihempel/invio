"""Offline tests of the OpenAI provider (recorded HTTP fixtures, no network, no real key)."""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from pydantic import BaseModel

import invio.llm
from invio.config.job import JobConfig
from invio.llm import factory
from invio.llm.base import (
    LLMAuthError,
    LLMInvalidOutputError,
    LLMUnavailableError,
    Usage,
)
from invio.llm.factory import _LoggedProvider
from invio.llm.openai import OpenAIProvider
from invio.llm.registry import default_registry
from tests.llm_helpers import Score, make_settings
from tests.openai_helpers import API_KEY, load_fixture, make_provider

SYSTEM = "You rate things."
USER = "PROMPT-TEXT-SENTINEL rate this"
MODEL = "gpt-4.1-mini-2025-04-14"


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
