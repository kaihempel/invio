"""Optional live checks against a real local Ollama server (opt-in: ``pytest -m live``).

The server is ``INVIO_OLLAMA_BASE_URL`` (default ``http://localhost:11434``) and the model
``INVIO_OLLAMA_LIVE_MODEL`` (default: the fast model of ``models.d/ollama.yaml``); pull it first
(``ollama pull llama3.2:3b``). The tests are skipped when the server is unreachable.
"""

import os

import httpx2
import pytest

from invio.llm.ollama import CONNECT_TIMEOUT_S, OllamaProvider
from invio.llm.registry import default_registry
from tests.llm_helpers import Score, make_settings

# Captured at import: the autouse ``isolated_settings`` fixture removes INVIO_* per test.
BASE_URL = os.environ.get("INVIO_OLLAMA_BASE_URL") or "http://localhost:11434"
MODEL = os.environ.get("INVIO_OLLAMA_LIVE_MODEL")


def _model() -> str:
    if MODEL:
        return MODEL
    models = default_registry().models_for("ollama")
    assert models
    return models[0].model_id


async def _provider() -> OllamaProvider:
    try:
        async with httpx2.AsyncClient(timeout=CONNECT_TIMEOUT_S) as client:
            (await client.get(BASE_URL.rstrip("/") + "/api/version")).raise_for_status()
    except httpx2.HTTPError:
        pytest.skip(f"no Ollama server reachable at {BASE_URL}")
    return OllamaProvider.from_settings(
        make_settings(ollama_base_url=BASE_URL, llm_timeout_seconds=300)
    )


@pytest.mark.live
async def test_live_connectivity_check() -> None:
    provider = await _provider()

    try:
        text, usage = await provider.complete(
            "Connectivity check.", "Reply with OK.", model=_model(), temperature=0, max_tokens=5
        )
    finally:
        await provider.aclose()

    assert text.strip()
    assert usage.input_tokens > 0


@pytest.mark.live
async def test_live_structured_output() -> None:
    # Checks that the server accepts the schema-valued ``format`` (or falls back to JSON mode).
    provider = await _provider()

    try:
        value, usage = await provider.complete_structured(
            "You rate how well a sentence describes a sunny day.",
            "The sun is shining and there is not a cloud in the sky.",
            Score,
            model=_model(),
            temperature=0,
        )
    finally:
        await provider.aclose()

    assert 0 <= value.score <= 1
    assert usage.input_tokens > 0
