"""Offline tests of the Google (Gemini) provider (recorded HTTP fixtures, no network, no key)."""

import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import httpx
import pytest
from google.genai import errors as google_errors
from pydantic import BaseModel, Field, ValidationError

from invio.graph.nodes.relevance import RelevanceResult
from invio.graph.nodes.summarize_item import ItemSummary
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
)
from invio.llm.factory import _LoggedProvider
from invio.llm.google import GoogleProvider, _classify, gemini_schema
from invio.llm.http_retry import RetryPolicy
from invio.llm.registry import ModelRegistry, default_registry, load_registry
from tests.google_helpers import API_KEY, BASE_URL, FIXTURE_DIR, HANG, Nested, make_provider
from tests.llm_helpers import Score, make_settings, write_registry

SYSTEM = "You rate things."
USER = "PROMPT-TEXT-SENTINEL rate this"
FAST = "gemini-3.5-flash-lite"
SMART = "gemini-3.8-flash"
MODEL = FAST
FAST_ALLOWANCE = 1024
SMART_ALLOWANCE = 4096
NOW = datetime(2026, 10, 9, 12, 0, tzinfo=UTC)


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


async def test_ignored_temperature_is_logged_at_debug_level(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _, _ = make_provider("text_ok")

    with caplog.at_level(logging.DEBUG, logger="invio.llm"):
        await _complete(provider, MODEL)

    [record] = [r for r in caplog.records if r.getMessage() == "llm.temperature_ignored"]
    assert record.levelno == logging.DEBUG
    assert (record.provider, record.model, record.temperature) == ("google", MODEL, 0.0)


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


@pytest.mark.parametrize("fields", [{}, {"thinking_level": "low"}])
def test_registry_entry_without_thinking_fields_is_a_config_error(
    tmp_path: Path, fields: dict[str, object]
) -> None:
    # An allowance without a level is already rejected when the registry is loaded.
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


# --- blocks (US2) ----------------------------------------------------------------------------


async def _call(provider: GoogleProvider, operation: str) -> object:
    if operation == "complete":
        return await _complete(provider)
    return await _structured(provider)


OPERATIONS = ["complete", "structured"]


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_blocked_prompt_raises_invalid_output_without_retry_or_repair(
    operation: str,
) -> None:
    provider, recorder, waits = make_provider("blocked_prompt", "json_ok")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _call(provider, operation)

    error = info.value
    assert "SAFETY" in str(error)
    assert "HARM_CATEGORY_DANGEROUS_CONTENT" in str(error)
    assert "HARM_CATEGORY_HARASSMENT" not in str(error)  # not blocked
    assert MODEL in str(error)
    assert (error.provider, error.model) == ("google", MODEL)
    assert "SAFETY" in error.errors
    assert error.usage == Usage(12, 0)
    assert "PROMPT-TEXT-SENTINEL" not in str(error) + error.errors
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_other_block_reasons_are_named(operation: str) -> None:
    provider, recorder, _ = make_provider("blocked_prompt_other")

    with pytest.raises(LLMInvalidOutputError, match="PROHIBITED_CONTENT"):
        await _call(provider, operation)

    assert len(recorder.requests) == 1


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize(
    ("fixture", "reason"),
    [
        ("stopped_safety", "SAFETY"),
        ("stopped_recitation", "RECITATION"),
        ("stopped_spii", "SPII"),
    ],
)
async def test_stopped_answers_name_the_reason_and_hide_partial_text(
    operation: str, fixture: str, reason: str
) -> None:
    provider, recorder, _ = make_provider(fixture, "json_ok")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _call(provider, operation)

    assert reason in str(info.value)
    assert reason in info.value.errors
    assert "PARTIAL-ANSWER-TEXT" not in str(info.value) + info.value.errors + repr(info.value)
    assert info.value.usage == Usage(12, 3)
    assert len(recorder.requests) == 1


async def test_stopped_answer_names_blocked_categories() -> None:
    provider, _, _ = make_provider("stopped_safety")

    with pytest.raises(LLMInvalidOutputError, match="HARM_CATEGORY_HATE_SPEECH"):
        await _complete(provider)


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_cut_off_answer_is_unavailable_without_retry_or_repair(operation: str) -> None:
    provider, recorder, _ = make_provider("text_max_tokens", "json_ok")

    with pytest.raises(LLMUnavailableError, match="MAX_TOKENS") as info:
        await _call(provider, operation)

    assert "PARTIAL-ANSWER-TEXT" not in str(info.value)
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("fixture", ["text_empty", "no_candidates"])
async def test_empty_free_text_is_unavailable(fixture: str) -> None:
    provider, recorder, _ = make_provider(fixture)

    with pytest.raises(LLMUnavailableError, match="no answer text"):
        await _complete(provider)

    assert len(recorder.requests) == 1


async def test_blocked_prompt_log_line_carries_the_input_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _, _ = make_provider("blocked_prompt")
    logged = _LoggedProvider(provider, name="google", registry=default_registry())

    with caplog.at_level(logging.INFO, logger="invio.llm"), pytest.raises(LLMInvalidOutputError):
        await logged.complete(SYSTEM, USER, model=MODEL, temperature=0, max_tokens=5)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.error"]
    assert record.error == "LLMInvalidOutputError"  # type: ignore[attr-defined]
    assert record.input_tokens == 12  # type: ignore[attr-defined]
    assert "PROMPT-TEXT-SENTINEL" not in str([r.__dict__ for r in caplog.records])


# --- error mapping and retries (US2) ---------------------------------------------------------

RETRIES = RetryPolicy(max_retries=2)


@pytest.mark.parametrize("fixture", ["error_401", "error_403", "error_400_api_key_invalid"])
async def test_rejected_key_is_an_auth_error_after_one_request(fixture: str) -> None:
    provider, recorder, waits = make_provider(fixture, "text_ok", retry=RETRIES)

    with pytest.raises(LLMAuthError, match="INVIO_GOOGLE_API_KEY") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("google", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize("fixture", ["error_400", "error_404_model"])
async def test_rejected_request_is_not_retried(fixture: str) -> None:
    provider, recorder, _ = make_provider(fixture, "text_ok", retry=RETRIES)

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert info.value.status == (400 if fixture == "error_400" else 404)
    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    ("fixture", "wait"),
    [
        ("error_429_retry_delay", 7.0),
        ("error_429_retry_after_header", 3.0),
        ("error_429_bare", 1.0),
    ],
)
async def test_rate_limit_is_retried_after_the_hinted_wait(fixture: str, wait: float) -> None:
    provider, recorder, waits = make_provider(fixture, "text_ok", retry=RETRIES)

    text, _ = await _complete(provider)

    assert text == "Hello"
    assert waits == [wait]
    assert len(recorder.requests) == 2


async def test_retry_info_wins_over_the_retry_after_header() -> None:
    response = httpx.Response(
        429,
        headers={"retry-after": "3"},
        json=json.loads((FIXTURE_DIR / "error_429_retry_delay.json").read_text())["body"],
    )
    provider, _, waits = make_provider(response, "text_ok", retry=RETRIES)

    await _complete(provider)

    assert waits == [7.0]


async def test_fractional_retry_delay_is_honoured() -> None:
    body = {
        "error": {
            "code": 429,
            "message": "slow down",
            "status": "RESOURCE_EXHAUSTED",
            "details": [
                {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "0.500s"}
            ],
        }
    }
    provider, _, waits = make_provider(httpx.Response(429, json=body), "text_ok", retry=RETRIES)

    await _complete(provider)

    assert waits == [0.5]


async def test_exhausted_rate_limit_raises_with_the_last_hint() -> None:
    provider, recorder, waits = make_provider(*["error_429_retry_delay"] * 3, retry=RETRIES)

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert not isinstance(info.value, LLMQuotaError)
    assert info.value.retry_after == 7.0
    assert len(recorder.requests) == 3
    assert waits == [7.0, 7.0]


async def test_retry_delay_above_the_cap_fails_at_once() -> None:
    provider, recorder, waits = make_provider("error_429_retry_delay_long", retry=RETRIES)

    with pytest.raises(LLMRateLimitError) as info:
        await _complete(provider)

    assert info.value.retry_after == 3600.0
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize("fixture", ["error_429_daily_quota", "error_429_zero_quota"])
async def test_exhausted_quota_is_not_retried(fixture: str) -> None:
    provider, recorder, waits = make_provider(fixture, "text_ok", retry=RETRIES)

    with pytest.raises(LLMQuotaError) as info:
        await _complete(provider)

    assert isinstance(info.value, LLMRateLimitError)
    assert info.value.retry_after is None
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize("fixture", ["error_500", "error_503", "error_504"])
async def test_server_errors_are_retried_then_unavailable(fixture: str) -> None:
    provider, recorder, waits = make_provider(*[fixture] * 3, retry=RETRIES)

    with pytest.raises(LLMUnavailableError):
        await _complete(provider)

    assert len(recorder.requests) == 3
    assert waits == [1.0, 2.0]


async def test_server_error_followed_by_success() -> None:
    provider, _, _ = make_provider("error_503", "text_ok", retry=RETRIES)

    assert (await _complete(provider))[0] == "Hello"


async def test_connect_error_is_retried_then_unavailable() -> None:
    provider, recorder, _ = make_provider(*[httpx.ConnectError("down")] * 3, retry=RETRIES)

    with pytest.raises(LLMUnavailableError, match="connection failed"):
        await _complete(provider)

    assert len(recorder.requests) == 3


async def test_read_timeout_is_not_retried() -> None:
    provider, recorder, _ = make_provider(httpx.ReadTimeout("slow"), "text_ok", retry=RETRIES)

    with pytest.raises(LLMUnavailableError, match="timed out"):
        await _complete(provider)

    assert len(recorder.requests) == 1


async def test_hanging_request_hits_the_call_deadline() -> None:
    provider, recorder, _ = make_provider(HANG, "text_ok", retry=RETRIES, timeout_seconds=0.05)

    with pytest.raises(LLMUnavailableError):
        await _complete(provider)

    assert len(recorder.requests) == 1


@pytest.mark.parametrize(
    "exc",
    [httpx.InvalidURL("bad"), httpx.LocalProtocolError("bad"), httpx.UnsupportedProtocol("x")],
)
async def test_unsendable_request_is_not_retried(exc: Exception) -> None:
    provider, recorder, _ = make_provider(exc, "text_ok", retry=RETRIES)

    with pytest.raises(LLMUnavailableError, match="could not be sent"):
        await _complete(provider)

    assert len(recorder.requests) == 1


async def test_non_json_answer_is_unavailable_without_retry() -> None:
    provider, recorder, _ = make_provider("malformed_200", "text_ok", retry=RETRIES)

    with pytest.raises(LLMUnavailableError, match="unexpected response"):
        await _complete(provider)

    assert len(recorder.requests) == 1


async def test_decoding_error_is_an_unexpected_response() -> None:
    provider, recorder, _ = make_provider(httpx.DecodingError("bad gzip"), "text_ok")

    with pytest.raises(LLMUnavailableError, match="unexpected response"):
        await _complete(provider)

    assert len(recorder.requests) == 1


async def test_unknown_exceptions_propagate_unchanged() -> None:
    provider, _, _ = make_provider(RuntimeError("boom"))

    with pytest.raises(RuntimeError, match="boom"):
        await _complete(provider)


def test_classify_ignores_unrecognized_exceptions() -> None:
    assert _classify(RuntimeError("x"), MODEL, lambda: NOW) is None


ERROR_FIXTURES = [
    "error_400",
    "error_400_api_key_invalid",
    "error_400_echo",
    "error_401",
    "error_403",
    "error_404_model",
    "error_429_bare",
    "error_429_daily_quota",
    "error_429_zero_quota",
    "error_429_retry_delay_long",
    "error_500",
    "error_503",
    "error_504",
    "malformed_200",
    "blocked_prompt",
    "stopped_safety",
    "text_max_tokens",
    "text_empty",
]


@pytest.mark.parametrize("fixture", ERROR_FIXTURES)
async def test_errors_and_logs_leak_nothing(fixture: str, caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = make_provider(fixture, retry=RetryPolicy(max_retries=0))

    with caplog.at_level(logging.DEBUG, logger="invio"), pytest.raises(LLMError) as info:
        await _complete(provider)

    error = info.value
    text = str(error) + repr(error) + getattr(error, "errors", "")
    assert MODEL in str(error)
    for secret in (API_KEY, SYSTEM, USER, "PROMPT-ECHO", "PARTIAL-ANSWER-TEXT", '{"error"'):
        assert secret not in text
        assert secret not in str([r.__dict__ for r in caplog.records])
    for chained in (error.__cause__, error.__context__):
        assert chained is None or not isinstance(chained, google_errors.APIError)


async def test_error_detail_is_truncated() -> None:
    provider, _, _ = make_provider("error_400_echo")

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert "bad request" in str(info.value)
    assert len(str(info.value)) < 500


async def test_retry_log_carries_only_the_known_fields(caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = make_provider("error_429_retry_delay", "error_503", "text_ok", retry=RETRIES)

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    first, second = [r for r in caplog.records if r.getMessage() == "llm.retry"]
    assert (first.provider, first.model, first.attempt) == ("google", MODEL, 1)  # type: ignore[attr-defined]
    assert (first.status, first.failure, first.wait_s) == (429, "rate_limit", 7.0)  # type: ignore[attr-defined]
    assert (second.attempt, second.status, second.failure) == (2, 503, "server")  # type: ignore[attr-defined]
    dump = str([r.__dict__ for r in caplog.records])
    assert API_KEY not in dump
    assert "PROMPT-TEXT-SENTINEL" not in dump
    assert all(r.exc_info is None for r in caplog.records)


# --- QA gap coverage (issue #32) -------------------------------------------------------------


def _response(candidate: dict[str, object] | None, usage: dict[str, int]) -> httpx.Response:
    body: dict[str, object] = {"usageMetadata": usage, "modelVersion": "recorded"}
    if candidate is not None:
        body["candidates"] = [{"index": 0, **candidate}]
    return httpx.Response(200, json=body)


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_stopped_answer_counts_thinking_tokens_in_the_attached_usage(
    operation: str,
) -> None:
    """FR-007 + FR-013b: a blocked answer carries the usage, thought tokens included."""
    stopped = _response(
        {"finishReason": "SAFETY", "content": {"role": "model", "parts": [{"text": "x"}]}},
        {"promptTokenCount": 12, "candidatesTokenCount": 3, "thoughtsTokenCount": 40},
    )
    provider, recorder, _ = make_provider(stopped, "json_ok")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _call(provider, operation)

    assert info.value.usage == Usage(12, 43)
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_blocked_prompt_with_partial_usage_attaches_zero_output(operation: str) -> None:
    """Edge case: missing counts are zero; the blocked call's input tokens stay attached."""
    provider, _, _ = make_provider("blocked_prompt_other")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _call(provider, operation)

    assert info.value.usage == Usage(12, 0)


async def test_partial_usage_counts_are_zero_and_summed_over_the_repair() -> None:
    """Edge case: ``no_candidates`` reports only the prompt count; the repair adds its own."""
    provider, _, _ = make_provider("no_candidates", "json_ok")

    _, usage = await _structured(provider)

    assert usage == Usage(24, 3, 2)


async def test_structured_usage_counts_thinking_tokens() -> None:
    """FR-013b for structured output: thought tokens are output tokens."""
    answer = _response(
        {
            "finishReason": "STOP",
            "content": {"role": "model", "parts": [{"text": '{"score": 0.8, "reason": "fits"}'}]},
        },
        {"promptTokenCount": 12, "candidatesTokenCount": 3, "thoughtsTokenCount": 20},
    )
    provider, _, _ = make_provider(answer)

    _, usage = await _structured(provider)

    assert usage == Usage(12, 23)


class _Holder(BaseModel):
    """An optional nested sub-model and an optional list of them."""

    first: Score | None = None
    rest: list[Score] | None = None


async def test_optional_nested_references_round_trip() -> None:
    """US1-5 / edge case: optional fields with shared sub-definitions validate completely."""
    text = json.dumps(
        {"first": {"score": 0.5, "reason": "a"}, "rest": [{"score": 0.1, "reason": "b"}]}
    )
    answer = _response(
        {"finishReason": "STOP", "content": {"role": "model", "parts": [{"text": text}]}},
        {"promptTokenCount": 1, "candidatesTokenCount": 1},
    )
    provider, recorder, _ = make_provider(answer)

    value, _ = await _structured(provider, _Holder)

    assert value.first == Score(score=0.5, reason="a")
    assert value.rest == [Score(score=0.1, reason="b")]
    sent = recorder.requests[0].body["generationConfig"]["responseJsonSchema"]
    assert "$ref" not in repr(sent)
    assert "$defs" not in sent


# SC-005: the structured path yields the same error type and retry behaviour per category.
STRUCTURED_FAILURES = [
    pytest.param(("error_401", "json_ok"), LLMAuthError, 1, id="auth"),
    pytest.param(("error_429_daily_quota", "json_ok"), LLMQuotaError, 1, id="quota"),
    pytest.param(("error_400", "json_ok"), LLMInvalidRequestError, 1, id="rejected"),
    pytest.param(("error_404_model", "json_ok"), LLMInvalidRequestError, 1, id="unknown-model"),
    pytest.param(("error_503",) * 3, LLMUnavailableError, 3, id="server"),
    pytest.param(("error_429_bare",) * 3, LLMRateLimitError, 3, id="rate-limit"),
    pytest.param((httpx.ReadTimeout("slow"), "json_ok"), LLMUnavailableError, 1, id="timeout"),
    pytest.param(("malformed_200", "json_ok"), LLMUnavailableError, 1, id="malformed"),
    pytest.param(("text_max_tokens", "json_ok"), LLMUnavailableError, 1, id="cut-off"),
]


@pytest.mark.parametrize(("replies", "error", "requests"), STRUCTURED_FAILURES)
async def test_structured_failure_categories_match_free_text(
    replies: tuple[object, ...], error: type[LLMError], requests: int
) -> None:
    provider, recorder, _ = make_provider(*replies, retry=RETRIES)

    with pytest.raises(error) as info:
        await _structured(provider)

    assert (info.value.provider, info.value.model) == ("google", MODEL)
    assert len(recorder.requests) == requests


async def test_structured_rate_limit_is_retried_after_the_hinted_wait() -> None:
    provider, recorder, waits = make_provider("error_429_retry_delay", "json_ok", retry=RETRIES)

    value, usage = await _structured(provider)

    assert value.score == 0.8
    assert usage.requests == 1
    assert waits == [7.0]
    assert len(recorder.requests) == 2


async def test_structured_hanging_request_hits_the_call_deadline() -> None:
    """FR-012 for structured output."""
    provider, recorder, _ = make_provider(HANG, "json_ok", retry=RETRIES, timeout_seconds=0.05)

    with pytest.raises(LLMUnavailableError):
        await _structured(provider)

    assert len(recorder.requests) == 1


# SC-008 / FR-011: every error path of both operations, including the ones not in ERROR_FIXTURES.
LEAK_REPLIES: list[object] = [
    *ERROR_FIXTURES,
    "error_429_retry_delay",
    "error_429_retry_after_header",
    "blocked_prompt_other",
    "stopped_recitation",
    "stopped_spii",
    "no_candidates",
    httpx.ReadTimeout("slow"),
    httpx.ConnectError("down"),
]


@pytest.mark.parametrize("operation", OPERATIONS)
@pytest.mark.parametrize("reply", LEAK_REPLIES, ids=str)
async def test_every_error_path_leaks_nothing_through_the_logged_provider(
    operation: str, reply: object, caplog: pytest.LogCaptureFixture
) -> None:
    replies = (reply,) if operation == "complete" else (reply, "json_invalid")
    provider, _, _ = make_provider(*replies, retry=RetryPolicy(max_retries=0))
    logged = _LoggedProvider(provider, name="google", registry=default_registry())

    with caplog.at_level(logging.DEBUG, logger="invio"), pytest.raises(LLMError) as info:
        if operation == "complete":
            await logged.complete(SYSTEM, USER, model=MODEL, temperature=0, max_tokens=5)
        else:
            await logged.complete_structured(SYSTEM, USER, Score, model=MODEL, temperature=0)

    error = info.value
    assert MODEL in str(error)
    assert error.provider == "google"
    text = str(error) + repr(error) + getattr(error, "errors", "")
    dump = str([r.__dict__ for r in caplog.records])
    for secret in (API_KEY, SYSTEM, USER, "PROMPT-ECHO", "PARTIAL-ANSWER-TEXT", '{"error"'):
        assert secret not in text
        assert secret not in dump
    assert [r for r in caplog.records if r.getMessage() == "llm.error"]


# FR-015: the per-call log line.


async def test_successful_call_logs_one_llm_call_record(caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = make_provider("text_with_thoughts")
    logged = _LoggedProvider(provider, name="google", registry=default_registry())

    with caplog.at_level(logging.INFO, logger="invio.llm"):
        await logged.complete(SYSTEM, USER, model=MODEL, temperature=0, max_tokens=50)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.call"]
    assert (record.provider, record.model) == ("google", MODEL)  # type: ignore[attr-defined]
    assert (record.input_tokens, record.output_tokens) == (12, 43)  # type: ignore[attr-defined]
    assert record.cost_usd is not None  # type: ignore[attr-defined]
    assert record.repaired is False  # type: ignore[attr-defined]
    assert record.duration_ms >= 0  # type: ignore[attr-defined]


async def test_repaired_structured_call_logs_the_repair_flag_and_summed_tokens(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, _, _ = make_provider("json_invalid", "json_ok")
    logged = _LoggedProvider(provider, name="google", registry=default_registry())

    with caplog.at_level(logging.INFO, logger="invio.llm"):
        await logged.complete_structured(SYSTEM, USER, Score, model=MODEL, temperature=0)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.call"]
    assert record.repaired is True  # type: ignore[attr-defined]
    assert (record.input_tokens, record.output_tokens) == (24, 6)  # type: ignore[attr-defined]


# --- hardening: unions, malformed bodies, finish reasons, retry hints ------------------------


class _Cat(BaseModel):
    kind: Literal["cat"]
    lives: int


class _Dog(BaseModel):
    kind: Literal["dog"]
    good: bool


class _Owner(BaseModel):
    pet: _Cat | _Dog = Field(discriminator="kind")


async def test_discriminated_union_round_trips_with_its_shape_sent() -> None:
    body = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [{"text": '{"pet": {"kind": "dog", "good": true}}'}],
                },
                "finishReason": "STOP",
            }
        ],
        "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3},
    }
    provider, recorder, _ = make_provider(httpx.Response(200, json=body))

    value, _ = await _structured(provider, _Owner)

    assert value.pet == _Dog(kind="dog", good=True)
    sent = json.dumps(recorder.requests[0].body["generationConfig"]["responseJsonSchema"])
    assert '"enum": ["dog"]' in sent
    assert '"enum": ["cat"]' in sent


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_malformed_response_body_is_unavailable_without_leaking(operation: str) -> None:
    provider, recorder, _ = make_provider("malformed_usage", "text_ok", retry=RETRIES)

    with pytest.raises(LLMUnavailableError, match="unexpected response") as info:
        await _call(provider, operation)

    text = str(info.value) + repr(info.value)
    assert "ANSWER-SENTINEL" not in text
    assert "PROMPT-TEXT-SENTINEL" not in text
    assert "promptTokenCount" not in text
    assert len(recorder.requests) == 1
    assert not isinstance(info.value.__cause__, ValidationError)
    assert not isinstance(info.value.__context__, ValidationError)


@pytest.mark.parametrize("operation", OPERATIONS)
async def test_non_policy_finish_reason_is_unavailable_without_repair(operation: str) -> None:
    provider, recorder, _ = make_provider("stopped_other", "json_ok")

    with pytest.raises(LLMUnavailableError, match="incomplete: OTHER") as info:
        await _call(provider, operation)

    assert "ANSWER-SENTINEL" not in str(info.value)
    assert len(recorder.requests) == 1


async def test_retry_delay_without_the_seconds_suffix_is_ignored() -> None:
    body = {
        "error": {
            "code": 429,
            "message": "slow down",
            "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7"}],
        }
    }
    provider, _, waits = make_provider(httpx.Response(429, json=body), "text_ok", retry=RETRIES)

    await _complete(provider)

    assert waits == [1.0]


async def test_retry_delay_does_not_change_non_rate_limit_failures() -> None:
    body = {
        "error": {
            "code": 503,
            "message": "overloaded",
            "status": "UNAVAILABLE",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}],
        }
    }
    provider, _, waits = make_provider(httpx.Response(503, json=body), "text_ok", retry=RETRIES)

    await _complete(provider)

    assert waits == [1.0]
