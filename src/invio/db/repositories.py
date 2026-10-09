"""Thin, flush-only access per table; callers own the unit of work.

Repositories take a :class:`~sqlalchemy.orm.Session`, flush their writes so ids and constraint
errors surface immediately, and never commit or roll back (see ``session_scope``).
"""

import builtins
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import Any, Final

from sqlalchemy import (
    ColumnElement,
    Date,
    Select,
    SQLColumnExpression,
    and_,
    case,
    exists,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from invio.db.models import Digest, Item, Job, LlmUsage, Notification, Run
from invio.db.types import utcnow
from invio.domain import (
    COST_PRECISION,
    STATS_VERSION,
    Candidate,
    ItemStatus,
    NotificationStatus,
    RunStatus,
)

__all__ = [
    "KEEP",
    "DigestRepository",
    "DueJob",
    "ItemRepository",
    "JobRepository",
    "NotificationRepository",
    "RunRepository",
    "UsageBucket",
    "UsageRepository",
    "UsageTotals",
]


class _Keep(Enum):
    KEEP = "keep"


# Rows ``RunRepository.failure_streak`` looks at; far more than any streak cap needs.
_STREAK_SCAN_LIMIT: Final = 50

# Sentinel for ``JobRepository.release``: leave ``next_run_at`` as it is (``None`` clears it).
KEEP: Final = _Keep.KEEP


@dataclass(frozen=True, slots=True)
class DueJob:
    """A job selected by :meth:`JobRepository.list_due`; just what ``run-due`` needs."""

    id: int
    name: str
    next_run_at: datetime
    locked_until: datetime | None


def _all[T](session: Session, stmt: Select[T]) -> builtins.list[T]:
    return builtins.list(session.scalars(stmt).all())


class JobRepository:
    """Access to the ``jobs`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_name(self, name: str) -> Job | None:
        """Return the job called ``name`` or ``None``."""
        return self._session.scalars(select(Job).where(Job.name == name)).one_or_none()

    def get(self, job_id: int) -> Job | None:
        """Return the job with ``job_id`` or ``None``."""
        return self._session.get(Job, job_id)

    def claim(
        self, job_id: int, *, now: datetime, until: datetime, due_by: datetime | None = None
    ) -> bool:
        """Take the run lock: set ``locked_until = until`` if it is free or expired (``<= now``).

        One conditional UPDATE (the SQL of #23 step 2), so of several concurrent callers exactly
        one gets ``True``. With ``due_by`` the same UPDATE also requires the job to be enabled
        and ``next_run_at <= due_by``: a job finished by another run since it was selected is not
        claimed. Does not commit; the caller owns the transaction. The ``until`` value doubles as
        the ownership token for :meth:`release`.
        """
        conditions = [Job.id == job_id, or_(Job.locked_until.is_(None), Job.locked_until <= now)]
        if due_by is not None:
            conditions += [
                Job.enabled.is_(True),
                Job.next_run_at.is_not(None),
                Job.next_run_at <= due_by,
            ]
        result = self._session.execute(
            update(Job)
            .where(*conditions)
            .values(locked_until=until)
            .execution_options(synchronize_session=False)
        )
        # Matched rows (not changed rows): MariaDB reports FOUND_ROWS for these drivers.
        return result.rowcount == 1  # type: ignore[attr-defined, no-any-return]

    def release(
        self, job_id: int, *, until: datetime, next_run_at: datetime | _Keep | None = KEEP
    ) -> bool:
        """Clear the lock if it still equals ``until`` (this run's token), in one UPDATE.

        ``next_run_at`` is written together with the release unless it is :data:`KEEP`; ``None``
        is a legal value that clears it. Returns ``False`` and changes nothing when the lock
        belongs to someone else now. Does not commit.
        """
        values: dict[str, Any] = {"locked_until": None}
        if next_run_at is not KEEP:
            values["next_run_at"] = next_run_at
        result = self._session.execute(
            update(Job)
            .where(Job.id == job_id, Job.locked_until == until)
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        return result.rowcount == 1  # type: ignore[attr-defined, no-any-return]

    def list(self, *, enabled_only: bool = False) -> builtins.list[Job]:
        """Return jobs ordered by name, optionally only the enabled ones."""
        stmt = select(Job).order_by(Job.name)
        if enabled_only:
            stmt = stmt.where(Job.enabled.is_(True))
        return _all(self._session, stmt)

    def list_due(self, now: datetime) -> builtins.list[DueJob]:
        """Return the enabled jobs with ``next_run_at <= now``, oldest first (then by id).

        Read-only, one SELECT over ``ix_jobs_enabled_next_run_at``. Locked jobs are included
        (``locked_until`` tells the caller). There is no limit: ``run-due`` sets the busy jobs
        aside first, so they never use up ``--limit``.
        """
        stmt = (
            select(Job.id, Job.name, Job.next_run_at, Job.locked_until)
            .where(Job.enabled.is_(True), Job.next_run_at.is_not(None), Job.next_run_at <= now)
            .order_by(Job.next_run_at, Job.id)
        )
        due: builtins.list[DueJob] = []
        for job_id, name, next_run_at, locked_until in self._session.execute(stmt):
            if next_run_at is not None:  # always true: the WHERE clause excludes NULL
                due.append(DueJob(job_id, name, next_run_at, locked_until))
        return due

    def add(self, job: Job) -> Job:
        """Insert ``job`` and flush; a duplicate name raises ``IntegrityError``."""
        self._session.add(job)
        self._session.flush()
        return job

    def delete(self, job: Job) -> None:
        """Delete ``job``; the database cascades to its history."""
        self._session.delete(job)
        self._session.flush()


class RunRepository:
    """Access to the ``runs`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def start(self, job_id: int, *, started_at: datetime | None = None) -> Run:
        """Create a run in status ``running``."""
        run = Run(job_id=job_id, status=RunStatus.RUNNING, started_at=started_at or utcnow())
        self._session.add(run)
        self._session.flush()
        return run

    def finish(
        self,
        run: Run,
        status: RunStatus,
        *,
        stats: dict[str, Any] | None = None,
        error: str | None = None,
        finished_at: datetime | None = None,
    ) -> Run:
        """Set the final status, statistics, error and finish time (default now)."""
        run.status = status
        run.stats = stats
        run.error = error
        run.finished_at = finished_at or utcnow()
        self._session.flush()
        return run

    def get(self, run_id: int) -> Run | None:
        """Return the run with ``run_id`` or ``None``."""
        return self._session.get(Run, run_id)

    def list_for_job(self, job_id: int, *, limit: int | None = None) -> builtins.list[Run]:
        """Return the job's runs, newest first."""
        stmt = (
            select(Run).where(Run.job_id == job_id).order_by(Run.started_at.desc(), Run.id.desc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        return _all(self._session, stmt)

    def failure_streak(self, job_id: int, *, exclude_run_id: int, cap: int) -> int:
        """Count the job's consecutive ``failed`` runs, newest first, up to ``cap``.

        Skips ``exclude_run_id``, runs still ``running`` (in SQL, so they never use up the scan
        limit) and dry runs (``stats["dry_run"]``, in Python: JSON). The count stops at the first
        ``succeeded`` or ``partial`` run. Reads at most ``_STREAK_SCAN_LIMIT`` finished rows, so
        a history of nothing but dry runs can under-count. Read-only.
        """
        rows = self._session.execute(
            select(Run.status, Run.stats)
            .where(
                Run.job_id == job_id,
                Run.id != exclude_run_id,
                Run.status != RunStatus.RUNNING,
            )
            .order_by(Run.started_at.desc(), Run.id.desc())
            .limit(_STREAK_SCAN_LIMIT)
        )
        streak = 0
        for status, stats in rows:
            if (stats or {}).get("dry_run") is True:
                continue
            if status == RunStatus.FAILED:
                streak += 1
                if streak >= cap:
                    break
            elif status in (RunStatus.SUCCEEDED, RunStatus.PARTIAL):
                break
        return streak

    def record_errors(
        self, run_id: int, entries: Sequence[Mapping[str, Any]], *, omitted: int = 0
    ) -> bool:
        """Merge the run's error list into ``stats`` (and ``errors_omitted`` when ``omitted > 0``).

        Returns ``False`` and changes nothing for an unknown run and for a run still
        ``running`` (its final stats are not written yet). ``status``, ``error`` and
        ``finished_at`` are never touched. ``stats`` is a plain JSON column: a new dict is
        assigned so the change is detected. Flushes only.
        """
        run = self._session.get(Run, run_id)
        if run is None or run.status == RunStatus.RUNNING:
            return False
        merged: dict[str, Any] = {
            **(run.stats or {"version": STATS_VERSION}),
            "errors": list(entries),
        }
        if omitted > 0:
            merged["errors_omitted"] = omitted
        run.stats = merged
        self._session.flush()
        return True

    def list_recent(
        self, *, job_id: int | None = None, limit: int = 20
    ) -> builtins.list[tuple[Run, str, str | None]]:
        """Return ``(run, job name, schedule timezone)`` newest first (``started_at DESC, id
        DESC``).

        One query that reads only ``config.schedule.timezone`` of the job config (unvalidated,
        ``None`` if absent); ``job_id`` restricts it to one job.
        """
        zone = Job.config["schedule"]["timezone"].as_string()
        stmt = (
            select(Run, Job.name, zone)
            .join(Job, Job.id == Run.job_id)
            .order_by(Run.started_at.desc(), Run.id.desc())
            .limit(limit)
        )
        if job_id is not None:
            stmt = stmt.where(Run.job_id == job_id)
        return [(run, name, tz) for run, name, tz in self._session.execute(stmt)]

    def has_successful_run(self, job_id: int) -> bool:
        """Whether the job has any run with status ``succeeded`` or ``partial``."""
        stmt = select(
            exists().where(
                Run.job_id == job_id,
                Run.status.in_((RunStatus.SUCCEEDED, RunStatus.PARTIAL)),
            )
        )
        return bool(self._session.scalar(stmt))

    def latest_status_by_job(self) -> dict[int, RunStatus]:
        """Return ``job_id -> status`` of each job's newest run (one query).

        "Newest" is by ``started_at`` descending, ties broken by the higher id; jobs without
        runs are absent.
        """
        rank = (
            func.row_number()
            .over(partition_by=Run.job_id, order_by=(Run.started_at.desc(), Run.id.desc()))
            .label("rn")
        )
        ranked = select(Run.job_id, Run.status, rank).subquery()
        stmt = select(ranked.c.job_id, ranked.c.status).where(ranked.c.rn == 1)
        return {job_id: RunStatus(status) for job_id, status in self._session.execute(stmt)}


_FIND_MANY_CHUNK = 500


class ItemRepository:
    """Access to the ``items`` table; ``add`` is idempotent per ``(job_id, url_hash)``."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def _find(self, job_id: int, url_hash: str) -> Item | None:
        stmt = select(Item).where(Item.job_id == job_id, Item.url_hash == url_hash)
        return self._session.scalars(stmt).one_or_none()

    def add(
        self, job_id: int, candidate: Candidate, *, run_id: int | None = None
    ) -> tuple[Item, bool]:
        """Store ``candidate`` unless the job already has it; return ``(item, created)``.

        A concurrent insert of the same item is detected through the unique constraint: the
        insert runs in a savepoint, so the session stays usable and the existing row is
        returned. Other integrity errors (for example an unknown ``job_id``) are re-raised.
        This relies on the engine rules of ``create_db_engine``: a real transaction around the
        savepoint (SQLite) and ``READ COMMITTED`` isolation (MariaDB).
        """
        existing = self._find(job_id, candidate.url_hash)
        if existing is not None:
            return existing, False
        item = Item(**_item_values(job_id, candidate, run_id=run_id))
        try:
            with self._session.begin_nested():
                self._session.add(item)
                self._session.flush()
        except IntegrityError:
            # Only a duplicate means "already stored". The concurrent winner's row is visible
            # because the engine runs READ COMMITTED on MariaDB (see ``create_db_engine``).
            winner = self._find(job_id, candidate.url_hash)
            if winner is None:
                raise
            return winner, False
        return item, True

    def add_new(
        self, job_id: int, candidates: Sequence[Candidate]
    ) -> builtins.list[tuple[Item, bool]]:
        """Insert candidates the caller found absent (``find_many``) in one bulk statement.

        Returns ``(item, created)`` per candidate in input order; ids follow the input order.
        If another writer inserted one of them meanwhile, the batch savepoint is rolled back
        and each candidate goes through :meth:`add`, which reports the concurrent rows as
        ``created=False``.
        """
        if not candidates:
            return []
        try:
            with self._session.begin_nested():
                self._session.execute(
                    insert(Item), [_item_values(job_id, candidate) for candidate in candidates]
                )
        except IntegrityError:
            return [self.add(job_id, candidate) for candidate in candidates]
        stored = self.find_many(job_id, [candidate.url_hash for candidate in candidates])
        return [(stored[candidate.url_hash], True) for candidate in candidates]

    def get(self, item_id: int) -> Item | None:
        """Return the item with ``item_id`` or ``None``."""
        return self._session.get(Item, item_id)

    def seen(self, job_id: int, url_hash: str) -> bool:
        """Return whether the job already stores an item with ``url_hash``."""
        return self._find(job_id, url_hash) is not None

    def set_status(self, item: Item, status: ItemStatus) -> None:
        """Set the item's processing status and flush."""
        item.status = status
        self._session.flush()

    def set_relevance(self, item: Item, relevance: Decimal, status: ItemStatus) -> None:
        """Store relevance and status, clear last_error, flush."""
        item.relevance = relevance
        item.status = status
        item.last_error = None
        self._session.flush()

    def set_summary(self, item: Item, summary: str) -> None:
        """Store the summary JSON, set status SUMMARIZED, clear last_error, flush."""
        item.summary = summary
        item.status = ItemStatus.SUMMARIZED
        item.last_error = None
        self._session.flush()

    def set_extracted(self, item: Item, text: str) -> None:
        """Store the extracted page text, set status EXTRACTED, flush."""
        item.raw_content = text
        item.status = ItemStatus.EXTRACTED
        self._session.flush()

    def mark_failed(self, item: Item, error: str) -> None:
        """Set status FAILED and last_error, flush (relevance unchanged)."""
        item.status = ItemStatus.FAILED
        item.last_error = error
        self._session.flush()

    def list_for_job(self, job_id: int, *, status: ItemStatus | None = None) -> builtins.list[Item]:
        """Return the job's items by id, optionally filtered by status."""
        stmt = select(Item).where(Item.job_id == job_id).order_by(Item.id)
        if status is not None:
            stmt = stmt.where(Item.status == status)
        return _all(self._session, stmt)

    def find_many(self, job_id: int, url_hashes: Collection[str]) -> dict[str, Item]:
        """Return the job's stored items for ``url_hashes``, keyed by url_hash (batched IN)."""
        wanted = builtins.list(url_hashes)
        found: dict[str, Item] = {}
        for start in range(0, len(wanted), _FIND_MANY_CHUNK):
            chunk = wanted[start : start + _FIND_MANY_CHUNK]
            stmt = select(Item).where(Item.job_id == job_id, Item.url_hash.in_(chunk))
            for item in self._session.scalars(stmt):
                found[item.url_hash] = item
        return found

    def list_pending(
        self, job_id: int, *, run_id: int, max_attempts: int, limit: int
    ) -> builtins.list[Item]:
        """Return up to ``limit`` of the job's pending items, newest first.

        Pending means waiting (status ``new``, 0 attempts) or retryable (``failed``, or ``new``
        after an interrupted run, with fewer than ``max_attempts`` attempts), and not taken by
        ``run_id``. Order: dated before undated, newer first, then id.
        """
        stmt = (
            select(Item)
            .where(*_pending(job_id, run_id, max_attempts))
            .order_by(Item.published_at.is_(None), Item.published_at.desc(), Item.id)
            .limit(limit)
        )
        return _all(self._session, stmt)

    def count_pending(self, job_id: int, *, run_id: int, max_attempts: int) -> tuple[int, int]:
        """Return ``(waiting, retryable)`` counts of the items :meth:`list_pending` selects from."""
        waiting = case((_waiting(), 1), else_=0)
        stmt = select(func.count(), func.coalesce(func.sum(waiting), 0)).where(
            *_pending(job_id, run_id, max_attempts)
        )
        total, waiting_count = self._session.execute(stmt).one()
        return int(waiting_count), int(total) - int(waiting_count)

    def reset_versions(self, changes: Iterable[tuple[Item, Candidate]]) -> None:
        """Store a new content version per ``(item, candidate)`` and restart processing.

        Sets url/type/title/teaser/published_at/content_hash from the candidate; status
        ``new``, attempts 0, last_error ``None``, run_id ``None``. One flush.
        """
        for item, candidate in changes:
            item.url = candidate.url
            item.type = candidate.type
            item.content_hash = candidate.content_hash
            item.title = candidate.title
            item.teaser = candidate.teaser
            item.published_at = candidate.published_at
            item.status = ItemStatus.NEW
            item.attempts = 0
            item.last_error = None
            item.run_id = None
        self._session.flush()

    def set_content_hashes(self, updates: Iterable[tuple[Item, str]]) -> None:
        """Store the content fingerprint of items that had none yet. One flush."""
        for item, content_hash in updates:
            item.content_hash = content_hash
        self._session.flush()

    def mark_taken(self, items: Iterable[Item], run_id: int) -> None:
        """Link ``items`` to ``run_id`` and increment each item's attempts by one.

        The read-modify-write increment relies on the per-job scheduler lock (#23) to keep
        concurrent runs of the same job apart.
        """
        for item in items:
            item.run_id = run_id
            item.attempts += 1
        self._session.flush()

    def release(self, items: Iterable[Item]) -> None:
        """Hand taken ``items`` back to the next run after a budget stop. One flush.

        Undoes the take: ``attempts`` goes down by one (never below 0) and ``run_id`` is cleared.
        The status becomes ``new`` so ``list_pending`` selects the item again (an item already
        rated ``relevant`` would otherwise never be picked up) and its ``relevance`` is cleared,
        since it will be rated again; an item that is still ``failed`` (a retry taken but not
        touched) stays ``failed`` (research R4).
        """
        for item in items:
            item.attempts = max(item.attempts - 1, 0)
            item.run_id = None
            if item.status != ItemStatus.FAILED:
                item.status = ItemStatus.NEW
                item.relevance = None
        self._session.flush()

    def mark_status(self, items: Iterable[Item], status: ItemStatus) -> None:
        """Set ``status`` on ``items``. One flush."""
        for item in items:
            item.status = status
        self._session.flush()


def _item_values(job_id: int, candidate: Candidate, *, run_id: int | None = None) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "run_id": run_id,
        "url": candidate.url,
        "url_hash": candidate.url_hash,
        "type": candidate.type,
        "title": candidate.title,
        "published_at": candidate.published_at,
        "teaser": candidate.teaser,
        "content_hash": candidate.content_hash,
    }


def _pending(job_id: int, run_id: int, max_attempts: int) -> tuple[ColumnElement[bool], ...]:
    """Filter of waiting or retryable items not taken by ``run_id`` (see ``list_pending``)."""
    retryable = and_(
        Item.attempts < max_attempts,
        or_(
            Item.status == ItemStatus.FAILED,
            and_(Item.status == ItemStatus.NEW, Item.attempts >= 1),
        ),
    )
    return (
        Item.job_id == job_id,
        or_(Item.run_id.is_(None), Item.run_id != run_id),
        or_(_waiting(), retryable),
    )


def _waiting() -> ColumnElement[bool]:
    return and_(Item.status == ItemStatus.NEW, Item.attempts == 0)


class DigestRepository:
    """Access to the ``digests`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        job_id: int,
        title: str,
        body: str,
        item_ids: builtins.list[int],
        *,
        run_id: int | None = None,
    ) -> Digest:
        """Store a digest."""
        digest = Digest(job_id=job_id, run_id=run_id, title=title, body=body, item_ids=item_ids)
        self._session.add(digest)
        self._session.flush()
        return digest

    def get(self, digest_id: int) -> Digest | None:
        """Return the digest with this id, or ``None``."""
        return self._session.get(Digest, digest_id)

    def get_for_run(self, run_id: int) -> Digest | None:
        """Return the digest stored by run ``run_id`` or ``None``."""
        stmt = select(Digest).where(Digest.run_id == run_id).order_by(Digest.id.desc()).limit(1)
        return self._session.scalars(stmt).first()

    def list_for_job(self, job_id: int) -> builtins.list[Digest]:
        """Return the job's digests, newest first."""
        stmt = (
            select(Digest)
            .where(Digest.job_id == job_id)
            .order_by(Digest.created_at.desc(), Digest.id.desc())
        )
        return _all(self._session, stmt)


class NotificationRepository:
    """Access to the ``notifications`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        job_id: int,
        channel: str,
        recipient: str,
        *,
        run_id: int | None = None,
        digest_id: int | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Notification:
        """Store a notification in status ``pending``."""
        notification = Notification(
            job_id=job_id,
            run_id=run_id,
            digest_id=digest_id,
            channel=channel,
            recipient=recipient,
            status=NotificationStatus.PENDING,
            payload=payload,
        )
        self._session.add(notification)
        self._session.flush()
        return notification

    def mark(
        self,
        notification: Notification,
        status: NotificationStatus,
        *,
        error: str | None = None,
        sent_at: datetime | None = None,
    ) -> Notification:
        """Set the delivery status and error (``sent`` passes none, which clears it).

        ``sent_at`` is only set for ``sent`` (default now).
        """
        notification.status = status
        notification.error = error
        if status is NotificationStatus.SENT:
            notification.sent_at = sent_at or utcnow()
        self._session.flush()
        return notification

    def begin_attempt(self, notification: Notification, *, now: datetime) -> Notification:
        """Count a send attempt and stamp its start; the status stays ``pending``."""
        notification.attempts += 1
        notification.last_attempt_at = now
        self._session.flush()
        return notification

    @staticmethod
    def _retryable(now: datetime, stale_after: timedelta) -> ColumnElement[bool]:
        """Predicate: ``failed``, or ``pending`` with its latest activity older than the cutoff."""
        last_activity = func.coalesce(Notification.last_attempt_at, Notification.created_at)
        return or_(
            Notification.status == NotificationStatus.FAILED,
            and_(
                Notification.status == NotificationStatus.PENDING, last_activity < now - stale_after
            ),
        )

    def retry_candidates(
        self, *, now: datetime, stale_after: timedelta
    ) -> builtins.list[Notification]:
        """Return ``failed`` and stale ``pending`` notifications by id (crash recovery)."""
        stmt = (
            select(Notification).where(self._retryable(now, stale_after)).order_by(Notification.id)
        )
        return _all(self._session, stmt)

    def claim_for_retry(
        self, notification_id: int, *, now: datetime, stale_after: timedelta
    ) -> bool:
        """Atomically take a retryable notification: ``pending``, one more attempt, stamped.

        A single conditional UPDATE, so of several concurrent callers exactly one gets ``True``.
        """
        result = self._session.execute(
            update(Notification)
            .where(Notification.id == notification_id, self._retryable(now, stale_after))
            .values(
                status=NotificationStatus.PENDING,
                attempts=Notification.attempts + 1,
                last_attempt_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        return result.rowcount == 1  # type: ignore[attr-defined, no-any-return]

    def give_up_stale(
        self,
        notification_id: int,
        *,
        now: datetime,
        stale_after: timedelta,
        max_attempts: int,
        error: str,
    ) -> bool:
        """Atomically mark a stale ``pending`` row that used its attempts as ``skipped``.

        A single conditional UPDATE, so of several concurrent callers exactly one gets ``True``.
        """
        cutoff = now - stale_after
        result = self._session.execute(
            update(Notification)
            .where(
                Notification.id == notification_id,
                Notification.status == NotificationStatus.PENDING,
                Notification.attempts >= max_attempts,
                func.coalesce(Notification.last_attempt_at, Notification.created_at) < cutoff,
            )
            .values(status=NotificationStatus.SKIPPED, error=error)
            .execution_options(synchronize_session=False)
        )
        self._session.flush()
        return result.rowcount == 1  # type: ignore[attr-defined, no-any-return]

    def list_for_job(
        self, job_id: int, *, status: NotificationStatus | None = None
    ) -> builtins.list[Notification]:
        """Return the job's notifications by id, optionally filtered by status."""
        stmt = select(Notification).where(Notification.job_id == job_id).order_by(Notification.id)
        if status is not None:
            stmt = stmt.where(Notification.status == status)
        return _all(self._session, stmt)

    def list_for_run(self, run_id: int) -> builtins.list[Notification]:
        """Return the run's notifications by id."""
        stmt = select(Notification).where(Notification.run_id == run_id).order_by(Notification.id)
        return _all(self._session, stmt)

    def delivery_counts(self, run_id: int) -> tuple[int, int]:
        """Return ``(sent, not sent)`` of the run's notifications.

        Every row that is not ``sent`` counts as not sent (a ``pending`` row was never
        delivered), like ``deliver_digest`` counts them.
        """
        rows = self.list_for_run(run_id)
        sent = sum(1 for row in rows if row.status == NotificationStatus.SENT)
        return sent, len(rows) - sent


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Summed LLM usage; ``cost_usd`` adds up the known costs only."""

    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageBucket:
    """Summed usage of one job, provider and model; per UTC ``day``, or ``None`` for all days."""

    job_name: str
    provider: str
    model: str
    day: date | None
    calls: int
    input_tokens: int
    output_tokens: int


class UsageRepository:
    """Access to the ``llm_usage`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        job_id: int,
        provider: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        *,
        run_id: int | None = None,
        purpose: str | None = None,
        cost_usd: Decimal | None = None,
        created_at: datetime | None = None,
    ) -> LlmUsage:
        """Store one LLM call's usage; ``created_at`` defaults to now (a replay passes its own)."""
        usage = LlmUsage(
            job_id=job_id,
            run_id=run_id,
            provider=provider,
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
            created_at=created_at or utcnow(),
        )
        self._session.add(usage)
        self._session.flush()
        return usage

    def totals_for_run(self, run_id: int) -> UsageTotals:
        """Return the summed usage of one run."""
        return self._totals(LlmUsage.run_id == run_id)

    def totals_for_job(self, job_id: int) -> UsageTotals:
        """Return the summed usage of one job, including rows without a run."""
        return self._totals(LlmUsage.job_id == job_id)

    def _totals(self, condition: Any) -> UsageTotals:
        stmt = select(
            func.coalesce(func.sum(LlmUsage.input_tokens), 0),
            func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            func.coalesce(func.sum(LlmUsage.cost_usd), 0),
        ).where(condition)
        tokens_in, tokens_out, cost = self._session.execute(stmt).one()
        # int(): MariaDB returns Decimal sums; Decimal(str()): SQLite sums Numeric as float.
        return UsageTotals(
            input_tokens=int(tokens_in),
            output_tokens=int(tokens_out),
            cost_usd=Decimal(str(cost)).quantize(COST_PRECISION),
        )

    def grouped(
        self,
        *,
        job_id: int | None = None,
        since: datetime | None = None,
        by_day: bool = False,
    ) -> builtins.list[UsageBucket]:
        """Sum usage per (job name, provider, model), and per UTC day if ``by_day``.

        ``job_id`` keeps one job's rows, ``since`` the rows created at or after it. The database
        groups in every case: ``created_at`` is stored as naive UTC, so ``DATE()`` (SQLite and
        MariaDB alike) is the UTC day. Ordered by the grouping fields.
        """
        conditions: builtins.list[ColumnElement[bool]] = []
        if job_id is not None:
            conditions.append(LlmUsage.job_id == job_id)
        if since is not None:
            conditions.append(LlmUsage.created_at >= since)
        keys: builtins.list[SQLColumnExpression[Any]] = [
            Job.name,
            LlmUsage.provider,
            LlmUsage.model,
        ]
        if by_day:
            keys.append(func.date(LlmUsage.created_at, type_=Date))
        stmt = (
            select(
                *keys,
                func.count(LlmUsage.id),
                func.coalesce(func.sum(LlmUsage.input_tokens), 0),
                func.coalesce(func.sum(LlmUsage.output_tokens), 0),
            )
            .join(Job, Job.id == LlmUsage.job_id)
            .where(*conditions)
            .group_by(*keys)
            .order_by(*keys)
        )
        # int(): MariaDB returns Decimal sums.
        return [
            UsageBucket(
                job_name=row[0],
                provider=row[1],
                model=row[2],
                day=row[3] if by_day else None,
                calls=int(row[-3]),
                input_tokens=int(row[-2]),
                output_tokens=int(row[-1]),
            )
            for row in self._session.execute(stmt)
        ]
