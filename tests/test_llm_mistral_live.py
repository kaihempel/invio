"""Optional live check against the real Mistral API (opt-in: ``pytest -m live``)."""

import os

import pytest

from invio.llm.mistral import MistralProvider
from invio.llm.registry import default_registry
from tests.llm_helpers import make_settings

# Captured at import: the autouse ``isolated_settings`` fixture removes INVIO_* per test.
API_KEY = os.environ.get("INVIO_MISTRAL_API_KEY")


@pytest.mark.live
async def test_live_connectivity_check() -> None:
    if not API_KEY:
        pytest.skip("INVIO_MISTRAL_API_KEY not set")
    registry = default_registry()
    models = [
        info
        for model_id in registry.model_ids()
        if (info := registry.get(model_id)) is not None and info.provider == "mistral"
    ]
    cheapest = min(
        models, key=lambda m: (m.input_price_per_mtok + m.output_price_per_mtok, m.model_id)
    )
    provider = MistralProvider.from_settings(make_settings(mistral_api_key=API_KEY))

    text, usage = await provider.complete(
        "Connectivity check.",
        "Reply with OK.",
        model=cheapest.model_id,
        temperature=0,
        max_tokens=5,
    )

    assert text.strip()
    assert usage.input_tokens > 0
