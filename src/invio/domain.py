"""Shared research item records used by all pipeline tracks.

Dependency-free on purpose: standard library imports only, so any module (sources, db, graph,
notify) can import these without pulling in pydantic, YAML, database or network packages.

Besides the item records it defines the shared status vocabularies (``ItemStatus``,
``RunStatus``, ``NotificationStatus``), ``COST_PRECISION`` and ``url_hash``, the identity of an
item within a job.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

ItemType = Literal["article", "video"]

COST_PRECISION = Decimal("0.000001")  # USD cost precision, matches llm_usage.cost_usd (12, 6)


class ItemStatus(StrEnum):
    """Processing state of a stored item."""

    NEW = "new"
    EXTRACTED = "extracted"
    SKIPPED_KEYWORD = "skipped_keyword"
    SKIPPED_IRRELEVANT = "skipped_irrelevant"
    SKIPPED_BASELINE = "skipped_baseline"
    RELEVANT = "relevant"
    SUMMARIZED = "summarized"
    FAILED = "failed"


class RunStatus(StrEnum):
    """Outcome of a job run."""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARTIAL = "partial"
    FAILED = "failed"


class NotificationStatus(StrEnum):
    """Delivery state of a notification."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    SKIPPED = "skipped"


def url_hash(url: str) -> str:
    """Return the lower-case SHA-256 hex digest (64 chars) of the UTF-8 encoded ``url``."""
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    """An item discovered by a source adapter, before any content is fetched."""

    url: str
    url_hash: str
    title: str
    published_at: datetime | None
    type: ItemType
    teaser: str | None
    content_hash: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessedItem:
    """A fetched item with optional relevance score, summary or processing error.

    Use ``dataclasses.replace`` to produce updated copies.
    """

    id: int
    url: str
    type: ItemType
    title: str
    raw_content: str
    relevance: float | None = None
    summary: str | None = None
    error: str | None = None
