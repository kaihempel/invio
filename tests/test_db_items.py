"""Item storage: uniqueness, defaults, status vocabulary, ranges and large values."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import bindparam, text
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from invio.db.models import Item
from invio.db.types import UTCDateTime
from invio.domain import ItemStatus, url_hash
from tests.db_helpers import assert_rejected, make_item, make_job, make_run

pytestmark = [
    pytest.mark.db,
    # SQLite stores Numeric as float; SQLAlchemy warns once about it (values still round-trip).
    pytest.mark.filterwarnings(
        "ignore:Dialect sqlite\\+pysqlite does \\*not\\* support Decimal objects natively"
        ":sqlalchemy.exc.SAWarning"
    ),
]

_RAW_INSERT = text(
    "INSERT INTO items (job_id, url, url_hash, type, title, status, created_at, updated_at)"
    " VALUES (:job_id, 'https://e.x/r', :h, :type, 't', :status, :now, :now)"
).bindparams(bindparam("now", type_=UTCDateTime()))

_RAW_INSERT_NO_STATUS = text(
    "INSERT INTO items (job_id, url, url_hash, type, title, created_at, updated_at)"
    " VALUES (:job_id, 'https://e.x/r', :h, 'article', 't', :now, :now)"
).bindparams(bindparam("now", type_=UTCDateTime()))


def _now() -> datetime:
    return datetime.now(UTC)


def test_duplicate_url_hash_in_same_job_is_rejected(db_session: Session) -> None:
    job = make_job(db_session)
    make_item(db_session, job)

    with pytest.raises(IntegrityError):
        make_item(db_session, job)


def test_same_url_hash_in_two_jobs_is_accepted(db_session: Session) -> None:
    job_a = make_job(db_session, "a")
    job_b = make_job(db_session, "b")

    first = make_item(db_session, job_a)
    second = make_item(db_session, job_b)

    assert first.url_hash == second.url_hash
    assert first.id != second.id


def test_new_item_has_default_status_and_attempts(db_session: Session) -> None:
    item = make_item(db_session, make_job(db_session))
    db_session.refresh(item)

    assert item.status == ItemStatus.NEW
    assert item.attempts == 0


def test_raw_insert_gets_server_defaults(db_session: Session) -> None:
    job = make_job(db_session)

    db_session.execute(_RAW_INSERT_NO_STATUS, {"job_id": job.id, "h": url_hash("x"), "now": _now()})
    item = db_session.query(Item).one()

    assert item.status == ItemStatus.NEW
    assert item.attempts == 0


@pytest.mark.parametrize("status", list(ItemStatus))
def test_every_status_round_trips(db_session: Session, status: ItemStatus) -> None:
    item = make_item(db_session, make_job(db_session), status=status)
    db_session.expire_all()

    assert db_session.get(Item, item.id).status == status  # type: ignore[union-attr]


def test_raw_bogus_status_is_rejected(db_session: Session) -> None:
    job = make_job(db_session)

    with assert_rejected():
        db_session.execute(
            _RAW_INSERT,
            {
                "job_id": job.id,
                "h": url_hash("x"),
                "type": "article",
                "status": "bogus",
                "now": _now(),
            },
        )


def test_orm_bogus_status_is_rejected_on_flush(db_session: Session) -> None:
    job = make_job(db_session)

    with pytest.raises(StatementError):
        make_item(db_session, job, status="bogus")


def test_raw_bogus_type_is_rejected(db_session: Session) -> None:
    job = make_job(db_session)

    with assert_rejected():
        db_session.execute(
            _RAW_INSERT,
            {
                "job_id": job.id,
                "h": url_hash("x"),
                "type": "podcast",
                "status": "new",
                "now": _now(),
            },
        )


def test_orm_other_type_is_rejected(db_session: Session) -> None:
    job = make_job(db_session)

    with pytest.raises(StatementError):
        make_item(db_session, job, type="podcast")


@pytest.mark.parametrize("value", ["0.75", "0.00", "1.00"])
def test_relevance_in_range_round_trips(db_session: Session, value: str) -> None:
    item = make_item(db_session, make_job(db_session), relevance=Decimal(value))
    db_session.expire_all()

    assert db_session.get(Item, item.id).relevance == Decimal(value)  # type: ignore[union-attr]


@pytest.mark.parametrize("value", ["1.01", "-0.01"])
def test_relevance_out_of_range_is_rejected(db_session: Session, value: str) -> None:
    with assert_rejected():
        make_item(db_session, make_job(db_session), relevance=Decimal(value))


def test_negative_attempts_are_rejected(db_session: Session) -> None:
    with assert_rejected():
        make_item(db_session, make_job(db_session), attempts=-1)


def test_large_values_round_trip(db_session: Session) -> None:
    url = "https://example.com/" + "a" * 4980
    raw = "x" * 5_000_000
    error = "e" * 10_000
    item = make_item(db_session, make_job(db_session), url=url, raw_content=raw, last_error=error)
    db_session.expire_all()

    stored = db_session.get(Item, item.id)
    assert stored is not None
    assert stored.url == url
    assert len(url) == 5000
    assert stored.raw_content == raw
    assert stored.last_error == error


def test_item_may_reference_a_run(db_session: Session) -> None:
    job = make_job(db_session)
    run = make_run(db_session, job)

    item = make_item(db_session, job, run_id=run.id)

    assert item.run_id == run.id
