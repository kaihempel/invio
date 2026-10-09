"""Job management: create, look up, update, enable/disable, delete, import and export jobs.

``JobService`` is the only way the CLI, scheduler and pipeline manage jobs. Every method runs in
one unit of work (``session_scope``) and returns plain :class:`JobRecord` values; no SQLAlchemy
type or exception crosses this boundary. Configuration contents are never logged.
"""

import builtins
import contextlib
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Self

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from invio.config.job import (
    JobConfig,
    JobConfigError,
    ScheduleConfig,
    SourceConfig,
    YoutubeChannelSource,
    dump_yaml,
    load_yaml,
    parse_youtube_channel,
    parse_youtube_playlist,
    validate_job,
    write_yaml,
)
from invio.config.settings import Settings, get_settings
from invio.db.models import Job
from invio.db.repositories import JobRepository, RunRepository
from invio.db.session import checked_session_factory, session_scope
from invio.db.types import utcnow
from invio.domain import RunStatus
from invio.scheduling.next_run import compute_next_run
from invio.sources.urls import canonical_url

__all__ = [
    "MAX_NAME_LENGTH",
    "JobExistsError",
    "JobNameError",
    "JobNotFoundError",
    "JobRecord",
    "JobService",
    "JobSummary",
    "NextRun",
    "SourceExistsError",
    "StoredJobConfigError",
    "check_job_name",
]

logger = logging.getLogger(__name__)

NextRun = Callable[[ScheduleConfig, datetime], datetime]
"""``(schedule, after) -> next UTC run time``."""

MAX_NAME_LENGTH: Final = 200


@dataclass(frozen=True, slots=True, kw_only=True)
class JobRecord:
    """A stored job as seen by service callers (storage-independent, no database id)."""

    name: str
    enabled: bool
    config: JobConfig
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class JobSummary:
    """One row of the job overview; unlike ``JobRecord`` it can describe an invalid job.

    ``config`` is ``None`` exactly when the stored configuration does not validate.
    """

    name: str
    enabled: bool
    next_run_at: datetime | None
    config: JobConfig | None
    last_run_status: RunStatus | None


class JobExistsError(Exception):
    """A job with this name already exists."""

    def __init__(self, name: str) -> None:
        super().__init__(f"job '{name}' already exists")
        self.name = name

    def __reduce__(self) -> tuple[type[Self], tuple[str]]:
        return type(self), (self.name,)


class JobNotFoundError(LookupError):
    """No job with this name exists."""

    def __init__(self, name: str) -> None:
        super().__init__(f"job '{name}' not found")
        self.name = name

    def __reduce__(self) -> tuple[type[Self], tuple[str]]:
        return type(self), (self.name,)


class JobNameError(ValueError):
    """The job name breaks a naming rule."""

    def __init__(self, name: str, rule: str) -> None:
        super().__init__(f"invalid job name: {rule}")
        self.name = name
        self.rule = rule

    def __reduce__(self) -> tuple[type[Self], tuple[str, str]]:
        return type(self), (self.name, self.rule)


class SourceExistsError(Exception):
    """The job already has a source with the same locator (not a configuration error)."""

    def __init__(self, name: str, source_type: str) -> None:
        super().__init__(f"job '{name}' already contains this {source_type} source")
        self.name = name
        self.source_type = source_type

    def __reduce__(self) -> tuple[type[Self], tuple[str, str]]:
        return type(self), (self.name, self.source_type)


class StoredJobConfigError(JobConfigError):
    """The configuration stored for a job no longer validates."""

    def __init__(self, name: str, errors: builtins.list[str]) -> None:
        super().__init__(None, errors)
        self.name = name

    def __str__(self) -> str:
        return "\n".join([f"invalid stored job '{self.name}':", *(f"  {e}" for e in self.errors)])


def check_job_name(name: str) -> None:
    """Raise ``JobNameError`` if ``name`` is empty, padded with whitespace or too long."""
    if name == "":
        raise JobNameError(name, "must not be empty")
    if name != name.strip():
        raise JobNameError(name, "must not have leading or trailing whitespace")
    if len(name) > MAX_NAME_LENGTH:
        raise JobNameError(name, f"must not be longer than {MAX_NAME_LENGTH} characters")


def _validate(config: JobConfig | Mapping[str, Any]) -> JobConfig:
    # Re-validate JobConfig instances too: ``model_construct`` skips validation.
    data = config.model_dump(mode="json") if isinstance(config, JobConfig) else config
    return validate_job(data)


def _record(job: Job, config: JobConfig | None = None) -> JobRecord:
    """Build the record; pass ``config`` when it was just validated and stored."""
    if config is None:
        try:
            config = validate_job(job.config or {})
        except JobConfigError as exc:
            raise StoredJobConfigError(job.name, exc.errors) from exc
    return JobRecord(
        name=job.name,
        enabled=job.enabled,
        config=config,
        next_run_at=job.next_run_at,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _source_identity(source: SourceConfig) -> tuple[str, str]:
    """``(type, locator)`` by which two sources of a job count as the same source."""
    if isinstance(source, YoutubeChannelSource):
        kind, token = parse_youtube_channel(source.channel_id)
        return source.type, f"{kind}:{token.lower() if kind == 'handle' else token}"
    if source.type == "youtube_playlist":
        return source.type, parse_youtube_playlist(source.playlist_id)
    return source.type, canonical_url(str(source.url))


def _require(repo: JobRepository, name: str) -> Job:
    job = repo.get_by_name(name)
    if job is None:
        raise JobNotFoundError(name)
    return job


def _log_change(event: str, name: str) -> None:
    # Never log configuration contents; ``job_name`` because ``job`` is reserved by the formatter.
    logger.info("job changed", extra={"event": event, "job_name": name})


class JobService:
    """Manage jobs; see ``specs/003-gh-issue-5/contracts/python-api.md``."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        next_run: NextRun = compute_next_run,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._session_factory = session_factory
        self._next_run = next_run
        self._clock = clock

    def _next_run_due(self, cfg: JobConfig) -> datetime:
        return self._next_run(cfg.schedule, self._clock())

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "JobService":
        """Build a service on the database from ``INVIO_DATABASE_URL``.

        Raises ``MissingSettingError`` when no database URL is configured and
        ``DatabaseConfigError`` when it is unusable.
        """
        url = (settings or get_settings()).require_secret("database_url")
        return cls(checked_session_factory(url))

    # --- shared write steps ---------------------------------------------------------------

    def _insert(self, session: Session, name: str, cfg: JobConfig) -> Job:
        job = Job(
            name=name,
            enabled=True,
            config=cfg.model_dump(mode="json"),
            next_run_at=self._next_run_due(cfg),
        )
        try:
            JobRepository(session).add(job)
        except IntegrityError as exc:
            # jobs.name is the only unique constraint a job insert can violate.
            raise JobExistsError(name) from exc
        return job

    def _apply_update(self, session: Session, job: Job, cfg: JobConfig) -> None:
        job.config = cfg.model_dump(mode="json")
        if job.enabled:
            job.next_run_at = self._next_run_due(cfg)
        session.flush()
        session.refresh(job)  # pick up ``updated_at`` set by ``onupdate``

    # --- operations -----------------------------------------------------------------------

    def create(self, name: str, config: JobConfig | Mapping[str, Any]) -> JobRecord:
        """Create an enabled job (``JobNameError``, ``JobConfigError``, ``JobExistsError``)."""
        check_job_name(name)
        cfg = _validate(config)
        with session_scope(self._session_factory) as session:
            if JobRepository(session).get_by_name(name) is not None:
                raise JobExistsError(name)
            record = _record(self._insert(session, name, cfg), cfg)
        _log_change("job.created", name)
        return record

    def get_by_name(self, name: str) -> JobRecord:
        """Return the job; raises ``JobNotFoundError`` or ``StoredJobConfigError``."""
        with session_scope(self._session_factory) as session:
            return _record(_require(JobRepository(session), name))

    def list(self, *, enabled_only: bool = False) -> builtins.list[JobRecord]:
        """Return jobs ordered by name; jobs with an invalid stored config are skipped."""
        records: builtins.list[JobRecord] = []
        with session_scope(self._session_factory) as session:
            for job in JobRepository(session).list(enabled_only=enabled_only):
                try:
                    records.append(_record(job))
                except StoredJobConfigError as exc:
                    logger.warning(
                        "skipping job with invalid stored configuration",
                        extra={
                            "event": "job.invalid_config",
                            "job_name": job.name,
                            "error_count": len(exc.errors),
                        },
                    )
        return records

    def overview(self) -> builtins.list[JobSummary]:
        """Return every job ordered by name, including ones whose stored config is invalid."""
        with session_scope(self._session_factory) as session:
            statuses = RunRepository(session).latest_status_by_job()
            summaries: builtins.list[JobSummary] = []
            for job in JobRepository(session).list():
                config: JobConfig | None = None
                with contextlib.suppress(JobConfigError):
                    config = validate_job(job.config or {})
                summaries.append(
                    JobSummary(
                        name=job.name,
                        enabled=job.enabled,
                        next_run_at=job.next_run_at,
                        config=config,
                        last_run_status=statuses.get(job.id),
                    )
                )
        return summaries

    def names(self) -> builtins.list[str]:
        """Return all job names ordered by name, without validating any configuration."""
        with session_scope(self._session_factory) as session:
            return [job.name for job in JobRepository(session).list()]

    def is_enabled(self, name: str) -> bool:
        """Return whether the job is enabled (even if its stored config is invalid)."""
        with session_scope(self._session_factory) as session:
            return _require(JobRepository(session), name).enabled

    def stored_config(self, name: str) -> dict[str, Any]:
        """Return the raw stored config (possibly invalid); ``JobNotFoundError`` if absent."""
        with session_scope(self._session_factory) as session:
            return dict(_require(JobRepository(session), name).config or {})

    def update(self, name: str, config: JobConfig | Mapping[str, Any]) -> JobRecord:
        """Replace the whole configuration; the name never changes."""
        cfg = _validate(config)
        with session_scope(self._session_factory) as session:
            job = _require(JobRepository(session), name)
            self._apply_update(session, job, cfg)
            record = _record(job, cfg)
        _log_change("job.updated", name)
        return record

    def append_source(self, name: str, source: SourceConfig) -> JobRecord:
        """Append ``source`` to the job's sources and save the re-validated job.

        Raises ``JobNotFoundError``, ``StoredJobConfigError`` (the stored job is invalid),
        ``SourceExistsError`` (same type and locator already present) or ``JobConfigError``
        (the result is invalid); nothing is written in any of these cases.
        """
        with session_scope(self._session_factory) as session:
            job = _require(JobRepository(session), name)
            current = _record(job).config
            identity = _source_identity(source)
            if any(_source_identity(existing) == identity for existing in current.sources):
                raise SourceExistsError(name, source.type)
            data = current.model_dump(mode="json")
            data["sources"] = [
                *data["sources"],
                source.model_dump(mode="json", exclude_defaults=True),
            ]
            cfg = _validate(data)
            self._apply_update(session, job, cfg)
            record = _record(job, cfg)
        _log_change("job.updated", name)
        return record

    def set_enabled(self, name: str, enabled: bool) -> JobRecord:
        """Enable or disable a job; a no-op (no write, no log) when already in that state.

        A job whose stored configuration no longer validates cannot be enabled, but it can
        always be paused: the disable is committed and logged, then ``StoredJobConfigError`` is
        raised because no record can be built. Repeating the call is a no-op that raises again.
        """
        with session_scope(self._session_factory) as session:
            job = _require(JobRepository(session), name)
            if job.enabled == enabled:
                return _record(job)
            if enabled:
                cfg = _record(job).config  # refuse to enable an invalid stored config
                job.enabled = True
                job.next_run_at = self._next_run_due(cfg)
            else:
                job.enabled = False
                job.next_run_at = None
            session.flush()
            session.refresh(job)
        _log_change("job.enabled" if enabled else "job.disabled", name)
        # Built after the commit so a job with a broken stored config can still be paused;
        # StoredJobConfigError is then raised with the disable already persisted.
        return _record(job)

    def delete(self, name: str) -> None:
        """Delete the job; the database cascades to its history."""
        with session_scope(self._session_factory) as session:
            repo = JobRepository(session)
            repo.delete(_require(repo, name))
        _log_change("job.deleted", name)

    def export_yaml(self, name: str, path: str | os.PathLike[str] | None = None) -> str:
        """Return the job as YAML text; also write it atomically to ``path`` when given."""
        config = self.get_by_name(name).config
        return dump_yaml(config) if path is None else write_yaml(config, path)

    def import_yaml(
        self,
        path: str | os.PathLike[str],
        name: str | None = None,
        *,
        replace: bool = False,
    ) -> JobRecord:
        """Store a job file under ``name`` (default: the file stem).

        An existing job is only changed with ``replace=True``.
        """
        cfg = load_yaml(path)
        job_name = name if name is not None else Path(path).stem
        check_job_name(job_name)
        with session_scope(self._session_factory) as session:
            job = JobRepository(session).get_by_name(job_name)
            if job is None:
                job = self._insert(session, job_name, cfg)
            elif replace:
                self._apply_update(session, job, cfg)
            else:
                raise JobExistsError(job_name)
            record = _record(job, cfg)
        _log_change("job.imported", job_name)
        return record
