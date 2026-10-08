"""SMTP delivery of digests and retry of failed notifications."""

import asyncio
import contextlib
import logging
import ssl
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from typing import Any, Final, Self
from zoneinfo import ZoneInfo

import aiosmtplib
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from invio.config.job import JobConfig
from invio.config.settings import Settings
from invio.db.models import Job, Notification
from invio.db.repositories import DigestRepository, NotificationRepository, RunRepository
from invio.db.session import (
    check_database_url,
    create_db_engine,
    scrub_database_url,
    session_scope,
)
from invio.db.session import session_factory as build_session_factory
from invio.db.types import utcnow
from invio.domain import NotificationStatus, RunStatus
from invio.log import run_context, run_id_var
from invio.notify.archive import archive_digest, archive_url
from invio.notify.payload import DigestStats, NotificationPayload
from invio.notify.render import build_message, render_mail, render_subject

logger = logging.getLogger(__name__)

MAX_ATTEMPTS: Final = 5
STALE_PENDING_AFTER: Final = timedelta(hours=1)
CHANNEL_EMAIL: Final = "email"

# Error texts are bounded so they fit the ``Text`` column and stay readable in logs.
_MAX_ERROR_CHARS: Final = 2000
_TRUNCATED: Final = "… [truncated]"


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    """Result of :func:`deliver_digest`; ``failed`` counts rows that ended failed or skipped.

    ``error`` is only set when delivery failed before any row could be created.
    """

    sent: int
    failed: int
    skipped_empty: bool
    notification_ids: tuple[int, ...]
    error: str | None = None


class SmtpMailer:
    """Sends prepared messages over one reusable SMTP connection (aiosmtplib)."""

    def __init__(self, settings: Settings, *, tls_context: ssl.SSLContext | None = None) -> None:
        self._settings = settings
        self._tls_context = tls_context
        self._client: aiosmtplib.SMTP | None = None

    def _new_client(self) -> aiosmtplib.SMTP:
        settings = self._settings
        return aiosmtplib.SMTP(
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            use_tls=settings.smtp_security == "ssl",
            start_tls=settings.smtp_security == "starttls",
            timeout=settings.smtp_timeout_seconds,
            tls_context=self._tls_context,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        client, self._client = self._client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.quit()
            client.close()

    async def _ensure_connected(self) -> aiosmtplib.SMTP:
        """Connect (and log in when credentials are set) unless already connected."""
        if self._client is None:
            self._client = self._new_client()
        client = self._client
        if client.is_connected:
            return client
        await client.connect()
        user, password = self._settings.smtp_user, self._settings.smtp_password
        if user and password:
            if self._settings.smtp_security == "none":
                logger.warning("smtp login without TLS: credentials are sent in cleartext")
            try:
                await client.login(user, password.get_secret_value())
            except BaseException:
                client.close()  # connected but unauthenticated: reconnect next time
                raise
        return client

    async def send(self, message: EmailMessage) -> None:
        """Send ``message``; raises SMTP/OS errors and reconnects on the next call."""
        client = await self._ensure_connected()
        try:
            await client.send_message(message)
        except BaseException:
            # The session state after a failure is unknown (open transaction, dead socket).
            client.close()
            raise


def scrub_secret(text: str, settings: Settings) -> str:
    """Mask the SMTP password and user in ``text`` and cut it to 2,000 characters."""
    secrets = [settings.smtp_user]
    if settings.smtp_password is not None:
        secrets.append(settings.smtp_password.get_secret_value())
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    if len(text) > _MAX_ERROR_CHARS:
        text = text[:_MAX_ERROR_CHARS] + _TRUNCATED
    return text


def _error_text(exc: BaseException, settings: Settings) -> str:
    return scrub_error(f"{type(exc).__name__}: {exc}", settings)


def missing_smtp_settings(settings: Settings) -> list[str]:
    """Return the env variables that block delivery (empty when delivery is possible)."""
    missing: list[str] = []
    if not (settings.smtp_host or "").strip():
        missing.append("INVIO_SMTP_HOST")
    if not (settings.smtp_from or "").strip():
        missing.append("INVIO_SMTP_FROM")
    return missing


def run_status_after_delivery(planned: RunStatus, outcome: DeliveryOutcome) -> RunStatus:
    """Return ``PARTIAL`` for a planned ``SUCCEEDED`` run whose delivery had any problem."""
    if planned is RunStatus.SUCCEEDED and (outcome.failed > 0 or outcome.error is not None):
        return RunStatus.PARTIAL
    return planned


def _digest_date(started_at: datetime, timezone: str) -> date:
    return started_at.astimezone(ZoneInfo(timezone)).date()


def _items_found(stats: dict[str, Any] | None) -> int | None:
    # ``runs.stats["found"]`` is what the pipeline writes (runs.stats layout, #19).
    value = (stats or {}).get("found")
    return value if type(value) is int and value >= 0 else None


def _log_extra(row: Notification) -> dict[str, Any]:
    return {"notification_id": row.id, "recipient": row.recipient, "digest_id": row.digest_id}


class _NotSendable(Exception):
    """A stored notification cannot be rebuilt; the message is recorded verbatim."""


async def _attempt(
    session_factory: sessionmaker[Session],
    mailer: SmtpMailer,
    notification_id: int,
    *,
    build: Callable[[Notification], EmailMessage],
    settings: Settings,
    clock: Callable[[], datetime],
    count_attempt: bool,
) -> tuple[NotificationStatus, str | None]:
    """Send one notification and record the result; never raises for a failed send.

    Returns the final status and the recorded error.

    ``count_attempt`` is False when the caller already counted it (a retry claim). Every state
    change is committed on its own, so no transaction is open while the mailer is busy.
    """
    with session_scope(session_factory) as session:
        row = session.get(Notification, notification_id)
        assert row is not None
        if count_attempt:
            NotificationRepository(session).begin_attempt(row, now=clock())
    try:
        message = build(row)
        await mailer.send(message)
    except Exception as exc:
        error = (
            scrub_secret(str(exc), settings)
            if isinstance(exc, _NotSendable)
            else _error_text(exc, settings)
        )
        with session_scope(session_factory) as session:
            row = session.get(Notification, notification_id)
            assert row is not None
            status = (
                NotificationStatus.SKIPPED
                if row.attempts >= MAX_ATTEMPTS
                else NotificationStatus.FAILED
            )
            NotificationRepository(session).mark(row, status, error=error)
        logger.warning("notification failed", extra={**_log_extra(row), "error": error})
        return status, error
    sent_row = row
    try:
        with session_scope(session_factory) as session:
            current = session.get(Notification, notification_id)
            assert current is not None
            NotificationRepository(session).mark(current, NotificationStatus.SENT, sent_at=clock())
            sent_row = current
    except Exception as exc:
        # The mail went out: leave the row ``pending`` (not ``failed``) so an immediate retry
        # does not send it again; stale recovery picks it up after STALE_PENDING_AFTER.
        logger.error(
            "notification sent but not recorded",
            extra={**_log_extra(sent_row), "error": _error_text(exc, settings)},
        )
        return NotificationStatus.SENT, None
    logger.info("notification sent", extra=_log_extra(sent_row))
    return NotificationStatus.SENT, None


def _fail_pending(session_factory: sessionmaker[Session], ids: tuple[int, ...], error: str) -> None:
    """Mark every listed row that is still ``pending`` as failed with ``error``."""
    with session_scope(session_factory) as session:
        notifications = NotificationRepository(session)
        for row in session.scalars(select(Notification).where(Notification.id.in_(ids))):
            if row.status is NotificationStatus.PENDING:
                notifications.mark(row, NotificationStatus.FAILED, error=error)


def _outcome_for(session_factory: sessionmaker[Session], ids: tuple[int, ...]) -> DeliveryOutcome:
    """Count the final row states of this delivery."""
    with session_scope(session_factory) as session:
        statuses = list(
            session.scalars(select(Notification.status).where(Notification.id.in_(ids)))
        )
    return DeliveryOutcome(
        sent=statuses.count(NotificationStatus.SENT),
        failed=statuses.count(NotificationStatus.FAILED)
        + statuses.count(NotificationStatus.SKIPPED),
        skipped_empty=False,
        notification_ids=ids,
    )


def _early_failure(digest_id: int, error: str) -> DeliveryOutcome:
    logger.error("digest delivery failed", extra={"digest_id": digest_id, "error": error})
    return DeliveryOutcome(sent=0, failed=0, skipped_empty=False, notification_ids=(), error=error)


async def deliver_digest(
    session_factory: sessionmaker[Session],
    digest_id: int,
    *,
    settings: Settings,
    mailer_factory: Callable[[Settings], SmtpMailer] = SmtpMailer,
    clock: Callable[[], datetime] = utcnow,
) -> DeliveryOutcome:
    """E-mail the digest to every distinct recipient of its job; one row per recipient.

    Never raises. Rows are committed before rendering or connecting, so a later error always
    lands on rows; every attempt is committed before and after its send, so no transaction is
    open across the network. A failure before the recipients are known creates no rows and is
    reported in ``DeliveryOutcome.error`` instead (logged without traceback: it would print
    the raw exception text, which may hold credentials).
    """
    try:
        return await _deliver(session_factory, digest_id, settings, mailer_factory, clock)
    except Exception as exc:
        return _early_failure(digest_id, _error_text(exc, settings))


async def _deliver(
    session_factory: sessionmaker[Session],
    digest_id: int,
    settings: Settings,
    mailer_factory: Callable[[Settings], SmtpMailer],
    clock: Callable[[], datetime],
) -> DeliveryOutcome:
    with session_scope(session_factory) as session:
        digest = DigestRepository(session).get(digest_id)
        if digest is None:
            return _early_failure(digest_id, f"digest {digest_id} not found")
        job = session.get(Job, digest.job_id)
        assert job is not None
        cfg = JobConfig.model_validate(job.config)
        if len(digest.item_ids) == 0 and not cfg.notification.send_if_empty:
            logger.info("empty digest not sent", extra={"digest_id": digest_id})
            return DeliveryOutcome(
                sent=0, failed=0, skipped_empty=True, notification_ids=(), error=None
            )
        run = RunRepository(session).get(digest.run_id) if digest.run_id is not None else None
        started_at = run.started_at if run is not None else digest.created_at
        digest_date = _digest_date(started_at, cfg.schedule.timezone)
        duration = max(0.0, (clock() - run.started_at).total_seconds()) if run else None
        payload = NotificationPayload(
            job_name=job.name,
            subject=render_subject(cfg.notification.subject, job_name=job.name, date=digest_date),
            digest_date=digest_date,
            is_empty=len(digest.item_ids) == 0,
            stats=DigestStats(
                items_found=_items_found(run.stats if run else None),
                items_included=len(digest.item_ids),
                duration_seconds=duration,
            ),
        )
        body, job_name, run_id = digest.body, job.name, digest.run_id
        job_id, digest_row_id = job.id, digest.id
        has_items = len(digest.item_ids) > 0
        recipients = list(dict.fromkeys(str(to) for to in cfg.notification.to))
    # No transaction is open while the archive is written (file I/O runs in a worker thread).
    with run_context(job=job_name, run_id=str(run_id) if run_id is not None else run_id_var.get()):
        if cfg.archive.enabled and has_items:
            payload = await _archive(settings, payload, body, started_at)
    with session_scope(session_factory) as session:
        notifications = NotificationRepository(session)
        ids = tuple(
            notifications.add(
                job_id,
                CHANNEL_EMAIL,
                recipient,
                run_id=run_id,
                digest_id=digest_row_id,
                payload=payload.model_dump(mode="json"),
            ).id
            for recipient in recipients
        )
    link = archive_url(
        cfg.archive.base_url,
        settings.archive_dir,
        payload.archive_page,
        enabled=cfg.archive.enabled,
    )
    # From here on the rows exist: any failure is recorded on them instead of being raised.
    with run_context(job=job_name, run_id=str(run_id) if run_id is not None else run_id_var.get()):
        try:
            await _send_all(
                session_factory, ids, payload, body, settings, mailer_factory, clock, link
            )
        except Exception as exc:
            error = _error_text(exc, settings)
            logger.error("digest delivery failed", extra={"digest_id": digest_id, "error": error})
            _fail_pending(session_factory, ids, error)
    return _outcome_for(session_factory, ids)


async def _archive(
    settings: Settings, payload: NotificationPayload, body: str, started_at: datetime
) -> NotificationPayload:
    """Write the archive page; return the payload with ``archive_page`` set, or unchanged.

    Never raises: the archive is a convenience, so a failure is logged and delivery goes on.
    """
    try:
        page = await asyncio.to_thread(
            archive_digest,
            settings.archive_dir,
            run_started_at=started_at,
            payload=payload,
            digest_markdown=body,
        )
    except Exception as exc:
        logger.warning("archive.failed", extra={"error": _error_text(exc, settings)})
        return payload
    return payload.model_copy(update={"archive_page": page.relative_path})


async def _send_all(
    session_factory: sessionmaker[Session],
    ids: tuple[int, ...],
    payload: NotificationPayload,
    body: str,
    settings: Settings,
    mailer_factory: Callable[[Settings], SmtpMailer],
    clock: Callable[[], datetime],
    link: str | None,
) -> None:
    missing = missing_smtp_settings(settings)
    if missing:
        error = f"smtp not configured: missing {missing[0]}"
        for notification_id in ids:
            with session_scope(session_factory) as session:
                row = session.get(Notification, notification_id)
                assert row is not None
                notifications = NotificationRepository(session)
                notifications.begin_attempt(row, now=clock())
                notifications.mark(row, NotificationStatus.FAILED, error=error)
            logger.warning("notification failed", extra={**_log_extra(row), "error": error})
        return
    mail = render_mail(payload, body, archive_url=link)
    sender = settings.smtp_from or ""

    def build(row: Notification) -> EmailMessage:
        return build_message(mail, sender=sender, recipient=row.recipient, now=clock())

    async with mailer_factory(settings) as mailer:
        for notification_id in ids:
            await _attempt(
                session_factory,
                mailer,
                notification_id,
                build=build,
                settings=settings,
                clock=clock,
                count_attempt=True,
            )


@dataclass(frozen=True, slots=True)
class RetryResult:
    """What a retry did with one notification (``error`` is already scrubbed)."""

    notification_id: int
    job_name: str
    recipient: str
    status: NotificationStatus
    error: str | None


@dataclass(frozen=True, slots=True)
class RetryOutcome:
    """Summary of :func:`retry_failed`; ``given_up`` rows ended ``skipped``."""

    retried: int
    sent: int
    failed: int
    given_up: int
    results: tuple[RetryResult, ...]


def _job_label(row: Notification) -> str:
    name = row.payload.get("job_name") if isinstance(row.payload, dict) else None
    return name if isinstance(name, str) and name else str(row.job_id)


def _current_config(job: Job | None) -> JobConfig | None:
    """The job's stored config, or ``None`` when the job is gone or its config is invalid."""
    if job is None:
        return None
    try:
        return JobConfig.model_validate(job.config)
    except ValidationError:
        return None


def _rebuilder(
    session_factory: sessionmaker[Session], settings: Settings, clock: Callable[[], datetime]
) -> Callable[[Notification], EmailMessage]:
    """Return a function that rebuilds a stored notification's message from its payload."""

    def rebuild(row: Notification) -> EmailMessage:
        if row.digest_id is None:
            raise _NotSendable("digest no longer available")
        with session_scope(session_factory) as session:
            digest = DigestRepository(session).get(row.digest_id)
            if digest is None:
                raise _NotSendable("digest no longer available")
            body = digest.body
            job = session.get(Job, row.job_id)
            cfg = _current_config(job)
        try:
            payload = NotificationPayload.model_validate(row.payload)
        except ValidationError as exc:
            raise _NotSendable(f"invalid notification payload: {exc.errors()[0]['msg']}") from exc
        link = (
            archive_url(
                cfg.archive.base_url,
                settings.archive_dir,
                payload.archive_page,
                enabled=cfg.archive.enabled,
            )
            if cfg is not None
            else None
        )
        mail = render_mail(payload, body, archive_url=link)
        sender = settings.smtp_from or ""
        return build_message(mail, sender=sender, recipient=row.recipient, now=clock())

    return rebuild


def _claim(
    session_factory: sessionmaker[Session], notification_id: int, now: datetime
) -> Notification | None:
    """Claim a candidate for sending, or give up on it; ``None`` when nothing is left to send.

    A stale ``pending`` row that already used its attempts was interrupted by a crash: it is
    set to ``skipped`` instead of being sent again.
    """
    with session_scope(session_factory) as session:
        notifications = NotificationRepository(session)
        row = session.get(Notification, notification_id)
        if row is None:
            return None
        if row.status is NotificationStatus.PENDING and row.attempts >= MAX_ATTEMPTS:
            if not notifications.give_up_stale(
                notification_id,
                now=now,
                stale_after=STALE_PENDING_AFTER,
                max_attempts=MAX_ATTEMPTS,
                error="interrupted: attempt limit reached",
            ):
                return None
            session.refresh(row)  # the UPDATE bypassed the identity map
            return row
        if not notifications.claim_for_retry(
            notification_id, now=now, stale_after=STALE_PENDING_AFTER
        ):
            return None
        session.refresh(row)  # the claim UPDATE bypassed the identity map
        return row


async def retry_failed(
    session_factory: sessionmaker[Session],
    *,
    settings: Settings,
    mailer_factory: Callable[[Settings], SmtpMailer] = SmtpMailer,
    clock: Callable[[], datetime] = utcnow,
) -> RetryOutcome:
    """Re-send ``failed`` and stale ``pending`` notifications from their stored payload.

    Each candidate is claimed atomically first (a row another process took is skipped
    silently), then rebuilt from the stored digest and payload and sent over one shared
    connection that opens only when something is sent. Never raises for a per-row error; the
    fifth failed attempt ends in ``skipped``. Callers check the SMTP settings beforehand.
    """
    now = clock()
    with session_scope(session_factory) as session:
        candidates = [
            row.id
            for row in NotificationRepository(session).retry_candidates(
                now=now, stale_after=STALE_PENDING_AFTER
            )
        ]
    rebuild = _rebuilder(session_factory, settings, clock)
    results: list[RetryResult] = []
    async with contextlib.AsyncExitStack() as stack:
        mailer: SmtpMailer | None = None
        for notification_id in candidates:
            row = _claim(session_factory, notification_id, now)
            if row is None:
                continue
            job_name = _job_label(row)
            status: NotificationStatus
            error: str | None
            with run_context(job=job_name):
                if row.status is NotificationStatus.SKIPPED:  # interrupted at the limit
                    status, error = NotificationStatus.SKIPPED, row.error
                    logger.warning("notification given up", extra=_log_extra(row))
                else:
                    if mailer is None:
                        mailer = await stack.enter_async_context(mailer_factory(settings))
                    status, error = await _attempt(
                        session_factory,
                        mailer,
                        notification_id,
                        build=rebuild,
                        settings=settings,
                        clock=clock,
                        count_attempt=False,
                    )
            results.append(RetryResult(notification_id, job_name, row.recipient, status, error))
    statuses = [result.status for result in results]
    return RetryOutcome(
        retried=len(results),
        sent=statuses.count(NotificationStatus.SENT),
        failed=statuses.count(NotificationStatus.FAILED),
        given_up=statuses.count(NotificationStatus.SKIPPED),
        results=tuple(results),
    )


def open_session_factory(settings: Settings) -> sessionmaker[Session]:
    """Return a session factory on ``INVIO_DATABASE_URL``.

    Raises :class:`~invio.config.settings.MissingSettingError` when the URL is not set and
    :class:`~invio.db.session.DatabaseConfigError` when it is unusable. It lets the CLI reach
    the database through this package (the CLI must not import ``invio.db``).
    """
    raw = settings.require_secret("database_url")
    return build_session_factory(create_db_engine(check_database_url(raw)))


def scrub_error(text: str, settings: Settings) -> str:
    """Mask the database URL (and password) and the SMTP credentials in ``text``."""
    raw = settings.database_url.get_secret_value() if settings.database_url else ""
    return scrub_secret(scrub_database_url(text, raw), settings)
