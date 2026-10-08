"""Two ``run-due`` processes at the same time never run a job twice (MariaDB only; #23, SC-001).

The invocations are forced to overlap: each selects its due set, then waits on a barrier until
the other has selected too, so both try to claim every due job. Each invocation has its own lock
duration, so their lock tokens differ and a release that ignored ownership would show.
"""

import asyncio
import threading
from collections.abc import Iterator
from datetime import datetime, timedelta

import pytest
from sqlalchemy import Engine, delete, func, select

from invio.db.models import Job, Run
from invio.db.repositories import DueJob, JobRepository
from invio.db.session import session_factory, session_scope
from invio.pipeline.due import DueOutcome, DueReport, run_due
from tests.conftest import FakeClock
from tests.db_helpers import uses_sqlite
from tests.pipeline_helpers import (
    FakePorts,
    RoutedFakeProvider,
    make_candidates,
    make_deps,
    make_job_config,
    store_job,
)

pytestmark = [
    pytest.mark.db,
    pytest.mark.usefixtures("clean_jobs"),
    pytest.mark.skipif(uses_sqlite(), reason="needs two connections; SQLite tests share one"),
]

FEED = "https://example.com/feed.xml"
JOBS = 10
INVOCATIONS = 2
WAIT_SECONDS = 60  # a barrier or join that takes longer is a deadlock: fail, do not hang CI


@pytest.fixture
def selected(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Barrier]:
    """A barrier every invocation waits on right after it selected its due jobs."""
    barrier = threading.Barrier(INVOCATIONS, timeout=WAIT_SECONDS)
    real = JobRepository.list_due

    def list_then_wait(self: JobRepository, now: datetime) -> list[DueJob]:
        due = real(self, now)
        barrier.wait()
        return due

    monkeypatch.setattr(JobRepository, "list_due", list_then_wait)
    yield barrier
    barrier.abort()  # never leave a thread waiting


def _race(engine: Engine, clock: FakeClock) -> list[DueReport]:
    """Run two invocations in threads; re-raise the first error of either thread."""
    start = threading.Barrier(INVOCATIONS, timeout=WAIT_SECONDS)
    reports: list[DueReport | None] = [None] * INVOCATIONS
    errors: list[BaseException] = []

    def invocation(index: int) -> None:
        try:
            deps = make_deps(
                session_factory(engine),
                FakePorts({FEED: make_candidates(3)}),
                RoutedFakeProvider(),
                clock=clock,
                lock_ttl=timedelta(hours=2, seconds=index),  # a distinct lock token per invocation
            )
            start.wait()
            reports[index] = asyncio.run(run_due(deps=deps))
        except BaseException as err:  # re-raised in the test thread below
            errors.append(err)

    threads = [threading.Thread(target=invocation, args=(i,)) for i in range(INVOCATIONS)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=WAIT_SECONDS)
    assert not any(thread.is_alive() for thread in threads), "an invocation hung"
    if errors:
        raise errors[0]
    assert all(report is not None for report in reports)
    return [report for report in reports if report is not None]


def _outcomes(reports: list[DueReport]) -> list[DueOutcome]:
    return [outcome for report in reports for outcome in report.outcomes]


def _assert_contended_exactly_once(outcomes: list[DueOutcome], ids: set[int]) -> None:
    """Each job ran in exactly one invocation and the other one found it taken."""
    for job_id in ids:
        kinds = sorted(o.kind for o in outcomes if o.job_id == job_id)
        assert kinds in (["busy", "ran"], ["ran", "skipped"]), (job_id, kinds)
    lost = [o for o in outcomes if o.job_id in ids and o.kind == "skipped"]
    assert {o.reason for o in lost} <= {"no longer due"}


def _run_counts(engine: Engine) -> dict[int, int]:
    with session_scope(session_factory(engine)) as session:
        rows = session.execute(select(Run.job_id, func.count()).group_by(Run.job_id)).all()
        return {job_id: count for job_id, count in rows}


def _locks(engine: Engine) -> dict[int, datetime | None]:
    with session_scope(session_factory(engine)) as session:
        return {job_id: lock for job_id, lock in session.execute(select(Job.id, Job.locked_until))}


def _fresh(engine: Engine) -> None:
    with session_scope(session_factory(engine)) as session:
        session.execute(delete(Job))


@pytest.mark.parametrize("round_", range(20))
def test_two_invocations_run_every_due_job_exactly_once(
    db_engine: Engine, fake_clock: FakeClock, selected: threading.Barrier, round_: int
) -> None:
    _fresh(db_engine)
    factory = session_factory(db_engine)
    due = fake_clock() - timedelta(hours=1)
    ids = {
        store_job(factory, make_job_config(), name=f"job-{n}", next_run_at=due) for n in range(JOBS)
    }

    reports = _race(db_engine, fake_clock)

    assert [report.due for report in reports] == [JOBS] * INVOCATIONS  # both selected every job
    outcomes = _outcomes(reports)
    _assert_contended_exactly_once(outcomes, ids)
    assert _run_counts(db_engine) == dict.fromkeys(ids, 1)
    assert _locks(db_engine) == dict.fromkeys(ids)  # every owner released its own lock


@pytest.mark.parametrize("round_", range(5))
def test_two_invocations_reclaim_each_stale_lock_exactly_once(
    db_engine: Engine, fake_clock: FakeClock, selected: threading.Barrier, round_: int
) -> None:
    _fresh(db_engine)
    factory = session_factory(db_engine)
    now = fake_clock()
    stale = {
        store_job(
            factory,
            make_job_config(),
            name=f"stale-{n}",
            next_run_at=now - timedelta(hours=3),
            locked_until=now - timedelta(minutes=1),  # a crashed run's expired claim
        )
        for n in range(JOBS)
    }
    held_lock = now + timedelta(hours=1)
    held = store_job(
        factory,
        make_job_config(),
        name="held",
        next_run_at=now - timedelta(hours=3),
        locked_until=held_lock,
    )

    reports = _race(db_engine, fake_clock)

    outcomes = _outcomes(reports)
    _assert_contended_exactly_once(outcomes, stale)
    assert [o.kind for o in outcomes if o.job_id == held] == ["busy"] * INVOCATIONS
    assert _run_counts(db_engine) == dict.fromkeys(stale, 1)
    locks = _locks(db_engine)
    assert {job_id: locks[job_id] for job_id in stale} == dict.fromkeys(stale)
    assert locks[held] == held_lock
