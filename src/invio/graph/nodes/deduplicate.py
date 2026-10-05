"""Deduplication node: select the items a run processes and record the outcome per job.

Per-job dedupe, changed-version detection, bounded retries, baseline mode for new jobs, the
pending backlog (waiting and retryable items) and the ``max_items_per_run`` cap, all
newest-first and deterministic. Repositories flush, the caller owns the unit of work, ORM
objects never leave this module.
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
    "MAX_ATTEMPTS",
    "DedupResult",
    "DedupStats",
    "SelectReason",
    "SelectedItem",
    "deduplicate",
]

logger = logging.getLogger(__name__)

MAX_ATTEMPTS: Final = 3

SelectReason = Literal["new", "changed", "retry", "waiting"]

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

    ``waiting`` and ``retried`` also count stored pending items no source reported
    (``stored_only``), so ``new + changed + retried + waiting + dropped == found + stored_only``
    and ``selected + limit_cut + baseline_skipped == new + changed + retried + waiting``.
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
    """One eligible stored item moving through the node, with its newest-first sort key."""

    item: Item
    kind: SelectReason
    source_index: int
    key: _SortKey
    baseline_rejected: bool = False


def _newest_key(
    published_at: datetime | None, origin: int, source_index: int, tiebreak: int
) -> _SortKey:
    """Sort key: dated before undated, newer first; ties: reported, source order, position/id.

    Stored-only items (same origin and source index) end up ordered like ``list_pending``.
    """
    stamp = -published_at.timestamp() if published_at is not None else 0.0
    return (published_at is None, stamp, origin, source_index, tiebreak)


def _pending_kind(item: Item, run_id: int) -> SelectReason | None:
    """Return ``waiting``/``retry`` for pending items, ``None`` otherwise.

    Mirrors the SQL filter of ``ItemRepository.list_pending``. An item already taken by this
    very run is not pending, so repeating a call within one run is idempotent; taken by an
    earlier run and still unfinished it counts as interrupted (retry).
    """
    if item.run_id == run_id:
        return None
    if item.status == ItemStatus.NEW and item.attempts == 0:
        return "waiting"
    if item.attempts < MAX_ATTEMPTS and (
        item.status == ItemStatus.FAILED or (item.status == ItemStatus.NEW and item.attempts >= 1)
    ):
        return "retry"
    return None


def _is_changed(candidate: Candidate, item: Item) -> bool:
    return (
        item.content_hash is not None
        and candidate.content_hash is not None
        and item.content_hash != candidate.content_hash
    )


def _unique_candidates(
    candidates: Mapping[str, Sequence[Candidate]],
) -> list[tuple[int, Candidate]]:
    """Flatten to ``(source_index, candidate)`` in source order; first occurrence per url_hash."""
    seen: set[str] = set()
    unique: list[tuple[int, Candidate]] = []
    for source_index, group in enumerate(candidates.values()):
        for candidate in group:
            if candidate.url_hash not in seen:
                seen.add(candidate.url_hash)
                unique.append((source_index, candidate))
    return unique


def _reported_entries(
    items: ItemRepository, job_id: int, run_id: int, unique: list[tuple[int, Candidate]]
) -> tuple[list[_Entry], list[Item], int]:
    """Classify, update and insert the reported candidates.

    Returns the eligible entries, the stored rows of all reported candidates and the number of
    dropped candidates. Changed versions are reset; unknown candidates are inserted in input
    order, and those another writer inserted meanwhile are dropped (research R8).
    """
    known = items.find_many(job_id, [candidate.url_hash for _, candidate in unique])
    classified: list[tuple[int, int, Item, SelectReason]] = []
    unknown: list[tuple[int, int, Candidate]] = []
    changed: list[tuple[Item, Candidate]] = []
    backfill: list[tuple[Item, str]] = []
    dropped = 0
    for position, (source_index, candidate) in enumerate(unique):
        item = known.get(candidate.url_hash)
        if item is None:
            unknown.append((position, source_index, candidate))
            continue
        kind: SelectReason | None
        if _is_changed(candidate, item):
            kind = "changed"
            changed.append((item, candidate))
        else:
            kind = _pending_kind(item, run_id)
            if item.content_hash is None and candidate.content_hash is not None:
                backfill.append((item, candidate.content_hash))
        if kind is None:
            dropped += 1
        else:
            classified.append((position, source_index, item, kind))
    items.reset_versions(changed)
    items.set_content_hashes(backfill)

    reported_items = list(known.values())
    added = items.add_new(job_id, [candidate for *_, candidate in unknown])
    for (position, source_index, _), (item, created) in zip(unknown, added, strict=True):
        reported_items.append(item)
        if created:
            classified.append((position, source_index, item, "new"))
        else:
            dropped += 1

    entries = [
        _Entry(
            item,
            kind,
            source_index,
            _newest_key(item.published_at, _REPORTED, source_index, position),
        )
        for position, source_index, item, kind in classified
    ]
    return entries, reported_items, dropped


def _reject_baseline(entries: list[_Entry], keep: int) -> None:
    """Mark all but the newest ``keep`` new entries of each source as rejected.

    Changed, retried and waiting items are known work and never rejected by the baseline.
    """
    by_source: dict[int, list[_Entry]] = {}
    for entry in entries:
        if entry.kind == "new":
            by_source.setdefault(entry.source_index, []).append(entry)
    for group in by_source.values():
        group.sort(key=lambda e: e.key)
        for entry in group[keep:]:
            entry.baseline_rejected = True


def _stored_entries(
    items: ItemRepository,
    job_id: int,
    run_id: int,
    reported: list[Item],
    limit: int,
) -> tuple[list[_Entry], int, int]:
    """Load the newest ``limit`` pending items no source reported.

    Returns them with the total waiting and retry counts of all such items, including those
    beyond ``limit``.
    """
    reported_kinds = [_pending_kind(item, run_id) for item in reported]
    total_waiting, total_retry = items.count_pending(
        job_id, run_id=run_id, max_attempts=MAX_ATTEMPTS
    )
    waiting = total_waiting - reported_kinds.count("waiting")
    retry = total_retry - reported_kinds.count("retry")
    # Reported pending rows may come back too; fetching that many extra keeps ``limit`` others.
    overlap = len(reported_kinds) - reported_kinds.count(None)
    reported_ids = {item.id for item in reported}
    entries: list[_Entry] = []
    for item in items.list_pending(
        job_id, run_id=run_id, max_attempts=MAX_ATTEMPTS, limit=limit + overlap
    ):
        kind = _pending_kind(item, run_id)
        if item.id in reported_ids or kind is None:
            continue
        entries.append(_Entry(item, kind, 0, _newest_key(item.published_at, _STORED, 0, item.id)))
    return entries[:limit], waiting, retry


def _selected_item(entry: _Entry) -> SelectedItem:
    item = entry.item
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
    (no succeeded/partial run → newest ``limits.baseline_items`` new items per source key, the
    rest stored as ``skipped_baseline``), pending backlog (waiting and retryable items, also
    when no source reports them), ``limits.max_items_per_run`` newest-first. Flushes, never
    commits. Logs one structured ``deduplicated`` line with the counts of ``DedupStats``.

    Repeating a call with the same ``run_id`` is idempotent. An item taken by a different,
    earlier ``run_id`` that no downstream node has finished counts as interrupted and is retried.
    """
    items = ItemRepository(session)
    unique = _unique_candidates(candidates)
    reported, reported_items, dropped = _reported_entries(items, job_id, run_id, unique)

    baseline = not RunRepository(session).has_successful_run(job_id)
    if baseline:
        _reject_baseline(reported, limits.baseline_items)
        items.mark_status(
            [e.item for e in reported if e.baseline_rejected], ItemStatus.SKIPPED_BASELINE
        )
    eligible = [e for e in reported if not e.baseline_rejected]

    stored, stored_waiting, stored_retry = _stored_entries(
        items, job_id, run_id, reported_items, limits.max_items_per_run
    )
    chosen = sorted(eligible + stored, key=lambda e: e.key)[: limits.max_items_per_run]
    items.mark_taken([e.item for e in chosen], run_id)

    stats = DedupStats(
        found=len(unique),
        new=sum(1 for e in reported if e.kind == "new"),
        changed=sum(1 for e in reported if e.kind == "changed"),
        retried=sum(1 for e in reported if e.kind == "retry") + stored_retry,
        waiting=sum(1 for e in reported if e.kind == "waiting") + stored_waiting,
        dropped=dropped,
        baseline_skipped=len(reported) - len(eligible),
        limit_cut=len(eligible) + stored_waiting + stored_retry - len(chosen),
        selected=len(chosen),
        baseline=baseline,
    )
    _log_stats(stats)
    return DedupResult(items=[_selected_item(e) for e in chosen], stats=stats)
