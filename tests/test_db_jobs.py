"""Job uniqueness, scheduling fields and the due-job lookup."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import Engine, inspect, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from invio.db.models import Job
from tests.db_helpers import make_job

pytestmark = pytest.mark.db


def test_duplicate_job_name_is_rejected(db_session: Session) -> None:
    make_job(db_session, "same")

    with pytest.raises(IntegrityError):
        make_job(db_session, "same")


def test_enabled_defaults_to_true(db_session: Session) -> None:
    job = make_job(db_session)
    db_session.refresh(job)

    assert job.enabled is True


def test_scheduling_timestamps_round_trip_as_utc(db_session: Session) -> None:
    berlin = datetime(2026, 10, 4, 7, 30, 0, 123456, tzinfo=ZoneInfo("Europe/Berlin"))
    job = make_job(db_session, next_run_at=berlin, locked_until=berlin)
    db_session.expire_all()

    stored = db_session.get(Job, job.id)
    assert stored is not None
    assert stored.next_run_at == berlin
    assert stored.locked_until == berlin
    assert stored.next_run_at is not None
    assert stored.next_run_at.utcoffset() == timedelta(0)
    assert stored.next_run_at.microsecond == 123456


def test_due_job_query_returns_enabled_due_jobs_in_order(db_session: Session) -> None:
    now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    make_job(db_session, "enabled-future", next_run_at=now + timedelta(hours=1))
    make_job(db_session, "enabled-now", next_run_at=now)
    make_job(db_session, "disabled-past", enabled=False, next_run_at=now - timedelta(hours=2))
    make_job(db_session, "enabled-past", next_run_at=now - timedelta(hours=1))
    make_job(db_session, "enabled-null")

    due = db_session.scalars(
        select(Job.name)
        .where(Job.enabled.is_(True), Job.next_run_at <= now)
        .order_by(Job.next_run_at)
    ).all()

    assert list(due) == ["enabled-past", "enabled-now"]


def test_due_index_exists(db_engine: Engine) -> None:
    indexes = {i["name"]: i["column_names"] for i in inspect(db_engine).get_indexes("jobs")}

    assert indexes["ix_jobs_enabled_next_run_at"] == ["enabled", "next_run_at"]


def test_updated_at_advances_on_modification(db_session: Session) -> None:
    job = make_job(db_session, updated_at=datetime(2020, 1, 1, tzinfo=UTC))

    job.enabled = False
    db_session.flush()
    db_session.expire_all()

    stored = db_session.get(Job, job.id)
    assert stored is not None
    assert stored.updated_at > datetime(2020, 1, 1, tzinfo=UTC)
