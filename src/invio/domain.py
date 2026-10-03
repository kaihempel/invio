"""Shared research item records used by all pipeline tracks.

Dependency-free on purpose: standard library imports only, so any module (sources, db, graph,
notify) can import these without pulling in pydantic, YAML, database or network packages.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ItemType = Literal["article", "video"]


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
