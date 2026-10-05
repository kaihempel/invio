"""Helpers for e-mail tests: an in-process SMTP server, TLS fixtures and a digest seeder.

Fixtures defined here are imported into the test modules that use them (see the ``F811``
per-file ignore in ``pyproject.toml``). No test talks to an external network.
"""

import email
import email.policy
import socket
import ssl
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from email.message import EmailMessage
from typing import Any

import pytest
import trustme
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP, AuthResult, Envelope, LoginPassword, Session
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session as DbSession

from invio.config.job import JobConfig
from invio.config.settings import Settings
from invio.db.models import Digest, Job, Notification, Run
from invio.db.repositories import DigestRepository, RunRepository
from invio.db.session import session_factory, session_scope
from invio.domain import RunStatus
from invio.notify.payload import DigestStats, NotificationPayload

DEFAULT_SUBJECT = "invio: {job_name} \u2013 {date}"
SMTP_USER = "bob@x"
SMTP_PASSWORD = "s3cr3t-pw"
JOB_TEMPLATE: dict[str, Any] = {
    "schedule": {"frequency": "weekly", "time": "07:30", "weekday": "monday"},
    "notification": {"to": ["research@example.com"], "subject": "invio digest"},
    "sources": [{"type": "rss", "url": "https://example.com/feed.xml"}],
    "search": {"semantic_description": "LLM agent frameworks"},
    "llm": {"provider": "openai", "models": {"fast": "gpt-small", "smart": "gpt-large"}},
}


@dataclass(frozen=True)
class ReceivedMail:
    """One message as the server saw it: envelope plus raw bytes."""

    mail_from: str
    rcpt_tos: tuple[str, ...]
    raw: bytes

    @property
    def message(self) -> EmailMessage:
        return email.message_from_bytes(self.raw, policy=email.policy.default)


class RecordingHandler:
    """aiosmtpd handler that stores every accepted message.

    Addresses in ``reject`` get ``550`` at RCPT time; ``drop_data`` is the number of upcoming
    DATA commands that drop the connection instead of answering.
    """

    def __init__(self) -> None:
        self.received: list[ReceivedMail] = []
        self.reject: set[str] = set()
        self.drop_data = 0

    async def handle_RCPT(
        self, server: SMTP, session: Session, envelope: Envelope, address: str, options: Any
    ) -> str:
        if address in self.reject:
            return "550 mailbox unavailable"
        envelope.rcpt_tos.append(address)
        return "250 OK"

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        if self.drop_data > 0:
            self.drop_data -= 1
            assert server.transport is not None
            server.transport.close()
            return "250 OK"  # never delivered: the transport is closed
        content = envelope.original_content or b""
        self.received.append(
            ReceivedMail(str(envelope.mail_from), tuple(envelope.rcpt_tos), content)
        )
        return "250 OK"


@dataclass
class SmtpServer:
    """A running test server: where it listens, what it received and its auth log."""

    host: str
    port: int
    handler: RecordingHandler
    auth_calls: list[str] = field(default_factory=list)
    auth_ok: bool = True  # set to False to make the AUTH-requiring server refuse every login

    @property
    def messages(self) -> list[EmailMessage]:
        return [mail.message for mail in self.handler.received]


def free_port() -> int:
    """Return a TCP port on 127.0.0.1 that nothing listens on right now."""
    with closing(socket.socket()) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@contextmanager
def _serve(**controller_kwargs: Any) -> Iterator[SmtpServer]:
    handler = RecordingHandler()
    server = SmtpServer("127.0.0.1", free_port(), handler)
    controller = Controller(
        handler,
        hostname=server.host,
        port=server.port,
        ready_timeout=10,
        **controller_kwargs,
    )
    controller.start()
    try:
        yield server
    finally:
        controller.stop()


def _authenticator(server: SmtpServer) -> Callable[..., AuthResult]:
    def authenticate(
        _smtp: SMTP, _session: Session, _envelope: Envelope, mechanism: str, data: Any
    ) -> AuthResult:
        server.auth_calls.append(mechanism)
        if (
            server.auth_ok
            and isinstance(data, LoginPassword)
            and data.login == SMTP_USER.encode()
            and data.password == SMTP_PASSWORD.encode()
        ):
            return AuthResult(success=True)
        return AuthResult(success=False, handled=False)

    return authenticate


@pytest.fixture(scope="session")
def tls_ca() -> trustme.CA:
    return trustme.CA()


@pytest.fixture
def client_tls_context(tls_ca: trustme.CA) -> ssl.SSLContext:
    """A client context that trusts the test CA."""
    context = ssl.create_default_context()
    tls_ca.configure_trust(context)
    return context


def _server_context(ca: trustme.CA) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ca.issue_cert("127.0.0.1").configure_cert(context)
    return context


@pytest.fixture
def smtp_server() -> Iterator[SmtpServer]:
    """A plain (unencrypted) server that accepts everything."""
    with _serve() as server:
        yield server


@pytest.fixture
def smtp_server_auth() -> Iterator[SmtpServer]:
    """A plain server that requires ``AUTH`` with ``SMTP_USER`` / ``SMTP_PASSWORD``."""
    handler = RecordingHandler()
    server = SmtpServer("127.0.0.1", free_port(), handler)
    controller = Controller(
        handler,
        hostname=server.host,
        port=server.port,
        ready_timeout=10,
        authenticator=_authenticator(server),
        auth_required=True,
        auth_require_tls=False,
    )
    controller.start()
    try:
        yield server
    finally:
        controller.stop()


@pytest.fixture
def smtp_server_starttls(tls_ca: trustme.CA) -> Iterator[SmtpServer]:
    """A server that offers (and requires) STARTTLS."""
    with _serve(tls_context=_server_context(tls_ca), require_starttls=True) as server:
        yield server


@pytest.fixture
def smtp_server_ssl(tls_ca: trustme.CA) -> Iterator[SmtpServer]:
    """A server speaking TLS from the first byte (SMTPS)."""
    with _serve(ssl_context=_server_context(tls_ca)) as server:
        yield server


@pytest.fixture
def closed_port() -> int:
    """A port nothing listens on."""
    return free_port()


def smtp_settings(server: SmtpServer | None = None, **overrides: Any) -> Settings:
    """Settings that point at ``server`` (unencrypted) unless overridden."""
    values: dict[str, Any] = {
        "smtp_host": "127.0.0.1" if server is None else server.host,
        "smtp_port": 25 if server is None else server.port,
        "smtp_from": "Invio <invio@localhost>",
        "smtp_security": "none",
        "smtp_timeout_seconds": 5,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def seed_digest(
    session: DbSession,
    *,
    job_name: str = "ai-news",
    to: list[str] | None = None,
    subject: str = DEFAULT_SUBJECT,
    send_if_empty: bool = False,
    body: str = "# News\n\n- item",
    item_ids: list[int] | None = None,
    timezone: str = "Europe/Berlin",
    run_stats: dict[str, Any] | None = None,
    started_at: datetime | None = None,
) -> Digest:
    """Create a job (valid ``JobConfig``), a finished run and a digest, then commit."""
    config = JobConfig.model_validate(
        {
            **JOB_TEMPLATE,
            "schedule": {**JOB_TEMPLATE["schedule"], "timezone": timezone},
            "notification": {
                "to": ["a@example.org", "b@example.org"] if to is None else to,
                "subject": subject,
                "send_if_empty": send_if_empty,
            },
        }
    )
    job = Job(name=job_name, config=config.model_dump(mode="json"))
    session.add(job)
    session.flush()
    runs = RunRepository(session)
    run: Run = runs.start(job.id, started_at=started_at or datetime(2026, 10, 4, 12, 0, tzinfo=UTC))
    stats = {"items_found": 7} if run_stats is None else run_stats
    runs.finish(run, RunStatus.SUCCEEDED, stats=stats, finished_at=run.started_at)
    digest = DigestRepository(session).add(
        job.id, "digest", body, [1, 2] if item_ids is None else item_ids, run_id=run.id
    )
    session.commit()
    return digest


def seed(engine: Engine, **kwargs: Any) -> int:
    """Seed a job, run and digest in their own committed session; return the digest id."""
    with session_scope(session_factory(engine)) as session:
        return seed_digest(session, **kwargs).id


def rows(engine: Engine) -> list[Notification]:
    """Read all notifications (by id) in a fresh session."""
    with session_scope(session_factory(engine)) as session:
        return list(session.scalars(select(Notification).order_by(Notification.id)))


def add_notification(engine: Engine, digest_id: int, **kw: Any) -> int:
    """Insert a notification (valid stored payload) for the digest's job; return its id."""
    with session_scope(session_factory(engine)) as session:
        digest = session.get(Digest, digest_id)
        assert digest is not None
        payload = NotificationPayload(
            job_name="ai-news",
            subject="stored subject",
            digest_date=date(2026, 10, 4),
            is_empty=False,
            stats=DigestStats(items_found=1, items_included=1, duration_seconds=2),
        ).model_dump(mode="json")
        values: dict[str, Any] = {
            "job_id": digest.job_id,
            "run_id": digest.run_id,
            "digest_id": digest.id,
            "channel": "email",
            "recipient": "c@example.org",
            "payload": payload,
        }
        values.update(kw)
        row = Notification(**values)
        session.add(row)
        session.flush()
        return row.id


def notification_by_id(engine: Engine, notification_id: int) -> Notification:
    """Read one notification in a fresh session."""
    (row,) = [r for r in rows(engine) if r.id == notification_id]
    return row


def parts(message: EmailMessage) -> tuple[str, str]:
    """Return the decoded ``(text, html)`` bodies of a received message."""
    plain = message.get_body(("plain",))
    html = message.get_body(("html",))
    assert plain is not None
    assert html is not None
    return plain.get_content(), html.get_content()
