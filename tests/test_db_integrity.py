"""Negative paths and edge cases from spec.md not covered elsewhere.

Covers referential integrity for every child table, NOT NULL rejection, ORM enum validation
for runs/notifications, exact url_hash semantics, duplicate rejection on update, deletion of
single records, naive timestamps through the models and multi-byte large content.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, func, inspect, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invio.db.migrate import alembic_config, current_revision, upgrade
from invio.db.models import Digest, Item, Job, LlmUsage, Notification, Run
from invio.db.session import create_db_engine
from invio.domain import url_hash
from tests.db_helpers import (
    assert_rejected,
    make_digest,
    make_item,
    make_job,
    make_llm_usage,
    make_notification,
    make_run,
    uses_sqlite,
)

pytestmark = [
    pytest.mark.db,
    pytest.mark.filterwarnings(
        "ignore:Dialect sqlite\\+pysqlite does \\*not\\* support Decimal objects natively"
        ":sqlalchemy.exc.SAWarning"
    ),
]

APP_TABLES = {"jobs", "runs", "items", "digests", "notifications", "llm_usage"}
MISSING_ID = 999_999


def _child(model: type, **kw: Any) -> Any:
    defaults: dict[type, dict[str, Any]] = {
        Run: {},
        Item: {"url": "u", "url_hash": url_hash("u"), "type": "article", "title": "t"},
        Digest: {"title": "d", "body": "b"},
        Notification: {"channel": "email", "recipient": "a@example.com"},
        LlmUsage: {"provider": "openai", "model": "m"},
    }
    return model(**{**defaults[model], **kw})


def _count(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model)) or 0


# --- FR-013: fixture provides the full schema -------------------------------------------


def test_fixture_database_has_the_full_schema(db_engine: Engine) -> None:
    assert set(inspect(db_engine).get_table_names()) >= APP_TABLES


@pytest.mark.skipif(not uses_sqlite(), reason="in-memory SQLite only")
def test_migrations_upgrade_an_in_memory_sqlite_database() -> None:
    """Acceptance 1.2: the in-memory variant (file-based is covered in test_db_migrations)."""
    engine = create_db_engine(
        "sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    try:
        with engine.begin() as conn:
            assert upgrade(alembic_config(connection=conn)) == "0004"
            assert current_revision(conn) == "0004"
        assert set(inspect(engine).get_table_names()) == APP_TABLES | {"alembic_version"}
    finally:
        engine.dispose()


# --- FR-008 / FR-008a: referential integrity ----------------------------------------------


@pytest.mark.parametrize("model", [Run, Digest, Notification, LlmUsage], ids=lambda m: m.__name__)
def test_child_with_missing_job_is_rejected(db_session: Session, model: type) -> None:
    db_session.add(_child(model, job_id=MISSING_ID))

    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.parametrize("model", [Item, Digest, Notification, LlmUsage], ids=lambda m: m.__name__)
def test_child_with_missing_run_is_rejected(db_session: Session, model: type) -> None:
    job = make_job(db_session)
    db_session.add(_child(model, job_id=job.id, run_id=MISSING_ID))

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_notification_with_missing_digest_is_rejected(db_session: Session) -> None:
    job = make_job(db_session)
    db_session.add(_child(Notification, job_id=job.id, digest_id=MISSING_ID))

    with pytest.raises(IntegrityError):
        db_session.flush()


@pytest.mark.parametrize("victim", ["digest", "notification", "usage", "item"])
def test_deleting_a_single_record_keeps_job_and_siblings(db_session: Session, victim: str) -> None:
    """Edge case: deleting a run/item/digest individually never deletes the parent job."""
    job = make_job(db_session)
    run = make_run(db_session, job)
    digest = make_digest(db_session, job, run_id=run.id)
    rows: dict[str, Any] = {
        "item": make_item(db_session, job, run_id=run.id),
        "digest": digest,
        "notification": make_notification(db_session, job, run_id=run.id, digest_id=digest.id),
        "usage": make_llm_usage(db_session, job, run_id=run.id),
    }

    db_session.delete(rows.pop(victim))
    db_session.flush()
    db_session.expire_all()

    assert db_session.get(Job, job.id) is not None
    assert db_session.get(Run, run.id) is not None
    for row in rows.values():
        assert db_session.get(type(row), row.id) is not None


# --- NOT NULL columns ----------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["url", "url_hash", "title", "type"])
def test_item_without_required_column_is_rejected(db_session: Session, missing: str) -> None:
    job = make_job(db_session)
    fields: dict[str, Any] = {
        "url": "u",
        "url_hash": url_hash("u"),
        "type": "article",
        "title": "t",
    }
    fields[missing] = None
    db_session.add(Item(job_id=job.id, **fields))

    with assert_rejected():
        db_session.flush()


def test_job_without_name_is_rejected(db_session: Session) -> None:
    db_session.add(Job())

    with assert_rejected():
        db_session.flush()


@pytest.mark.parametrize("missing", ["channel", "recipient"])
def test_notification_without_required_column_is_rejected(
    db_session: Session, missing: str
) -> None:
    job = make_job(db_session)
    db_session.add(_child(Notification, job_id=job.id, **{missing: None}))

    with assert_rejected():
        db_session.flush()


# --- FR-006a: ORM-level status validation for runs and notifications ----------------------


def test_orm_bogus_run_status_is_rejected(db_session: Session) -> None:
    with pytest.raises(StatementError):
        make_run(db_session, make_job(db_session), status="done")


def test_orm_bogus_notification_status_is_rejected(db_session: Session) -> None:
    with pytest.raises(StatementError):
        make_notification(db_session, make_job(db_session), status="bounced")


def test_status_is_rejected_on_update_too(db_session: Session) -> None:
    item = make_item(db_session, make_job(db_session))

    item.status = "bogus"  # type: ignore[assignment]
    with pytest.raises(StatementError):
        db_session.flush()


# --- FR-004 / SC-003: url_hash semantics ---------------------------------------------------


def test_url_hash_column_reserves_exactly_64_characters(db_engine: Engine) -> None:
    columns = {c["name"]: c for c in inspect(db_engine).get_columns("items")}

    assert getattr(columns["url_hash"]["type"], "length", None) == 64
    assert columns["url_hash"]["nullable"] is False


def test_url_hash_comparison_is_case_sensitive(db_session: Session) -> None:
    """Binary collation: a differently-cased hash is a different fingerprint."""
    job = make_job(db_session)
    digest = url_hash("https://example.com/a")
    make_item(db_session, job, url_hash=digest)

    other = make_item(db_session, job, url="https://example.com/b", url_hash=digest.upper())

    assert other.url_hash == digest.upper()
    assert _count(db_session, Item) == 2


def test_rejected_duplicate_leaves_exactly_one_item(db_session: Session) -> None:
    job = make_job(db_session)
    make_item(db_session, job)

    for _ in range(3):
        with pytest.raises(IntegrityError), db_session.begin_nested():
            make_item(db_session, job)

    assert _count(db_session, Item) == 1


def test_update_to_an_existing_url_hash_is_rejected(db_session: Session) -> None:
    job = make_job(db_session)
    first = make_item(db_session, job, url="https://example.com/a")
    second = make_item(db_session, job, url="https://example.com/b")

    second.url_hash = first.url_hash
    with pytest.raises(IntegrityError):
        db_session.flush()


def test_same_url_with_different_case_is_a_different_item(db_session: Session) -> None:
    """URL normalisation is out of scope: the hash is taken of the URL exactly as stored."""
    job = make_job(db_session)

    make_item(db_session, job, url="https://example.com/Page")
    make_item(db_session, job, url="https://example.com/page")

    assert _count(db_session, Item) == 2


# --- FR-005 / FR-006: values ---------------------------------------------------------------


def test_relevance_is_stored_with_two_decimals(db_session: Session) -> None:
    item = make_item(db_session, make_job(db_session), relevance=Decimal("0.756"))
    db_session.expire_all()

    stored = db_session.get(Item, item.id)
    assert stored is not None
    assert stored.relevance == Decimal("0.76")


def test_relevance_is_optional(db_session: Session) -> None:
    item = make_item(db_session, make_job(db_session))
    db_session.expire_all()

    stored = db_session.get(Item, item.id)
    assert stored is not None
    assert stored.relevance is None
    assert stored.last_error is None


def test_attempts_and_last_error_round_trip(db_session: Session) -> None:
    item = make_item(db_session, make_job(db_session))

    item.attempts += 2
    item.last_error = "timeout after 30 s — Übertragung abgebrochen 🚫"
    db_session.flush()
    db_session.expire_all()

    stored = db_session.get(Item, item.id)
    assert stored is not None
    assert stored.attempts == 2
    assert stored.last_error == "timeout after 30 s — Übertragung abgebrochen 🚫"


def test_multibyte_raw_content_round_trips(db_session: Session) -> None:
    """Several MB of 4-byte characters (UTF-8 size, not character count, is what counts)."""
    raw = "Ä🚀" * 500_000  # 3 MB in UTF-8
    url = "https://example.com/ü/" + "ß" * 3000
    item = make_item(db_session, make_job(db_session), url=url, raw_content=raw)
    db_session.expire_all()

    stored = db_session.get(Item, item.id)
    assert stored is not None
    assert stored.raw_content == raw
    assert stored.url == url
    assert stored.url_hash == url_hash(url)


# --- FR-016: naive timestamps are rejected through the models -----------------------------


@pytest.mark.parametrize("column", ["next_run_at", "locked_until"])
def test_naive_job_timestamp_is_rejected(db_session: Session, column: str) -> None:
    with pytest.raises(StatementError, match="naive datetime not allowed"):
        make_job(db_session, "naive", **{column: datetime(2026, 10, 4, 7, 30)})


def test_naive_run_finished_at_is_rejected(db_session: Session) -> None:
    with pytest.raises(StatementError, match="naive datetime not allowed"):
        make_run(db_session, make_job(db_session), finished_at=datetime(2026, 10, 4))
