"""Tests for the Ollama provider, driven through recorded HTTP fixtures (no network)."""

import asyncio
import json
import logging
import time
import traceback
from pathlib import Path
from typing import Any

import httpx2
import pytest
import yaml

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
)
from invio.llm.http_retry import RetryPolicy
from invio.llm.ollama import CONNECT_TIMEOUT_S, OllamaProvider, _classify
from invio.llm.registry import default_registry
from tests.llm_helpers import Score, make_settings
from tests.ollama_helpers import BASE_URL, HANG, make_provider

SYSTEM = "You rate things."
USER = "PROMPT-TEXT-SENTINEL rate this"
MODEL = "llama3.2:3b"


async def _complete(provider: OllamaProvider) -> tuple[str, Usage]:
    return await provider.complete(SYSTEM, USER, model=MODEL, temperature=0.2, max_tokens=50)


async def _structured(provider: OllamaProvider) -> tuple[Score, Usage]:
    return await provider.complete_structured(SYSTEM, USER, Score, model=MODEL, temperature=0.2)


def _chat(content: Any, **extra: Any) -> httpx2.Response:
    return httpx2.Response(
        200, json={"message": {"role": "assistant", "content": content}, **extra}
    )


# --- construction and registration --------------------------------------------------------


def test_from_settings_needs_no_api_key() -> None:
    provider = OllamaProvider.from_settings(make_settings(llm_timeout_seconds=7))

    assert provider.timeout_seconds == 7
    assert "OllamaProvider" in repr(provider)


def test_from_settings_with_blank_base_url_is_a_config_error() -> None:
    with pytest.raises(LLMConfigError, match="INVIO_OLLAMA_BASE_URL"):
        OllamaProvider.from_settings(make_settings(ollama_base_url="  "))


def test_connect_timeout_is_at_most_five_seconds() -> None:
    provider = OllamaProvider.from_settings(make_settings(llm_timeout_seconds=60))

    assert CONNECT_TIMEOUT_S <= 5
    assert provider.http_timeout.connect == CONNECT_TIMEOUT_S
    assert provider.http_timeout.read == 60


async def test_every_request_carries_the_connect_timeout() -> None:
    provider, recorder, _ = make_provider("chat_ok", timeout_seconds=30)

    await _complete(provider)

    (timeout,) = recorder.timeouts
    assert timeout["connect"] == CONNECT_TIMEOUT_S
    assert timeout["read"] == 30


async def test_default_client_uses_the_connect_timeout() -> None:
    provider = OllamaProvider(BASE_URL, timeout_seconds=60)

    client = provider._client_factory()
    try:
        assert client.timeout.connect == CONNECT_TIMEOUT_S
    finally:
        await client.aclose()


def test_provider_is_registered_and_discoverable() -> None:
    factory._discover()

    provider = factory.get_provider("ollama", make_settings())

    assert provider is not None
    assert factory._REGISTRY["ollama"] is OllamaProvider


def test_constructing_the_provider_creates_no_http_client() -> None:
    _, recorder, _ = make_provider()

    assert recorder.clients_created == 0


# --- complete() ---------------------------------------------------------------------------


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
    assert (request.method, request.host, request.path) == ("POST", "ollama.test", "/api/chat")
    assert not request.has_authorization
    assert request.body == {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": USER},
        ],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 50},
    }


async def test_temperature_zero_is_sent_explicitly() -> None:
    provider, recorder, _ = make_provider("chat_ok")

    await provider.complete(SYSTEM, USER, model=MODEL, temperature=0, max_tokens=5)

    assert recorder.requests[0].body["options"] == {"temperature": 0, "num_predict": 5}


@pytest.mark.parametrize("base_url", ["http://ollama.test:11434/", " http://ollama.test:11434 "])
async def test_base_url_is_normalised(base_url: str) -> None:
    provider, recorder, _ = make_provider("chat_ok", base_url=base_url)

    await _complete(provider)

    assert recorder.requests[0].path == "/api/chat"


async def test_base_url_path_prefix_is_kept() -> None:
    provider, recorder, _ = make_provider("chat_ok", base_url="http://proxy.test/ollama")

    await _complete(provider)

    assert recorder.requests[0].path == "/ollama/api/chat"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        ({}, Usage(0, 0)),
        ({"prompt_eval_count": 9}, Usage(9, 0)),
        ({"eval_count": 4}, Usage(0, 4)),
        ({"prompt_eval_count": "9", "eval_count": True}, Usage(0, 0)),
        ({"prompt_eval_count": -1, "eval_count": 2}, Usage(0, 2)),
    ],
)
async def test_missing_or_odd_token_counts_are_zero(extra: dict[str, Any], expected: Usage) -> None:
    provider, _, _ = make_provider(_chat("Hello", **extra))

    _, usage = await _complete(provider)

    assert usage == expected


@pytest.mark.parametrize("content", ["", None, 42, ["Hello"]])
async def test_empty_or_non_text_content_is_unavailable(content: Any) -> None:
    provider, recorder, waits = make_provider(_chat(content))

    with pytest.raises(LLMUnavailableError, match="no answer text") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("ollama", MODEL)
    assert len(recorder.requests) == 1
    assert waits == []


@pytest.mark.parametrize(
    "reply",
    [
        "malformed_200",
        httpx2.Response(200, content=b"<html>not json</html>"),
        httpx2.Response(200, json=["a", "list"]),
        httpx2.Response(200, json={"message": "text"}),
    ],
    ids=["no-message", "html", "list", "message-not-object"],
)
async def test_malformed_body_is_unavailable_without_retry(reply: Any) -> None:
    provider, recorder, waits = make_provider(reply)

    with pytest.raises(LLMUnavailableError, match="unexpected response") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("ollama", MODEL)
    assert info.value.__context__ is None
    assert len(recorder.requests) == 1
    assert waits == []


def test_provider_survives_successive_event_loops() -> None:
    provider, recorder, _ = make_provider("chat_ok", "chat_ok")

    first = asyncio.run(_complete(provider))
    second = asyncio.run(_complete(provider))

    assert first[0] == second[0] == "Hello"
    assert recorder.clients_created == 2


async def test_aclose_closes_the_client_of_the_running_loop() -> None:
    provider, recorder, _ = make_provider("chat_ok", "chat_ok")
    await _complete(provider)

    await provider.aclose()
    await provider.aclose()  # a second close is a no-op

    assert recorder.clients[0].is_closed
    await _complete(provider)
    assert recorder.clients_created == 2
    await provider.aclose()


# --- complete_structured() ----------------------------------------------------------------


async def test_structured_returns_validated_value() -> None:
    provider, _, _ = make_provider("structured_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert usage == Usage(12, 3)


async def test_structured_request_sends_the_json_schema_as_format() -> None:
    provider, recorder, _ = make_provider("structured_ok")

    await _structured(provider)

    (request,) = recorder.requests
    assert request.body["format"] == Score.model_json_schema()
    assert request.body["options"] == {"temperature": 0.2}
    assert request.body["stream"] is False
    assert request.body["model"] == MODEL


async def test_structured_repairs_once() -> None:
    provider, recorder, _ = make_provider("structured_invalid", "structured_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert usage == Usage(24, 6, requests=2)
    second = recorder.requests[1].body
    assert second["format"] == Score.model_json_schema()
    assert "Your previous answer" in second["messages"][1]["content"]


async def test_structured_invalid_twice_raises_invalid_output() -> None:
    provider, _, _ = make_provider("structured_invalid", "structured_invalid")

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(provider)

    assert info.value.usage == Usage(24, 6, requests=2)
    assert (info.value.provider, info.value.model) == ("ollama", MODEL)
    assert info.value.__context__ is None


async def test_rejected_schema_format_falls_back_to_json_mode(
    caplog: pytest.LogCaptureFixture,
) -> None:
    provider, recorder, waits = make_provider("error_400_format", "structured_ok")

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert usage == Usage(12, 3)
    first, second = recorder.requests
    assert first.body["format"] == Score.model_json_schema()
    assert second.body["format"] == "json"
    assert second.body["messages"] == first.body["messages"]
    assert waits == []
    (record,) = [r for r in caplog.records if r.getMessage() == "llm.format_fallback"]
    assert (record.provider, record.model) == ("ollama", MODEL)  # type: ignore[attr-defined]


async def test_json_mode_fallback_is_remembered_per_model() -> None:
    provider, recorder, _ = make_provider(
        "error_400_format", "structured_ok", "structured_ok", "structured_ok"
    )

    await _structured(provider)
    await _structured(provider)
    await provider.complete_structured(SYSTEM, USER, Score, model="qwen2.5:14b", temperature=0)

    formats = [request.body["format"] for request in recorder.requests]
    assert formats == [Score.model_json_schema(), "json", "json", Score.model_json_schema()]


async def test_json_mode_repair_stays_in_json_mode() -> None:
    provider, recorder, _ = make_provider("error_400_format", "structured_invalid", "structured_ok")

    value, usage = await _structured(provider)

    assert value == Score(score=0.8, reason="fits")
    assert usage == Usage(24, 6, requests=2)
    formats = [request.body["format"] for request in recorder.requests]
    assert formats == [Score.model_json_schema(), "json", "json"]


async def test_400_in_json_mode_too_raises_and_is_not_remembered() -> None:
    provider, recorder, _ = make_provider("error_400", "error_400", "structured_ok")

    with pytest.raises(LLMInvalidRequestError) as info:
        await _structured(provider)

    assert info.value.status == 400
    assert info.value.__context__ is None
    await _structured(provider)
    formats = [request.body["format"] for request in recorder.requests]
    assert formats == [Score.model_json_schema(), "json", Score.model_json_schema()]


@pytest.mark.parametrize("fixture", ["error_404_model", "error_401"])
async def test_other_rejections_do_not_fall_back(fixture: str) -> None:
    provider, recorder, _ = make_provider(fixture)

    with pytest.raises(LLMError):
        await _structured(provider)

    assert len(recorder.requests) == 1


# --- errors -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectError("[Errno 61] Connection refused"),
        httpx2.ConnectTimeout("connect timed out"),
        httpx2.ConnectError("[Errno 8] nodename nor servname provided"),
    ],
    ids=["refused", "connect-timeout", "dns"],
)
async def test_unreachable_server_is_unavailable_at_once_without_retry(
    failure: Exception,
) -> None:
    provider, recorder, waits = make_provider(failure, "chat_ok", retry=RetryPolicy(max_retries=3))

    started = time.perf_counter()
    with pytest.raises(LLMUnavailableError, match="unreachable") as info:
        await _complete(provider)

    assert time.perf_counter() - started < 1
    assert (info.value.provider, info.value.model) == ("ollama", MODEL)
    assert "INVIO_OLLAMA_BASE_URL" in str(info.value)
    assert len(recorder.requests) == 1
    assert waits == []


async def test_refused_connection_on_a_real_closed_port_fails_fast() -> None:
    # A port that was just bound and released: nothing listens there.
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    provider = OllamaProvider(f"http://127.0.0.1:{port}", timeout_seconds=30)

    started = time.perf_counter()
    try:
        with pytest.raises(LLMUnavailableError, match="unreachable"):
            await _complete(provider)
    finally:
        await provider.aclose()

    assert time.perf_counter() - started < CONNECT_TIMEOUT_S


async def test_model_not_found_is_an_invalid_request() -> None:
    provider, recorder, waits = make_provider("error_404_model")

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert info.value.status == 404
    assert "not found, try pulling it first" in str(info.value)
    assert "HTTP 404" in str(info.value)
    assert len(recorder.requests) == 1
    assert waits == []


async def test_rejected_request_names_status_and_detail() -> None:
    provider, _, waits = make_provider("error_400")

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert str(info.value) == (
        f"Ollama rejected the request (HTTP 400, model {MODEL}): invalid options: num_predict"
    )
    assert waits == []


async def test_proxy_refusing_access_is_an_auth_error_without_retry() -> None:
    provider, recorder, _ = make_provider("error_401")

    with pytest.raises(LLMAuthError, match="INVIO_OLLAMA_BASE_URL") as info:
        await _complete(provider)

    assert (info.value.provider, info.value.model) == ("ollama", MODEL)
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("fixture", ["error_500", "error_503"])
async def test_server_errors_are_retried_with_backoff(fixture: str) -> None:
    provider, recorder, waits = make_provider(*[fixture] * 4)

    with pytest.raises(LLMUnavailableError, match="server error") as info:
        await _complete(provider)

    assert len(recorder.requests) == 4
    assert waits == [1.0, 2.0, 4.0]
    assert f"HTTP {fixture[-3:]}" in str(info.value)


async def test_server_error_then_success() -> None:
    provider, recorder, waits = make_provider("error_500", "chat_ok")

    text, _ = await _complete(provider)

    assert text == "Hello"
    assert len(recorder.requests) == 2
    assert waits == [1.0]


async def test_busy_server_is_a_retried_rate_limit() -> None:
    provider, recorder, _ = make_provider(*["error_429"] * 4)

    with pytest.raises(LLMRateLimitError):
        await _complete(provider)

    assert len(recorder.requests) == 4


async def test_dropped_connection_is_retried() -> None:
    provider, recorder, waits = make_provider(httpx2.ReadError("reset"), "chat_ok")

    text, _ = await _complete(provider)

    assert text == "Hello"
    assert len(recorder.requests) == 2
    assert waits == [1.0]


async def test_read_timeout_is_unavailable_and_not_retried() -> None:
    provider, recorder, waits = make_provider(httpx2.ReadTimeout("x"))

    with pytest.raises(LLMUnavailableError, match="timed out"):
        await _complete(provider)

    assert len(recorder.requests) == 1
    assert waits == []


async def test_deadline_is_unavailable_and_not_retried() -> None:
    provider, recorder, waits = make_provider(HANG, timeout_seconds=0.5)

    with pytest.raises(LLMUnavailableError, match=r"did not answer within 0\.5 s"):
        await _complete(provider)

    assert len(recorder.requests) == 1
    assert waits == []


async def test_malformed_base_url_is_unavailable_without_retry() -> None:
    provider = OllamaProvider("localhost:11434", timeout_seconds=5)

    try:
        with pytest.raises(LLMUnavailableError, match="could not be sent") as info:
            await _complete(provider)
    finally:
        await provider.aclose()

    assert "INVIO_OLLAMA_BASE_URL" in str(info.value)


@pytest.mark.parametrize(
    ("failure", "kind"),
    [
        (httpx2.DecodingError("x"), "bad_response"),
        (httpx2.TooManyRedirects("x"), "bad_response"),
        (httpx2.UnsupportedProtocol("x"), "unsendable"),
        (httpx2.PoolTimeout("x"), "timeout"),
        (httpx2.RemoteProtocolError("x"), "connection"),
    ],
)
def test_classify_other_httpx_errors(failure: Exception, kind: str) -> None:
    result = _classify(failure, MODEL, lambda: None)  # type: ignore[arg-type,return-value]

    assert result is not None
    assert result.kind == kind
    assert result.retryable is (kind == "connection")


def test_classify_other_exception_returns_none() -> None:
    assert _classify(ValueError("x"), MODEL, lambda: None) is None  # type: ignore[arg-type,return-value]


async def test_error_message_is_bounded_and_free_of_the_response_body() -> None:
    huge = httpx2.Response(400, json={"error": "x" * 10_000, "extra": "BODY-LEAK"})
    provider, _, _ = make_provider(huge)

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert len(str(info.value)) <= 400
    assert "BODY-LEAK" not in str(info.value)


async def test_non_json_error_body_still_names_the_status() -> None:
    provider, _, _ = make_provider(httpx2.Response(404, content=b"<html>BODY-LEAK</html>"))

    with pytest.raises(LLMInvalidRequestError) as info:
        await _complete(provider)

    assert str(info.value) == f"Ollama rejected the request (HTTP 404, model {MODEL})"


@pytest.mark.parametrize(
    "reply",
    [
        "error_401",
        "error_404_model",
        "error_429",
        "error_503",
        "malformed_200",
        httpx2.ConnectError("boom SECRET-IN-EXC"),
        httpx2.ReadError("boom SECRET-IN-EXC"),
    ],
    ids=["401", "404", "429", "503", "malformed", "connect", "read"],
)
async def test_errors_never_expose_prompt_url_or_exception_text(reply: Any) -> None:
    provider, _, _ = make_provider(
        *([reply] * 4),
        retry=RetryPolicy(max_retries=3),
        base_url="http://user:URL-SECRET@ollama.test:11434",
    )

    with pytest.raises(LLMError) as info:
        await _complete(provider)

    chain = "".join(traceback.format_exception(info.value, chain=True))
    for text in (str(info.value), repr(info.value), repr(provider), chain):
        assert "PROMPT-TEXT-SENTINEL" not in text
        assert "SECRET-IN-EXC" not in text
        assert "URL-SECRET" not in text
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


async def test_retries_log_without_prompt_text(caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = make_provider("error_503", "chat_ok")

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        await _complete(provider)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.retry"]
    assert (record.provider, record.status, record.failure) == ("ollama", 503, "server")  # type: ignore[attr-defined]
    assert "PROMPT-TEXT-SENTINEL" not in str([r.__dict__ for r in caplog.records])


# --- registry, factory, cost --------------------------------------------------------------


def _shipped_document() -> dict[str, Any]:
    path = Path(invio.llm.__path__[0]) / "models.d" / "ollama.yaml"
    document: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    return document


def test_shipped_registry_lists_free_local_models() -> None:
    document = _shipped_document()
    models = default_registry().models_for("ollama")

    assert document["provider"] == "ollama"
    assert [info.model_id for info in models] == ["llama3.2:3b", "qwen2.5:14b"]
    assert all(info.input_price_per_mtok == info.output_price_per_mtok == 0 for info in models)
    assert all(info.context_window > 0 for info in models)


def test_cost_of_a_local_call_is_zero() -> None:
    registry = default_registry()

    for info in registry.models_for("ollama"):
        cost = registry.cost(info.model_id, Usage(1_000_000, 1_000_000))
        assert cost is not None
        assert cost == 0


def test_shipped_models_resolve_for_both_roles(job_data: dict[str, Any]) -> None:
    first, second = (info.model_id for info in default_registry().models_for("ollama"))
    job_data["llm"] = {"provider": "ollama", "models": {"fast": first, "smart": second}}
    llm = JobConfig.model_validate(job_data).llm

    fast, fast_model = factory.resolve(llm, "fast", make_settings())
    smart, smart_model = factory.resolve(llm, "smart", make_settings())

    assert (fast_model, smart_model) == (first, second)
    assert fast is not None and smart is not None


async def test_logged_call_reports_zero_cost(caplog: pytest.LogCaptureFixture) -> None:
    provider, _, _ = make_provider("chat_ok")
    logged = factory._LoggedProvider(provider, name="ollama", registry=default_registry())

    with caplog.at_level(logging.INFO, logger="invio.llm"):
        await logged.complete(SYSTEM, USER, model=MODEL, temperature=0, max_tokens=5)

    (record,) = [r for r in caplog.records if r.getMessage() == "llm.call"]
    assert record.cost_usd == "0.000000"  # type: ignore[attr-defined]
    assert json.dumps(record.__dict__, default=str).count("PROMPT-TEXT-SENTINEL") == 0
