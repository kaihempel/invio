"""Offline tests of the Google (Gemini) provider (recorded HTTP fixtures, no network, no key)."""

import asyncio
from pathlib import Path

import pytest
from pydantic import BaseModel

from invio.graph.nodes.relevance import RelevanceResult
from invio.graph.nodes.summarize_item import ItemSummary
from invio.llm.base import (
    LLMAuthError,
    LLMConfigError,
    LLMInvalidOutputError,
    Usage,
)
from invio.llm.google import GoogleProvider, gemini_schema
from invio.llm.registry import ModelRegistry, load_registry
from tests.google_helpers import API_KEY, BASE_URL, Nested, make_provider
from tests.llm_helpers import Score, make_settings, write_registry

SYSTEM = "You rate things."
USER = "PROMPT-TEXT-SENTINEL rate this"
FAST = "gemini-3.5-flash-lite"
SMART = "gemini-3.8-flash"
MODEL = FAST
FAST_ALLOWANCE = 1024
SMART_ALLOWANCE = 4096


async def _complete(
    provider: GoogleProvider, model: str = MODEL, *, max_tokens: int = 50
) -> tuple[str, Usage]:
    return await provider.complete(
        SYSTEM, USER, model=model, temperature=0.0, max_tokens=max_tokens
    )


async def _structured[T: BaseModel](
    provider: GoogleProvider, schema: type[T] = Score, model: str = MODEL
) -> tuple[T, Usage]:
    result: tuple[T, Usage] = await provider.complete_structured(
        SYSTEM, USER, schema, model=model, temperature=0.0
    )
    return result


def _registry(tmp_path: Path, **entries: dict[str, object]) -> ModelRegistry:
    base: dict[str, object] = {
        "input_price_per_mtok": 1,
        "output_price_per_mtok": 1,
        "context_window": 10,
    }
    models = {name: {**base, **extra} for name, extra in entries.items()}
    return load_registry([write_registry(tmp_path, "google", models)])


# --- free text -------------------------------------------------------------------------------


async def test_complete_returns_text_and_usage() -> None:
    provider, recorder, _ = make_provider("text_ok")

    text, usage = await _complete(provider)

    assert (text, usage) == ("Hello", Usage(12, 3))
    assert len(recorder.requests) == 1


async def test_complete_request_shape() -> None:
    provider, recorder, _ = make_provider("text_ok")

    await _complete(provider, max_tokens=50)

    request = recorder.requests[0]
    assert request.method == "POST"
    assert request.path == f"/v1beta/models/{MODEL}:generateContent"
    assert request.host == "generativelanguage.googleapis.test"
    assert request.body["systemInstruction"]["parts"] == [{"text": SYSTEM}]
    assert request.body["contents"] == [{"role": "user", "parts": [{"text": USER}]}]
    config = request.body["generationConfig"]
    assert config["maxOutputTokens"] == 50 + FAST_ALLOWANCE
    assert config["thinkingConfig"]["thinking_level"] == "MINIMAL"
    assert "responseMimeType" not in config


async def test_smart_model_gets_its_own_level_and_allowance() -> None:
    provider, recorder, _ = make_provider("text_ok")

    await _complete(provider, SMART, max_tokens=10)

    config = recorder.requests[0].body["generationConfig"]
    assert config["maxOutputTokens"] == 10 + SMART_ALLOWANCE
    assert config["thinkingConfig"]["thinking_level"] == "LOW"


async def test_empty_system_text_omits_the_system_instruction() -> None:
    provider, recorder, _ = make_provider("text_ok")

    await provider.complete("", USER, model=MODEL, temperature=0.0, max_tokens=5)

    assert "systemInstruction" not in recorder.requests[0].body


async def test_thinking_parts_are_not_part_of_the_answer_but_are_billed() -> None:
    provider, _, _ = make_provider("text_with_thoughts")

    text, usage = await _complete(provider)

    assert text == "Hello"
    assert usage == Usage(12, 43)


async def test_missing_usage_is_zero() -> None:
    provider, _, _ = make_provider("text_no_usage")

    assert await _complete(provider) == ("Hello", Usage(0, 0))


# --- temperature -----------------------------------------------------------------------------


@pytest.mark.parametrize("operation", ["complete", "structured"])
async def test_temperature_is_omitted_only_for_flagged_models(
    tmp_path: Path, operation: str
) -> None:
    thinking = {"thinking_level": "low", "thinking_allowance_tokens": 10}
    registry = _registry(
        tmp_path,
        flagged={**thinking, "keep_default_temperature": True},
        plain=thinking,
    )
    reply = "text_ok" if operation == "complete" else "json_ok"
    provider, recorder, _ = make_provider(reply, reply, registry=registry)

    for model in ("flagged", "plain"):
        if operation == "complete":
            await _complete(provider, model)
        else:
            await _structured(provider, Score, model)

    flagged, plain = (r.body["generationConfig"] for r in recorder.requests)
    assert "temperature" not in flagged
    assert plain["temperature"] == 0.0


# --- structured ------------------------------------------------------------------------------


async def test_structured_request_shape_and_result() -> None:
    provider, recorder, _ = make_provider("json_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert usage == Usage(12, 3)
    config = recorder.requests[0].body["generationConfig"]
    assert config["responseMimeType"] == "application/json"
    assert config["responseJsonSchema"] == gemini_schema(Score.model_json_schema())
    assert "maxOutputTokens" not in config
    assert config["thinkingConfig"]["thinking_level"] == "MINIMAL"


async def test_fenced_answer_validates() -> None:
    provider, recorder, _ = make_provider("json_fenced")

    value, _ = await _structured(provider)

    assert value.score == 0.8
    assert len(recorder.requests) == 1


async def test_relevance_result_round_trips() -> None:
    provider, _, _ = make_provider("json_relevance")

    value, _ = await _structured(provider, RelevanceResult)

    assert value.score == 0.75
    assert value.key_points == ["first", "second"]


async def test_item_summary_round_trips() -> None:
    provider, _, _ = make_provider("json_item_summary")

    value, _ = await _structured(provider, ItemSummary)

    assert len(value.bullets) == 4


async def test_locally_enforced_constraint_triggers_one_repair() -> None:
    provider, recorder, _ = make_provider("json_item_summary_too_few", "json_item_summary")

    value, usage = await _structured(provider, ItemSummary)

    assert len(value.bullets) == 4
    assert usage.requests == 2
    repair_text = recorder.requests[1].body["contents"][0]["parts"][0]["text"]
    assert "bullets" in repair_text
    assert "invalid" in repair_text.lower()


async def test_nested_model_round_trips() -> None:
    provider, recorder, _ = make_provider("json_nested")

    value, _ = await _structured(provider, Nested)

    assert value.main.name == "a"
    assert [tag.weight for tag in value.others] == [2, 3]
    sent = recorder.requests[0].body["generationConfig"]["responseJsonSchema"]
    assert "$defs" not in sent
    assert "$ref" not in repr(sent)


async def test_invalid_twice_raises_with_summed_usage() -> None:
    provider, recorder, _ = make_provider("json_invalid", "json_invalid")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.usage == Usage(24, 6, 2)
    assert (info.value.provider, info.value.model) == ("google", MODEL)
    assert len(recorder.requests) == 2


@pytest.mark.parametrize("fixture", ["text_empty", "no_candidates"])
async def test_empty_structured_answer_is_repaired_once(fixture: str) -> None:
    provider, recorder, _ = make_provider(fixture, "json_ok")

    value, usage = await _structured(provider)

    assert value.score == 0.8
    assert (len(recorder.requests), usage.requests) == (2, 2)


class _Recursive(Score):
    child: "_Recursive | None" = None


async def test_recursive_schema_is_a_config_error_without_a_request() -> None:
    provider, recorder, _ = make_provider("json_ok")

    with pytest.raises(LLMConfigError, match="recursive"):
        await _structured(provider, _Recursive)

    assert recorder.requests == []


# --- construction ----------------------------------------------------------------------------


def test_from_settings_builds_provider_whose_repr_hides_the_key() -> None:
    provider = GoogleProvider.from_settings(
        make_settings(google_api_key=API_KEY, llm_timeout_seconds=5)
    )

    assert provider.timeout_seconds == 5
    assert API_KEY not in repr(provider)
    assert "GoogleProvider" in repr(provider)


def test_from_settings_without_key_names_the_env_var() -> None:
    with pytest.raises(LLMAuthError, match="INVIO_GOOGLE_API_KEY"):
        GoogleProvider.from_settings(make_settings())


@pytest.mark.parametrize("missing", ["thinking_level", "thinking_allowance_tokens"])
def test_registry_entry_without_thinking_fields_is_a_config_error(
    tmp_path: Path, missing: str
) -> None:
    fields: dict[str, object] = {"thinking_level": "low", "thinking_allowance_tokens": 10}
    del fields[missing]
    registry = _registry(tmp_path, broken=fields)

    with pytest.raises(
        LLMConfigError, match="must define thinking_level and thinking_allowance_tokens"
    ) as info:
        make_provider(registry=registry)

    assert "broken" in str(info.value)


def test_from_settings_uses_a_given_registry(tmp_path: Path) -> None:
    registry = _registry(tmp_path, m={})

    with pytest.raises(LLMConfigError, match="thinking_level"):
        GoogleProvider.from_settings(make_settings(google_api_key=API_KEY), registry=registry)


async def test_unregistered_model_is_a_config_error_before_any_request() -> None:
    provider, recorder, _ = make_provider("text_ok")

    with pytest.raises(LLMConfigError, match="gpt-x"):
        await _complete(provider, "gpt-x")
    with pytest.raises(LLMConfigError, match="gpt-x"):
        await _structured(provider, Score, "gpt-x")

    assert recorder.requests == []


# --- credentials and clients -----------------------------------------------------------------


async def test_key_is_sent_only_as_google_header_to_the_configured_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GOOGLE_API_KEY", "ENV-KEY-1")
    monkeypatch.setenv("GEMINI_API_KEY", "ENV-KEY-2")
    monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", "https://evil.test/")
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    provider, recorder, _ = make_provider("text_ok")

    await _complete(provider)

    request = recorder.requests[0]
    assert request.headers["x-goog-api-key"] == API_KEY
    assert not request.has_authorization
    assert request.host == BASE_URL.split("//")[1].rstrip("/")
    assert request.path.startswith("/v1beta/models/")
    assert "ENV-KEY" not in repr(request.headers)


async def test_aclose_closes_the_client_of_the_running_loop() -> None:
    provider, recorder, _ = make_provider("text_ok", "text_ok")
    await _complete(provider)

    await provider.aclose()
    await provider.aclose()

    assert recorder.clients[0].is_closed
    await _complete(provider)
    assert recorder.clients_created == 2
    await provider.aclose()


async def test_concurrent_calls_in_one_loop_share_one_client() -> None:
    provider, recorder, _ = make_provider("text_ok", "json_ok")

    await asyncio.gather(_complete(provider), _structured(provider))

    assert recorder.clients_created == 1
    await provider.aclose()
