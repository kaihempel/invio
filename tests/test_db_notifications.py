"""Persistence tests for notification attempts, retry selection and exclusive claiming."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from invio.db.models import Job, Notification
from invio.db.repositories import DigestRepository, NotificationRepository
from invio.db.session import session_factory, session_scope
from invio.domain import NotificationStatus
from tests.db_helpers import make_digest, make_job, make_notification, make_run

pytestmark = pytest.mark.db

NOW = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)


def _job(session: Session) -> Job:
    return make_job(session, "notify-job")


def _row(session: Session, job: Job, **kw: object) -> Notification:
    return make_notification(session, job, **kw)


def test_new_rows_default_to_no_attempts(db_session: Session) -> None:
    row = _row(db_session, _job(db_session))
    db_session.refresh(row)

    assert row.attempts == 0
    assert row.last_attempt_at is None


def test_begin_attempt_counts_and_stamps(db_session: Session) -> None:
    row = _row(db_session, _job(db_session))

    NotificationRepository(db_session).begin_attempt(row, now=NOW)

    assert row.attempts == 1
    assert row.last_attempt_at == NOW
    assert row.status is NotificationStatus.PENDING


def test_mark_sent_clears_previous_error(db_session: Session) -> None:
    row = _row(db_session, _job(db_session), status=NotificationStatus.FAILED, error="boom")

    NotificationRepository(db_session).mark(row, NotificationStatus.SENT)

    assert row.status is NotificationStatus.SENT
    assert row.error is None
    assert row.sent_at is not None


def test_retry_candidates_select_failed_and_stale_pending_in_id_order(
    db_session: Session,
) -> None:
    job = _job(db_session)
    stale = NOW - timedelta(minutes=61)
    fresh = NOW - timedelta(minutes=59)
    old_created = {"created_at": stale}
    failed = _row(db_session, job, status=NotificationStatus.FAILED)
    _row(db_session, job, status=NotificationStatus.SENT)
    _row(db_session, job, status=NotificationStatus.SKIPPED)
    _row(db_session, job, last_attempt_at=fresh, created_at=stale)  # fresh pending
    stale_attempted = _row(db_session, job, last_attempt_at=stale)
    stale_never_tried = _row(db_session, job, **old_created)  # falls back to created_at
    _row(db_session, job, created_at=fresh)  # fresh, never attempted

    rows = NotificationRepository(db_session).retry_candidates(now=NOW, stale_after=HOUR)

    assert [r.id for r in rows] == [failed.id, stale_attempted.id, stale_never_tried.id]


def test_claim_is_exclusive(db_session: Session) -> None:
    row = _row(db_session, _job(db_session), status=NotificationStatus.FAILED, attempts=1)
    repo = NotificationRepository(db_session)

    first = repo.claim_for_retry(row.id, now=NOW, stale_after=HOUR)
    second = repo.claim_for_retry(row.id, now=NOW, stale_after=HOUR)

    assert (first, second) == (True, False)
    db_session.refresh(row)
    assert row.status is NotificationStatus.PENDING
    assert row.attempts == 2
    assert row.last_attempt_at == NOW


def test_claim_rejects_sent_and_fresh_pending_rows(db_session: Session) -> None:
    job = _job(db_session)
    sent = _row(db_session, job, status=NotificationStatus.SENT)
    fresh = _row(db_session, job, last_attempt_at=NOW - timedelta(minutes=5))
    repo = NotificationRepository(db_session)

    assert repo.claim_for_retry(sent.id, now=NOW, stale_after=HOUR) is False
    assert repo.claim_for_retry(fresh.id, now=NOW, stale_after=HOUR) is False


def test_claim_recovers_stale_pending(db_session: Session) -> None:
    row = _row(db_session, _job(db_session), attempts=1, last_attempt_at=NOW - 2 * HOUR)

    assert NotificationRepository(db_session).claim_for_retry(row.id, now=NOW, stale_after=HOUR)

    db_session.refresh(row)
    assert (row.attempts, row.last_attempt_at) == (2, NOW)


def test_digest_get_returns_digest_or_none(db_session: Session) -> None:
    job = _job(db_session)
    run = make_run(db_session, job)
    digest = make_digest(db_session, job, run_id=run.id)
    repo = DigestRepository(db_session)

    assert repo.get(digest.id) is digest
    assert repo.get(digest.id + 1000) is None


def _claim(session: Session, row_id: int) -> bool:
    return NotificationRepository(session).claim_for_retry(row_id, now=NOW, stale_after=HOUR)


def test_pending_exactly_at_the_cutoff_is_not_stale(db_session: Session) -> None:
    job = _job(db_session)
    attempted = _row(db_session, job, attempts=1, last_attempt_at=NOW - HOUR)
    never_tried = _row(db_session, job, created_at=NOW - HOUR)

    assert NotificationRepository(db_session).retry_candidates(now=NOW, stale_after=HOUR) == []
    assert _claim(db_session, attempted.id) is False
    assert _claim(db_session, never_tried.id) is False


def test_recent_attempt_wins_over_old_creation_time(db_session: Session) -> None:
    row = _row(db_session, _job(db_session), created_at=NOW - 5 * HOUR, last_attempt_at=NOW)

    assert _claim(db_session, row.id) is False


def test_claim_rejects_skipped_rows(db_session: Session) -> None:
    row = _row(db_session, _job(db_session), status=NotificationStatus.SKIPPED, attempts=5)

    assert _claim(db_session, row.id) is False
    db_session.refresh(row)
    assert (row.status, row.attempts) == (NotificationStatus.SKIPPED, 5)


def test_claim_sees_a_claim_committed_by_another_session(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    with session_scope(factory) as session:
        row_id = _row(session, _job(session), status=NotificationStatus.FAILED, attempts=1).id
    with Session(db_engine, expire_on_commit=False) as late:
        # ``late`` still holds the row as failed in memory when another process claims it
        # (its transaction is closed: the SQLite test engine shares one connection).
        stale_view = late.get(Notification, row_id)
        assert stale_view is not None
        late.commit()
        with session_scope(factory) as early:
            assert _claim(early, row_id) is True
        assert stale_view.status is NotificationStatus.FAILED
        claimed = _claim(late, row_id)
        late.commit()
    assert claimed is False
    with session_scope(factory) as session:
        row = session.get(Notification, row_id)
        assert row is not None
        assert (row.status, row.attempts) == (NotificationStatus.PENDING, 2)


# --- give_up_stale: crash-interrupted rows at the attempt limit --------------------------------


def _give_up(session: Session, row_id: int) -> bool:
    return NotificationRepository(session).give_up_stale(
        row_id, now=NOW, stale_after=HOUR, max_attempts=5, error="interrupted"
    )


def test_give_up_stale_skips_once(db_session: Session) -> None:
    row = _row(db_session, _job(db_session), attempts=5, last_attempt_at=NOW - 2 * HOUR)

    first, second = _give_up(db_session, row.id), _give_up(db_session, row.id)

    assert (first, second) == (True, False)
    db_session.refresh(row)
    assert (row.status, row.error, row.attempts) == (NotificationStatus.SKIPPED, "interrupted", 5)


def test_give_up_stale_uses_creation_time_when_never_attempted(db_session: Session) -> None:
    row = _row(db_session, _job(db_session), attempts=5, created_at=NOW - 2 * HOUR)

    assert _give_up(db_session, row.id) is True


@pytest.mark.parametrize(
    "kw",
    [
        pytest.param({"attempts": 5, "last_attempt_at": NOW - timedelta(minutes=5)}, id="fresh"),
        pytest.param({"attempts": 5, "last_attempt_at": NOW - HOUR}, id="exactly-at-cutoff"),
        pytest.param({"attempts": 4, "last_attempt_at": NOW - 2 * HOUR}, id="under-limit"),
        pytest.param(
            {"attempts": 5, "last_attempt_at": NOW - 2 * HOUR, "status": NotificationStatus.FAILED},
            id="failed",
        ),
        pytest.param(
            {"attempts": 5, "last_attempt_at": NOW - 2 * HOUR, "status": NotificationStatus.SENT},
            id="sent",
        ),
    ],
)
def test_give_up_stale_leaves_other_rows_alone(db_session: Session, kw: dict[str, object]) -> None:
    row = _row(db_session, _job(db_session), **kw)
    status_before = row.status

    assert _give_up(db_session, row.id) is False
    db_session.refresh(row)
    assert row.status is status_before
    assert row.error is None


def test_give_up_stale_unknown_id_returns_false(db_session: Session) -> None:
    assert _give_up(db_session, 999_999) is False
