"""Thin, flush-only access per table; callers own the unit of work.

Repositories take a :class:`~sqlalchemy.orm.Session`, flush their writes so ids and constraint
errors surface immediately, and never commit or roll back (see ``session_scope``).
"""

import builtins
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from invio.db.models import Digest, Item, Job, LlmUsage, Notification, Run
from invio.db.types import utcnow
from invio.domain import Candidate, ItemStatus, NotificationStatus, RunStatus

__all__ = [
    "DigestRepository",
    "ItemRepository",
    "JobRepository",
    "NotificationRepository",
    "RunRepository",
    "UsageRepository",
    "UsageTotals",
]


def _all[T](session: Session, stmt: Select[T]) -> builtins.list[T]:
    return builtins.list(session.scalars(stmt).all())


class JobRepository:
    """Access to the ``jobs`` table."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get_by_name(self, name: str) -> Job | None:
        """Return the job called ``name`` or ``None``."""
        return self._session.scalars(select(Job).where(Job.name == name)).one_or_none()

    def list(self, *, enabled_only: bool = False) -> builtins.list[Job]:
        """Return jobs ordered by name, optionally only the enabled ones."""
        stmt = select(Job).order_by(Job.name)
        if enabled_only:
            stmt = stmt.where(Job.enabled.is_(True))
        return _all(self._session, stmt)

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
        item = Item(
            job_id=job_id,
            run_id=run_id,
            url=candidate.url,
            url_hash=candidate.url_hash,
            type=candidate.type,
            title=candidate.title,
            published_at=candidate.published_at,
            teaser=candidate.teaser,
            content_hash=candidate.content_hash,
        )
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
        """Set the delivery status; ``sent_at`` is only set for ``sent`` (default now)."""
        notification.status = status
        notification.error = error
        if status is NotificationStatus.SENT:
            notification.sent_at = sent_at or utcnow()
        self._session.flush()
        return notification

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


@dataclass(frozen=True, slots=True)
class UsageTotals:
    """Summed LLM usage; ``cost_usd`` adds up the known costs only."""

    input_tokens: int
    output_tokens: int
    cost_usd: Decimal


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
    ) -> LlmUsage:
        """Store one LLM call's usage."""
        usage = LlmUsage(
            job_id=job_id,
            run_id=run_id,
            provider=provider,
            model=model,
            purpose=purpose,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=cost_usd,
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
            cost_usd=Decimal(str(cost)).quantize(Decimal("0.000001")),
        )
