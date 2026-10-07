"""``run_job``: run one job end to end (contracts/run-job.md).

The caller gets a :class:`RunResult` for every run that started, whatever its status. The
refusals (:class:`~invio.services.jobs.JobNotFoundError`, :class:`JobDisabledError`,
:class:`JobBusyError`, ``ValueError`` for a bad ``concurrency``) happen before any run row is
created or any lock is changed.

Order: claim the job lock (own committed transaction), start the run row (own committed
transaction), then build and invoke the graph *inside* ``run_context(job, run_id)``: LangGraph
runs each node in a task with a copy of the caller's context, so a context variable set inside
one node would not reach the next (research R10).
"""

import contextlib
import dataclasses
import logging
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from invio.config.settings import get_settings
from invio.db.repositories import JobRepository, RunRepository
from invio.db.session import session_scope
from invio.domain import RunStatus
from invio.graph.build import build_graph, new_scope
from invio.graph.nodes.persist import DigestDraft
from invio.graph.ports import RunDeps, RunObserver
from invio.graph.scope import RunScope
from invio.graph.stages import (
    record_errors,
    record_failure,
    release_lock,
    rollback_work_session,
)
from invio.graph.state import STAGE_ORDER, RunError, RunState, error_of
from invio.log import run_context
from invio.pipeline.deps import default_deps
from invio.services.jobs import JobNotFoundError

__all__ = ["JobBusyError", "JobDisabledError", "RunResult", "run_job", "run_job_by_name"]

logger = logging.getLogger("invio.pipeline")


class JobBusyError(LookupError):
    """Another run holds an unexpired lock on the job."""

    def __init__(self, job_id: int, locked_until: datetime | None) -> None:
        super().__init__(f"job {job_id} is locked by another run until {locked_until}")
        self.job_id = job_id
        self.locked_until = locked_until


class JobDisabledError(ValueError):
    """The job is disabled and cannot be run."""

    def __init__(self, job_id: int) -> None:
        super().__init__(f"job {job_id} is disabled")
        self.job_id = job_id


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult:
    """The outcome of one :func:`run_job` call.

    ``status`` is final (after the synthesis and delivery rules) and equals ``runs.status``.
    ``digest`` is the only copy of the digest in a dry run. ``errors`` lists every recorded
    :class:`RunError`, sorted by stage order, then item id.
    """

    job_id: int
    run_id: int
    status: RunStatus
    dry_run: bool
    digest: DigestDraft | None
    stats: Mapping[str, Any]
    errors: tuple[RunError, ...]
    notifications_sent: int
    notifications_failed: int
    error: str | None  # runs.error: the sanitized reason of a failed run
    started_at: datetime
    finished_at: datetime | None


def _sorted_errors(errors: list[RunError]) -> tuple[RunError, ...]:
    return tuple(
        sorted(
            errors,
            key=lambda e: (STAGE_ORDER.index(e.stage), e.item_id if e.item_id is not None else -1),
        )
    )


async def run_job(
    job_id: int,
    *,
    dry_run: bool = False,
    deps: RunDeps | None = None,
    concurrency: int | None = None,
    max_items: int | None = None,
    observer: RunObserver | None = None,
) -> RunResult:
    """Run job ``job_id`` once and return the result.

    ``deps`` defaults to the production wiring (:func:`~invio.pipeline.deps.default_deps`);
    ``concurrency`` overrides ``deps.concurrency``. A dry run produces the digest in the result
    only: nothing is sent, ``next_run_at`` stays as it is, and only the run row remains.

    ``max_items`` (>= 1) lowers this run's item cap to ``min(max_items, limits.max_items_per_run)``
    in memory; the stored config is never written. ``observer`` receives progress events; its
    failures never fail the run.
    """
    if concurrency is not None and concurrency < 1:
        raise ValueError(f"concurrency must be >= 1, got {concurrency}")
    if max_items is not None and max_items < 1:
        raise ValueError(f"max_items must be >= 1, got {max_items}")
    async with _deps_or_default(deps) as resolved:
        if concurrency is not None:
            resolved = dataclasses.replace(resolved, concurrency=concurrency)
        return await _run(
            job_id, dry_run=dry_run, deps=resolved, max_items=max_items, observer=observer
        )


async def run_job_by_name(
    name: str,
    *,
    dry_run: bool = False,
    deps: RunDeps | None = None,
    concurrency: int | None = None,
    max_items: int | None = None,
    observer: RunObserver | None = None,
) -> RunResult:
    """Resolve ``name`` to the job id, then :func:`run_job`.

    An unknown name raises :class:`~invio.services.jobs.JobNotFoundError` before any run row is
    created or any lock changed. The CLI uses this because it must not import ``invio.db``.
    """
    async with _deps_or_default(deps) as resolved:
        with session_scope(resolved.session_factory) as session:
            job = JobRepository(session).get_by_name(name)
            if job is None:
                raise JobNotFoundError(name)
            job_id = job.id
        return await run_job(
            job_id,
            dry_run=dry_run,
            deps=resolved,
            concurrency=concurrency,
            max_items=max_items,
            observer=observer,
        )


@contextlib.asynccontextmanager
async def _deps_or_default(deps: RunDeps | None) -> AsyncIterator[RunDeps]:
    """``deps`` itself, or the production wiring for the duration of the block."""
    if deps is not None:
        yield deps
        return
    async with default_deps(get_settings()) as built:
        yield built


async def _run(
    job_id: int,
    *,
    dry_run: bool,
    deps: RunDeps,
    max_items: int | None,
    observer: RunObserver | None,
) -> RunResult:
    now = deps.clock()
    token = now + deps.lock_ttl
    with session_scope(deps.session_factory) as session:
        jobs = JobRepository(session)
        job = jobs.get(job_id)
        if job is None:
            raise JobNotFoundError(str(job_id))
        if not job.enabled:
            raise JobDisabledError(job_id)
        name = job.name
        if not jobs.claim(job_id, now=now, until=token):
            session.refresh(job)
            raise JobBusyError(job_id, job.locked_until)
    try:
        with session_scope(deps.session_factory) as session:
            run_id = RunRepository(session).start(job_id, started_at=now).id
    except BaseException:
        _release_quietly(deps, job_id, token)
        raise
    scope = new_scope(
        deps,
        job_id=job_id,
        run_id=run_id,
        token=token,
        dry_run=dry_run,
        max_items=max_items,
        observer=observer,
    )
    scope.job_name = name
    with run_context(job=name, run_id=str(run_id)):
        logger.info("run.started", extra={"dry_run": dry_run, "job_id": job_id})
        try:
            graph = build_graph(deps, scope)
            final = cast(RunState, await graph.ainvoke({"items": [], "errors": []}))
        except BaseException as err:
            # A graph that cannot be built, a cancellation, an interrupt or an error inside
            # ``finalize`` itself: the graph did not close the run, so do it here, inside the
            # run context (research R8).
            if not scope.finalized:
                _safety_net(deps, scope, err)
            raise
    with session_scope(deps.session_factory) as session:
        run = RunRepository(session).get(run_id)
        if run is None:
            raise RuntimeError(f"run {run_id} vanished before its result was read")
        status, stats = RunStatus(run.status), dict(run.stats or {})
        error, started_at, finished_at = run.error, run.started_at, run.finished_at
    delivery = scope.delivery
    return RunResult(
        job_id=job_id,
        run_id=run_id,
        status=status,
        dry_run=dry_run,
        digest=final.get("digest"),
        stats=stats,
        errors=_sorted_errors(final.get("errors", [])),
        notifications_sent=delivery.sent if delivery is not None else 0,
        notifications_failed=delivery.failed if delivery is not None else 0,
        error=error,
        started_at=started_at,
        finished_at=finished_at,
    )


def _safety_net(deps: RunDeps, scope: RunScope, error: BaseException) -> None:
    """Record the run failed and release the lock; never raises (the caller re-raises ``error``).

    A failing recovery transaction leaves the run ``running`` (``record_failed_run`` logs it);
    the lock is still released in a separate attempt. Unless ``finalize`` already stored the
    run's error list, the fatal error is stored as its only entry.
    """
    rollback_work_session(scope)  # first: a failing recovery must not leave a transaction open
    with contextlib.suppress(Exception):  # logged by record_failed_run (error class only)
        record_failure(deps, scope, error)
    if not scope.errors_recorded:
        # The graph state is gone: store the fatal error at least, so ``run show`` names it.
        record_errors(deps, scope, [error_of(scope.stage, error)], [], error)
    try:
        release_lock(deps, scope)
    except Exception as release_error:
        logger.error(
            "run.release_failed",
            extra={"error": type(release_error).__name__, "db_run_id": scope.run_id},
        )
    scope.finalized = True


def _release_quietly(deps: RunDeps, job_id: int, token: datetime) -> None:
    """Release the claimed lock without touching ``next_run_at``; never masks the caller's error."""
    try:
        with session_scope(deps.session_factory) as session:
            JobRepository(session).release(job_id, until=token)
    except Exception as err:
        logger.error("run.release_failed", extra={"error": type(err).__name__, "job_id": job_id})
