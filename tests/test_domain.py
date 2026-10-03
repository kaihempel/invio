"""Tests for the dependency-free domain records."""

import dataclasses
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from invio.domain import Candidate, ProcessedItem


def _candidate() -> Candidate:
    return Candidate(
        url="https://example.com/a",
        url_hash="h",
        title="T",
        published_at=datetime(2026, 10, 4, tzinfo=UTC),
        type="article",
        teaser="t",
        content_hash="c",
    )


def test_candidate_all_fields() -> None:
    c = _candidate()

    assert (c.url, c.url_hash, c.title, c.type, c.teaser, c.content_hash) == (
        "https://example.com/a",
        "h",
        "T",
        "article",
        "t",
        "c",
    )
    assert c.published_at is not None


def test_processed_item_defaults() -> None:
    item = ProcessedItem(id=1, url="u", type="video", title="T", raw_content="r")

    assert item.relevance is None
    assert item.summary is None
    assert item.error is None


def test_records_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        _candidate().title = "x"  # type: ignore[misc]


def test_records_are_keyword_only() -> None:
    with pytest.raises(TypeError):
        ProcessedItem(1, "u", "article", "T", "r")  # type: ignore[misc]


def test_replace_returns_updated_copy() -> None:
    item = ProcessedItem(id=1, url="u", type="video", title="T", raw_content="r")

    updated = dataclasses.replace(item, relevance=0.9, summary="s")

    assert updated.relevance == 0.9
    assert item.relevance is None


def test_import_loads_no_heavy_dependencies() -> None:
    code = (
        "import sys, invio.domain; "
        "bad = {'pydantic','yaml','sqlalchemy','httpx','requests','langgraph'} "
        "& {m.split('.')[0] for m in sys.modules}; "
        "sys.exit(sorted(bad) or 0)"
    )

    subprocess.run([sys.executable, "-c", code], check=True)
