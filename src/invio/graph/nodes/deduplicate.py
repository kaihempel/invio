"""Deduplication node: select the items a run processes and record the outcome per job.

Per-job dedupe, changed-version detection, bounded retries, baseline mode for new jobs, the
waiting backlog and the ``max_items_per_run`` cap, all newest-first and deterministic.
Repositories flush, the caller owns the unit of work, ORM objects never leave this module.
"""

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal, cast

from sqlalchemy.orm import Session

from invio.config.job import LimitsConfig
from invio.db.models import Item
from invio.db.repositories import ItemRepository, RunRepository
from invio.domain import Candidate, ItemStatus, ItemType

__all__ = [
    "BASELINE_REASON",
    "MAX_ATTEMPTS",
    "DedupResult",
    "DedupStats",
    "SelectReason",
    "SelectedItem",
    "deduplicate",
]

logger = logging.getLogger(__name__)

MAX_ATTEMPTS: Final = 3
BASELINE_REASON: Final = "baseline"

SelectReason = Literal["new", "changed", "retry", "waiting"]
_Kind = Literal["new", "changed", "retry", "waiting", "drop"]
_KINDS: Final[tuple[_Kind, ...]] = ("new", "changed", "retry", "waiting", "drop")

_REPORTED: Final = 0
_STORED: Final = 1

_SortKey = tuple[bool, float, int, int, int]


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedItem:
    """An item this run processes, copied from the stored row after the update."""

    id: int
    url: str
    url_hash: str
    type: ItemType
    title: str
    published_at: datetime | None
    teaser: str | None
    content_hash: str | None
    attempts: int
    reason: SelectReason


@dataclass(frozen=True, slots=True, kw_only=True)
class DedupStats:
    """Counts of one deduplication call.

    ``waiting`` also counts stored waiting items no source reported (``stored_only``), so
    ``new + changed + retried + waiting + dropped == found + stored_only`` and
    ``selected + limit_cut + baseline_skipped == new + changed + retried + waiting``.
    """

    found: int
    new: int
    changed: int
    retried: int
    waiting: int
    dropped: int
    baseline_skipped: int
    limit_cut: int
    selected: int
    baseline: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class DedupResult:
    """Selected items (newest first) and the counts."""

    items: list[SelectedItem]
    stats: DedupStats


@dataclass(slots=True)
class _Entry:
    """One unique reported candidate or stored waiting item moving through the node."""

    candidate: Candidate | None
    item: Item | None
    source_index: int
    position: int
    kind: _Kind = "drop"
    baseline_rejected: bool = False
    origin: int = _REPORTED

    @property
    def published_at(self) -> datetime | None:
        if self.item is not None:
            return self.item.published_at
        return self.candidate.published_at if self.candidate is not None else None


def _newest_key(
    published_at: datetime | None, origin: int, source_index: int, tiebreak: int
) -> _SortKey:
    """Sort key: dated before undated, newer first; ties: reported, source order, position/id."""
    stamp = -published_at.timestamp() if published_at is not None else 0.0
    return (published_at is None, stamp, origin, source_index, tiebreak)


def _entry_key(entry: _Entry) -> _SortKey:
    # Known items are ordered by their stored published_at (the candidate date is ignored for
    # retry/waiting items); changed items already carry the candidate's date via reset_version.
    tiebreak = entry.item.id if entry.origin == _STORED and entry.item else entry.position
    return _newest_key(entry.published_at, entry.origin, entry.source_index, tiebreak)


def _classify(candidate: Candidate, item: Item | None, run_id: int) -> _Kind:
    """Return the class of a reported candidate; the first matching rule wins.

    An item already taken by this very run is dropped, so repeating a call within one run is
    idempotent; taken by an earlier run and still unfinished it counts as interrupted (retry).
    """
    if item is None:
        return "new"
    if (
        item.content_hash is not None
        and candidate.content_hash is not None
        and item.content_hash != candidate.content_hash
    ):
        return "changed"
    if item.run_id == run_id:
        return "drop"
    if item.status == ItemStatus.NEW and item.attempts == 0:
        return "waiting"
    if item.attempts < MAX_ATTEMPTS and (
        item.status == ItemStatus.FAILED or (item.status == ItemStatus.NEW and item.attempts >= 1)
    ):
        return "retry"
    return "drop"


def _unique_entries(candidates: Mapping[str, Sequence[Candidate]]) -> list[_Entry]:
    """Flatten in source order then position; the first occurrence per url_hash wins."""
    seen: set[str] = set()
    entries: list[_Entry] = []
    for source_index, group in enumerate(candidates.values()):
        for candidate in group:
            if candidate.url_hash not in seen:
                seen.add(candidate.url_hash)
                entries.append(_Entry(candidate, None, source_index, len(entries)))
    return entries


def _reject_baseline(entries: list[_Entry], keep: int) -> None:
    """Mark all but the newest ``keep`` eligible entries of each source as rejected."""
    by_source: dict[int, list[_Entry]] = {}
    for entry in entries:
        if entry.kind != "drop":
            by_source.setdefault(entry.source_index, []).append(entry)
    for group in by_source.values():
        group.sort(key=_entry_key)
        for entry in group[keep:]:
            entry.baseline_rejected = True


def _persist(items: ItemRepository, job_id: int, entries: list[_Entry]) -> None:
    """Insert unknown entries in input order and apply the baseline skips."""
    for entry in entries:
        if entry.kind == "drop":
            continue
        if entry.item is None and entry.candidate is not None:
            entry.item, created = items.add(job_id, entry.candidate)
            if not created:
                # Inserted concurrently by another writer: not ours to process (research R8).
                entry.kind = "drop"
                entry.baseline_rejected = False
                continue
        if entry.baseline_rejected and entry.item is not None:
            items.mark_skipped(entry.item, ItemStatus.SKIPPED_IRRELEVANT, reason=BASELINE_REASON)


def _selected_item(entry: _Entry) -> SelectedItem:
    item = entry.item
    if item is None or entry.kind == "drop":
        raise ValueError("only persisted, eligible entries can be selected")
    return SelectedItem(
        id=item.id,
        url=item.url,
        url_hash=item.url_hash,
        type=cast(ItemType, item.type),
        title=item.title,
        published_at=item.published_at,
        teaser=item.teaser,
        content_hash=item.content_hash,
        attempts=item.attempts,
        reason=entry.kind,
    )


def _log_stats(stats: DedupStats) -> None:
    logger.info(
        "deduplicated",
        extra={
            "event": "deduplicate",
            "found": stats.found,
            "new": stats.new,
            "changed": stats.changed,
            "retried": stats.retried,
            "waiting": stats.waiting,
            "dropped": stats.dropped,
            "baseline_skipped": stats.baseline_skipped,
            "limit_cut": stats.limit_cut,
            "selected": stats.selected,
            "baseline": stats.baseline,
        },
    )


def deduplicate(
    session: Session,
    *,
    job_id: int,
    run_id: int,
    candidates: Mapping[str, Sequence[Candidate]],
    limits: LimitsConfig,
) -> DedupResult:
    """Select the items this run processes and record the outcome for the job.

    Per-job dedupe, changed-version detection, retries (< MAX_ATTEMPTS), baseline mode
    (no succeeded/partial run → newest ``limits.baseline_items`` per source key), waiting
    backlog, ``limits.max_items_per_run`` newest-first. Flushes, never commits. Logs one
    structured ``deduplicated`` line with the counts of ``DedupStats``.

    Repeating a call with the same ``run_id`` is idempotent. An item taken by a different,
    earlier ``run_id`` that no downstream node has finished counts as interrupted and is retried.
    """
    items = ItemRepository(session)
    entries = _unique_entries(candidates)

    known = items.find_many(job_id, [e.candidate.url_hash for e in entries if e.candidate])
    for entry in entries:
        if entry.candidate is None:
            continue
        entry.item = known.get(entry.candidate.url_hash)
        entry.kind = _classify(entry.candidate, entry.item, run_id)
        if entry.kind == "changed" and entry.item is not None:
            items.reset_version(entry.item, entry.candidate)

    baseline = not RunRepository(session).has_successful_run(job_id)
    if baseline:
        _reject_baseline(entries, limits.baseline_items)
    _persist(items, job_id, entries)

    reported = {e.candidate.url_hash for e in entries if e.candidate}
    stored = [
        _Entry(None, item, 0, 0, kind="waiting", origin=_STORED)
        for item in items.list_waiting(job_id)
        if item.url_hash not in reported
    ]

    eligible = sorted(
        [e for e in entries if e.kind != "drop" and not e.baseline_rejected] + stored,
        key=_entry_key,
    )
    chosen = eligible[: limits.max_items_per_run]
    items.mark_taken([e.item for e in chosen if e.item is not None], run_id)

    counts = {kind: sum(1 for e in [*entries, *stored] if e.kind == kind) for kind in _KINDS}
    stats = DedupStats(
        found=len(entries),
        new=counts["new"],
        changed=counts["changed"],
        retried=counts["retry"],
        waiting=counts["waiting"],
        dropped=counts["drop"],
        baseline_skipped=sum(1 for e in entries if e.baseline_rejected),
        limit_cut=len(eligible) - len(chosen),
        selected=len(chosen),
        baseline=baseline,
    )
    _log_stats(stats)
    return DedupResult(items=[_selected_item(e) for e in chosen], stats=stats)
