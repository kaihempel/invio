"""Tests for the deduplication node (US1-US5 of issue #14, no network)."""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.orm import Session

from invio.config.job import LimitsConfig
from invio.db.models import Item, Job, Run
from invio.db.repositories import ItemRepository, RunRepository
from invio.domain import Candidate, ItemStatus, RunStatus
from invio.graph.nodes.deduplicate import (
    BASELINE_REASON,
    MAX_ATTEMPTS,
    DedupResult,
    deduplicate,
)
from tests.db_helpers import make_candidate, make_item, make_job, make_run

pytestmark = pytest.mark.db

DAY0 = datetime(2026, 1, 1, tzinfo=UTC)


def _job_with_success(
    session: Session, name: str = "job-a", status: RunStatus = RunStatus.SUCCEEDED
) -> Job:
    job = make_job(session, name)
    make_run(session, job, status=status)
    return job


def _current_run(session: Session, job: Job) -> Run:
    return make_run(session, job, status=RunStatus.RUNNING)


def _limits(**kw: int) -> LimitsConfig:
    return LimitsConfig(**kw)


def _cand(n: int, *, day: int | None = None, **kw: Any) -> Candidate:
    """Candidate ``n`` published ``day`` days after DAY0 (default ``n``)."""
    published = DAY0 + timedelta(days=n if day is None else day)
    return make_candidate(f"https://example.com/{n}", published_at=published, **kw)


def _undated(n: int, **kw: Any) -> Candidate:
    return make_candidate(f"https://example.com/{n}", published_at=None, **kw)


def _run(
    session: Session,
    job: Job,
    candidates: Mapping[str, Sequence[Candidate]],
    limits: LimitsConfig | None = None,
    run: Run | None = None,
) -> DedupResult:
    current = run or _current_run(session, job)
    return deduplicate(
        session,
        job_id=job.id,
        run_id=current.id,
        candidates=candidates,
        limits=limits or _limits(),
    )


def _urls(result: DedupResult) -> list[str]:
    return [i.url.rsplit("/", 1)[1] for i in result.items]


def _stored(session: Session, job: Job, n: int) -> Item:
    item = ItemRepository(session).list_for_job(job.id)
    (match,) = [i for i in item if i.url == f"https://example.com/{n}"]
    return match


def _finish(session: Session, result: DedupResult) -> None:
    """Simulate the downstream nodes: selected items end up processed."""
    for selected in result.items:
        item = ItemRepository(session).get(selected.id)
        assert item is not None
        item.status = ItemStatus.SUMMARIZED
    session.flush()


def _check_invariants(result: DedupResult, stored_only_waiting: int = 0) -> None:
    s = result.stats
    assert s.new + s.changed + s.retried + s.waiting + s.dropped == s.found + stored_only_waiting
    assert s.selected + s.limit_cut + s.baseline_skipped == (
        s.new + s.changed + s.retried + s.waiting
    )
    assert s.selected == len(result.items)


# --- US1 -----------------------------------------------------------------------------------


def test_unknown_candidates_stored_and_selected(db_session: Session) -> None:
    job = _job_with_success(db_session)
    run = _current_run(db_session, job)
    result = _run(db_session, job, {"s": [_cand(1), _cand(2)]}, run=run)

    assert _urls(result) == ["2", "1"]
    assert {i.reason for i in result.items} == {"new"}
    assert result.stats.new == 2 and result.stats.selected == 2
    for n in (1, 2):
        stored = _stored(db_session, job, n)
        assert (stored.attempts, stored.run_id) == (1, run.id)
    _check_invariants(result)


def test_second_identical_call_returns_nothing(db_session: Session) -> None:
    job = _job_with_success(db_session)
    batch = {"s": [_cand(1), _cand(2)]}
    _finish(db_session, _run(db_session, job, batch))
    before = db_session.query(Item).count()

    second = _run(db_session, job, batch)

    assert second.items == []
    assert db_session.query(Item).count() == before
    assert second.stats.dropped == second.stats.found == 2
    _check_invariants(second)


def test_repeated_call_in_same_run_is_idempotent(db_session: Session) -> None:
    job = _job_with_success(db_session)
    run = _current_run(db_session, job)
    batch = {"s": [_cand(1), _cand(2)]}
    assert len(_run(db_session, job, batch, run=run).items) == 2

    second = _run(db_session, job, batch, run=run)

    assert second.items == [] and second.stats.dropped == 2
    assert _stored(db_session, job, 1).attempts == 1


def test_unfinished_item_of_earlier_run_is_retried(db_session: Session) -> None:
    job = _job_with_success(db_session)
    batch = {"s": [_cand(1)]}
    _run(db_session, job, batch)  # not finished: interrupted

    result = _run(db_session, job, batch)

    assert [(i.reason, i.attempts) for i in result.items] == [("retry", 2)]


def test_dedupe_is_per_job(db_session: Session) -> None:
    a = _job_with_success(db_session, "a")
    b = _job_with_success(db_session, "b")
    batch = {"s": [_cand(1)]}

    assert len(_run(db_session, a, batch).items) == 1
    assert len(_run(db_session, b, batch).items) == 1


def test_duplicate_urls_in_input_selected_once(db_session: Session) -> None:
    job = _job_with_success(db_session)
    result = _run(db_session, job, {"a": [_cand(1), _cand(1)], "b": [_cand(1), _cand(2)]})

    assert sorted(_urls(result)) == ["1", "2"]
    assert result.stats.found == 2 and result.stats.dropped == 0
    assert db_session.query(Item).count() == 2


def test_empty_input_without_waiting_changes_nothing(db_session: Session) -> None:
    job = _job_with_success(db_session)
    result = _run(db_session, job, {})

    assert result.items == []
    assert result.stats.found == 0 and result.stats.selected == 0
    assert db_session.query(Item).count() == 0


def test_concurrent_insert_is_dropped(db_session: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    job = _job_with_success(db_session)
    real_add = ItemRepository.add
    winner = make_item(db_session, job, "https://example.com/1", status=ItemStatus.SUMMARIZED)

    def add(
        self: ItemRepository, job_id: int, candidate: Candidate, **kw: Any
    ) -> tuple[Item, bool]:
        if candidate.url.endswith("/1"):
            return winner, False
        return real_add(self, job_id, candidate, **kw)

    monkeypatch.setattr(ItemRepository, "add", add)
    # the stored winner is invisible to the batch lookup in this simulation
    monkeypatch.setattr(ItemRepository, "find_many", lambda self, job_id, hashes: {})

    result = _run(db_session, job, {"s": [_cand(1), _cand(2)]})

    assert _urls(result) == ["2"]
    assert result.stats.new == 1 and result.stats.dropped == 1
    _check_invariants(result)


def test_concurrent_insert_during_baseline_is_not_skipped(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job(db_session)
    winner = make_item(db_session, job, "https://example.com/1", status=ItemStatus.SUMMARIZED)
    monkeypatch.setattr(ItemRepository, "add", lambda self, job_id, c, **kw: (winner, False))
    monkeypatch.setattr(ItemRepository, "find_many", lambda self, job_id, hashes: {})

    result = _run(db_session, job, {"s": [_cand(1)]})

    assert result.items == [] and result.stats.dropped == 1
    assert winner.status == ItemStatus.SUMMARIZED and winner.last_error is None


def test_summary_log_line(db_session: Session, caplog: pytest.LogCaptureFixture) -> None:
    job = _job_with_success(db_session)
    with caplog.at_level(logging.INFO, logger="invio.graph.nodes.deduplicate"):
        _run(db_session, job, {"s": [_cand(1), _cand(2)]})

    records = [r for r in caplog.records if r.name == "invio.graph.nodes.deduplicate"]
    assert len(records) == 1
    record = records[0]
    assert record.getMessage() == "deduplicated"
    assert (record.found, record.new, record.selected) == (2, 2, 2)  # type: ignore[attr-defined]
    assert record.baseline is False  # type: ignore[attr-defined]
    assert "example.com" not in str(record.__dict__)


# --- US4 -----------------------------------------------------------------------------------


def test_baseline_keeps_newest_per_source(db_session: Session) -> None:
    job = make_job(db_session)
    a = [_cand(n) for n in range(1, 26)]
    b = [_cand(100 + n) for n in range(5)]
    batch = {"a": a, "b": b}

    result = _run(db_session, job, batch, _limits(baseline_items=10))

    kept_a = {str(n) for n in range(16, 26)}
    kept_b = {str(100 + n) for n in range(5)}
    assert set(_urls(result)) == kept_a | kept_b
    assert result.stats.baseline is True and result.stats.baseline_skipped == 15
    skipped = ItemRepository(db_session).list_for_job(job.id, status=ItemStatus.SKIPPED_IRRELEVANT)
    assert len(skipped) == 15
    for item in skipped:
        assert (item.last_error, item.attempts, item.run_id) == (BASELINE_REASON, 0, None)
    _finish(db_session, result)
    assert _run(db_session, job, batch, _limits(baseline_items=10)).items == []


def test_baseline_small_source_keeps_all(db_session: Session) -> None:
    job = make_job(db_session)
    result = _run(db_session, job, {"s": [_cand(1), _cand(2)]}, _limits(baseline_items=5))

    assert len(result.items) == 2 and result.stats.baseline_skipped == 0


@pytest.mark.parametrize("status", [RunStatus.SUCCEEDED, RunStatus.PARTIAL])
def test_no_baseline_after_successful_run(db_session: Session, status: RunStatus) -> None:
    job = _job_with_success(db_session, status=status)
    result = _run(
        db_session, job, {"s": [_cand(n) for n in range(1, 6)]}, _limits(baseline_items=2)
    )

    assert len(result.items) == 5 and result.stats.baseline is False


def test_failed_runs_do_not_end_baseline(db_session: Session) -> None:
    job = make_job(db_session)
    make_run(db_session, job, status=RunStatus.FAILED)
    result = _run(
        db_session, job, {"s": [_cand(n) for n in range(1, 6)]}, _limits(baseline_items=2)
    )

    assert len(result.items) == 2 and result.stats.baseline is True


def test_baseline_undated_lose_and_ties_keep_source_order(db_session: Session) -> None:
    job = make_job(db_session)
    batch = {
        "s": [
            _undated(1),
            _cand(2, day=5),
            _cand(3, day=5),
            _cand(4, day=5),
        ]
    }
    result = _run(db_session, job, batch, _limits(baseline_items=3))

    assert _urls(result) == ["2", "3", "4"]
    assert _stored(db_session, job, 1).status == ItemStatus.SKIPPED_IRRELEVANT


def test_baseline_skips_known_eligible_items_incl_failed(db_session: Session) -> None:
    job = make_job(db_session)
    # A reported retry item outside the baseline cut becomes skipped_irrelevant (data-model.md,
    # "any eligible known -> baseline reject"); attempts stay unchanged.
    failed = make_item(
        db_session, job, "https://example.com/1", status=ItemStatus.FAILED, attempts=1
    )
    result = _run(
        db_session,
        job,
        {"s": [_cand(1, day=1), _cand(2, day=2)]},
        _limits(baseline_items=1),
    )

    assert _urls(result) == ["2"]
    assert (failed.status, failed.last_error, failed.attempts) == (
        ItemStatus.SKIPPED_IRRELEVANT,
        BASELINE_REASON,
        1,
    )
    assert result.stats.baseline_skipped == 1


# --- US2 -----------------------------------------------------------------------------------


def test_changed_hash_is_selected_as_changed(db_session: Session) -> None:
    job = _job_with_success(db_session)
    make_item(
        db_session,
        job,
        "https://example.com/1",
        content_hash="x" * 64,
        status=ItemStatus.SUMMARIZED,
    )
    cand = _cand(1, content_hash="y" * 64, title="new title")

    result = _run(db_session, job, {"s": [cand]})

    (item,) = result.items
    assert item.reason == "changed" and item.title == "new title"
    stored = _stored(db_session, job, 1)
    assert (stored.content_hash, stored.attempts, stored.last_error) == ("y" * 64, 1, None)
    assert result.stats.changed == 1
    _finish(db_session, result)
    assert _run(db_session, job, {"s": [cand]}).items == []


@pytest.mark.parametrize(("stored_hash", "new_hash"), [(None, "y" * 64), ("x" * 64, None)])
def test_missing_hash_never_counts_as_change(
    db_session: Session, stored_hash: str | None, new_hash: str | None
) -> None:
    job = _job_with_success(db_session)
    make_item(
        db_session,
        job,
        "https://example.com/1",
        content_hash=stored_hash,
        status=ItemStatus.SUMMARIZED,
    )

    assert _run(db_session, job, {"s": [_cand(1, content_hash=new_hash)]}).items == []


def test_changed_failed_item_with_max_attempts_starts_fresh(db_session: Session) -> None:
    job = _job_with_success(db_session)
    make_item(
        db_session,
        job,
        "https://example.com/1",
        content_hash="x" * 64,
        status=ItemStatus.FAILED,
        attempts=MAX_ATTEMPTS,
        last_error="boom",
    )

    result = _run(db_session, job, {"s": [_cand(1, content_hash="y" * 64)]})

    assert [i.reason for i in result.items] == ["changed"]
    assert result.items[0].attempts == 1


def test_changed_item_rejected_by_baseline_stays_skipped(db_session: Session) -> None:
    job = make_job(db_session)
    make_item(
        db_session,
        job,
        "https://example.com/1",
        content_hash="x" * 64,
        status=ItemStatus.SUMMARIZED,
    )

    result = _run(
        db_session,
        job,
        {"s": [_cand(1, day=1, content_hash="y" * 64), _cand(2, day=2)]},
        _limits(baseline_items=1),
    )

    assert _urls(result) == ["2"]
    stored = _stored(db_session, job, 1)
    assert stored.status == ItemStatus.SKIPPED_IRRELEVANT
    assert stored.last_error == BASELINE_REASON
    assert stored.content_hash == "y" * 64


# --- US3 -----------------------------------------------------------------------------------


def test_failed_item_retried_three_times_then_dropped(db_session: Session) -> None:
    job = _job_with_success(db_session)
    cand = _cand(1)
    first = _run(db_session, job, {"s": [cand]})
    assert first.items[0].reason == "new"
    for expected in (1, 2, 3):
        stored = _stored(db_session, job, 1)
        assert stored.attempts == expected
        stored.status = ItemStatus.FAILED
        db_session.flush()
        if expected < MAX_ATTEMPTS:
            result = _run(db_session, job, {"s": [cand]})
            assert [i.reason for i in result.items] == ["retry"]
            assert result.items[0].attempts == expected + 1
            assert _stored(db_session, job, 1).status == ItemStatus.FAILED
    assert _run(db_session, job, {"s": [cand]}).items == []


@pytest.mark.parametrize(("attempts", "selected"), [(1, True), (2, True), (3, False)])
def test_interrupted_new_item_retried_below_limit(
    db_session: Session, attempts: int, selected: bool
) -> None:
    job = _job_with_success(db_session)
    make_item(db_session, job, "https://example.com/1", content_hash="c" * 64, attempts=attempts)

    result = _run(db_session, job, {"s": [_cand(1)]})

    assert [i.reason for i in result.items] == (["retry"] if selected else [])
    assert result.stats.retried == int(selected)


@pytest.mark.parametrize(
    "status",
    [
        ItemStatus.SUMMARIZED,
        ItemStatus.RELEVANT,
        ItemStatus.EXTRACTED,
        ItemStatus.SKIPPED_KEYWORD,
        ItemStatus.SKIPPED_IRRELEVANT,
    ],
)
def test_finished_items_are_dropped(db_session: Session, status: ItemStatus) -> None:
    job = _job_with_success(db_session)
    make_item(
        db_session, job, "https://example.com/1", content_hash="c" * 64, status=status, attempts=1
    )

    assert _run(db_session, job, {"s": [_cand(1)]}).items == []


# --- US5 -----------------------------------------------------------------------------------


def test_run_limit_selects_newest(db_session: Session) -> None:
    job = _job_with_success(db_session)
    result = _run(
        db_session, job, {"s": [_cand(n) for n in range(1, 9)]}, _limits(max_items_per_run=5)
    )

    assert _urls(result) == ["8", "7", "6", "5", "4"]
    assert result.stats.limit_cut == 3 and result.stats.selected == 5
    _check_invariants(result)
    for n in (1, 2, 3):
        cut = _stored(db_session, job, n)
        assert (cut.status, cut.attempts, cut.run_id) == (ItemStatus.NEW, 0, None)


def test_under_limit_selects_all(db_session: Session) -> None:
    job = _job_with_success(db_session)
    result = _run(db_session, job, {"s": [_cand(1), _cand(2)]}, _limits(max_items_per_run=5))

    assert len(result.items) == 2 and result.stats.limit_cut == 0


def test_cut_items_are_picked_up_later(db_session: Session) -> None:
    job = _job_with_success(db_session)
    limits = _limits(max_items_per_run=5)
    _run(db_session, job, {"s": [_cand(n) for n in range(1, 9)]}, limits)

    later = _run(db_session, job, {}, limits)

    assert _urls(later) == ["3", "2", "1"]
    assert {i.reason for i in later.items} == {"waiting"}
    assert later.stats.waiting == 3 and later.stats.found == 0
    assert all(i.attempts == 1 for i in later.items)
    assert _run(db_session, job, {}, limits).items == []


def test_waiting_compete_with_fresh_candidates_by_date(db_session: Session) -> None:
    job = _job_with_success(db_session)
    limits = _limits(max_items_per_run=2)
    _run(db_session, job, {"s": [_cand(1), _cand(2), _cand(3), _cand(4)]}, limits)  # 1, 2 wait

    result = _run(db_session, job, {"s": [_cand(10, day=1)]}, limits)

    # waiting 2 (day 2) and new 10 (day 1) beat waiting 1 (day 1) by date/origin
    assert _urls(result) == ["2", "10"]
    _check_invariants(result, stored_only_waiting=2)


def test_reported_waiting_item_counted_once(db_session: Session) -> None:
    job = _job_with_success(db_session)
    limits = _limits(max_items_per_run=1)
    _run(db_session, job, {"s": [_cand(1), _cand(2)]}, limits)  # 1 waits

    result = _run(db_session, job, {"s": [_cand(1)]}, limits)

    assert _urls(result) == ["1"] and result.stats.waiting == 1
    assert result.stats.found == 1
    _check_invariants(result)


def test_stored_waiting_items_skip_baseline(db_session: Session) -> None:
    job = make_job(db_session)
    for n in range(1, 4):
        make_item(db_session, job, f"https://example.com/{n}")

    result = _run(db_session, job, {}, _limits(baseline_items=1))

    assert len(result.items) == 3 and result.stats.baseline is True
    assert result.stats.baseline_skipped == 0


def test_cut_changed_item_is_stored_reset(db_session: Session) -> None:
    job = _job_with_success(db_session)
    make_item(
        db_session,
        job,
        "https://example.com/1",
        content_hash="x" * 64,
        status=ItemStatus.SUMMARIZED,
        attempts=2,
    )

    result = _run(
        db_session,
        job,
        {"s": [_cand(1, day=1, content_hash="y" * 64), _cand(2, day=2)]},
        _limits(max_items_per_run=1),
    )

    assert _urls(result) == ["2"]
    stored = _stored(db_session, job, 1)
    assert (stored.content_hash, stored.status, stored.attempts) == ("y" * 64, ItemStatus.NEW, 0)
    assert result.stats.changed == 1 and result.stats.limit_cut == 1


def test_cut_retry_item_keeps_state(db_session: Session) -> None:
    job = _job_with_success(db_session)
    failed = make_item(
        db_session,
        job,
        "https://example.com/1",
        content_hash="c" * 64,
        status=ItemStatus.FAILED,
        attempts=2,
    )

    result = _run(
        db_session,
        job,
        {"s": [_cand(1, day=1), _cand(2, day=2)]},
        _limits(max_items_per_run=1),
    )

    assert _urls(result) == ["2"]
    assert (failed.status, failed.attempts, failed.run_id) == (ItemStatus.FAILED, 2, None)
    assert result.stats.limit_cut == 1


def test_selected_item_fields_come_from_stored_item(db_session: Session) -> None:
    job = _job_with_success(db_session)
    make_item(
        db_session,
        job,
        "https://example.com/1",
        title="stored",
        status=ItemStatus.FAILED,
        attempts=1,
        content_hash="c" * 64,
    )

    (item,) = _run(db_session, job, {"s": [_cand(1, title="other")]}).items

    assert (item.title, item.reason, item.type) == ("stored", "retry", "article")
    assert item.attempts == 2


def test_undated_sort_after_dated_across_sources(db_session: Session) -> None:
    job = _job_with_success(db_session)
    result = _run(db_session, job, {"a": [_undated(1), _cand(2, day=1)], "b": [_undated(3)]})

    assert _urls(result) == ["2", "1", "3"]


# --- Cross-story interactions (QA) ------------------------------------------------------


def test_baseline_then_run_limit_across_sources(
    db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    job = make_job(db_session)
    a = [_cand(n) for n in range(1, 7)]  # days 1..6
    b = [_cand(11, day=2), _cand(12, day=4), _cand(13, day=8), _cand(14, day=10)]
    limits = _limits(baseline_items=3, max_items_per_run=4)

    with caplog.at_level(logging.INFO, logger="invio.graph.nodes.deduplicate"):
        result = _run(db_session, job, {"a": a, "b": b}, limits)

    # baseline first (a: 6,5,4 / b: 14,13,12), then the run limit across sources;
    # the day-4 tie between a/4 and b/12 is broken by source order
    assert _urls(result) == ["14", "13", "6", "5"]
    s = result.stats
    assert (s.found, s.new, s.baseline_skipped, s.limit_cut, s.selected) == (10, 10, 4, 2, 4)
    assert s.baseline is True
    _check_invariants(result)
    for n in (1, 2, 3, 11):
        skipped = _stored(db_session, job, n)
        assert (skipped.status, skipped.last_error) == (
            ItemStatus.SKIPPED_IRRELEVANT,
            BASELINE_REASON,
        )
    for n in (4, 12):
        cut = _stored(db_session, job, n)
        assert (cut.status, cut.attempts, cut.run_id) == (ItemStatus.NEW, 0, None)
    # SC-005: every selected item is at least as new as every eligible item left out
    oldest_selected = min(i.published_at for i in result.items if i.published_at)
    for n in (4, 12):
        published = _stored(db_session, job, n).published_at
        assert published is not None and published <= oldest_selected
    (record,) = [r for r in caplog.records if r.name == "invio.graph.nodes.deduplicate"]
    logged = {
        key: getattr(record, key)
        for key in ("new", "changed", "retried", "dropped", "baseline_skipped", "limit_cut")
    }
    assert logged == {
        "new": 10,
        "changed": 0,
        "retried": 0,
        "dropped": 0,
        "baseline_skipped": 4,
        "limit_cut": 2,
    }

    # still no successful run: stored waiting items are exempt from baseline
    later = _run(db_session, job, {}, limits)
    assert _urls(later) == ["4", "12"] and later.stats.baseline is True
    assert later.stats.baseline_skipped == 0


def test_duplicate_url_across_sources_counts_for_first_source_in_baseline(
    db_session: Session,
) -> None:
    job = make_job(db_session)
    batch = {"a": [_cand(1, day=1)], "b": [_cand(1, day=1), _cand(2, day=2)]}

    result = _run(db_session, job, batch, _limits(baseline_items=1))

    assert _urls(result) == ["2", "1"] and result.stats.baseline_skipped == 0


def test_waiting_backlog_drains_newest_first_across_runs(db_session: Session) -> None:
    job = _job_with_success(db_session)
    limits = _limits(max_items_per_run=3)
    taken: list[str] = []

    first = _run(db_session, job, {"s": [_cand(n) for n in range(1, 9)]}, limits)
    assert _urls(first) == ["8", "7", "6"]
    taken += _urls(first)
    _finish(db_session, first)

    # fresh 20 is newest; fresh 21 ties with waiting 3 on day 3 and wins as reported
    second = _run(db_session, job, {"s": [_cand(20), _cand(21, day=3)]}, limits)
    assert _urls(second) == ["20", "5", "4"]
    assert [i.reason for i in second.items] == ["new", "waiting", "waiting"]
    assert second.stats.limit_cut == 4
    _check_invariants(second, stored_only_waiting=5)
    taken += _urls(second)
    _finish(db_session, second)

    # from storage only: the day-3 tie is broken by storage order (3 before 21)
    third = _run(db_session, job, {}, limits)
    assert _urls(third) == ["3", "21", "2"]
    taken += _urls(third)
    _finish(db_session, third)

    fourth = _run(db_session, job, {}, limits)
    assert _urls(fourth) == ["1"]
    taken += _urls(fourth)
    _finish(db_session, fourth)

    assert _run(db_session, job, {}, limits).items == []
    assert sorted(taken, key=int) == [str(n) for n in [*range(1, 9), 20, 21]]
    assert all(i.attempts == 1 for i in ItemRepository(db_session).list_for_job(job.id))


def test_empty_input_returns_waiting_up_to_limit_in_storage_order(db_session: Session) -> None:
    job = _job_with_success(db_session)
    make_item(db_session, job, "https://example.com/4", published_at=None)
    for n in (3, 1, 2, 5):
        make_item(db_session, job, f"https://example.com/{n}", published_at=DAY0)
    limits = _limits(max_items_per_run=3)

    result = _run(db_session, job, {}, limits)

    assert _urls(result) == ["3", "1", "2"]
    assert result.stats.waiting == 5 and result.stats.limit_cut == 2
    _check_invariants(result, stored_only_waiting=5)
    assert _urls(_run(db_session, job, {}, limits)) == ["5", "4"]


def test_same_run_repeat_with_changed_hash_reselects_once(db_session: Session) -> None:
    job = _job_with_success(db_session)
    run = _current_run(db_session, job)
    _run(db_session, job, {"s": [_cand(1, content_hash="x" * 64), _cand(2)]}, run=run)

    again = _run(db_session, job, {"s": [_cand(1, content_hash="y" * 64), _cand(2)]}, run=run)

    assert [(i.url.rsplit("/", 1)[1], i.reason, i.attempts) for i in again.items] == [
        ("1", "changed", 1)
    ]
    assert again.stats.dropped == 1
    stored = _stored(db_session, job, 1)
    assert (stored.content_hash, stored.attempts, stored.run_id) == ("y" * 64, 1, run.id)
    assert _stored(db_session, job, 2).attempts == 1


def test_end_to_end_baseline_then_regular_runs_until_retries_exhausted(
    db_session: Session,
) -> None:
    job = make_job(db_session)
    runs = RunRepository(db_session)
    limits = _limits(baseline_items=2)
    archive = [_cand(n) for n in range(1, 6)]

    # run 1: no successful run yet -> baseline keeps the 2 newest
    run1 = _current_run(db_session, job)
    first = _run(db_session, job, {"s": archive}, limits, run=run1)
    assert _urls(first) == ["5", "4"] and first.stats.baseline is True
    _stored(db_session, job, 5).status = ItemStatus.SUMMARIZED
    _stored(db_session, job, 4).status = ItemStatus.FAILED
    runs.finish(run1, RunStatus.SUCCEEDED)
    db_session.flush()
    db_session.expire_all()  # force reloads: values must survive the store round trip

    # run 2: baseline over; archive stays known, 6 is new, 4 is retried
    second = _run(db_session, job, {"s": [*archive, _cand(6)]}, limits)
    assert [(i.reason, i.attempts) for i in second.items] == [("new", 1), ("retry", 2)]
    assert _urls(second) == ["6", "4"]
    s = second.stats
    assert (s.baseline, s.new, s.retried, s.dropped, s.baseline_skipped) == (False, 1, 1, 4, 0)
    _check_invariants(second)
    _stored(db_session, job, 6).status = ItemStatus.SUMMARIZED
    db_session.flush()
    db_session.expire_all()

    # run 3: the last allowed attempt; run 4: retries exhausted
    third = _run(db_session, job, {"s": [*archive, _cand(6)]}, limits)
    assert [(i.reason, i.attempts) for i in third.items] == [("retry", 3)]
    db_session.expire_all()
    fourth = _run(db_session, job, {"s": [*archive, _cand(6)]}, limits)
    assert fourth.items == [] and fourth.stats.dropped == 6
    assert _stored(db_session, job, 4).attempts == MAX_ATTEMPTS


# --- Determinism ---------------------------------------------------------------------------


def test_determinism_across_identical_jobs(db_session: Session) -> None:
    batch = {
        "a": [_cand(1, day=3), _undated(2), _cand(3, day=3)],
        "b": [_cand(4, day=3), _cand(5, day=9), _undated(6)],
    }
    outcomes = []
    for name in ("one", "two"):
        job = _job_with_success(db_session, name)
        make_item(db_session, job, "https://example.com/9")
        make_item(db_session, job, "https://example.com/8")
        result = _run(db_session, job, batch, _limits(max_items_per_run=4))
        outcomes.append(([(i.url, i.reason, i.attempts) for i in result.items], result.stats))

    assert outcomes[0] == outcomes[1]
