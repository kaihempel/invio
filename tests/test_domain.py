"""Tests for the dependency-free domain records."""

import dataclasses
import enum
import hashlib
import subprocess
import sys
from datetime import UTC, datetime

import pytest

from invio.domain import (
    Candidate,
    ItemStatus,
    NotificationStatus,
    ProcessedItem,
    RunStatus,
    url_hash,
)


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
        "bad = {'pydantic','yaml','sqlalchemy','httpx2','requests','langgraph'} "
        "& {m.split('.')[0] for m in sys.modules}; "
        "sys.exit(sorted(bad) or 0)"
    )

    subprocess.run([sys.executable, "-c", code], check=True)


@pytest.mark.parametrize(
    ("enum_cls", "values"),
    [
        (
            ItemStatus,
            [
                "new",
                "extracted",
                "skipped_keyword",
                "skipped_irrelevant",
                "relevant",
                "summarized",
                "failed",
            ],
        ),
        (RunStatus, ["running", "succeeded", "partial", "failed"]),
        (NotificationStatus, ["pending", "sent", "failed", "skipped"]),
    ],
)
def test_status_enums_have_exact_values(enum_cls: type[enum.StrEnum], values: list[str]) -> None:
    assert [m.value for m in enum_cls] == values
    assert issubclass(enum_cls, enum.StrEnum)


def test_status_members_compare_equal_to_strings() -> None:
    assert ItemStatus.SKIPPED_KEYWORD == "skipped_keyword"


def test_url_hash_is_sha256_hex_of_utf8() -> None:
    url = "https://example.com/ä"

    digest = url_hash(url)

    assert len(digest) == 64
    assert digest == digest.lower()
    assert digest == hashlib.sha256(url.encode("utf-8")).hexdigest()
