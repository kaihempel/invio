"""``run_due``: run every due job once, safely, for an unattended timer (#23).

Selection reads the due set once. Each job is then claimed with the due-aware atomic UPDATE of
:func:`~invio.pipeline.run.run_job` (``due_by``): of several overlapping invocations exactly one
runs a job, and a job another invocation finished since the selection is not run again. A busy
or no-longer-due job is reported, never an error. One job failing never stops the others.
"""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from invio.db.models import Job
from invio.db.repositories import DueJob, JobRepository
from invio.db.session import session_scope
from invio.domain import RunStatus
from invio.graph.ports import RunDeps
from invio.pipeline.run import (
    JobBusyError,
    JobDisabledError,
    JobNotDueError,
    _deps_or_default,
    run_job,
)
from invio.services.jobs import JobNotFoundError

__all__ = ["DueOutcome", "DueReport", "run_due"]

logger = logging.getLogger("invio.pipeline")

OutcomeKind = Literal["ran", "busy", "skipped", "error"]


@dataclass(frozen=True, slots=True)
class DueOutcome:
    """What happened to one due job.

    ``ran``: the job ran (``run_id``, ``status``, ``next_run_at``, ``retry``). ``busy``: another
    run holds an unexpired lock (``locked_until``). ``skipped``: the job vanished, was disabled
    or finished by someone else after the selection (``reason``). ``error``: the job raised
    outside a run result; ``reason`` is only the exception class, as the message may hold
    secrets. ``timezone`` is the job's schedule time zone, for display only.
    """

    job_id: int
    job_name: str
    kind: OutcomeKind
    run_id: int | None = None
    status: RunStatus | None = None
    reason: str | None = None
    locked_until: datetime | None = None
    next_run_at: datetime | None = None
    retry: bool = False
    timezone: str = "UTC"


@dataclass(frozen=True, slots=True)
class DueReport:
    """The result of one ``run-due`` invocation, outcomes in due order.

    ``due`` counts every due job; ``deferred`` those left for the next invocation by ``--limit``
    (they have no outcome). ``parallel`` is the parallelism actually used.
    """

    started_at: datetime
    due: int
    outcomes: tuple[DueOutcome, ...]
    deferred: int = 0
    parallel: int = 1

    @property
    def failed(self) -> bool:
        """A run failed or a job raised. Partial runs, busy and skipped jobs are not failures."""
        return any(
            o.kind == "error" or (o.kind == "ran" and o.status is RunStatus.FAILED)
            for o in self.outcomes
        )

    @property
    def exit_code(self) -> int:
        return 1 if self.failed else 0


async def run_due(
    *, limit: int | None = None, parallel: int = 1, deps: RunDeps | None = None
) -> DueReport:
    """Run the due jobs and report what happened to each.

    At most ``limit`` runnable jobs are started (busy ones do not count, the rest wait for the
    next invocation); up to ``parallel`` run at the same time, started in due order. On SQLite
    ``parallel`` is lowered to 1: one file serializes writers. Errors before the first job
    (database, settings) propagate; ``limit`` or ``parallel`` below 1 raise ``ValueError``
    before anything is read.
    """
    if limit is not None and limit < 1:
        raise ValueError(f"limit must be >= 1, got {limit}")
    if parallel < 1:
        raise ValueError(f"parallel must be >= 1, got {parallel}")
    async with _deps_or_default(deps) as resolved:
        return await _run_due(resolved, limit, parallel)


async def _run_due(deps: RunDeps, limit: int | None, parallel: int) -> DueReport:
    now = deps.clock()
    if parallel > 1 and _is_sqlite(deps):
        logger.warning("run_due.parallel_sqlite", extra={"requested": parallel})
        parallel = 1
    with session_scope(deps.session_factory) as session:
        repo = JobRepository(session)
        due = repo.list_due(now)
        zones = {job.id: _time_zone(job.id, repo.get(job.id)) for job in due}
    slots: list[DueOutcome | None] = [None] * len(due)
    candidates: list[tuple[int, DueJob]] = []
    for index, job in enumerate(due):
        if job.locked_until is not None and job.locked_until > now:
            slots[index] = DueOutcome(
                job.id, job.name, "busy", locked_until=job.locked_until, timezone=zones[job.id]
            )
        else:
            candidates.append((index, job))
    selected = candidates if limit is None else candidates[:limit]
    deferred = len(candidates) - len(selected)
    logger.info(
        "run_due.started",
        extra={"due": len(due), "candidates": len(selected), "deferred": deferred},
    )

    async def one(index: int, job: DueJob) -> None:
        slots[index] = await _run_one(job, deps, now, zones[job.id])

    if parallel == 1:
        for index, job in selected:
            await one(index, job)
    else:
        gate = asyncio.Semaphore(parallel)

        async def gated(index: int, job: DueJob) -> None:
            async with gate:
                await one(index, job)

        async with asyncio.TaskGroup() as group:
            for index, job in selected:
                group.create_task(gated(index, job))
    report = DueReport(
        started_at=now,
        due=len(due),
        outcomes=tuple(o for o in slots if o is not None),
        deferred=deferred,
        parallel=parallel,
    )
    logger.info(
        "run_due.finished",
        extra={
            "due": report.due,
            "ran": sum(o.kind == "ran" for o in report.outcomes),
            "busy": sum(o.kind == "busy" for o in report.outcomes),
            "skipped": sum(o.kind == "skipped" for o in report.outcomes),
            "errors": sum(o.kind == "error" for o in report.outcomes),
            "deferred": deferred,
            "exit_code": report.exit_code,
        },
    )
    return report


async def _run_one(job: DueJob, deps: RunDeps, due_by: datetime, timezone: str) -> DueOutcome:
    """Run one due job and turn every ``Exception`` into an outcome.

    ``KeyboardInterrupt`` and ``CancelledError`` are not ``Exception``: they propagate, and the
    run's own safety net records the run failed and releases the lock.
    """
    try:
        result = await run_job(job.id, deps=deps, due_by=due_by)
    except JobBusyError as err:
        return DueOutcome(
            job.id, job.name, "busy", locked_until=err.locked_until, timezone=timezone
        )
    except JobNotDueError:
        return DueOutcome(job.id, job.name, "skipped", reason="no longer due", timezone=timezone)
    except JobDisabledError:
        return DueOutcome(job.id, job.name, "skipped", reason="disabled", timezone=timezone)
    except JobNotFoundError:
        return DueOutcome(job.id, job.name, "skipped", reason="deleted", timezone=timezone)
    except Exception as err:  # one job must never stop the others; the message may hold secrets
        logger.error("run_due.job_error", extra={"job_id": job.id, "error": type(err).__name__})
        return DueOutcome(job.id, job.name, "error", reason=type(err).__name__, timezone=timezone)
    return DueOutcome(
        job.id,
        job.name,
        "ran",
        run_id=result.run_id,
        status=result.status,
        next_run_at=result.next_run_at,
        retry=result.retry_scheduled,
        timezone=timezone,
    )


def _is_sqlite(deps: RunDeps) -> bool:
    bind = deps.session_factory.kw.get("bind")
    return bind is not None and bind.dialect.name == "sqlite"


def _time_zone(job_id: int, job: Job | None) -> str:
    """The job's schedule time zone for display; ``UTC`` when it is missing or invalid."""
    try:
        if job is None:
            raise LookupError("job vanished")
        name = str((job.config or {})["schedule"]["timezone"])
        ZoneInfo(name)
    except Exception:  # missing job, key, bad or unknown zone: it is display only
        logger.warning("run_due.timezone_unreadable", extra={"job_id": job_id})
        return "UTC"
    return name
