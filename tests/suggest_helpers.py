"""Shared builders for the ``invio job suggest`` tests (not shipped)."""

import json
from typing import Any

from invio.services.suggest import SearchSuggestion

DESCRIPTION = "Relevant: heat pump news.\nNot relevant: job ads."
HINT = "Trade portals\nIndustry associations"


def answer_data(**overrides: Any) -> dict[str, Any]:
    """A valid model answer as a mapping; ``overrides`` replace single fields."""
    data: dict[str, Any] = {
        "keywords_any": ["Wärmepumpe", "heat pump"],
        "keywords_all": [],
        "keywords_exclude": ["Stellenangebot"],
        "semantic_description": DESCRIPTION,
        "suggested_sources_hint": HINT,
    }
    data.update(overrides)
    return data


def answer_json(**overrides: Any) -> str:
    """A valid model answer as JSON text."""
    return json.dumps(answer_data(**overrides))


def make_suggestion(**overrides: Any) -> SearchSuggestion:
    """A normalised suggestion built from ``answer_data``."""
    data = answer_data(**overrides)
    return SearchSuggestion(
        keywords_any=tuple(data["keywords_any"]),
        keywords_all=tuple(data["keywords_all"]),
        keywords_exclude=tuple(data["keywords_exclude"]),
        semantic_description=data["semantic_description"],
        suggested_sources_hint=data["suggested_sources_hint"],
    )
