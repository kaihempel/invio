"""Offline tests of the Anthropic provider (recorded HTTP fixtures, no network, no real key)."""

import asyncio
import logging
import threading
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import BaseModel

import invio.llm
from invio.config.job import JobConfig
from invio.llm import factory
from invio.llm.anthropic import AnthropicProvider
from invio.llm.base import (
    LLMAuthError,
    LLMConfigError,
    LLMInvalidOutputError,
    LLMUnavailableError,
    Usage,
)
from invio.llm.factory import _LoggedProvider
from invio.llm.registry import default_registry, load_registry
from tests.anthropic_helpers import API_KEY, BASE_URL, make_provider
from tests.llm_helpers import Score, make_settings, write_registry

SYSTEM = "You rate things."
USER = "PROMPT-TEXT-SENTINEL rate this"
HAIKU = "claude-haiku-4-5-20251001"
SONNET = "claude-sonnet-4-6"
MODEL = HAIKU


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
