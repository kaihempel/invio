"""The queries of ``run-due`` (#23): ``list_due`` and ``failure_streak``."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import StatementError

from invio.db.repositories import DueJob, JobRepository, RunRepository
from invio.db.session import session_factory, session_scope
from invio.domain import RunStatus
from tests.db_helpers import make_job
from tests.pipeline_helpers import add_run

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_jobs")]

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _seed(engine: Engine, **jobs: dict[str, object]) -> dict[str, int]:
    with session_scope(session_factory(engine)) as session:
        return {name: make_job(session, name, config={}, **kw).id for name, kw in jobs.items()}


def _list_due(engine: Engine) -> list[DueJob]:
    with session_scope(session_factory(engine)) as session:
        return JobRepository(session).list_due(NOW)


def test_list_due_includes_enabled_jobs_due_at_or_before_now(db_engine: Engine) -> None:
    ids = _seed(
        db_engine,
        past={"next_run_at": NOW - timedelta(days=1)},
        exact={"next_run_at": NOW},
    )

    assert [job.id for job in _list_due(db_engine)] == [ids["past"], ids["exact"]]


def test_list_due_excludes_future_disabled_and_unscheduled_jobs(db_engine: Engine) -> None:
    _seed(
        db_engine,
        future={"next_run_at": NOW + timedelta(seconds=1)},
        disabled={"next_run_at": NOW - timedelta(hours=1), "enabled": False},
        unscheduled={"next_run_at": None},
    )

    assert _list_due(db_engine) == []


def test_list_due_orders_by_next_run_then_id(db_engine: Engine) -> None:
    same = NOW - timedelta(hours=1)
    ids = _seed(
        db_engine,
        b={"next_run_at": same},
        a={"next_run_at": same},
        oldest={"next_run_at": NOW - timedelta(days=2)},
    )

    assert [job.id for job in _list_due(db_engine)] == [ids["oldest"], ids["b"], ids["a"]]


def test_list_due_returns_due_jobs_with_their_lock(db_engine: Engine) -> None:
    due = NOW - timedelta(hours=1)
    lock = NOW + timedelta(hours=1)
    _seed(
        db_engine,
        free={"next_run_at": due},
        locked={"next_run_at": due + timedelta(minutes=1), "locked_until": lock},
    )

    free, locked = _list_due(db_engine)

    assert (free.name, free.next_run_at, free.locked_until) == ("free", due, None)
    assert (locked.name, locked.locked_until) == ("locked", lock)
    assert isinstance(free, DueJob)


def test_list_due_rejects_a_naive_datetime(db_engine: Engine) -> None:
    with (
        session_scope(session_factory(db_engine)) as session,
        pytest.raises((ValueError, StatementError)),
    ):
        JobRepository(session).list_due(datetime(2026, 10, 7, 12, 0))


# --- failure_streak -----------------------------------------------------------------------------


def _runs(engine: Engine, job_id: int, *statuses: RunStatus) -> list[int]:
    """Store one run per status, oldest first, one minute apart; return the run ids."""
    factory = session_factory(engine)
    return [
        add_run(factory, job_id, status, started_at=NOW + timedelta(minutes=n))
        for n, status in enumerate(statuses)
    ]


def _streak(engine: Engine, job_id: int, *, exclude: int = 0, cap: int = 6) -> int:
    with session_scope(session_factory(engine)) as session:
        return RunRepository(session).failure_streak(job_id, exclude_run_id=exclude, cap=cap)


def _one_job(engine: Engine) -> int:
    return _seed(engine, streaky={})["streaky"]


F, S, P, R = RunStatus.FAILED, RunStatus.SUCCEEDED, RunStatus.PARTIAL, RunStatus.RUNNING


def test_failure_streak_without_runs_is_zero(db_engine: Engine) -> None:
    assert _streak(db_engine, _one_job(db_engine)) == 0


def test_failure_streak_counts_the_failures_since_the_last_success(db_engine: Engine) -> None:
    job_id = _one_job(db_engine)
    _runs(db_engine, job_id, S, F, F)

    assert _streak(db_engine, job_id) == 2


def test_failure_streak_stops_at_a_partial_run(db_engine: Engine) -> None:
    job_id = _one_job(db_engine)
    _runs(db_engine, job_id, F, F, P, F)

    assert _streak(db_engine, job_id) == 1


def test_failure_streak_skips_dry_runs_whatever_their_status(db_engine: Engine) -> None:
    job_id = _one_job(db_engine)
    factory = session_factory(db_engine)
    add_run(factory, job_id, F, started_at=NOW)
    add_run(factory, job_id, S, stats={"dry_run": True}, started_at=NOW + timedelta(minutes=1))
    add_run(factory, job_id, F, stats={"dry_run": True}, started_at=NOW + timedelta(minutes=2))

    assert _streak(db_engine, job_id) == 1


def test_failure_streak_skips_running_runs(db_engine: Engine) -> None:
    job_id = _one_job(db_engine)
    _runs(db_engine, job_id, F, R, F)

    assert _streak(db_engine, job_id) == 2


def test_failure_streak_running_rows_do_not_use_up_the_scan_limit(db_engine: Engine) -> None:
    job_id = _one_job(db_engine)
    _runs(db_engine, job_id, F, F, *[R] * 60)  # crashed runs newer than the failures

    assert _streak(db_engine, job_id) == 2


def test_failure_streak_skips_the_excluded_run(db_engine: Engine) -> None:
    job_id = _one_job(db_engine)
    ids = _runs(db_engine, job_id, F, F)

    assert _streak(db_engine, job_id, exclude=ids[-1]) == 1


def test_failure_streak_stops_at_the_cap(db_engine: Engine) -> None:
    job_id = _one_job(db_engine)
    _runs(db_engine, job_id, *[F] * 8)

    assert _streak(db_engine, job_id, cap=6) == 6


def test_failure_streak_counts_only_the_given_job(db_engine: Engine) -> None:
    ids = _seed(db_engine, one={}, other={})
    _runs(db_engine, ids["other"], F, F, F)
    _runs(db_engine, ids["one"], F)

    assert _streak(db_engine, ids["one"]) == 1
