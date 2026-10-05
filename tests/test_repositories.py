"""Tests for the record repositories (flush-only, never commit)."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from invio.db.models import Item, Job, Run
from invio.db.repositories import (
    DigestRepository,
    ItemRepository,
    JobRepository,
    NotificationRepository,
    RunRepository,
    UsageRepository,
    UsageTotals,
)
from invio.db.session import session_factory, session_scope
from invio.domain import ItemStatus, NotificationStatus, RunStatus, url_hash
from tests.db_helpers import (
    assert_rejected,
    make_candidate,
    make_item,
    make_job,
    make_run,
    uses_sqlite,
)

pytestmark = pytest.mark.db


# --- JobRepository -------------------------------------------------------------------------


def test_job_add_flushes_and_assigns_id(db_session: Session) -> None:
    job = JobRepository(db_session).add(Job(name="a", config={}))
    assert job.id is not None


def test_job_get_by_name(db_session: Session) -> None:
    repo = JobRepository(db_session)
    added = repo.add(Job(name="a", config={}))
    assert repo.get_by_name("a") is added
    assert repo.get_by_name("missing") is None


def test_job_list_ordered_and_filtered(db_session: Session) -> None:
    repo = JobRepository(db_session)
    for name in ("b", "a"):
        repo.add(Job(name=name, config={}))
    repo.add(Job(name="c", config={}, enabled=False))
    assert [j.name for j in repo.list()] == ["a", "b", "c"]
    assert [j.name for j in repo.list(enabled_only=True)] == ["a", "b"]


def test_job_add_duplicate_raises(db_session: Session) -> None:
    repo = JobRepository(db_session)
    repo.add(Job(name="a", config={}))
    with pytest.raises(IntegrityError):
        repo.add(Job(name="a", config={}))
    db_session.rollback()


def test_job_delete_cascades_runs(db_session: Session) -> None:
    repo = JobRepository(db_session)
    job = make_job(db_session, "a")
    make_run(db_session, job)
    repo.delete(job)
    db_session.expire_all()
    assert repo.get_by_name("a") is None
    assert db_session.query(Run).count() == 0


def test_job_repository_never_commits(db_session: Session) -> None:
    repo = JobRepository(db_session)
    repo.add(Job(name="a", config={}))
    db_session.rollback()
    assert repo.get_by_name("a") is None


# --- RunRepository -------------------------------------------------------------------------


def test_run_start_defaults(db_session: Session) -> None:
    job = make_job(db_session)
    run = RunRepository(db_session).start(job.id)
    assert run.status == RunStatus.RUNNING
    assert run.started_at.tzinfo is not None
    assert run.finished_at is None


def test_run_start_explicit_started_at(db_session: Session) -> None:
    job = make_job(db_session)
    at = datetime(2026, 1, 2, 3, 4, tzinfo=UTC)
    assert RunRepository(db_session).start(job.id, started_at=at).started_at == at


def test_run_finish_success(db_session: Session) -> None:
    repo = RunRepository(db_session)
    run = repo.start(make_job(db_session).id)
    repo.finish(run, RunStatus.SUCCEEDED, stats={"items": 3}, error=None)
    assert run.status == RunStatus.SUCCEEDED
    assert run.stats == {"items": 3}
    assert run.finished_at is not None and run.finished_at.tzinfo is not None
    assert run.error is None


def test_run_finish_failed_with_error_and_time(db_session: Session) -> None:
    repo = RunRepository(db_session)
    run = repo.start(make_job(db_session).id)
    at = datetime(2026, 5, 6, tzinfo=UTC)
    repo.finish(run, RunStatus.FAILED, error="boom", finished_at=at)
    assert run.status == RunStatus.FAILED
    assert run.error == "boom"
    assert run.finished_at == at


def test_run_get(db_session: Session) -> None:
    repo = RunRepository(db_session)
    run = repo.start(make_job(db_session).id)
    assert repo.get(run.id) is run
    assert repo.get(999) is None


def test_run_list_for_job_newest_first(db_session: Session) -> None:
    repo = RunRepository(db_session)
    job, other = make_job(db_session, "a"), make_job(db_session, "b")
    t = datetime(2026, 1, 1, tzinfo=UTC)
    old = repo.start(job.id, started_at=t)
    tie1 = repo.start(job.id, started_at=t + timedelta(days=1))
    tie2 = repo.start(job.id, started_at=t + timedelta(days=1))
    repo.start(other.id, started_at=t + timedelta(days=5))
    assert repo.list_for_job(job.id) == [tie2, tie1, old]
    assert repo.list_for_job(job.id, limit=1) == [tie2]


def test_latest_status_by_job_newest_run_per_job(db_session: Session) -> None:
    repo = RunRepository(db_session)
    job, other = make_job(db_session, "a"), make_job(db_session, "b")
    make_job(db_session, "never")
    t = datetime(2026, 1, 1, tzinfo=UTC)
    old = repo.start(job.id, started_at=t)
    repo.finish(old, RunStatus.FAILED)
    tie1 = repo.start(job.id, started_at=t + timedelta(days=1))
    repo.finish(tie1, RunStatus.FAILED)
    tie2 = repo.start(job.id, started_at=t + timedelta(days=1))
    repo.finish(tie2, RunStatus.PARTIAL)
    only = repo.start(other.id, started_at=t - timedelta(days=9))
    repo.finish(only, RunStatus.SUCCEEDED)

    assert repo.latest_status_by_job() == {
        job.id: RunStatus.PARTIAL,
        other.id: RunStatus.SUCCEEDED,
    }


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([], False),
        ([RunStatus.RUNNING, RunStatus.FAILED], False),
        ([RunStatus.SUCCEEDED], True),
        ([RunStatus.PARTIAL], True),
    ],
    ids=["none", "running-failed", "succeeded", "partial"],
)
def test_has_successful_run(db_session: Session, statuses: list[RunStatus], expected: bool) -> None:
    job = make_job(db_session)
    for status in statuses:
        make_run(db_session, job, status=status)
    assert RunRepository(db_session).has_successful_run(job.id) is expected


def test_has_successful_run_ignores_other_jobs(db_session: Session) -> None:
    job, other = make_job(db_session, "a"), make_job(db_session, "b")
    make_run(db_session, other, status=RunStatus.SUCCEEDED)
    assert RunRepository(db_session).has_successful_run(job.id) is False


def test_latest_status_by_job_empty(db_session: Session) -> None:
    make_job(db_session, "a")

    assert RunRepository(db_session).latest_status_by_job() == {}


def test_latest_status_by_job_is_one_select(db_session: Session) -> None:
    repo = RunRepository(db_session)
    for name in ("a", "b", "c"):
        repo.start(make_job(db_session, name).id)
    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        result = repo.latest_status_by_job()
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert len(result) == 3
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT")


# --- ItemRepository ------------------------------------------------------------------------


def test_item_add_creates_with_copied_fields(db_session: Session) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)
    cand = make_candidate()
    item, created = ItemRepository(db_session).add(job.id, cand, run_id=run.id)
    assert created is True
    assert item.status == ItemStatus.NEW
    assert item.attempts == 0
    assert item.run_id == run.id
    assert (item.url, item.url_hash, item.title, item.type) == (
        cand.url,
        cand.url_hash,
        cand.title,
        cand.type,
    )
    assert (item.published_at, item.teaser, item.content_hash) == (
        cand.published_at,
        cand.teaser,
        cand.content_hash,
    )


def test_item_add_is_idempotent(db_session: Session) -> None:
    job = make_job(db_session)
    repo = ItemRepository(db_session)
    first, created1 = repo.add(job.id, make_candidate())
    second, created2 = repo.add(job.id, make_candidate())
    assert (created1, created2) == (True, False)
    assert second.id == first.id
    assert db_session.query(Item).count() == 1


def test_item_add_same_url_other_job(db_session: Session) -> None:
    a, b = make_job(db_session, "a"), make_job(db_session, "b")
    repo = ItemRepository(db_session)
    repo.add(a.id, make_candidate())
    _, created = repo.add(b.id, make_candidate())
    assert created is True


def test_item_add_reraises_non_duplicate_integrity_error(db_session: Session) -> None:
    make_job(db_session)
    with pytest.raises(IntegrityError):
        ItemRepository(db_session).add(99999, make_candidate())
    db_session.rollback()


def test_item_seen_get_and_list(db_session: Session) -> None:
    job = make_job(db_session)
    repo = ItemRepository(db_session)
    one, _ = repo.add(job.id, make_candidate("https://example.com/1"))
    two, _ = repo.add(job.id, make_candidate("https://example.com/2"))
    two.status = ItemStatus.FAILED
    db_session.flush()
    assert repo.seen(job.id, url_hash("https://example.com/1")) is True
    assert repo.seen(job.id, url_hash("https://example.com/zzz")) is False
    assert repo.get(one.id) is one
    assert repo.get(9999) is None
    assert repo.list_for_job(job.id) == [one, two]
    assert repo.list_for_job(job.id, status=ItemStatus.FAILED) == [two]


def test_item_find_many_is_scoped_to_job_and_keyed_by_hash(db_session: Session) -> None:
    a, b = make_job(db_session, "a"), make_job(db_session, "b")
    repo = ItemRepository(db_session)
    one, _ = repo.add(a.id, make_candidate("https://example.com/1"))
    repo.add(a.id, make_candidate("https://example.com/2"))
    repo.add(b.id, make_candidate("https://example.com/3"))
    hashes = [url_hash(f"https://example.com/{n}") for n in (1, 3, 9)]
    assert repo.find_many(a.id, hashes) == {one.url_hash: one}


def test_item_find_many_empty(db_session: Session) -> None:
    job = make_job(db_session)
    assert ItemRepository(db_session).find_many(job.id, []) == {}


def test_item_find_many_chunks_large_input(db_session: Session) -> None:
    job = make_job(db_session)
    repo = ItemRepository(db_session)
    for n in range(3):
        repo.add(job.id, make_candidate(f"https://example.com/{n}"))
    hashes = [url_hash(f"https://example.com/{n}") for n in range(1200)]
    found = repo.find_many(job.id, hashes)
    assert set(found) == {url_hash(f"https://example.com/{n}") for n in range(3)}


@pytest.mark.parametrize(("size", "queries"), [(499, 1), (500, 1), (501, 2), (1000, 2), (1001, 3)])
def test_item_find_many_chunk_boundaries(db_session: Session, size: int, queries: int) -> None:
    job = make_job(db_session)
    repo = ItemRepository(db_session)
    # stored rows sit at both edges of each 500-hash chunk
    stored = [n for n in (0, 499, 500, 999, 1000) if n < size]
    for n in stored:
        repo.add(job.id, make_candidate(f"https://example.com/{n}"))
    hashes = [url_hash(f"https://example.com/{n}") for n in range(size)]
    statements: list[str] = []

    def record(conn: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    engine = db_session.get_bind()
    event.listen(engine, "before_cursor_execute", record)
    try:
        found = repo.find_many(job.id, hashes)
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert set(found) == {url_hash(f"https://example.com/{n}") for n in stored}
    assert len(statements) == queries


def test_item_add_new_inserts_in_input_order(db_session: Session) -> None:
    job = make_job(db_session)
    repo = ItemRepository(db_session)
    cands = [make_candidate(f"https://example.com/{n}") for n in (3, 1, 2)]

    added = repo.add_new(job.id, cands)

    assert [(i.url, created) for i, created in added] == [(c.url, True) for c in cands]
    ids = [i.id for i, _ in added]
    assert ids == sorted(ids)
    assert added[0][0].status == ItemStatus.NEW and added[0][0].attempts == 0
    assert repo.add_new(job.id, []) == []


def test_item_add_new_falls_back_on_concurrent_insert(db_session: Session) -> None:
    job = make_job(db_session)
    repo = ItemRepository(db_session)
    winner, _ = repo.add(job.id, make_candidate("https://example.com/2"))
    cands = [make_candidate(f"https://example.com/{n}") for n in (1, 2, 3)]

    added = repo.add_new(job.id, cands)

    assert [(i.url, created) for i, created in added] == [
        ("https://example.com/1", True),
        ("https://example.com/2", False),
        ("https://example.com/3", True),
    ]
    assert added[1][0] is winner
    assert len(repo.list_for_job(job.id)) == 3


def test_item_add_new_reraises_other_integrity_errors(db_session: Session) -> None:
    with assert_rejected():
        ItemRepository(db_session).add_new(999_999, [make_candidate()])


def _pending_urls(session: Session, job: Job, run: Run, *, limit: int = 100) -> list[str]:
    items = ItemRepository(session).list_pending(job.id, run_id=run.id, max_attempts=3, limit=limit)
    return [i.url.rsplit("/", 1)[1] for i in items]


def test_item_list_pending_filters_waiting_and_retryable(db_session: Session) -> None:
    job, other = make_job(db_session, "a"), make_job(db_session, "b")
    earlier, current = make_run(db_session, job), make_run(db_session, job)
    day = datetime(2026, 1, 1, tzinfo=UTC)
    rows: list[tuple[str, dict[str, Any]]] = [
        ("1", {}),  # waiting
        ("2", {"attempts": 1, "run_id": earlier.id}),  # interrupted
        ("3", {"status": ItemStatus.FAILED, "attempts": 2}),  # retryable
        ("4", {"status": ItemStatus.FAILED, "attempts": 3}),  # exhausted
        ("5", {"attempts": 3}),  # exhausted
        ("6", {"attempts": 1, "run_id": current.id}),  # taken by this run
        ("7", {"status": ItemStatus.SUMMARIZED, "attempts": 1}),
        ("8", {"status": ItemStatus.SKIPPED_BASELINE}),
    ]
    for n, kw in rows:
        make_item(
            db_session, job, f"https://example.com/{n}", published_at=day + timedelta(int(n)), **kw
        )
    make_item(db_session, other, "https://example.com/9")

    assert _pending_urls(db_session, job, current) == ["3", "2", "1"]
    assert ItemRepository(db_session).count_pending(job.id, run_id=current.id, max_attempts=3) == (
        1,
        2,
    )


def test_item_list_pending_orders_newest_first_and_limits(db_session: Session) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)
    day = datetime(2026, 1, 1, tzinfo=UTC)
    make_item(db_session, job, "https://example.com/1", published_at=None)
    make_item(db_session, job, "https://example.com/2", published_at=day)
    make_item(db_session, job, "https://example.com/3", published_at=day + timedelta(1))
    make_item(db_session, job, "https://example.com/4", published_at=day)
    make_item(db_session, job, "https://example.com/5", published_at=None)

    assert _pending_urls(db_session, job, run) == ["3", "2", "4", "1", "5"]
    assert _pending_urls(db_session, job, run, limit=2) == ["3", "2"]
    assert ItemRepository(db_session).count_pending(job.id, run_id=run.id, max_attempts=3) == (
        5,
        0,
    )


def test_item_count_pending_empty(db_session: Session) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)
    assert ItemRepository(db_session).count_pending(job.id, run_id=run.id, max_attempts=3) == (
        0,
        0,
    )


def test_item_mark_taken_links_run_and_increments_attempts(db_session: Session) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)
    one = make_item(db_session, job, "https://example.com/1")
    two = make_item(db_session, job, "https://example.com/2", attempts=2)
    ItemRepository(db_session).mark_taken([one, two], run.id)
    assert (one.run_id, one.attempts) == (run.id, 1)
    assert (two.run_id, two.attempts) == (run.id, 3)
    ItemRepository(db_session).mark_taken([], run.id)


def test_item_mark_status_sets_status(db_session: Session) -> None:
    job = make_job(db_session)
    one = make_item(db_session, job, "https://example.com/1")
    two = make_item(db_session, job, "https://example.com/2")
    ItemRepository(db_session).mark_status([one, two], ItemStatus.SKIPPED_BASELINE)
    assert (one.status, two.status) == (ItemStatus.SKIPPED_BASELINE,) * 2
    assert one.last_error is None
    ItemRepository(db_session).mark_status([], ItemStatus.SKIPPED_BASELINE)


def test_item_set_content_hashes(db_session: Session) -> None:
    job = make_job(db_session)
    item = make_item(db_session, job, status=ItemStatus.SUMMARIZED)
    ItemRepository(db_session).set_content_hashes([(item, "z" * 64)])
    assert (item.content_hash, item.status) == ("z" * 64, ItemStatus.SUMMARIZED)


def test_item_reset_versions_stores_candidate_and_restarts(db_session: Session) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)
    item = make_item(
        db_session,
        job,
        content_hash="x" * 64,
        status=ItemStatus.FAILED,
        attempts=3,
        last_error="boom",
        run_id=run.id,
    )
    cand = make_candidate(
        "https://example.com/a?x=1",
        title="New",
        teaser="t2",
        content_hash="y" * 64,
        published_at=datetime(2026, 3, 1, tzinfo=UTC),
    )
    ItemRepository(db_session).reset_versions([(item, cand)])
    assert (item.url, item.type) == ("https://example.com/a?x=1", cand.type)
    assert (item.content_hash, item.title, item.teaser) == ("y" * 64, "New", "t2")
    assert item.published_at == datetime(2026, 3, 1, tzinfo=UTC)
    assert (item.status, item.attempts, item.last_error, item.run_id) == (
        ItemStatus.NEW,
        0,
        None,
        None,
    )


def test_item_set_status_updates_and_flushes(db_session: Session) -> None:
    job = make_job(db_session)
    repo = ItemRepository(db_session)
    item, _ = repo.add(job.id, make_candidate())
    repo.set_status(item, ItemStatus.SKIPPED_KEYWORD)
    db_session.expire_all()
    assert repo.list_for_job(job.id, status=ItemStatus.SKIPPED_KEYWORD) == [item]
    assert item.status == ItemStatus.SKIPPED_KEYWORD


def test_item_set_relevance_stores_status_and_clears_error(db_session: Session) -> None:
    job = make_job(db_session)
    item = make_item(db_session, job, status=ItemStatus.FAILED, last_error="boom")
    repo = ItemRepository(db_session)
    repo.set_relevance(item, Decimal("0.80"), ItemStatus.RELEVANT)
    db_session.expire_all()
    assert item.relevance == Decimal("0.80")
    assert item.status == ItemStatus.RELEVANT
    assert item.last_error is None


def test_item_mark_failed_keeps_relevance(db_session: Session) -> None:
    job = make_job(db_session)
    item = make_item(db_session, job, relevance=Decimal("0.50"))
    repo = ItemRepository(db_session)
    repo.mark_failed(item, "LLMUnavailableError: down")
    db_session.expire_all()
    assert item.relevance == Decimal("0.50")
    assert item.status == ItemStatus.FAILED
    assert item.last_error == "LLMUnavailableError: down"


# --- DigestRepository ----------------------------------------------------------------------


def test_digest_add_and_list_newest_first(db_session: Session) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)
    repo = DigestRepository(db_session)
    first = repo.add(job.id, "t1", "b1", [1, 2], run_id=run.id)
    second = repo.add(job.id, "t2", "b2", [])
    assert (first.title, first.body, first.item_ids, first.run_id) == ("t1", "b1", [1, 2], run.id)
    assert second.run_id is None
    assert repo.list_for_job(job.id) == [second, first]


# --- NotificationRepository ----------------------------------------------------------------


def test_notification_add_pending(db_session: Session) -> None:
    job = make_job(db_session)
    n = NotificationRepository(db_session).add(job.id, "email", "a@example.com", payload={"k": "v"})
    assert n.status == NotificationStatus.PENDING
    assert n.payload == {"k": "v"}


def test_notification_mark_sent_sets_time(db_session: Session) -> None:
    job = make_job(db_session)
    repo = NotificationRepository(db_session)
    n = repo.add(job.id, "email", "a@example.com")
    repo.mark(n, NotificationStatus.SENT)
    assert n.status == NotificationStatus.SENT
    assert n.sent_at is not None and n.sent_at.tzinfo is not None


def test_notification_mark_sent_explicit_time(db_session: Session) -> None:
    job = make_job(db_session)
    repo = NotificationRepository(db_session)
    n = repo.add(job.id, "email", "a@example.com")
    at = datetime(2026, 2, 3, tzinfo=UTC)
    repo.mark(n, NotificationStatus.SENT, sent_at=at)
    assert n.sent_at == at


def test_notification_mark_failed_keeps_sent_at_none(db_session: Session) -> None:
    job = make_job(db_session)
    repo = NotificationRepository(db_session)
    n = repo.add(job.id, "email", "a@example.com")
    repo.mark(n, NotificationStatus.FAILED, error="smtp")
    assert n.error == "smtp"
    assert n.sent_at is None


def test_notification_lists(db_session: Session) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)
    repo = NotificationRepository(db_session)
    pending = repo.add(job.id, "email", "a@example.com", run_id=run.id)
    sent = repo.add(job.id, "email", "b@example.com")
    repo.mark(sent, NotificationStatus.SENT)
    assert repo.list_for_job(job.id) == [pending, sent]
    assert repo.list_for_job(job.id, status=NotificationStatus.SENT) == [sent]
    assert repo.list_for_run(run.id) == [pending]


# --- UsageRepository -----------------------------------------------------------------------

decimals = pytest.mark.filterwarnings(
    "ignore:Dialect sqlite\\+pysqlite does \\*not\\* support Decimal objects natively"
    ":sqlalchemy.exc.SAWarning"
)


@decimals
def test_usage_totals_for_run_and_job(db_session: Session) -> None:
    a, b = make_job(db_session, "a"), make_job(db_session, "b")
    run = make_run(db_session, a)
    run2 = make_run(db_session, a)
    repo = UsageRepository(db_session)
    repo.add(a.id, "openai", "m", 10, 1, run_id=run.id, purpose="x", cost_usd=Decimal("0.001000"))
    repo.add(a.id, "openai", "m", 20, 2, run_id=run.id, cost_usd=Decimal("0.002500"))
    repo.add(a.id, "openai", "m", 30, 3, run_id=run.id)
    repo.add(a.id, "openai", "m", 5, 5, run_id=run2.id, cost_usd=Decimal("1"))
    repo.add(a.id, "openai", "m", 1, 1)
    repo.add(b.id, "openai", "m", 1000, 1000, cost_usd=Decimal("9"))
    assert repo.totals_for_run(run.id) == UsageTotals(60, 6, Decimal("0.003500"))
    assert repo.totals_for_job(a.id) == UsageTotals(66, 12, Decimal("1.003500"))


@decimals
def test_usage_totals_empty(db_session: Session) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)
    repo = UsageRepository(db_session)
    empty = UsageTotals(0, 0, Decimal("0"))
    assert repo.totals_for_run(run.id) == empty
    assert repo.totals_for_job(job.id) == empty
    assert isinstance(repo.totals_for_job(job.id).input_tokens, int)


# --- Edge cases ----------------------------------------------------------------------------


def test_item_add_race_creates_no_duplicate_and_keeps_winner(
    db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = make_job(db_session)
    winner_run = make_run(db_session, job)
    loser_run = make_run(db_session, job)
    existing = make_item(db_session, job, run_id=winner_run.id, title="winner")
    # Hiding the row from the private pre-check (only) is the only deterministic way to reach
    # the unique-constraint branch in one session: it simulates another writer committing
    # right after the check.
    real_find = ItemRepository._find
    calls: list[str] = []

    def find_after_first(self: ItemRepository, job_id: int, h: str) -> Item | None:
        calls.append(h)
        return None if len(calls) == 1 else real_find(self, job_id, h)

    monkeypatch.setattr(ItemRepository, "_find", find_after_first)
    repo = ItemRepository(db_session)
    item, created = repo.add(job.id, make_candidate(title="loser"), run_id=loser_run.id)
    assert created is False
    assert item is existing
    assert (item.title, item.run_id) == ("winner", winner_run.id)
    assert db_session.query(Item).count() == 1
    # the session remains usable after the savepoint rollback
    other, created = repo.add(job.id, make_candidate("https://example.com/b"))
    assert created is True and other.id != existing.id


@pytest.mark.skipif(uses_sqlite(), reason="needs two connections; SQLite tests share one")
@pytest.mark.usefixtures("clean_jobs")
def test_item_add_concurrent_writer_returns_winner(db_engine: Engine) -> None:
    # Two real sessions: the loser's snapshot (REPEATABLE READ on MariaDB) predates the winner's
    # commit, so only a locking re-query after the duplicate-key error can see the winner.
    factory = session_factory(db_engine)
    with session_scope(factory) as session:
        job_id = JobRepository(session).add(Job(name="a", config={})).id
    with session_scope(factory) as loser:
        repo = ItemRepository(loser)
        assert repo.seen(job_id, make_candidate().url_hash) is False  # takes the snapshot
        with session_scope(factory) as winner:
            won, _ = ItemRepository(winner).add(job_id, make_candidate(title="winner"))
        item, created = repo.add(job_id, make_candidate(title="loser"))
        assert created is False
        assert (item.id, item.title) == (won.id, "winner")


def test_item_add_fk_violation_keeps_outer_transaction(db_session: Session) -> None:
    job = make_job(db_session)
    repo = ItemRepository(db_session)
    kept, _ = repo.add(job.id, make_candidate("https://example.com/kept"))
    with pytest.raises(IntegrityError):
        repo.add(99999, make_candidate())
    # Only the savepoint was rolled back: earlier work survives and the session is usable.
    assert repo.get(kept.id) is kept
    assert JobRepository(db_session).get_by_name(job.name) is job
    _, created = repo.add(job.id, make_candidate("https://example.com/next"))
    assert created is True
    assert db_session.query(Item).count() == 2


def test_item_add_unknown_run_id_is_reraised(db_session: Session) -> None:
    job = make_job(db_session)
    with pytest.raises(IntegrityError):
        ItemRepository(db_session).add(job.id, make_candidate(), run_id=99999)
    assert ItemRepository(db_session).seen(job.id, url_hash("https://example.com/a")) is False


@decimals
def test_no_repository_commits(db_session: Session) -> None:
    job = JobRepository(db_session).add(Job(name="a", config={}))
    run = RunRepository(db_session).start(job.id)
    RunRepository(db_session).finish(run, RunStatus.SUCCEEDED)
    items = ItemRepository(db_session)
    item, _ = items.add(job.id, make_candidate(), run_id=run.id)
    items.mark_taken([item], run.id)
    items.mark_status([item], ItemStatus.SKIPPED_BASELINE)
    items.reset_versions([(item, make_candidate(content_hash="d" * 64))])
    items.set_content_hashes([(item, "e" * 64)])
    items.add_new(job.id, [make_candidate("https://example.com/new")])
    DigestRepository(db_session).add(job.id, "t", "b", [], run_id=run.id)
    n = NotificationRepository(db_session).add(job.id, "email", "a@example.com", run_id=run.id)
    NotificationRepository(db_session).mark(n, NotificationStatus.SENT)
    UsageRepository(db_session).add(job.id, "openai", "m", 1, 1, run_id=run.id)
    db_session.rollback()
    assert db_session.query(Job).count() == 0
    assert db_session.query(Run).count() == 0
    assert db_session.query(Item).count() == 0


@decimals
@pytest.mark.parametrize(
    ("costs", "expected"),
    [
        (["0.1", "0.2"], "0.300000"),  # 0.1 + 0.2 != 0.3 in binary floating point
        (["0.000001"] * 7, "0.000007"),
        (["999999.999999"] * 3 + ["0.000001"], "2999999.999998"),
        (["123456.654321", "0.000009", "0.333333"], "123456.987663"),
    ],
    ids=["tenths", "micro", "max-column", "mixed"],
)
def test_usage_totals_exact_decimal(db_session: Session, costs: list[str], expected: str) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)
    repo = UsageRepository(db_session)
    for cost in costs:
        repo.add(job.id, "openai", "m", 1, 1, run_id=run.id, cost_usd=Decimal(cost))
    for totals in (repo.totals_for_run(run.id), repo.totals_for_job(job.id)):
        assert isinstance(totals.cost_usd, Decimal)
        assert str(totals.cost_usd) == expected
        assert totals.cost_usd.as_tuple().exponent == -6


@decimals
def test_usage_totals_empty_has_six_places(db_session: Session) -> None:
    totals = UsageRepository(db_session).totals_for_job(make_job(db_session).id)
    assert str(totals.cost_usd) == "0.000000"
