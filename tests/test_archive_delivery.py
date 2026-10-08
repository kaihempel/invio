"""Archive and mail link wiring in ``deliver_digest`` / ``retry_failed`` (no network)."""

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, delete

from invio.config.settings import Settings
from invio.db.models import Digest, Job, Run
from invio.db.session import session_factory, session_scope
from invio.domain import RunStatus
from invio.notify.email import DeliveryOutcome, deliver_digest, retry_failed
from invio.notify.payload import NotificationPayload
from tests.conftest import FakeClock
from tests.smtp_helpers import (
    SmtpServer,
    closed_port,  # noqa: F401
    notification_by_id,
    parts,
    rows,
    seed,
    smtp_server,  # noqa: F401
    smtp_settings,
)

pytestmark = pytest.mark.db

STARTED = datetime(2026, 10, 8, 9, 30, 41, tzinfo=UTC)
PAGE = "ai-news/2026-10-08-0930.html"
BASE = "https://digests.example.org/invio"
ENABLED = {"enabled": True}
WITH_URL = {"enabled": True, "base_url": BASE}


@pytest.fixture(autouse=True)
def _cleanup(clean_jobs: None) -> None:
    """Delete committed jobs after every test (the MariaDB engine is shared)."""


async def deliver(
    engine: Engine, digest_id: int, settings: Settings, clock: FakeClock
) -> DeliveryOutcome:
    return await deliver_digest(session_factory(engine), digest_id, settings=settings, clock=clock)


def _settings(server: SmtpServer, archive_dir: Path, **overrides: Any) -> Settings:
    return smtp_settings(server, archive_dir=archive_dir, **overrides)


def _payloads(engine: Engine) -> list[NotificationPayload]:
    return [NotificationPayload.model_validate(row.payload) for row in rows(engine)]


# --- US1: the page ---------------------------------------------------------------------------


async def test_enabled_job_gets_a_page_and_the_payload_records_it(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock, tmp_path: Path
) -> None:
    archive_dir = tmp_path / "not-yet-there"
    digest_id = seed(db_engine, started_at=STARTED, archive=ENABLED, body="# Hello\n\n- thing")

    outcome = await deliver(db_engine, digest_id, _settings(smtp_server, archive_dir), fake_clock)

    page = archive_dir / PAGE
    assert outcome.sent == 2
    html = page.read_text(encoding="utf-8")
    assert "ai-news" in html
    assert "2026-10-08 09:30 UTC" in html
    assert "<li>thing</li>" in html
    assert (archive_dir / "ai-news" / "index.html").is_file()
    assert (archive_dir / "index.html").is_file()
    assert {p.archive_page for p in _payloads(db_engine)} == {PAGE}


@pytest.mark.parametrize("archive", [None, {"enabled": False, "base_url": BASE}])
async def test_disabled_or_omitted_archive_leaves_the_directory_alone(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    tmp_path: Path,
    archive: dict[str, Any] | None,
) -> None:
    archive_dir = tmp_path / "archive"
    digest_id = seed(db_engine, started_at=STARTED, archive=archive)

    outcome = await deliver(db_engine, digest_id, _settings(smtp_server, archive_dir), fake_clock)

    assert outcome.sent == 2
    assert not archive_dir.exists()
    assert {p.archive_page for p in _payloads(db_engine)} == {None}
    for message in smtp_server.messages:
        text, html = parts(message)
        assert "Archived version" not in text
        assert "Archived version" not in html


async def test_empty_digest_is_not_archived_even_when_it_is_mailed(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock, tmp_path: Path
) -> None:
    archive_dir = tmp_path / "archive"
    digest_id = seed(db_engine, item_ids=[], send_if_empty=True, archive=WITH_URL)

    outcome = await deliver(db_engine, digest_id, _settings(smtp_server, archive_dir), fake_clock)

    assert outcome.sent == 2
    assert not archive_dir.exists()
    assert {p.archive_page for p in _payloads(db_engine)} == {None}
    assert all("Archived version" not in parts(m)[0] for m in smtp_server.messages)


async def test_partial_run_digest_is_archived(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock, tmp_path: Path
) -> None:
    digest_id = seed(db_engine, started_at=STARTED, archive=ENABLED, run_status=RunStatus.PARTIAL)

    await deliver(db_engine, digest_id, _settings(smtp_server, tmp_path), fake_clock)

    assert (tmp_path / PAGE).is_file()


async def test_digest_without_run_is_named_by_its_creation_minute(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock, tmp_path: Path
) -> None:
    digest_id = seed(db_engine, archive=ENABLED)
    with session_scope(session_factory(db_engine)) as session:
        session.execute(delete(Run))
        digest = session.get(Digest, digest_id)
        assert digest is not None
        session.refresh(digest)
        created = digest.created_at.astimezone(UTC)

    await deliver(db_engine, digest_id, _settings(smtp_server, tmp_path), fake_clock)

    assert (tmp_path / "ai-news" / f"{created:%Y-%m-%d-%H%M}.html").is_file()


async def test_unwritable_archive_dir_does_not_affect_delivery(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    blocker = tmp_path / "archive"
    blocker.write_text("a file where the directory should be", encoding="utf-8")
    digest_id = seed(db_engine, started_at=STARTED, archive=WITH_URL)

    with caplog.at_level(logging.WARNING, logger="invio.notify.email"):
        outcome = await deliver(db_engine, digest_id, _settings(smtp_server, blocker), fake_clock)

    assert (outcome.sent, outcome.failed, outcome.error) == (2, 0, None)
    assert any(record.getMessage() == "archive.failed" for record in caplog.records)
    assert {p.archive_page for p in _payloads(db_engine)} == {None}
    for message in smtp_server.messages:
        assert "Archived version" not in parts(message)[0]


# --- US3: the link in the mail -----------------------------------------------------------------


async def test_mail_links_the_archived_page(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock, tmp_path: Path
) -> None:
    digest_id = seed(db_engine, started_at=STARTED, archive=WITH_URL)

    await deliver(db_engine, digest_id, _settings(smtp_server, tmp_path), fake_clock)

    assert len(smtp_server.messages) == 2
    for message in smtp_server.messages:
        text, html = parts(message)
        assert f"Archived version: {BASE}/{PAGE}" in text
        assert f'<a href="{BASE}/{PAGE}">' in html


@pytest.mark.parametrize("base_url", [BASE, BASE + "/"])
async def test_base_url_slash_does_not_matter(
    db_engine: Engine,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    tmp_path: Path,
    base_url: str,
) -> None:
    digest_id = seed(
        db_engine,
        started_at=STARTED,
        to=["a@example.org"],
        archive={**WITH_URL, "base_url": base_url},
    )

    await deliver(db_engine, digest_id, _settings(smtp_server, tmp_path), fake_clock)

    text, _ = parts(smtp_server.messages[0])
    assert f"{BASE}/{PAGE}" in text
    assert f"{BASE}//" not in text


async def test_enabled_without_base_url_writes_the_page_but_no_link(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock, tmp_path: Path
) -> None:
    digest_id = seed(db_engine, started_at=STARTED, archive=ENABLED)

    await deliver(db_engine, digest_id, _settings(smtp_server, tmp_path), fake_clock)

    assert (tmp_path / PAGE).is_file()
    assert all("Archived version" not in parts(m)[0] for m in smtp_server.messages)


async def test_same_minute_second_digest_links_the_suffixed_page(
    db_engine: Engine, smtp_server: SmtpServer, fake_clock: FakeClock, tmp_path: Path
) -> None:
    first = seed(db_engine, started_at=STARTED, to=["a@example.org"], archive=WITH_URL)
    await deliver(db_engine, first, _settings(smtp_server, tmp_path), fake_clock)

    await deliver(db_engine, first, _settings(smtp_server, tmp_path), fake_clock)

    text, _ = parts(smtp_server.messages[-1])
    assert f"{BASE}/ai-news/2026-10-08-0930-2.html" in text
    assert (tmp_path / "ai-news" / "2026-10-08-0930-2.html").is_file()


# --- retry -------------------------------------------------------------------------------------


async def _failed_delivery(
    engine: Engine, closed_port: int, archive_dir: Path, clock: FakeClock
) -> int:
    digest_id = seed(engine, started_at=STARTED, to=["a@example.org"], archive=WITH_URL)
    settings = smtp_settings(smtp_port=closed_port, archive_dir=archive_dir)
    await deliver(engine, digest_id, settings, clock)
    (row,) = rows(engine)
    assert row.status.value == "failed"
    assert NotificationPayload.model_validate(row.payload).archive_page == PAGE
    return row.id


async def _retry(engine: Engine, server: SmtpServer, archive_dir: Path, clock: FakeClock) -> None:
    clock.advance(timedelta(hours=2))
    await retry_failed(
        session_factory(engine), settings=_settings(server, archive_dir), clock=clock
    )


async def test_retry_includes_the_link_while_the_page_exists(
    db_engine: Engine,
    closed_port: int,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> None:
    notification_id = await _failed_delivery(db_engine, closed_port, tmp_path, fake_clock)

    await _retry(db_engine, smtp_server, tmp_path, fake_clock)

    assert notification_by_id(db_engine, notification_id).status.value == "sent"
    text, html = parts(smtp_server.messages[0])
    assert f"Archived version: {BASE}/{PAGE}" in text
    assert BASE in html


async def test_retry_omits_the_link_when_the_page_was_deleted(
    db_engine: Engine,
    closed_port: int,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> None:
    await _failed_delivery(db_engine, closed_port, tmp_path, fake_clock)
    (tmp_path / PAGE).unlink()

    await _retry(db_engine, smtp_server, tmp_path, fake_clock)

    assert "Archived version" not in parts(smtp_server.messages[0])[0]


async def test_retry_omits_the_link_when_base_url_was_removed_from_the_job(
    db_engine: Engine,
    closed_port: int,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> None:
    await _failed_delivery(db_engine, closed_port, tmp_path, fake_clock)
    with session_scope(session_factory(db_engine)) as session:
        job = session.query(Job).one()
        job.config = {**job.config, "archive": {"enabled": True, "base_url": None}}

    await _retry(db_engine, smtp_server, tmp_path, fake_clock)

    assert "Archived version" not in parts(smtp_server.messages[0])[0]


async def test_retry_with_an_unreadable_job_config_sends_without_link(
    db_engine: Engine,
    closed_port: int,
    smtp_server: SmtpServer,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> None:
    await _failed_delivery(db_engine, closed_port, tmp_path, fake_clock)
    with session_scope(session_factory(db_engine)) as session:
        job = session.query(Job).one()
        job.config = {"schema_version": 1, "garbage": True}

    await _retry(db_engine, smtp_server, tmp_path, fake_clock)

    assert len(smtp_server.messages) == 1
    assert "Archived version" not in parts(smtp_server.messages[0])[0]
