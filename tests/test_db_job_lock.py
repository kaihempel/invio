"""``JobRepository.get/claim/release``: the run lock of ``run_job`` (#21) and ``run-due`` (#23)."""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine

from invio.db.repositories import KEEP, JobRepository
from invio.db.session import session_factory, session_scope
from tests.db_helpers import make_job, uses_sqlite

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_jobs")]

NOW = datetime(2026, 10, 4, 12, 0, 0, 123456, tzinfo=UTC)
UNTIL = NOW + timedelta(hours=2)
NEXT = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)


def _make_job(engine: Engine, **fields: object) -> int:
    with session_scope(session_factory(engine)) as session:
        return make_job(session, "locky", config={}, **fields).id


def _claim(engine: Engine, job_id: int, *, now: datetime = NOW, until: datetime = UNTIL) -> bool:
    with session_scope(session_factory(engine)) as session:
        return JobRepository(session).claim(job_id, now=now, until=until)


def _release(engine: Engine, job_id: int, **kwargs: object) -> bool:
    with session_scope(session_factory(engine)) as session:
        return JobRepository(session).release(job_id, **kwargs)  # type: ignore[arg-type]


def _state(engine: Engine, job_id: int) -> tuple[datetime | None, datetime | None]:
    with session_scope(session_factory(engine)) as session:
        job = JobRepository(session).get(job_id)
        assert job is not None
        return job.locked_until, job.next_run_at


def test_get_returns_job_or_none(db_engine: Engine) -> None:
    job_id = _make_job(db_engine)

    with session_scope(session_factory(db_engine)) as session:
        repo = JobRepository(session)
        job = repo.get(job_id)
        assert job is not None and job.name == "locky"
        assert repo.get(job_id + 1000) is None


def test_claim_on_unlocked_job_succeeds(db_engine: Engine) -> None:
    job_id = _make_job(db_engine)

    assert _claim(db_engine, job_id) is True
    assert _state(db_engine, job_id)[0] == UNTIL


def test_second_claim_before_expiry_fails_and_keeps_the_lock(db_engine: Engine) -> None:
    job_id = _make_job(db_engine)
    assert _claim(db_engine, job_id) is True

    later = UNTIL + timedelta(hours=1)
    assert _claim(db_engine, job_id, now=NOW + timedelta(minutes=1), until=later) is False

    assert _state(db_engine, job_id)[0] == UNTIL


def test_claim_on_expired_lock_succeeds(db_engine: Engine) -> None:
    job_id = _make_job(db_engine, locked_until=NOW - timedelta(seconds=1))

    assert _claim(db_engine, job_id) is True
    assert _state(db_engine, job_id)[0] == UNTIL


def test_claim_exactly_at_expiry_succeeds(db_engine: Engine) -> None:
    job_id = _make_job(db_engine, locked_until=NOW)

    assert _claim(db_engine, job_id) is True
    assert _state(db_engine, job_id)[0] == UNTIL


def test_claim_on_unknown_job_is_false(db_engine: Engine) -> None:
    assert _claim(db_engine, 987654) is False


def test_release_with_token_clears_lock_and_sets_next_run(db_engine: Engine) -> None:
    job_id = _make_job(db_engine)
    _claim(db_engine, job_id)

    assert _release(db_engine, job_id, until=UNTIL, next_run_at=NEXT) is True

    assert _state(db_engine, job_id) == (None, NEXT)


def test_release_with_keep_leaves_next_run_alone(db_engine: Engine) -> None:
    job_id = _make_job(db_engine, next_run_at=NEXT)
    _claim(db_engine, job_id)

    assert _release(db_engine, job_id, until=UNTIL, next_run_at=KEEP) is True
    assert _state(db_engine, job_id) == (None, NEXT)


def test_release_defaults_to_keep(db_engine: Engine) -> None:
    job_id = _make_job(db_engine, next_run_at=NEXT)
    _claim(db_engine, job_id)

    assert _release(db_engine, job_id, until=UNTIL) is True
    assert _state(db_engine, job_id) == (None, NEXT)


def test_release_with_none_clears_next_run(db_engine: Engine) -> None:
    job_id = _make_job(db_engine, next_run_at=NEXT)
    _claim(db_engine, job_id)

    assert _release(db_engine, job_id, until=UNTIL, next_run_at=None) is True
    assert _state(db_engine, job_id) == (None, None)


def test_release_with_stale_token_changes_nothing(db_engine: Engine) -> None:
    job_id = _make_job(db_engine, next_run_at=NEXT)
    _claim(db_engine, job_id)

    stale = UNTIL - timedelta(minutes=5)
    assert _release(db_engine, job_id, until=stale, next_run_at=None) is False

    assert _state(db_engine, job_id) == (UNTIL, NEXT)


def test_locked_until_round_trips_at_microsecond_precision(db_engine: Engine) -> None:
    job_id = _make_job(db_engine)
    precise = datetime(2026, 10, 4, 14, 0, 0, 654321, tzinfo=UTC)

    _claim(db_engine, job_id, until=precise)

    stored = _state(db_engine, job_id)[0]
    assert stored == precise
    assert stored is not None and stored.microsecond == 654321


@pytest.mark.skipif(uses_sqlite(), reason="needs two connections; SQLite tests share one")
def test_concurrent_claims_grant_exactly_one(db_engine: Engine) -> None:
    job_id = _make_job(db_engine)
    barrier = threading.Barrier(2)
    results: list[bool] = []

    def worker(offset: int) -> None:
        barrier.wait()
        results.append(
            _claim(db_engine, job_id, until=UNTIL + timedelta(seconds=offset)),
        )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(results) == [False, True]
