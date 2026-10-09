"""Optional live checks against the real Gemini API (opt-in: ``pytest -m live``)."""

import os

import pytest

from invio.graph.nodes.relevance import RelevanceResult
from invio.graph.nodes.summarize_item import ItemSummary
from invio.llm.google import GoogleProvider
from invio.llm.registry import default_registry
from tests.llm_helpers import make_settings

# Captured at import: the autouse ``isolated_settings`` fixture removes INVIO_* per test.
API_KEY = os.environ.get("INVIO_GOOGLE_API_KEY")

SYSTEM = "You rate how well a sentence describes a sunny day."
USER = "The sun is shining and there is not a cloud in the sky."


def _cheapest_model() -> str:
    cheapest = default_registry().cheapest("google")
    assert cheapest is not None
    return cheapest.model_id


def _smartest_model() -> str:
    return default_registry().models_for("google")[-1].model_id


def _provider() -> GoogleProvider:
    if not API_KEY:
        pytest.skip("INVIO_GOOGLE_API_KEY not set")
    return GoogleProvider.from_settings(make_settings(google_api_key=API_KEY))


@pytest.mark.live
async def test_live_connectivity_check() -> None:
    provider = _provider()

    try:
        text, usage = await provider.complete(
            "Connectivity check.",
            "Reply with OK.",
            model=_cheapest_model(),
            temperature=0,
            max_tokens=5,
        )
    finally:
        await provider.aclose()

    assert text.strip()
    assert usage.input_tokens > 0


@pytest.mark.live
async def test_live_structured_relevance_result() -> None:
    # Checks that the service accepts the converted RelevanceResult schema.
    provider = _provider()

    try:
        value, usage = await provider.complete_structured(
            SYSTEM, USER, RelevanceResult, model=_cheapest_model(), temperature=0
        )
    finally:
        await provider.aclose()

    assert 0 <= value.score <= 1
    assert usage.input_tokens > 0


@pytest.mark.live
async def test_live_structured_item_summary() -> None:
    # Checks that the service accepts the converted ItemSummary schema (minItems/maxItems).
    provider = _provider()

    try:
        value, usage = await provider.complete_structured(
            "Summarize the text for a research digest.",
            USER,
            ItemSummary,
            model=_smartest_model(),
            temperature=0,
        )
    finally:
        await provider.aclose()

    assert 3 <= len(value.bullets) <= 6
    assert usage.input_tokens > 0
