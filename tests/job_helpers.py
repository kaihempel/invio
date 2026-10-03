"""Shared constants and helpers for the job configuration tests."""

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from invio.config.job import JobConfig

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "docs" / "job.example.yaml"
SCHEMA = REPO_ROOT / "docs" / "job.schema.json"

# The broken job from specs/001-gh-issue-3/quickstart.md: field errors in every section, plus
# cross-field errors (weekday missing, fallback == provider) hidden until those are fixed.
BAD_JOB = """\
schedule: {frequency: weekly, time: "25:00", timezone: Europe/Atlantis}
notification: {to: [not-an-email], subject: x}
sources: []
search: {semantic_description: " ", min_relevance: 1.5}
llm: {provider: openai, models: {fast: a, smart: b}, fallback_provider: openai, frequncy: 1}
"""


def set_path(data: dict[str, Any], path: str, value: Any) -> None:
    """Set a dotted ``path`` in ``data``, creating missing intermediate mappings."""
    *parents, last = path.split(".")
    node = data
    for key in parents:
        node = node.setdefault(key, {})
    node[last] = value


def validation_errors(data: dict[str, Any]) -> list[tuple[tuple[Any, ...], str]]:
    """Validate ``data`` as a ``JobConfig`` (which must fail); return ``(loc, msg)`` per error."""
    with pytest.raises(ValidationError) as info:
        JobConfig.model_validate(data)
    return [(e["loc"], e["msg"]) for e in info.value.errors()]
