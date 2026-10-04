"""Runs, digests, notifications and LLM usage records."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from invio.db.models import Digest, Job, LlmUsage, Notification, Run
from invio.db.types import UTCDateTime
from invio.domain import NotificationStatus, RunStatus
from tests.db_helpers import (
    assert_rejected,
    make_digest,
    make_job,
    make_llm_usage,
    make_notification,
    make_run,
)

pytestmark = [
    pytest.mark.db,
    # SQLite stores Numeric as float; SQLAlchemy warns once about it (values still round-trip).
    pytest.mark.filterwarnings(
        "ignore:Dialect sqlite\\+pysqlite does \\*not\\* support Decimal objects natively"
        ":sqlalchemy.exc.SAWarning"
    ),
]


def _raw(table: str, status: str) -> tuple[object, dict[str, object]]:
    extra = {
        "runs": ("", ""),
        "notifications": (", channel, recipient", ", 'email', 'a@example.com'"),
    }[table]
    stmt = text(
        f"INSERT INTO {table} (job_id, status, {'started_at' if table == 'runs' else 'created_at'}"
        f"{extra[0]}) VALUES (:job_id, :status, :now{extra[1]})"
    ).bindparams(bindparam("now", type_=UTCDateTime()))
    return stmt, {"status": status, "now": datetime.now(UTC)}


def test_run_defaults_to_running(db_session: Session) -> None:
    run = make_run(db_session, make_job(db_session))
    db_session.refresh(run)

    assert run.status == RunStatus.RUNNING


@pytest.mark.parametrize("status", list(RunStatus))
def test_every_run_status_round_trips(db_session: Session, status: RunStatus) -> None:
    run = make_run(db_session, make_job(db_session), status=status)
    db_session.expire_all()

    assert db_session.get(Run, run.id).status == status  # type: ignore[union-attr]


def test_raw_bogus_run_status_is_rejected(db_session: Session) -> None:
    job = make_job(db_session)
    stmt, params = _raw("runs", "done")

    with assert_rejected():
        db_session.execute(stmt, {**params, "job_id": job.id})  # type: ignore[arg-type]


def test_notification_defaults_to_pending(db_session: Session) -> None:
    notification = make_notification(db_session, make_job(db_session))
    db_session.refresh(notification)

    assert notification.status == NotificationStatus.PENDING


@pytest.mark.parametrize("status", list(NotificationStatus))
def test_every_notification_status_round_trips(
    db_session: Session, status: NotificationStatus
) -> None:
    notification = make_notification(db_session, make_job(db_session), status=status)
    db_session.expire_all()

    assert db_session.get(Notification, notification.id).status == status  # type: ignore[union-attr]


def test_raw_bogus_notification_status_is_rejected(db_session: Session) -> None:
    job = make_job(db_session)
    stmt, params = _raw("notifications", "bounced")

    with assert_rejected():
        db_session.execute(stmt, {**params, "job_id": job.id})  # type: ignore[arg-type]


def test_json_columns_round_trip(db_session: Session) -> None:
    config = {"sources": [{"type": "rss", "url": "https://e.x/ä"}], "n": 1}
    job = make_job(db_session, config=config)
    run = make_run(db_session, job, stats={"items": {"new": 3}})
    digest = make_digest(db_session, job, item_ids=[1, 2])
    notification = make_notification(db_session, job, payload={"subject": "Grüße"})
    db_session.expire_all()

    assert db_session.get(Job, job.id).config == config  # type: ignore[union-attr]
    assert db_session.get(Run, run.id).stats == {"items": {"new": 3}}  # type: ignore[union-attr]
    assert db_session.get(Digest, digest.id).item_ids == [1, 2]  # type: ignore[union-attr]
    assert db_session.get(Notification, notification.id).payload == {"subject": "Grüße"}  # type: ignore[union-attr]


def test_json_defaults(db_session: Session) -> None:
    job = make_job(db_session)
    digest = make_digest(db_session, job)
    run = make_run(db_session, job)
    db_session.expire_all()

    assert db_session.get(Digest, digest.id).item_ids == []  # type: ignore[union-attr]
    assert db_session.get(Job, job.id).config is None  # type: ignore[union-attr]
    assert db_session.get(Run, run.id).stats is None  # type: ignore[union-attr]


def test_llm_cost_round_trips_exactly(db_session: Session) -> None:
    usage = make_llm_usage(db_session, make_job(db_session), cost_usd=Decimal("0.001234"))
    db_session.expire_all()

    stored = db_session.get(LlmUsage, usage.id)
    assert stored is not None
    assert stored.cost_usd == Decimal("0.001234")
    assert stored.input_tokens == 0


@pytest.mark.parametrize("column", ["input_tokens", "output_tokens"])
def test_negative_tokens_are_rejected(db_session: Session, column: str) -> None:
    with assert_rejected():
        make_llm_usage(db_session, make_job(db_session), **{column: -1})


def test_timestamps_read_back_aware_utc(db_session: Session) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)
    db_session.expire_all()

    stored = db_session.get(Run, run.id)
    assert stored is not None
    assert stored.started_at.tzinfo == UTC
    assert db_session.get(Job, job.id).created_at.tzinfo == UTC  # type: ignore[union-attr]
