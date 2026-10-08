"""Optional live checks against the real OpenAI API (opt-in: ``pytest -m live``)."""

import os

import pytest

from invio.llm.openai import OpenAIProvider
from invio.llm.registry import default_registry
from tests.llm_helpers import Score, make_settings

# Captured at import: the autouse ``isolated_settings`` fixture removes INVIO_* per test.
API_KEY = os.environ.get("INVIO_OPENAI_API_KEY")


def _cheapest_model() -> str:
    cheapest = default_registry().cheapest("openai")
    assert cheapest is not None
    return cheapest.model_id


def _provider() -> OpenAIProvider:
    if not API_KEY:
        pytest.skip("INVIO_OPENAI_API_KEY not set")
    return OpenAIProvider.from_settings(make_settings(openai_api_key=API_KEY))


@pytest.mark.live
async def test_live_connectivity_check() -> None:
    provider = _provider()

    try:
        text, usage = await provider.complete(
            "Connectivity check.",
            "Reply with OK.",
            model=_cheapest_model(),
            temperature=0,
            # Below the Responses API minimum of 16: checks that the provider raises it.
            max_tokens=5,
        )
    finally:
        await provider.aclose()

    assert text.strip()
    assert usage.input_tokens > 0


@pytest.mark.live
async def test_live_structured_output() -> None:
    # Checks that OpenAI's strict mode accepts the schema the provider sends.
    provider = _provider()

    try:
        value, usage = await provider.complete_structured(
            "You rate how well a sentence describes a sunny day.",
            "The sun is shining and there is not a cloud in the sky.",
            Score,
            model=_cheapest_model(),
            temperature=0,
        )
    finally:
        await provider.aclose()

    assert 0 <= value.score <= 1
    assert usage.input_tokens > 0
