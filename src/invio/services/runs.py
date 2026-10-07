"""Run history: the read model behind ``invio run list`` and ``invio run show``.

``RunService`` returns frozen records only; no ORM object crosses this boundary. Usage, cost and
counts come from ``runs.stats`` (version 1, additive keys); a missing key reads as "unknown",
and a missing ``errors`` key as "item errors are not available" (a run from before #22).
"""

import builtins
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

from sqlalchemy.orm import Session, sessionmaker

from invio.config.job import JobConfigError, validate_job
from invio.config.settings import Settings, get_settings
from invio.db.repositories import JobRepository, RunRepository
from invio.db.session import create_db_engine, session_factory, session_scope
from invio.domain import RunStatus
from invio.services.jobs import JobNotFoundError

__all__ = ["RunDetail", "RunErrorView", "RunNotFoundError", "RunService", "RunSummary"]

UNREADABLE_ENTRY = "<unreadable error entry>"


class RunNotFoundError(LookupError):
    """No run with this id exists."""

    def __init__(self, run_id: int) -> None:
        super().__init__(f"run {run_id} not found")
        self.run_id = run_id

    def __reduce__(self) -> tuple[type[Self], tuple[int]]:
        return type(self), (self.run_id,)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunSummary:
    """One row of ``run list``. ``timezone`` is the job's schedule zone (``UTC`` if unreadable)."""

    id: int
    job_name: str
    status: RunStatus
    dry_run: bool
    started_at: datetime
    finished_at: datetime | None
    found: int | None
    new: int | None
    relevant: int | None
    timezone: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunErrorView:
    """One stored error of a run."""

    stage: str
    error_class: str
    message: str
    item_id: int | None = None
    title: str | None = None
    url: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class RunDetail(RunSummary):
    """A run with everything ``run show`` prints; ``errors`` is ``None`` for a legacy run."""

    error: str | None
    stats: Mapping[str, Any]
    errors: tuple[RunErrorView, ...] | None
    errors_omitted: int


def _timezone(config: Mapping[str, Any] | None) -> str:
    try:
        return validate_job(dict(config or {})).schedule.timezone
    except JobConfigError:
        return "UTC"


def _count(stats: Mapping[str, Any], key: str) -> int | None:
    value = stats.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _parse_entry(raw: object) -> RunErrorView:
    if isinstance(raw, dict):
        stage, error_class, message = (
            _text(raw.get(k)) for k in ("stage", "error_class", "message")
        )
        if stage and error_class and message is not None:
            return RunErrorView(
                stage=stage,
                error_class=error_class,
                message=message,
                item_id=_count(raw, "item_id"),
                title=_text(raw.get("title")),
                url=_text(raw.get("url")),
                source=_text(raw.get("source")),
            )
    return RunErrorView(stage="unknown", error_class="unknown", message=UNREADABLE_ENTRY)


def _parse_errors(stats: Mapping[str, Any]) -> tuple[RunErrorView, ...] | None:
    raw = stats.get("errors")
    if not isinstance(raw, list):
        return None
    return tuple(_parse_entry(entry) for entry in raw)


class RunService:
    """Read runs; see ``specs/015-gh-issue-22/contracts/pipeline-and-stats-delta.md``."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "RunService":
        """Build a service on the database from ``INVIO_DATABASE_URL`` (``MissingSettingError``)."""
        url = (settings or get_settings()).require_secret("database_url")
        return cls(session_factory(create_db_engine(url)))

    def list(self, *, job: str | None = None, limit: int = 20) -> builtins.list[RunSummary]:
        """Return runs newest first (``started_at DESC, id DESC``), optionally of one job.

        An unknown ``job`` raises ``JobNotFoundError``.
        """
        with session_scope(self._session_factory) as session:
            job_id: int | None = None
            if job is not None:
                row = JobRepository(session).get_by_name(job)
                if row is None:
                    raise JobNotFoundError(job)
                job_id = row.id
            zones: dict[str, str] = {}
            summaries: builtins.list[RunSummary] = []
            for run, name, config in RunRepository(session).list_recent(job_id=job_id, limit=limit):
                if name not in zones:
                    zones[name] = _timezone(config)
                stats = run.stats or {}
                summaries.append(
                    RunSummary(
                        id=run.id,
                        job_name=name,
                        status=RunStatus(run.status),
                        dry_run=stats.get("dry_run") is True,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                        found=_count(stats, "found"),
                        new=_count(stats, "new"),
                        relevant=_count(stats, "relevant"),
                        timezone=zones[name],
                    )
                )
            return summaries

    def get(self, run_id: int) -> RunDetail:
        """Return one run with its stats and stored errors; ``RunNotFoundError`` if absent."""
        with session_scope(self._session_factory) as session:
            run = RunRepository(session).get(run_id)
            if run is None:
                raise RunNotFoundError(run_id)
            job = JobRepository(session).get(run.job_id)
            name = job.name if job is not None else ""
            stats = dict(run.stats or {})
            return RunDetail(
                id=run.id,
                job_name=name,
                status=RunStatus(run.status),
                dry_run=stats.get("dry_run") is True,
                started_at=run.started_at,
                finished_at=run.finished_at,
                found=_count(stats, "found"),
                new=_count(stats, "new"),
                relevant=_count(stats, "relevant"),
                timezone=_timezone(job.config if job is not None else None),
                error=run.error,
                stats=stats,
                errors=_parse_errors(stats),
                errors_omitted=_count(stats, "errors_omitted") or 0,
            )
