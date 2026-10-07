"""`run_due` over fakes: selection, locking, missed slots, retry and `--parallel` (#23)."""

import asyncio
import dataclasses
import logging
import signal
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, event, select, update
from sqlalchemy.exc import OperationalError

from invio.config.job import ScheduleConfig
from invio.db.models import Job, Run
from invio.db.repositories import DueJob, JobRepository, RunRepository
from invio.db.session import session_factory, session_scope
from invio.domain import RunStatus
from invio.graph.ports import RunDeps
from invio.pipeline import due as due_module
from invio.pipeline.due import DueOutcome, DueReport, run_due
from invio.pipeline.run import RunResult, run_job
from invio.scheduling.next_run import compute_next_run
from invio.sources.errors import FetchError
from tests.conftest import FakeClock
from tests.db_helpers import uses_sqlite
from tests.pipeline_helpers import (
    FakePorts,
    RoutedFakeProvider,
    add_run,
    make_candidates,
    make_deps,
    make_job_config,
    plus_one_hour,
    source_key,
    store_job,
)

pytestmark = pytest.mark.usefixtures("clean_jobs")

REAL_IS_SQLITE = due_module._is_sqlite  # captured before the autouse fixture replaces it

FEED = "https://example.com/feed.xml"
FEED_BAD = "https://example.com/bad.xml"

NextRun = Callable[[ScheduleConfig, datetime], datetime]


def _deps(
    engine: Engine,
    clock: Callable[[], datetime],
    *,
    ports: FakePorts | None = None,
    next_run: NextRun = plus_one_hour,
    **kw: Any,
) -> RunDeps:
    ports = ports or FakePorts({FEED: make_candidates(3)})
    return make_deps(
        session_factory(engine), ports, RoutedFakeProvider(), clock=clock, next_run=next_run, **kw
    )


def _store(
    engine: Engine, name: str, next_run_at: datetime | None, *, bad: bool = False, **kw: Any
) -> int:
    config = make_job_config(sources=[{"type": "rss", "url": FEED_BAD if bad else FEED}])
    return store_job(session_factory(engine), config, name=name, next_run_at=next_run_at, **kw)


def _job(engine: Engine, job_id: int) -> Job:
    with session_scope(session_factory(engine)) as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def _run_order(engine: Engine) -> list[str]:
    """The names of the jobs that ran, in run-id order."""
    with session_scope(session_factory(engine)) as session:
        rows = session.execute(
            select(Job.name).join(Run, Run.job_id == Job.id).order_by(Run.id)
        ).all()
        return [name for (name,) in rows]


def _run_count(engine: Engine) -> int:
    return len(_run_order(engine))


def _kinds(report: DueReport) -> list[tuple[str, str]]:
    return [(o.job_name, o.kind) for o in report.outcomes]


@pytest.fixture(autouse=True)
def _not_sqlite_clamped(monkeypatch: pytest.MonkeyPatch) -> None:
    """Most tests do not care about the SQLite clamp of ``--parallel``."""
    monkeypatch.setattr(due_module, "_is_sqlite", lambda deps: False)


# --- US1: run every due job ---------------------------------------------------------------------


async def test_the_due_jobs_run_oldest_first(db_engine: Engine, fake_clock: FakeClock) -> None:
    now = fake_clock()
    _store(db_engine, "recent", now - timedelta(hours=1))
    _store(db_engine, "oldest", now - timedelta(days=2))
    _store(db_engine, "middle", now - timedelta(hours=5))

    report = await run_due(deps=_deps(db_engine, fake_clock))

    assert _run_order(db_engine) == ["oldest", "middle", "recent"]
    assert _kinds(report) == [("oldest", "ran"), ("middle", "ran"), ("recent", "ran")]
    assert report.due == 3
    assert report.exit_code == 0


async def test_future_and_disabled_jobs_are_left_alone(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    now = fake_clock()
    due = _store(db_engine, "due", now - timedelta(hours=1))
    future = _store(db_engine, "future", now + timedelta(hours=1))
    disabled = _store(db_engine, "disabled", now - timedelta(hours=1), enabled=False)

    report = await run_due(deps=_deps(db_engine, fake_clock))

    assert [o.job_id for o in report.outcomes] == [due]
    assert _run_order(db_engine) == ["due"]
    assert _job(db_engine, future).next_run_at == now + timedelta(hours=1)
    assert _job(db_engine, disabled).next_run_at == now - timedelta(hours=1)


async def test_a_run_unlocks_the_job_and_moves_it_to_its_next_slot(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    now = fake_clock()
    job_id = _store(db_engine, "daily", now - timedelta(hours=1))

    report = await run_due(deps=_deps(db_engine, fake_clock))

    job = _job(db_engine, job_id)
    assert job.locked_until is None
    assert job.next_run_at == now + timedelta(hours=1)
    (outcome,) = report.outcomes
    assert (outcome.kind, outcome.status) == ("ran", RunStatus.SUCCEEDED)
    assert outcome.next_run_at == job.next_run_at
    assert outcome.run_id is not None
    assert outcome.timezone == "Europe/Berlin"


async def test_nothing_due_is_an_empty_report_with_one_select(
    db_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _store(db_engine, "future", fake_clock() + timedelta(days=1))
    deps = _deps(db_engine, fake_clock)

    async def must_not_run(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("run_job must not be called")

    monkeypatch.setattr(due_module, "run_job", must_not_run)
    with _statements(db_engine) as statements:
        report = await run_due(deps=deps)

    assert (report.outcomes, report.due, report.exit_code) == ((), 0, 0)
    statements = [s for s in statements if s != "BEGIN"]
    assert len(statements) == 1
    assert statements[0].lstrip().upper().startswith("SELECT") and "jobs" in statements[0]


@contextmanager
def _statements(engine: Engine) -> Iterator[list[str]]:
    seen: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        yield seen
    finally:
        event.remove(engine, "before_cursor_execute", record)


async def test_limit_runs_only_the_oldest_jobs_and_reports_the_rest_as_deferred(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    now = fake_clock()
    ids = [_store(db_engine, f"job{n}", now - timedelta(hours=10 - n)) for n in range(5)]

    report = await run_due(limit=2, deps=_deps(db_engine, fake_clock))

    assert _kinds(report) == [("job0", "ran"), ("job1", "ran")]
    assert (report.due, report.deferred) == (5, 3)
    still_due = [_job(db_engine, i).next_run_at for i in ids[2:]]
    assert all(moment is not None and moment <= now for moment in still_due)


async def test_a_failed_run_does_not_stop_the_other_jobs(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    now = fake_clock()
    _store(db_engine, "a-bad", now - timedelta(hours=3), bad=True)
    _store(db_engine, "b-good", now - timedelta(hours=2))
    ports = FakePorts({FEED: make_candidates(3), FEED_BAD: FetchError("timeout", url=FEED_BAD)})

    report = await run_due(deps=_deps(db_engine, fake_clock, ports=ports))

    statuses = [(o.job_name, o.status) for o in report.outcomes]
    assert statuses == [("a-bad", RunStatus.FAILED), ("b-good", RunStatus.SUCCEEDED)]
    assert (report.failed, report.exit_code) == (True, 1)


async def test_a_job_that_raises_becomes_an_error_outcome(
    db_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = fake_clock()
    _store(db_engine, "first", now - timedelta(hours=2))
    _store(db_engine, "second", now - timedelta(hours=1))
    real = due_module.run_job

    async def flaky(job_id: int, **kwargs: Any) -> Any:
        if job_id == first_id:
            raise RuntimeError("secret mysql://user:pw@host/db")
        return await real(job_id, **kwargs)

    first_id = _job_id(db_engine, "first")
    monkeypatch.setattr(due_module, "run_job", flaky)

    report = await run_due(deps=_deps(db_engine, fake_clock))

    first, second = report.outcomes
    assert (first.kind, first.reason) == ("error", "RuntimeError")
    assert second.kind == "ran"
    assert report.exit_code == 1


def _job_id(engine: Engine, name: str) -> int:
    with session_scope(session_factory(engine)) as session:
        job = JobRepository(session).get_by_name(name)
        assert job is not None
        return job.id


@pytest.mark.parametrize("change", ["deleted", "disabled"])
async def test_a_job_changed_after_selection_is_skipped(
    db_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    job_id = _store(db_engine, "gone", fake_clock() - timedelta(hours=1))
    stale = DueJob(job_id, "gone", fake_clock() - timedelta(hours=1), None)
    monkeypatch.setattr(JobRepository, "list_due", lambda self, now: [stale])
    with session_scope(session_factory(db_engine)) as session:
        if change == "deleted":
            session.delete(session.get(Job, job_id))
        else:
            session.execute(update(Job).where(Job.id == job_id).values(enabled=False))

    report = await run_due(deps=_deps(db_engine, fake_clock))

    (outcome,) = report.outcomes
    assert (outcome.kind, outcome.reason) == ("skipped", change)
    assert report.exit_code == 0
    assert _run_count(db_engine) == 0


@pytest.mark.parametrize("kwargs", [{"limit": 0}, {"limit": -1}, {"parallel": 0}])
async def test_invalid_limit_or_parallel_is_rejected_before_any_query(
    db_engine: Engine, fake_clock: FakeClock, kwargs: dict[str, int]
) -> None:
    deps = _deps(db_engine, fake_clock)

    with _statements(db_engine) as statements, pytest.raises(ValueError):
        await run_due(deps=deps, **kwargs)

    assert statements == []


async def test_an_unreadable_time_zone_falls_back_to_utc(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    config = make_job_config(schedule__timezone="Europe/Berlin")
    config["schedule"]["timezone"] = "Mars/Olympus"
    store_job(
        session_factory(db_engine), config, name="odd", next_run_at=fake_clock() - timedelta(1)
    )

    report = await run_due(deps=_deps(db_engine, fake_clock))

    assert report.outcomes[0].timezone == "UTC"


async def test_default_deps_are_built_when_none_are_given(
    db_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    deps = _deps(db_engine, fake_clock)
    _store(db_engine, "due", fake_clock() - timedelta(hours=1))

    class Built:
        async def __aenter__(self) -> RunDeps:
            return deps

        async def __aexit__(self, *exc: object) -> None:
            return None

    monkeypatch.setattr("invio.pipeline.run.default_deps", lambda settings: Built())
    monkeypatch.setenv("INVIO_DATABASE_URL", "sqlite://")

    report = await run_due()

    assert [o.kind for o in report.outcomes] == ["ran"]


def test_the_outcome_and_report_types() -> None:
    ran_failed = DueOutcome(1, "a", "ran", status=RunStatus.FAILED)
    ran_ok = DueOutcome(2, "b", "ran", status=RunStatus.PARTIAL)
    busy = DueOutcome(3, "c", "busy")
    now = datetime(2026, 10, 7, tzinfo=UTC)

    assert DueReport(now, 3, (ran_ok, busy)).exit_code == 0
    assert DueReport(now, 3, (ran_failed, busy)).exit_code == 1
    assert DueReport(now, 1, (DueOutcome(4, "d", "error", reason="X"),)).exit_code == 1
    assert DueReport(now, 0, ()).failed is False


# --- US2: overlapping invocations never run a job twice -------------------------------------------


async def test_a_locked_job_is_busy_keeps_its_lock_and_does_not_use_up_the_limit(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    now = fake_clock()
    lock = now + timedelta(hours=1)
    busy_id = _store(db_engine, "busy", now - timedelta(hours=5), locked_until=lock)
    _store(db_engine, "one", now - timedelta(hours=4))
    _store(db_engine, "two", now - timedelta(hours=3))

    report = await run_due(limit=1, deps=_deps(db_engine, fake_clock))

    assert _kinds(report) == [("busy", "busy"), ("one", "ran")]
    assert report.outcomes[0].locked_until == lock
    assert _job(db_engine, busy_id).locked_until == lock
    assert report.deferred == 1
    assert report.exit_code == 0


async def test_a_job_finished_by_someone_else_after_selection_is_skipped(
    db_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = fake_clock()
    job_id = _store(db_engine, "raced", now - timedelta(hours=1))
    real_claim = JobRepository.claim

    def claim_after_another_run(self: JobRepository, *args: Any, **kwargs: Any) -> bool:
        self._session.execute(  # the other invocation finished the job
            update(Job).where(Job.id == job_id).values(next_run_at=now + timedelta(days=1))
        )
        return real_claim(self, *args, **kwargs)

    monkeypatch.setattr(JobRepository, "claim", claim_after_another_run)

    report = await run_due(deps=_deps(db_engine, fake_clock))

    (outcome,) = report.outcomes
    assert (outcome.kind, outcome.reason) == ("skipped", "no longer due")
    assert _run_count(db_engine) == 0
    assert report.exit_code == 0


async def test_a_lock_taken_after_selection_is_busy(
    db_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = fake_clock()
    job_id = _store(db_engine, "raced", now - timedelta(hours=1))
    lock = now + timedelta(hours=1)
    real_claim = JobRepository.claim

    def claim_after_another_claim(self: JobRepository, *args: Any, **kwargs: Any) -> bool:
        self._session.execute(  # the other invocation claimed the job first
            update(Job).where(Job.id == job_id).values(locked_until=lock)
        )
        return real_claim(self, *args, **kwargs)

    monkeypatch.setattr(JobRepository, "claim", claim_after_another_claim)

    report = await run_due(deps=_deps(db_engine, fake_clock))

    (outcome,) = report.outcomes
    assert (outcome.kind, outcome.locked_until) == ("busy", lock)
    assert _run_count(db_engine) == 0


# --- US3: stale locks ---------------------------------------------------------------------------


@pytest.mark.parametrize("offset", [timedelta(seconds=-1), timedelta(0)])
async def test_an_expired_lock_is_reclaimed(
    db_engine: Engine, fake_clock: FakeClock, offset: timedelta
) -> None:
    now = fake_clock()
    job_id = _store(db_engine, "stale", now - timedelta(hours=1), locked_until=now + offset)

    report = await run_due(deps=_deps(db_engine, fake_clock))

    assert _kinds(report) == [("stale", "ran")]
    assert _job(db_engine, job_id).locked_until is None


async def test_a_run_that_lost_its_lock_leaves_the_new_owners_lock_and_schedule_alone(
    db_engine: Engine, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    now = fake_clock()
    due_at = now - timedelta(hours=1)
    job_id = _store(db_engine, "slow", due_at)
    ports = FakePorts({FEED: make_candidates(3)})
    other_lock = now + timedelta(hours=9)
    real_fetch = ports.fetch_source

    async def slow_fetch(config: Any) -> Any:
        fake_clock.advance(timedelta(hours=3))  # past the 2 h lock: another invocation takes over
        with session_scope(session_factory(db_engine)) as session:
            session.execute(update(Job).where(Job.id == job_id).values(locked_until=other_lock))
        return await real_fetch(config)

    deps = dataclasses.replace(_deps(db_engine, fake_clock, ports=ports), fetch_source=slow_fetch)

    with caplog.at_level(logging.WARNING):
        await run_due(deps=deps)

    job = _job(db_engine, job_id)
    assert (job.locked_until, job.next_run_at) == (other_lock, due_at)
    assert "run.lock_lost" in caplog.text


# --- US4: missed slots collapse -----------------------------------------------------------------


async def test_several_missed_slots_run_once_and_wait_for_the_next_future_slot(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    schedule = {"frequency": "daily", "time": "07:00", "timezone": "Europe/Berlin"}
    config = make_job_config(schedule=schedule)
    now = fake_clock()
    job_id = store_job(
        session_factory(db_engine), config, name="daily", next_run_at=now - timedelta(days=3)
    )
    deps = _deps(db_engine, fake_clock, next_run=compute_next_run)
    expected = compute_next_run(ScheduleConfig.model_validate(schedule), now)

    first = await run_due(deps=deps)
    second = await run_due(deps=deps)

    (outcome,) = first.outcomes
    assert outcome.next_run_at == expected > now
    assert _job(db_engine, job_id).next_run_at == expected
    assert second.outcomes == ()
    assert _run_count(db_engine) == 1


# --- US5: failed runs are retried later with a growing delay ------------------------------------


def _in_a_week(schedule: ScheduleConfig, after: datetime) -> datetime:
    return after + timedelta(days=7)


def _in_half_an_hour(schedule: ScheduleConfig, after: datetime) -> datetime:
    return after + timedelta(minutes=30)


def _failing_ports() -> FakePorts:
    return FakePorts({FEED: make_candidates(3), FEED_BAD: FetchError("timeout", url=FEED_BAD)})


async def test_a_failure_retries_after_one_hour_then_two_then_four(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    job_id = _store(db_engine, "flaky", fake_clock() - timedelta(hours=1), bad=True)
    deps = _deps(db_engine, fake_clock, ports=_failing_ports(), next_run=_in_a_week)

    for hours in (1, 2, 4):
        report = await run_due(deps=deps)

        (outcome,) = report.outcomes
        expected = fake_clock() + timedelta(hours=hours)
        assert (outcome.status, outcome.retry) == (RunStatus.FAILED, True)
        assert outcome.next_run_at == expected
        assert _job(db_engine, job_id).next_run_at == expected
        assert _job(db_engine, job_id).locked_until is None
        fake_clock.advance(timedelta(hours=hours))


async def test_a_success_after_failures_returns_to_the_regular_slot(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    job_id = _store(db_engine, "healing", fake_clock() - timedelta(hours=1), bad=True)
    ports = _failing_ports()
    deps = _deps(db_engine, fake_clock, ports=ports, next_run=_in_a_week)
    await run_due(deps=deps)
    fake_clock.advance(timedelta(hours=1))
    ports.sources[source_key(FEED_BAD)] = make_candidates(3)

    report = await run_due(deps=deps)

    (outcome,) = report.outcomes
    assert (outcome.status, outcome.retry) == (RunStatus.SUCCEEDED, False)
    assert _job(db_engine, job_id).next_run_at == fake_clock() + timedelta(days=7)


async def test_a_regular_slot_earlier_than_the_retry_wins(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    job_id = _store(db_engine, "frequent", fake_clock() - timedelta(hours=1), bad=True)
    deps = _deps(db_engine, fake_clock, ports=_failing_ports(), next_run=_in_half_an_hour)

    report = await run_due(deps=deps)

    (outcome,) = report.outcomes
    assert (outcome.status, outcome.retry) == (RunStatus.FAILED, False)
    assert _job(db_engine, job_id).next_run_at == fake_clock() + timedelta(minutes=30)


async def test_a_retry_equal_to_the_regular_slot_counts_as_the_regular_slot(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    _store(db_engine, "tie", fake_clock() - timedelta(hours=1), bad=True)
    deps = _deps(db_engine, fake_clock, ports=_failing_ports(), next_run=plus_one_hour)

    report = await run_due(deps=deps)

    assert report.outcomes[0].retry is False


async def test_a_failed_job_is_not_run_again_straight_away(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    _store(db_engine, "once", fake_clock() - timedelta(hours=1), bad=True)
    deps = _deps(db_engine, fake_clock, ports=_failing_ports(), next_run=_in_a_week)
    await run_due(deps=deps)

    second = await run_due(deps=deps)

    assert second.outcomes == ()
    assert _run_count(db_engine) == 1


async def test_an_unreadable_streak_counts_as_the_first_failure_and_the_lock_is_released(
    db_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = _store(db_engine, "streak-broken", fake_clock() - timedelta(hours=1), bad=True)

    def broken(self: RunRepository, *args: Any, **kwargs: Any) -> int:
        raise OperationalError("SELECT", {}, Exception("gone"))

    monkeypatch.setattr(RunRepository, "failure_streak", broken)
    deps = _deps(db_engine, fake_clock, ports=_failing_ports(), next_run=_in_a_week)

    await run_due(deps=deps)

    job = _job(db_engine, job_id)
    assert job.locked_until is None
    assert job.next_run_at == fake_clock() + timedelta(hours=1)


async def test_a_failed_run_with_an_unreadable_schedule_is_retried_after_the_delay(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    # spec Edge Cases: no regular slot, so the retry delay alone sets the next run (no loop).
    config = make_job_config()
    config["schedule"] = {"frequency": "never"}
    job_id = store_job(
        session_factory(db_engine), config, name="corrupt", next_run_at=fake_clock() - timedelta(1)
    )
    deps = _deps(db_engine, fake_clock)

    first = await run_due(deps=deps)
    second = await run_due(deps=deps)

    (outcome,) = first.outcomes
    expected = fake_clock() + timedelta(hours=1)
    assert (outcome.status, outcome.retry, outcome.next_run_at) == (
        RunStatus.FAILED,
        True,
        expected,
    )
    job = _job(db_engine, job_id)
    assert (job.next_run_at, job.locked_until) == (expected, None)
    assert second.outcomes == ()
    assert _run_count(db_engine) == 1


async def test_a_failed_run_whose_next_slot_cannot_be_computed_is_retried_after_the_delay(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    def broken(schedule: ScheduleConfig, after: datetime) -> datetime:
        raise ValueError("no slot")

    job_id = _store(db_engine, "no-slot", fake_clock() - timedelta(hours=1), bad=True)

    report = await run_due(
        deps=_deps(db_engine, fake_clock, ports=_failing_ports(), next_run=broken)
    )

    (outcome,) = report.outcomes
    assert (outcome.status, outcome.retry) == (RunStatus.FAILED, True)
    assert _job(db_engine, job_id).next_run_at == fake_clock() + timedelta(hours=1)


async def test_a_failed_dry_run_with_an_unreadable_schedule_keeps_its_next_run(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    config = make_job_config()
    config["schedule"] = {"frequency": "never"}
    due_at = fake_clock() - timedelta(days=1)
    job_id = store_job(session_factory(db_engine), config, name="corrupt", next_run_at=due_at)

    result = await run_job(job_id, deps=_deps(db_engine, fake_clock), dry_run=True)

    assert (result.status, result.next_run_at, result.retry_scheduled) == (
        RunStatus.FAILED,
        None,
        False,
    )
    assert (_job(db_engine, job_id).next_run_at, _job(db_engine, job_id).locked_until) == (
        due_at,
        None,
    )


async def test_the_retry_delay_stops_growing_at_a_day(
    db_engine: Engine, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = _store(db_engine, "hopeless", fake_clock() - timedelta(hours=1), bad=True)
    factory = session_factory(db_engine)
    for n in range(8):  # eight earlier failures, oldest first
        add_run(factory, job_id, RunStatus.FAILED, started_at=fake_clock() - timedelta(days=9 - n))
    deps = _deps(db_engine, fake_clock, ports=_failing_ports(), next_run=_in_a_week)

    with caplog.at_level(logging.INFO, logger="invio.graph"):
        report = await run_due(deps=deps)

    (outcome,) = report.outcomes
    assert (outcome.retry, outcome.next_run_at) == (True, fake_clock() + timedelta(hours=24))
    (record,) = [r for r in caplog.records if r.getMessage() == "run.retry_scheduled"]
    assert record.streak == 7  # type: ignore[attr-defined]  # read cap 6 + this run


async def test_a_job_that_becomes_due_during_the_invocation_waits_for_the_next_one(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    now = fake_clock()
    _store(db_engine, "first", now - timedelta(hours=1))
    ports = FakePorts({FEED: make_candidates(3)})
    real_fetch = ports.fetch_source
    late: list[int] = []

    async def fetch_and_add_a_due_job(config: Any) -> Any:
        if not late:  # appears after the selection, already due at the invocation's start
            late.append(_store(db_engine, "late", now))
        return await real_fetch(config)

    deps = dataclasses.replace(
        _deps(db_engine, fake_clock, ports=ports), fetch_source=fetch_and_add_a_due_job
    )

    first = await run_due(deps=deps)
    second = await run_due(deps=deps)

    assert _kinds(first) == [("first", "ran")]
    assert first.due == 1
    assert _kinds(second) == [("late", "ran")]


def test_ctrl_c_during_a_run_stops_the_invocation_and_releases_the_lock(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    # The CLI path: ``asyncio.run`` turns SIGINT into a cancellation of the running invocation.
    # It installs that handler only on the main thread over Python's default SIGINT handler.
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("asyncio handles SIGINT on the main thread only")
    if signal.getsignal(signal.SIGINT) is not signal.default_int_handler:
        pytest.skip("another SIGINT handler is installed")
    now = fake_clock()
    first = _store(db_engine, "first", now - timedelta(hours=2))
    second = _store(db_engine, "second", now - timedelta(hours=1))
    ports = FakePorts({FEED: make_candidates(3)})
    real_fetch = ports.fetch_source

    async def interrupted(config: Any) -> Any:
        signal.raise_signal(signal.SIGINT)
        await asyncio.sleep(0)
        return await real_fetch(config)

    deps = dataclasses.replace(_deps(db_engine, fake_clock, ports=ports), fetch_source=interrupted)

    with pytest.raises(KeyboardInterrupt):
        asyncio.run(run_due(deps=deps))

    job = _job(db_engine, first)
    assert job.locked_until is None
    assert job.next_run_at == now + timedelta(hours=1)  # failed: retry rule (tie: regular slot)
    with session_scope(session_factory(db_engine)) as session:
        runs = [tuple(row) for row in session.execute(select(Run.job_id, Run.status)).all()]
    assert runs == [(first, RunStatus.FAILED)]  # the second job was never started
    assert _job(db_engine, second).locked_until is None


# --- US7: --parallel ------------------------------------------------------------------------------


class Probe:
    """A stand-in for ``run_job`` that records how many calls overlap and in which order."""

    def __init__(self, fail: set[int] | None = None) -> None:
        self.active = 0
        self.max_active = 0
        self.started: list[int] = []
        self.fail = fail or set()

    async def __call__(self, job_id: int, **kwargs: Any) -> RunResult:
        self.started.append(job_id)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            for _ in range(5):
                await asyncio.sleep(0)
        finally:
            self.active -= 1
        if job_id in self.fail:
            raise RuntimeError("boom")
        now = datetime(2026, 10, 4, tzinfo=UTC)
        return RunResult(
            job_id=job_id,
            run_id=job_id * 10,
            status=RunStatus.SUCCEEDED,
            dry_run=False,
            digest=None,
            stats={},
            errors=(),
            notifications_sent=0,
            notifications_failed=0,
            error=None,
            started_at=now,
            finished_at=now,
            next_run_at=None,
            retry_scheduled=False,
        )


def _four_due_jobs(engine: Engine, clock: FakeClock) -> list[int]:
    return [_store(engine, f"job{n}", clock() - timedelta(hours=10 - n)) for n in range(4)]


@pytest.mark.parametrize(("parallel", "expected_max"), [(1, 1), (2, 2), (3, 3), (9, 4)])
async def test_at_most_parallel_jobs_run_at_once_and_start_in_due_order(
    db_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    parallel: int,
    expected_max: int,
) -> None:
    ids = _four_due_jobs(db_engine, fake_clock)
    probe = Probe()
    monkeypatch.setattr(due_module, "run_job", probe)

    report = await run_due(parallel=parallel, deps=_deps(db_engine, fake_clock))

    # Deterministic: tasks and semaphore waiters resume in FIFO order and every call yields five
    # times, so the first ``min(parallel, 4)`` calls are all active before any of them returns.
    assert probe.max_active == expected_max
    assert probe.started == ids
    assert [o.job_id for o in report.outcomes] == ids
    assert [o.kind for o in report.outcomes] == ["ran"] * 4
    assert report.parallel == parallel


async def test_limit_applies_before_parallelism(
    db_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _four_due_jobs(db_engine, fake_clock)
    probe = Probe()
    monkeypatch.setattr(due_module, "run_job", probe)

    report = await run_due(limit=2, parallel=3, deps=_deps(db_engine, fake_clock))

    assert probe.started == ids[:2]
    assert report.deferred == 2


async def test_one_failing_job_does_not_stop_the_parallel_group(
    db_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _four_due_jobs(db_engine, fake_clock)
    monkeypatch.setattr(due_module, "run_job", Probe(fail={ids[1]}))

    report = await run_due(parallel=2, deps=_deps(db_engine, fake_clock))

    assert [o.kind for o in report.outcomes] == ["ran", "error", "ran", "ran"]
    assert report.exit_code == 1


@pytest.mark.skipif(not uses_sqlite(), reason="SQLite only: the clamp applies to SQLite")
async def test_parallel_is_lowered_to_one_on_sqlite(
    db_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(due_module, "_is_sqlite", REAL_IS_SQLITE)
    _four_due_jobs(db_engine, fake_clock)
    probe = Probe()
    monkeypatch.setattr(due_module, "run_job", probe)

    with caplog.at_level(logging.WARNING, logger="invio.pipeline"):
        report = await run_due(parallel=3, deps=_deps(db_engine, fake_clock))

    assert (report.parallel, probe.max_active) == (1, 1)
    (record,) = [r for r in caplog.records if r.getMessage() == "run_due.parallel_sqlite"]
    assert record.requested == 3  # type: ignore[attr-defined]


@pytest.mark.skipif(uses_sqlite(), reason="real overlapping runs need a server database")
async def test_real_runs_in_parallel_all_finish(db_engine: Engine, fake_clock: FakeClock) -> None:
    _four_due_jobs(db_engine, fake_clock)
    ports = FakePorts({FEED: make_candidates(3)}, hold=3)

    report = await run_due(parallel=2, deps=_deps(db_engine, fake_clock, ports=ports))

    assert [o.status for o in report.outcomes] == [RunStatus.SUCCEEDED] * 4
    assert _run_count(db_engine) == 4


async def test_cancelling_a_run_releases_its_lock(db_engine: Engine, fake_clock: FakeClock) -> None:
    job_id = _store(db_engine, "interrupted", fake_clock() - timedelta(hours=1))
    ports = FakePorts({FEED: make_candidates(3)}, gate=asyncio.Event())
    task = asyncio.create_task(run_due(deps=_deps(db_engine, fake_clock, ports=ports)))
    await asyncio.wait_for(ports.entered.wait(), timeout=10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _job(db_engine, job_id).locked_until is None
