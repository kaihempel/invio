"""Tests for ``retry_failed``: re-sending failed and stale pending notifications."""

import io
import json
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from invio.config.settings import Settings
from invio.db.models import Digest, Job, Notification, Run
from invio.db.repositories import DigestRepository, NotificationRepository
from invio.db.session import session_factory, session_scope
from invio.domain import NotificationStatus
from invio.log import JsonFormatter
from invio.notify import email as notify_email
from invio.notify.email import RetryOutcome, deliver_digest, retry_failed
from invio.notify.payload import NotificationPayload
from invio.notify.render import render_mail
from tests.conftest import FakeClock
from tests.smtp_helpers import (
    SmtpServer,
    add_notification,
    closed_port,  # noqa: F401
    notification_by_id,
    parts,
    rows,
    seed,
    smtp_server,  # noqa: F401
    smtp_settings,
)

pytestmark = pytest.mark.db

S = NotificationStatus
HOUR = timedelta(hours=1)


@pytest.fixture(autouse=True)
def _cleanup(clean_jobs: None) -> None:
    """Delete committed jobs after every test (the MariaDB engine is shared)."""


async def deliver(engine: Engine, digest_id: int, settings: Settings, clock: FakeClock) -> None:
    await deliver_digest(session_factory(engine), digest_id, settings=settings, clock=clock)


async def retry(engine: Engine, settings: Settings, clock: FakeClock, **kw: Any) -> RetryOutcome:
    return await retry_failed(session_factory(engine), settings=settings, clock=clock, **kw)


async def deliver_while_down(engine: Engine, clock: FakeClock, port: int, **seed_kw: Any) -> int:
    """Deliver a fresh digest while the server is down; return the digest id."""
    digest_id = seed(engine, **seed_kw)
    await deliver(engine, digest_id, smtp_settings(smtp_port=port), clock)
    return digest_id


async def test_failed_become_sent(
    db_engine: Engine, closed_port: int, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    await deliver_while_down(db_engine, fake_clock, closed_port)
    assert {r.status for r in rows(db_engine)} == {S.FAILED}

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    assert (outcome.retried, outcome.sent, outcome.failed, outcome.given_up) == (2, 2, 0, 0)
    assert [r.status for r in outcome.results] == [S.SENT, S.SENT]
    assert [r.recipient for r in outcome.results] == ["a@example.org", "b@example.org"]
    assert {r.job_name for r in outcome.results} == {"ai-news"}
    for row in rows(db_engine):
        assert row.status is S.SENT
        assert row.error is None
        assert row.attempts == 2
        assert row.sent_at is not None
    assert len(smtp_server.messages) == 2


async def test_same_subject_and_body(
    db_engine: Engine, closed_port: int, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = await deliver_while_down(db_engine, fake_clock, closed_port, to=["a@example.org"])
    # Rename the job and change its subject: a retry must still use the first delivery's data.
    with session_scope(session_factory(db_engine)) as session:
        job = session.scalars(select(Job)).one()
        job.name = "renamed"
        assert job.config is not None
        job.config = {**job.config, "notification": {**job.config["notification"], "subject": "x"}}
        digest = session.get(Digest, digest_id)
        assert digest is not None
        body = digest.body
    (row,) = rows(db_engine)
    expected = render_mail(NotificationPayload.model_validate(row.payload), body)
    digests_before = _count(db_engine, Digest)
    runs_before = _count(db_engine, Run)

    await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    (message,) = smtp_server.messages
    text, html = parts(message)
    assert message["Subject"] == expected.subject == "invio: ai-news – 2026-10-04"
    assert text.replace("\r\n", "\n").strip() == expected.text.strip()
    assert html.replace("\r\n", "\n").strip() == expected.html.strip()
    assert "renamed" not in text
    assert _count(db_engine, Digest) == digests_before
    assert _count(db_engine, Run) == runs_before


def _count(engine: Engine, model: type) -> int:
    with session_scope(session_factory(engine)) as session:
        return session.scalar(select(func.count()).select_from(model)) or 0


async def test_still_failing(db_engine: Engine, closed_port: int, fake_clock: FakeClock) -> None:
    await deliver_while_down(db_engine, fake_clock, closed_port, to=["a@example.org"])

    outcome = await retry(db_engine, smtp_settings(smtp_port=closed_port), fake_clock)

    (row,) = rows(db_engine)
    assert (row.status, row.attempts) == (S.FAILED, 2)
    assert row.error is not None
    assert (outcome.retried, outcome.sent, outcome.failed, outcome.given_up) == (1, 0, 1, 0)
    assert outcome.results[0].error == row.error


async def test_ignores_sent_skipped_fresh_pending(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    add_notification(db_engine, digest_id, status=S.SENT)
    add_notification(db_engine, digest_id, status=S.SKIPPED)
    add_notification(
        db_engine,
        digest_id,
        status=S.PENDING,
        attempts=1,
        last_attempt_at=fake_clock.now - timedelta(minutes=59),
    )

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    assert outcome.retried == 0
    assert outcome.results == ()
    assert smtp_server.messages == []


async def test_stale_pending_recovered(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(
        db_engine,
        digest_id,
        status=S.PENDING,
        attempts=1,
        last_attempt_at=fake_clock.now - timedelta(minutes=61),
    )

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert (row.status, row.attempts) == (S.SENT, 2)
    assert outcome.sent == 1
    assert len(smtp_server.messages) == 1


async def test_stale_pending_at_limit(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(
        db_engine,
        digest_id,
        status=S.PENDING,
        attempts=5,
        last_attempt_at=fake_clock.now - timedelta(minutes=61),
    )

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert row.status is S.SKIPPED
    assert row.error == "interrupted: attempt limit reached"
    assert row.attempts == 5
    assert smtp_server.messages == []
    assert (outcome.retried, outcome.given_up, outcome.sent, outcome.failed) == (1, 1, 0, 0)


async def test_fifth_attempt_gives_up(
    db_engine: Engine, closed_port: int, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(db_engine, digest_id, status=S.FAILED, attempts=4, error="earlier")
    settings = smtp_settings(smtp_port=closed_port)

    outcome = await retry(db_engine, settings, fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert (row.status, row.attempts) == (S.SKIPPED, 5)
    assert row.error is not None
    assert row.error != "earlier"
    assert (outcome.given_up, outcome.failed) == (1, 0)
    assert (await retry(db_engine, settings, fake_clock)).retried == 0


async def test_deleted_digest(
    db_engine: Engine, closed_port: int, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    gone = await deliver_while_down(
        db_engine, fake_clock, closed_port, job_name="gone", to=["a@example.org"]
    )
    await deliver_while_down(
        db_engine, fake_clock, closed_port, job_name="kept", to=["b@example.org"]
    )
    with session_scope(session_factory(db_engine)) as session:
        digest = session.get(Digest, gone)
        assert digest is not None
        session.delete(digest)

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    first, second = rows(db_engine)
    assert first.digest_id is None
    assert (first.status, first.error) == (S.FAILED, "digest no longer available")
    assert second.status is S.SENT
    assert (outcome.sent, outcome.failed) == (1, 1)


async def test_invalid_payload(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(db_engine, digest_id, status=S.FAILED, payload={"x": 1})

    await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert row.status is S.FAILED
    assert row.error is not None
    assert row.error.startswith("invalid notification payload: ")
    assert smtp_server.messages == []


async def test_claim_skips_taken_row(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    taken = add_notification(db_engine, digest_id, status=S.FAILED, recipient="taken@example.org")
    free = add_notification(db_engine, digest_id, status=S.FAILED, recipient="free@example.org")
    original = NotificationRepository.claim_for_retry

    def claim(self: NotificationRepository, notification_id: int, **kw: Any) -> bool:
        return False if notification_id == taken else original(self, notification_id, **kw)

    monkeypatch.setattr(NotificationRepository, "claim_for_retry", claim)

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    assert (outcome.retried, outcome.sent) == (1, 1)
    assert [r.notification_id for r in outcome.results] == [free]
    assert notification_by_id(db_engine, taken).status is S.FAILED
    assert notification_by_id(db_engine, taken).attempts == 0
    assert [m.rcpt_tos for m in smtp_server.handler.received] == [("free@example.org",)]


async def test_nothing_to_retry_never_connects(db_engine: Engine, fake_clock: FakeClock) -> None:
    def forbidden(settings: Settings) -> Any:
        pytest.fail("no connection may be attempted")

    outcome = await retry(db_engine, smtp_settings(), fake_clock, mailer_factory=forbidden)

    assert outcome == RetryOutcome(retried=0, sent=0, failed=0, given_up=0, results=())


# --- stale pending boundary ---------------------------------------------------------------------


async def test_pending_exactly_one_hour_old_is_not_retried(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(
        db_engine, digest_id, status=S.PENDING, attempts=1, last_attempt_at=fake_clock.now - HOUR
    )

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    assert outcome.retried == 0
    assert notification_by_id(db_engine, row_id).attempts == 1
    assert smtp_server.messages == []


async def test_never_attempted_pending_recovered_from_creation_time(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(
        db_engine, digest_id, status=S.PENDING, created_at=fake_clock.now - 2 * HOUR
    )

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert (row.status, row.attempts, row.last_attempt_at) == (S.SENT, 1, fake_clock.now)
    assert outcome.sent == 1


# --- attempt limit ------------------------------------------------------------------------------


async def test_failure_under_the_limit_stays_failed(
    db_engine: Engine, closed_port: int, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(db_engine, digest_id, status=S.FAILED, attempts=3)

    outcome = await retry(db_engine, smtp_settings(smtp_port=closed_port), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert (row.status, row.attempts) == (S.FAILED, 4)
    assert (outcome.failed, outcome.given_up) == (1, 0)


async def test_lifecycle_gives_up_after_exactly_five_attempts(
    db_engine: Engine, closed_port: int, fake_clock: FakeClock
) -> None:
    await deliver_while_down(db_engine, fake_clock, closed_port, to=["a@example.org"])
    settings = smtp_settings(smtp_port=closed_port)
    seen: list[tuple[S, int]] = []

    for _ in range(4):
        fake_clock.advance(HOUR)
        outcome = await retry(db_engine, settings, fake_clock)
        assert outcome.retried == 1
        (row,) = rows(db_engine)
        seen.append((row.status, row.attempts))

    assert seen == [(S.FAILED, 2), (S.FAILED, 3), (S.FAILED, 4), (S.SKIPPED, 5)]
    (row,) = rows(db_engine)
    assert row.error is not None
    assert row.error.split(":")[0].isidentifier()  # the last SMTP error is kept
    assert (await retry(db_engine, settings, fake_clock)).retried == 0


async def test_stale_pending_with_four_attempts_gives_up_when_it_fails(
    db_engine: Engine, closed_port: int, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(
        db_engine,
        digest_id,
        status=S.PENDING,
        attempts=4,
        last_attempt_at=fake_clock.now - 2 * HOUR,
    )

    outcome = await retry(db_engine, smtp_settings(smtp_port=closed_port), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert (row.status, row.attempts) == (S.SKIPPED, 5)
    assert row.error != "interrupted: attempt limit reached"
    assert outcome.given_up == 1


async def test_stale_pending_at_limit_refreshed_concurrently_is_left_alone(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(
        db_engine,
        digest_id,
        status=S.PENDING,
        attempts=5,
        last_attempt_at=fake_clock.now - 2 * HOUR,
    )

    def touched_by_another_process(session: Session) -> None:
        row = session.get(Notification, row_id)
        assert row is not None
        row.last_attempt_at = fake_clock.now

    _after_candidates(monkeypatch, db_engine, touched_by_another_process)

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    assert outcome.retried == 0
    row = notification_by_id(db_engine, row_id)
    assert (row.status, row.attempts, row.error) == (S.PENDING, 5, None)
    assert smtp_server.messages == []


# --- concurrency: the candidate list is a snapshot, every send needs a claim ------------------


def _after_candidates(
    monkeypatch: pytest.MonkeyPatch, engine: Engine, change: Callable[[Session], None]
) -> None:
    """Run ``change`` once, in its own committed session, after the candidates are selected.

    It runs right before the first claim, as another process would between ``SELECT`` and
    ``UPDATE`` (no session of the retry is open then; the SQLite engine has one connection).
    """
    original = notify_email._claim
    pending = [change]

    def claim(factory: Any, notification_id: int, now: Any) -> Notification | None:
        if pending:
            with session_scope(session_factory(engine)) as session:
                pending.pop()(session)
        return original(factory, notification_id, now)

    monkeypatch.setattr(notify_email, "_claim", claim)


async def test_row_claimed_by_another_process_is_not_sent(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    taken = add_notification(db_engine, digest_id, status=S.FAILED, attempts=1, recipient="t@x.org")
    free = add_notification(db_engine, digest_id, status=S.FAILED, attempts=1, recipient="f@x.org")

    def claim_elsewhere(session: Session) -> None:
        assert NotificationRepository(session).claim_for_retry(
            taken, now=fake_clock.now, stale_after=HOUR
        )

    _after_candidates(monkeypatch, db_engine, claim_elsewhere)

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    assert [r.notification_id for r in outcome.results] == [free]
    row = notification_by_id(db_engine, taken)
    assert (row.status, row.attempts) == (S.PENDING, 2)  # only the other process counted
    assert [m.rcpt_tos for m in smtp_server.handler.received] == [("f@x.org",)]


async def test_row_deleted_before_its_claim_is_skipped(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    gone = add_notification(db_engine, digest_id, status=S.FAILED, recipient="gone@x.org")
    kept = add_notification(db_engine, digest_id, status=S.FAILED, recipient="kept@x.org")

    def delete_row(session: Session) -> None:
        row = session.get(Notification, gone)
        assert row is not None
        session.delete(row)

    _after_candidates(monkeypatch, db_engine, delete_row)

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    assert [r.notification_id for r in outcome.results] == [kept]
    assert outcome.sent == 1


async def test_one_invocation_attempts_each_row_once(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    smtp_server.handler.reject = {"a@example.org"}
    digest_id = seed(db_engine, to=["a@example.org"])
    rejected = add_notification(db_engine, digest_id, status=S.FAILED, recipient="a@example.org")
    accepted = add_notification(db_engine, digest_id, status=S.FAILED, recipient="b@example.org")

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    assert [r.notification_id for r in outcome.results] == [rejected, accepted]
    assert notification_by_id(db_engine, rejected).attempts == 1
    assert notification_by_id(db_engine, accepted).attempts == 1
    assert len(smtp_server.messages) == 1


# --- digest gone, invalid payload ----------------------------------------------------------------


async def test_deleted_digest_at_the_limit_is_given_up(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(db_engine, digest_id, status=S.FAILED, attempts=4)
    with session_scope(session_factory(db_engine)) as session:
        session.execute(delete(Digest))

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert (row.status, row.attempts) == (S.SKIPPED, 5)
    assert row.error == "digest no longer available"
    assert outcome.results[0].error == "digest no longer available"
    assert smtp_server.messages == []


async def test_dangling_digest_id_is_reported_as_gone(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(db_engine, digest_id, status=S.FAILED, attempts=1)
    # e.g. a database without enforced foreign keys keeps the id of a deleted digest
    monkeypatch.setattr(DigestRepository, "get", lambda self, digest_id: None)

    await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert (row.status, row.error) == (S.FAILED, "digest no longer available")
    assert smtp_server.messages == []


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(None, id="null"),
        pytest.param({"schema_version": 2}, id="unknown-version"),
        pytest.param({"job_name": "ai-news", "subject": "s"}, id="missing-fields"),
    ],
)
async def test_unusable_payload_fails_without_sending(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock, payload: Any
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(db_engine, digest_id, status=S.FAILED, attempts=1, payload=payload)

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert row.status is S.FAILED
    assert row.attempts == 2
    assert row.error is not None
    assert row.error.startswith("invalid notification payload: ")
    assert smtp_server.messages == []
    expected_label = "ai-news" if isinstance(payload, dict) and "job_name" in payload else None
    assert outcome.results[0].job_name == (expected_label or str(row.job_id))


async def test_invalid_payload_at_the_limit_is_given_up(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(db_engine, digest_id, status=S.FAILED, attempts=4, payload={})

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert (row.status, row.attempts) == (S.SKIPPED, 5)
    assert outcome.given_up == 1


async def test_stored_subject_with_line_breaks_never_injects_headers(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    # A tampered (or pre-validation) payload: retry must not turn it into extra headers.
    digest_id = seed(db_engine, to=["a@example.org"])
    payload = {
        "job_name": "ai-news",
        "subject": "hi\r\nBcc: evil@example.org",
        "digest_date": "2026-10-04",
        "is_empty": False,
        "stats": {"items_included": 1},
    }
    row_id = add_notification(db_engine, digest_id, status=S.FAILED, attempts=1, payload=payload)

    await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    for mail in smtp_server.handler.received:
        assert mail.rcpt_tos == ("c@example.org",)
        assert mail.message["Bcc"] is None
        assert all("\n" not in v and "\r" not in v for v in mail.message.get_all("Subject", []))
    # The header policy refuses the value, so the attempt fails instead of sending.
    row = notification_by_id(db_engine, row_id)
    assert row.status is S.FAILED
    assert row.error is not None
    assert row.error.startswith("ValueError: ")
    assert smtp_server.messages == []


# --- stored data wins over the current job config -----------------------------------------------


async def test_retry_sends_to_the_stored_recipient(
    db_engine: Engine, closed_port: int, smtp_server: SmtpServer, fake_clock: FakeClock
) -> None:
    await deliver_while_down(db_engine, fake_clock, closed_port, to=["a@example.org"])
    with session_scope(session_factory(db_engine)) as session:
        job = session.scalars(select(Job)).one()
        assert job.config is not None
        notification = {**job.config["notification"], "to": ["new@example.org"]}
        job.config = {**job.config, "notification": notification}

    await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    assert [m.rcpt_tos for m in smtp_server.handler.received] == [("a@example.org",)]


@pytest.mark.parametrize("send_if_empty_now", [True, False])
async def test_empty_digest_retry_follows_the_stored_payload(
    db_engine: Engine,
    closed_port: int,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    send_if_empty_now: bool,
) -> None:
    await deliver_while_down(
        db_engine,
        fake_clock,
        closed_port,
        to=["a@example.org"],
        item_ids=[],
        send_if_empty=True,
        body="SHOULD NOT APPEAR",
    )
    with session_scope(session_factory(db_engine)) as session:
        job = session.scalars(select(Job)).one()
        assert job.config is not None
        notification = {**job.config["notification"], "send_if_empty": send_if_empty_now}
        job.config = {**job.config, "notification": notification}

    outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    # send_if_empty only gates creating rows; an existing row is always re-sent as stored.
    assert outcome.sent == 1
    (message,) = smtp_server.messages
    text, html = parts(message)
    for body in (text, html):
        assert "No new items were found for this run." in body
        assert "SHOULD NOT APPEAR" not in body


# --- logging and the "sent but not recorded" path ------------------------------------------------


async def test_retry_logs_carry_job_notification_and_recipient(
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
        row_id = add_notification(db_engine, digest_id, status=S.FAILED, attempts=1)
        await retry(db_engine, smtp_settings(smtp_port=closed_port), fake_clock)
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    (failure,) = [r for r in records if r["message"] == "notification failed"]
    assert failure["job"] == "ai-news"
    assert failure["notification_id"] == row_id
    assert failure["recipient"] == "c@example.org"


async def test_retry_sent_but_not_recorded_stays_pending(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    digest_id = seed(db_engine, to=["a@example.org"])
    row_id = add_notification(db_engine, digest_id, status=S.FAILED, attempts=1, error="old")
    original = NotificationRepository.mark

    def mark(self: NotificationRepository, row: Any, status: S, **kw: Any) -> Any:
        if status is S.SENT:
            raise RuntimeError("db write failed")
        return original(self, row, status, **kw)

    monkeypatch.setattr(NotificationRepository, "mark", mark)

    with caplog.at_level(logging.DEBUG, logger="invio.notify"):
        outcome = await retry(db_engine, smtp_settings(smtp_server), fake_clock)

    row = notification_by_id(db_engine, row_id)
    assert (row.status, row.attempts) == (S.PENDING, 2)  # not failed: no immediate re-send
    assert len(smtp_server.messages) == 1
    assert outcome.failed == 0
    assert "notification sent but not recorded" in caplog.text
    # The row is fresh pending now, so an immediate second retry leaves it alone.
    assert (await retry(db_engine, smtp_settings(smtp_server), fake_clock)).retried == 0
    assert len(smtp_server.messages) == 1
