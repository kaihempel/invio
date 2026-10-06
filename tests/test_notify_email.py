"""Tests for ``invio.notify.email``: delivery of digests over a local SMTP server."""

import asyncio
import io
import json
import logging
import ssl
import sys
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP, Envelope, Session
from sqlalchemy import Engine, delete, select

from invio.config.settings import Settings
from invio.db.models import Digest, Job, Run
from invio.db.repositories import NotificationRepository
from invio.db.session import session_factory, session_scope
from invio.domain import NotificationStatus, RunStatus
from invio.log import JsonFormatter
from invio.notify import DatabaseConfigError, open_session_factory
from invio.notify.email import (
    DeliveryOutcome,
    SmtpMailer,
    deliver_digest,
    run_status_after_delivery,
    scrub_error,
    scrub_secret,
)
from invio.notify.payload import NotificationPayload
from tests.conftest import FakeClock
from tests.smtp_helpers import (
    SMTP_PASSWORD,
    SMTP_USER,
    RecordingHandler,
    SmtpServer,
    client_tls_context,  # noqa: F401
    closed_port,  # noqa: F401
    free_port,
    parts,
    rows,
    seed,
    smtp_server,  # noqa: F401
    smtp_server_auth,  # noqa: F401
    smtp_server_ssl,  # noqa: F401
    smtp_server_starttls,  # noqa: F401
    smtp_settings,
    tls_ca,  # noqa: F401
)

pytestmark = pytest.mark.db

ZONE = ZoneInfo("Europe/Berlin")  # the seeded job's schedule time zone


@pytest.fixture(autouse=True)
def _cleanup(clean_jobs: None) -> None:
    """Delete committed jobs after every test (the MariaDB engine is shared)."""


async def deliver(
    db_engine: Engine, digest_id: int, settings: Settings, **kwargs: Any
) -> DeliveryOutcome:
    return await deliver_digest(session_factory(db_engine), digest_id, settings=settings, **kwargs)


async def test_sends_one_multipart_per_recipient(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    # 23:30 UTC is already the next day in Europe/Berlin.
    started = datetime(2026, 10, 4, 23, 30, tzinfo=UTC)
    digest_id = seed(db_engine, to=["a@example.org", "b@example.org"], started_at=started)

    outcome = await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    assert outcome.sent == 2
    assert outcome.failed == 0
    assert outcome.skipped_empty is False
    mails = smtp_server.handler.received
    assert [m.rcpt_tos for m in mails] == [("a@example.org",), ("b@example.org",)]
    for mail in mails:
        message = mail.message
        assert message.get_content_type() == "multipart/alternative"
        text, html = parts(message)
        assert "# News" in text
        assert "<h1>News</h1>" in html
        assert "ai-news" in text
        assert "Items found: 7 · Included: 2 · Run time:" in text
        assert message["Subject"] == "invio: ai-news – 2026-10-05"
        assert message["From"] == "Invio <invio@localhost>"
    assert [m.message["To"] for m in mails] == ["a@example.org", "b@example.org"]


async def test_duplicate_recipients_deduped(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org", "a@example.org"])

    outcome = await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    assert outcome.sent == 1
    assert len(smtp_server.messages) == 1
    assert len(rows(db_engine)) == 1


@pytest.mark.parametrize(
    ("fixture", "security"),
    [("smtp_server_starttls", "starttls"), ("smtp_server_ssl", "ssl"), ("smtp_server", "none")],
)
async def test_security_modes(
    request: pytest.FixtureRequest,
    db_engine: Engine,
    client_tls_context: ssl.SSLContext,
    fake_clock: FakeClock,
    fixture: str,
    security: str,
) -> None:
    server: SmtpServer = request.getfixturevalue(fixture)
    digest_id = seed(db_engine, to=["a@example.org"])
    settings = smtp_settings(server, smtp_security=security)

    outcome = await deliver(
        db_engine,
        digest_id,
        settings,
        clock=fake_clock,
        mailer_factory=lambda s: SmtpMailer(s, tls_context=client_tls_context),
    )

    assert (outcome.sent, outcome.failed) == (1, 0)
    assert len(server.messages) == 1


async def test_login_happens_when_credentials_are_set(
    db_engine: Engine, smtp_server_auth: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    settings = smtp_settings(
        smtp_server_auth, smtp_user=SMTP_USER, smtp_password=SMTP_PASSWORD, smtp_security="none"
    )

    outcome = await deliver(db_engine, digest_id, settings, clock=fake_clock)

    assert outcome.sent == 1
    assert smtp_server_auth.auth_calls
    assert len(smtp_server_auth.messages) == 1


async def test_no_login_without_a_password(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    settings = smtp_settings(smtp_server, smtp_user=SMTP_USER)

    outcome = await deliver(db_engine, digest_id, settings, clock=fake_clock)

    # A server without an authenticator rejects AUTH, so success proves none was attempted.
    assert outcome.sent == 1


async def test_unicode_roundtrip(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    name = "Nüsse ß \U0001f389"
    digest_id = seed(
        db_engine,
        job_name=name,
        to=["a@example.org"],
        subject="Grüße \U0001f389 {job_name}",
        body="# Größe \U0001f389",
    )

    await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    (message,) = smtp_server.messages
    assert message["Subject"] == f"Grüße \U0001f389 {name}"
    text, html = parts(message)
    assert "# Größe \U0001f389" in text
    assert "Größe \U0001f389" in html
    assert name in text
    assert name in html


# --- failures are recorded on the rows (US3) ---------------------------------------------------


class RaisingMailer(SmtpMailer):
    """A mailer whose ``send`` raises ``error`` without any network."""

    def __init__(self, settings: Settings, error: Exception) -> None:
        super().__init__(settings)
        self._error = error

    async def send(self, message: EmailMessage) -> None:
        raise self._error


def raising(error: Exception) -> Callable[[Settings], SmtpMailer]:
    return lambda settings: RaisingMailer(settings, error)


def forbidden_mailer(settings: Settings) -> SmtpMailer:
    pytest.fail("no connection may be attempted")


async def test_rows_marked_sent(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine)

    await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    notifications = rows(db_engine)
    assert len(notifications) == 2
    for row in notifications:
        assert row.status is NotificationStatus.SENT
        assert row.sent_at is not None
        assert row.error is None
        assert row.attempts == 1
        assert row.last_attempt_at == fake_clock.now
        assert row.channel == "email"
        assert row.run_id is not None
        assert row.digest_id == digest_id
        assert NotificationPayload.model_validate(row.payload).job_name == "ai-news"


async def test_unreachable_server_marks_failed(
    db_engine: Engine, closed_port: int, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine)

    outcome = await deliver(
        db_engine, digest_id, smtp_settings(smtp_port=closed_port), clock=fake_clock
    )

    assert outcome.failed == 2
    assert outcome.sent == 0
    assert run_status_after_delivery(RunStatus.SUCCEEDED, outcome) is RunStatus.PARTIAL
    for row in rows(db_engine):
        assert row.status is NotificationStatus.FAILED
        assert row.attempts == 1
        assert row.error is not None
        assert row.error.split(":")[0].isidentifier()
        assert row.sent_at is None


async def test_one_recipient_rejected(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    smtp_server.handler.reject = {"b@example.org"}
    digest_id = seed(db_engine)

    outcome = await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    first, second = rows(db_engine)
    assert first.status is NotificationStatus.SENT
    assert second.status is NotificationStatus.FAILED
    assert second.error is not None
    assert "550" in second.error
    assert (outcome.sent, outcome.failed) == (1, 1)


async def test_rejected_recipient_does_not_block_the_next_one(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    smtp_server.handler.reject = {"a@example.org"}
    digest_id = seed(db_engine)

    await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    first, second = rows(db_engine)
    assert (first.status, second.status) == (NotificationStatus.FAILED, NotificationStatus.SENT)
    assert [m.rcpt_tos for m in smtp_server.handler.received] == [("b@example.org",)]


async def test_mid_batch_disconnect_reconnects(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    smtp_server.handler.drop_data = 1
    digest_id = seed(db_engine)

    outcome = await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    first, second = rows(db_engine)
    assert first.status is NotificationStatus.FAILED
    assert second.status is NotificationStatus.SENT
    assert (outcome.sent, outcome.failed) == (1, 1)


@pytest.mark.parametrize(
    ("overrides", "variable"),
    [({"smtp_host": None}, "INVIO_SMTP_HOST"), ({"smtp_from": None}, "INVIO_SMTP_FROM")],
)
async def test_missing_smtp_settings(
    db_engine: Engine, fake_clock: FakeClock, overrides: dict[str, Any], variable: str
) -> None:
    digest_id = seed(db_engine)
    settings = smtp_settings(**overrides)

    outcome = await deliver(
        db_engine, digest_id, settings, clock=fake_clock, mailer_factory=forbidden_mailer
    )

    assert outcome.failed == 2
    for row in rows(db_engine):
        assert row.status is NotificationStatus.FAILED
        assert row.error == f"smtp not configured: missing {variable}"
        assert row.attempts == 1


def test_scrub_secret_masks_password_and_user() -> None:
    settings = smtp_settings(smtp_user="bob@x", smtp_password="s3cr3t-pw")

    text = scrub_secret("login bob@x / s3cr3t-pw failed", settings)

    assert text == "login *** / *** failed"


def test_scrub_secret_without_credentials_keeps_text() -> None:
    assert scrub_secret("plain text", smtp_settings()) == "plain text"


def test_scrub_secret_truncates_long_text() -> None:
    text = scrub_secret("x" * 5000, smtp_settings())

    assert text == "x" * 2000 + "… [truncated]"
    assert scrub_secret("x" * 2000, smtp_settings()) == "x" * 2000


async def test_password_never_in_error_or_logs(
    db_engine: Engine,
    smtp_server_auth: SmtpServer,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    smtp_server_auth.auth_ok = False
    digest_id = seed(db_engine)
    settings = smtp_settings(
        smtp_server_auth, smtp_user="bob@x", smtp_password="s3cr3t-pw", smtp_security="none"
    )
    leaky = RuntimeError("login failed for bob@x with s3cr3t-pw " + "x" * 3000)

    with caplog.at_level(logging.DEBUG):
        await deliver(db_engine, digest_id, settings, clock=fake_clock)
        await deliver(
            db_engine, digest_id, settings, clock=fake_clock, mailer_factory=raising(leaky)
        )

    captured = capsys.readouterr()
    notifications = rows(db_engine)
    assert len(notifications) == 4
    assert {r.status for r in notifications} == {NotificationStatus.FAILED}
    assert smtp_server_auth.auth_calls
    for row in notifications:
        assert row.error is not None
        assert "s3cr3t-pw" not in row.error
        assert "bob@x" not in row.error
    leaked_row = notifications[-1]
    assert leaked_row.error is not None
    assert leaked_row.error.startswith("RuntimeError: login failed for *** with ***")
    assert leaked_row.error.endswith("… [truncated]")
    for output in (caplog.text, captured.out, captured.err):
        assert "s3cr3t-pw" not in output
        assert "bob@x" not in output


async def test_unexpected_error_never_raises(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine)

    outcome = await deliver(
        db_engine,
        digest_id,
        smtp_settings(smtp_server),
        clock=fake_clock,
        mailer_factory=raising(RuntimeError("kaputt")),
    )

    assert outcome.failed == 2
    assert {r.error for r in rows(db_engine)} == {"RuntimeError: kaputt"}


async def test_delivery_errors_mask_the_database_url(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    raw = "mysql+pymysql://invio:db-pw-123@db.example/x"
    digest_id = seed(db_engine)

    outcome = await deliver(
        db_engine,
        digest_id,
        smtp_settings(smtp_server, database_url=raw),
        clock=fake_clock,
        mailer_factory=raising(RuntimeError(f"cannot reach {raw}")),
    )

    assert outcome.failed == 2
    assert {r.error for r in rows(db_engine)} == {"RuntimeError: cannot reach ***"}


async def test_digest_missing_reports_error(db_engine: Engine, fake_clock: FakeClock) -> None:
    outcome = await deliver(db_engine, 999_999, smtp_settings(), clock=fake_clock)

    assert outcome.error is not None
    assert "not found" in outcome.error
    assert (outcome.sent, outcome.failed, outcome.notification_ids) == (0, 0, ())
    assert rows(db_engine) == []
    assert run_status_after_delivery(RunStatus.SUCCEEDED, outcome) is RunStatus.PARTIAL


async def test_invalid_job_config_reports_error(db_engine: Engine, fake_clock: FakeClock) -> None:
    digest_id = seed(db_engine)
    with session_scope(session_factory(db_engine)) as session:
        job = session.scalars(select(Job)).one()
        job.config = {"x": 1}

    outcome = await deliver(
        db_engine, digest_id, smtp_settings(), clock=fake_clock, mailer_factory=forbidden_mailer
    )

    assert outcome.error is not None
    assert rows(db_engine) == []


async def test_render_failure_marks_rows_failed(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("invio.notify.email.render_mail", boom)
    digest_id = seed(db_engine)

    outcome = await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    assert outcome.failed == 2
    assert outcome.error is None
    notifications = rows(db_engine)
    assert len(notifications) == 2
    assert {(r.status, r.error) for r in notifications} == {
        (NotificationStatus.FAILED, "RuntimeError: boom")
    }
    assert smtp_server.messages == []


async def test_starttls_required_but_unsupported(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine)
    settings = smtp_settings(smtp_server, smtp_security="starttls")

    outcome = await deliver(db_engine, digest_id, settings, clock=fake_clock)

    assert outcome.failed == 2
    assert {r.status for r in rows(db_engine)} == {NotificationStatus.FAILED}
    assert smtp_server.messages == []


async def test_failure_log_carries_job_and_run(
    db_engine: Engine, closed_port: int, fake_clock: FakeClock
) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("invio.notify")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        digest_id = seed(db_engine, to=["a@example.org"])
        await deliver(db_engine, digest_id, smtp_settings(smtp_port=closed_port), clock=fake_clock)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    (failure,) = [r for r in records if r["message"] == "notification failed"]
    (row,) = rows(db_engine)
    assert failure["job"] == "ai-news"
    assert failure["run_id"] == str(row.run_id)
    assert failure["recipient"] == "a@example.org"
    assert failure["notification_id"] == row.id


# --- empty digests (US4) -----------------------------------------------------------------------


async def test_empty_digest_not_sent(
    db_engine: Engine, closed_port: int, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, item_ids=[], send_if_empty=False)

    outcome = await deliver(
        db_engine,
        digest_id,
        smtp_settings(smtp_port=closed_port),
        clock=fake_clock,
        mailer_factory=forbidden_mailer,
    )

    assert outcome.skipped_empty is True
    assert (outcome.sent, outcome.failed, outcome.notification_ids) == (0, 0, ())
    assert outcome.error is None
    assert rows(db_engine) == []


async def test_empty_digest_sent_when_requested(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, item_ids=[], send_if_empty=True, body="SHOULD NOT APPEAR")

    outcome = await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    assert (outcome.sent, outcome.skipped_empty) == (2, False)
    assert len(smtp_server.messages) == 2
    for message in smtp_server.messages:
        text, html = parts(message)
        for body in (text, html):
            assert "No new items were found for this run." in body
            assert "Included: 0" in body
            assert "SHOULD NOT APPEAR" not in body
    notifications = rows(db_engine)
    assert {r.status for r in notifications} == {NotificationStatus.SENT}
    assert all(r.payload is not None and r.payload["is_empty"] is True for r in notifications)


async def test_empty_digest_requested_but_server_down_fails_rows(
    db_engine: Engine, closed_port: int, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, item_ids=[], send_if_empty=True)

    outcome = await deliver(
        db_engine, digest_id, smtp_settings(smtp_port=closed_port), clock=fake_clock
    )

    assert (outcome.sent, outcome.failed, outcome.skipped_empty) == (0, 2, False)
    assert {r.status for r in rows(db_engine)} == {NotificationStatus.FAILED}
    assert run_status_after_delivery(RunStatus.SUCCEEDED, outcome) is RunStatus.PARTIAL


# --- header injection, recipients, settings edge cases -----------------------------------------


async def test_line_breaks_in_job_name_cannot_inject_headers(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    evil = "ai-news\r\nBcc: evil@example.org\r\nX-Injected: yes"
    digest_id = seed(db_engine, job_name=evil, to=["a@example.org"], subject="{job_name}")

    outcome = await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    assert outcome.sent == 1
    (mail,) = smtp_server.handler.received
    assert mail.rcpt_tos == ("a@example.org",)
    message = mail.message
    assert message["Bcc"] is None
    assert message["X-Injected"] is None
    assert message.get_all("Subject") == ["ai-news Bcc: evil@example.org X-Injected: yes"]
    (row,) = rows(db_engine)
    assert row.payload is not None
    assert "\n" not in row.payload["subject"]
    assert "\r" not in row.payload["subject"]


async def test_dedupe_keeps_first_seen_order(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["b@example.org", "a@example.org", "b@example.org"])

    await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    assert [r.recipient for r in rows(db_engine)] == ["b@example.org", "a@example.org"]
    assert [m.rcpt_tos for m in smtp_server.handler.received] == [
        ("b@example.org",),
        ("a@example.org",),
    ]


@pytest.mark.parametrize(
    ("overrides", "variable"),
    [({"smtp_host": "   "}, "INVIO_SMTP_HOST"), ({"smtp_from": " "}, "INVIO_SMTP_FROM")],
)
async def test_blank_smtp_settings_count_as_missing(
    db_engine: Engine, fake_clock: FakeClock, overrides: dict[str, Any], variable: str
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])

    outcome = await deliver(
        db_engine,
        digest_id,
        smtp_settings(**overrides),
        clock=fake_clock,
        mailer_factory=forbidden_mailer,
    )

    assert outcome.failed == 1
    (row,) = rows(db_engine)
    assert row.error == f"smtp not configured: missing {variable}"


async def test_digest_without_run_uses_its_creation_date_and_dashes(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    with session_scope(session_factory(db_engine)) as session:
        session.execute(delete(Run))  # digests.run_id is SET NULL by the database
        digest = session.get(Digest, digest_id)
        assert digest is not None
        session.refresh(digest)
        assert digest.run_id is None
        created = digest.created_at

    outcome = await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    assert outcome.sent == 1
    (row,) = rows(db_engine)
    assert row.run_id is None
    payload = NotificationPayload.model_validate(row.payload)
    assert payload.stats.duration_seconds is None
    assert payload.stats.items_found is None
    assert payload.digest_date == created.astimezone(ZONE).date()
    text, _ = parts(smtp_server.messages[0])
    assert "Items found: – · Included: 2 · Run time: –" in text


@pytest.mark.parametrize(
    "stats",
    [{}, {"found": -1}, {"found": True}, {"found": "7"}, {"found": 1.5}],
    ids=["absent", "negative", "bool", "string", "float"],
)
async def test_unusable_items_found_renders_a_dash(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock, stats: dict[str, Any]
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"], run_stats=stats)

    await deliver(db_engine, digest_id, smtp_settings(smtp_server), clock=fake_clock)

    (row,) = rows(db_engine)
    assert NotificationPayload.model_validate(row.payload).stats.items_found is None
    text, html = parts(smtp_server.messages[0])
    assert "Items found: –" in text
    assert "Items found: –" in html


async def test_mailer_exit_error_keeps_sent_rows_sent(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    class ExplodingExit(SmtpMailer):
        async def __aexit__(self, *exc: object) -> None:
            await super().__aexit__(*exc)
            raise RuntimeError("quit failed")

    digest_id = seed(db_engine)

    outcome = await deliver(
        db_engine,
        digest_id,
        smtp_settings(smtp_server),
        clock=fake_clock,
        mailer_factory=ExplodingExit,
    )

    assert (outcome.sent, outcome.failed, outcome.error) == (2, 0, None)
    assert {r.status for r in rows(db_engine)} == {NotificationStatus.SENT}


# --- an SMTP server that stops answering mid-transfer ------------------------------------------


class HangingHandler(RecordingHandler):
    """Never answers the next ``hang_data`` DATA commands (the client must time out)."""

    def __init__(self) -> None:
        super().__init__()
        self.hang_data = 0

    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        if self.hang_data > 0:
            self.hang_data -= 1
            await asyncio.Event().wait()  # cancelled when the controller stops
        return await super().handle_DATA(server, session, envelope)


@pytest.fixture
def hanging_server() -> Iterator[SmtpServer]:
    handler = HangingHandler()
    server = SmtpServer("127.0.0.1", free_port(), handler)
    controller = Controller(handler, hostname=server.host, port=server.port, ready_timeout=10)
    controller.start()
    try:
        yield server
    finally:
        controller.stop()


async def test_timeout_mid_transfer_fails_row_and_continues(
    db_engine: Engine, hanging_server: SmtpServer, fake_clock: FakeClock
) -> None:
    handler = hanging_server.handler
    assert isinstance(handler, HangingHandler)
    handler.hang_data = 1
    digest_id = seed(db_engine)
    settings = smtp_settings(hanging_server, smtp_timeout_seconds=1.5)

    outcome = await deliver(db_engine, digest_id, settings, clock=fake_clock)

    first, second = rows(db_engine)
    assert first.status is NotificationStatus.FAILED
    assert first.error is not None
    assert "Timeout" in first.error
    assert second.status is NotificationStatus.SENT
    assert (outcome.sent, outcome.failed) == (1, 1)
    assert [m.rcpt_tos for m in handler.received] == [("b@example.org",)]


# --- sent but not recorded / cleartext login warning --------------------------------------------


def _fail_marking_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    original = NotificationRepository.mark

    def mark(self: NotificationRepository, row: Any, status: NotificationStatus, **kw: Any) -> Any:
        if status is NotificationStatus.SENT:
            raise RuntimeError(f"db write failed for {SMTP_PASSWORD}")
        return original(self, row, status, **kw)

    monkeypatch.setattr(NotificationRepository, "mark", mark)


async def test_sent_but_not_recorded_stays_pending(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    _fail_marking_sent(monkeypatch)
    settings = smtp_settings(smtp_server, smtp_password=SMTP_PASSWORD)

    with caplog.at_level(logging.DEBUG, logger="invio.notify"):
        outcome = await deliver(db_engine, digest_id, settings, clock=fake_clock)

    (row,) = rows(db_engine)
    assert row.status is NotificationStatus.PENDING  # not failed: the mail did go out
    assert (row.attempts, row.error, row.sent_at) == (1, None, None)
    assert len(smtp_server.messages) == 1
    assert outcome.failed == 0
    assert outcome.error is None
    (record,) = [
        r for r in caplog.records if r.getMessage() == "notification sent but not recorded"
    ]
    assert record.levelno == logging.ERROR
    assert SMTP_PASSWORD not in str(record.__dict__)


async def test_cleartext_login_is_warned_about(
    db_engine: Engine,
    smtp_server_auth: SmtpServer,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    settings = smtp_settings(
        smtp_server_auth, smtp_user=SMTP_USER, smtp_password=SMTP_PASSWORD, smtp_security="none"
    )

    with caplog.at_level(logging.DEBUG, logger="invio.notify"):
        outcome = await deliver(db_engine, digest_id, settings, clock=fake_clock)

    assert outcome.sent == 1
    warnings = [
        r
        for r in caplog.records
        if r.getMessage() == "smtp login without TLS: credentials are sent in cleartext"
    ]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].name.startswith("invio.notify")
    assert SMTP_PASSWORD not in caplog.text


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"smtp_user": SMTP_USER}, id="no-password"),
        pytest.param({"smtp_password": SMTP_PASSWORD}, id="no-user"),
        pytest.param({}, id="no-credentials"),
    ],
)
async def test_no_cleartext_warning_without_login(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    overrides: dict[str, Any],
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])

    with caplog.at_level(logging.DEBUG, logger="invio.notify"):
        outcome = await deliver(
            db_engine, digest_id, smtp_settings(smtp_server, **overrides), clock=fake_clock
        )

    assert outcome.sent == 1
    assert "cleartext" not in caplog.text


async def test_no_cleartext_warning_with_tls(
    db_engine: Engine,
    smtp_server_starttls: SmtpServer,
    client_tls_context: ssl.SSLContext,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    settings = smtp_settings(
        smtp_server_starttls,
        smtp_user=SMTP_USER,
        smtp_password=SMTP_PASSWORD,
        smtp_security="starttls",
    )

    with caplog.at_level(logging.DEBUG, logger="invio.notify"):
        await deliver(
            db_engine,
            digest_id,
            settings,
            clock=fake_clock,
            mailer_factory=lambda s: SmtpMailer(s, tls_context=client_tls_context),
        )

    # The STARTTLS test server offers no AUTH, so the login itself fails; no warning either way.
    assert "cleartext" not in caplog.text


# --- scrubbing of database URLs and opening the database ---------------------------------------

DB_SECRET = "s3cret-db"


def test_scrub_error_without_database_url_only_masks_smtp_secrets() -> None:
    settings = smtp_settings(smtp_user="bob@x", smtp_password="pw-1")

    assert scrub_error("bob@x pw-1 mysql://u:p@h/d", settings) == "*** *** mysql://u:p@h/d"


def test_scrub_error_masks_raw_and_normalized_database_url() -> None:
    raw = f"mysql+pymysql://invio:{DB_SECRET}@db.example/x"
    settings = smtp_settings(database_url=raw)
    normalized = f"{raw}?charset=utf8mb4"

    text = scrub_error(f"a {raw} b {normalized} c {DB_SECRET}", settings)

    assert DB_SECRET not in text
    assert "db.example" not in text
    assert text.startswith("a *** b ")


def test_scrub_error_handles_an_unparsable_database_url() -> None:
    raw = f"mysql://u:{DB_SECRET}@h:notaport/db"
    settings = smtp_settings(database_url=raw)

    text = scrub_error(f"cannot use {raw}", settings)

    assert text == "cannot use ***"


@pytest.mark.parametrize(
    ("url", "prefix"),
    [
        (f"mysql+nope://u:{DB_SECRET}@h/db", "invalid database URL: "),
        (f"mysql://u:{DB_SECRET}@h:notaport/db", "invalid database URL: "),
        (f"postgresql+psycopg2://u:{DB_SECRET}@h/db", "database driver not installed: "),
    ],
    ids=["unknown-dialect", "unparsable", "missing-driver"],
)
def test_open_session_factory_rejects_unusable_urls(
    monkeypatch: pytest.MonkeyPatch, url: str, prefix: str
) -> None:
    monkeypatch.setitem(sys.modules, "psycopg2", None)  # ImportError even if installed

    with pytest.raises(DatabaseConfigError) as exc_info:
        open_session_factory(smtp_settings(database_url=url))

    assert isinstance(exc_info.value, ValueError)
    message = str(exc_info.value)
    assert message.startswith(prefix)
    assert DB_SECRET not in message
    assert exc_info.value.__cause__ is None  # ``from None``: the raw text is not chained
    assert exc_info.value.__suppress_context__ is True


def test_open_session_factory_opens_a_sqlite_database(tmp_path: Path) -> None:
    factory = open_session_factory(smtp_settings(database_url=f"sqlite:///{tmp_path}/x.sqlite"))

    with factory() as session:
        assert session.bind is not None


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        ({"found": 7}, 7),
        ({"items_found": 5}, None),
        ({"found": 0}, 0),
        ({"found": -1}, None),
        ({"found": True}, None),
        ({"found": "7"}, None),
        ({"found": 1.5}, None),
        ({}, None),
        (None, None),
    ],
    ids=[
        "found",
        "other-key-is-ignored",
        "zero",
        "negative",
        "bool",
        "string",
        "float",
        "absent",
        "no-stats",
    ],
)
def test_items_found_reads_the_found_statistic(
    stats: dict[str, Any] | None, expected: int | None
) -> None:
    from invio.notify.email import _items_found

    assert _items_found(stats) == expected
