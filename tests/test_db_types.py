"""Unit tests for UTCDateTime and the engine/URL helpers (no database server needed)."""

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, insert, select
from sqlalchemy.exc import ArgumentError, StatementError

from invio.db.session import connect_args_for, normalize_url, redact
from invio.db.types import UTCDateTime, utcnow

_table = Table("t", MetaData(), Column("id", Integer, primary_key=True), Column("ts", UTCDateTime))


def _roundtrip(value: datetime | None) -> datetime | None:
    engine = create_engine("sqlite://")
    _table.metadata.create_all(engine)
    try:
        with engine.begin() as conn:
            conn.execute(insert(_table).values(ts=value))
            return conn.execute(select(_table.c.ts)).scalar_one()
    finally:
        engine.dispose()


def test_utc_datetime_roundtrip_returns_same_instant_in_utc() -> None:
    value = datetime(2026, 10, 4, 9, 30, 1, 123456, tzinfo=timezone(timedelta(hours=2)))

    result = _roundtrip(value)

    assert result == value
    assert result is not None
    assert result.tzinfo == UTC


def test_utc_datetime_passes_none_through() -> None:
    assert _roundtrip(None) is None


def test_utc_datetime_rejects_naive_values() -> None:
    with pytest.raises((ValueError, StatementError), match="naive datetime not allowed"):
        _roundtrip(datetime(2026, 10, 4))


def test_utcnow_is_aware_utc() -> None:
    now = utcnow()

    assert now.tzinfo == UTC


@pytest.mark.parametrize(
    "raw",
    [
        "mysql+pymysql://u:p@h/db",
        "mysql+pymysql://u:p@h/db?charset=latin1",
        "mariadb+pymysql://u:p@h/db",
    ],
)
def test_normalize_url_forces_utf8mb4_on_mysql_family(raw: str) -> None:
    assert normalize_url(raw).query["charset"] == "utf8mb4"


def test_normalize_url_leaves_sqlite_unchanged() -> None:
    assert str(normalize_url("sqlite://")) == "sqlite://"


def test_normalize_url_accepts_url_objects() -> None:
    url = normalize_url("mysql+pymysql://u:p@h/db")

    assert normalize_url(url) == url


def test_normalize_url_rejects_garbage() -> None:
    with pytest.raises(ArgumentError):
        normalize_url("not a url")


def test_connect_args_for_mysql_sets_strict_mode_and_utc() -> None:
    args = connect_args_for(normalize_url("mysql+pymysql://u:p@h/db"))

    assert "STRICT_ALL_TABLES" in args["init_command"]
    assert "time_zone='+00:00'" in args["init_command"]


def test_connect_args_for_sqlite_is_empty() -> None:
    assert connect_args_for(normalize_url("sqlite://")) == {}


def test_redact_removes_password_in_all_forms() -> None:
    url = "mysql+pymysql://u:s3cret@h/db"
    text = f"boom s3cret at {url} and {normalize_url(url).render_as_string(hide_password=False)}"

    result = redact(text, url)

    assert "s3cret" not in result
    assert "***" in result


def test_redact_removes_url_encoded_password() -> None:
    url = "mysql+pymysql://u:p%40ss%2Fword@h/db"

    result = redact("failed for p%40ss%2Fword and p@ss/word", url)

    assert "p%40ss" not in result
    assert "p@ss/word" not in result


def test_redact_without_password_keeps_text() -> None:
    assert redact("hello world", "sqlite://") == "hello world"


def test_redact_with_unparsable_url_replaces_raw_string() -> None:
    assert redact("bad: not a url!", "not a url") == "bad: ***!"


def test_normalize_url_rejects_invalid_port_with_argument_error() -> None:
    with pytest.raises(ArgumentError):
        normalize_url("mysql+pymysql://u:p@h:badport/db")


def test_redact_with_invalid_port_replaces_raw_string() -> None:
    url = "mysql+pymysql://u:s3cret@h:badport/db"
    assert redact(f"failed: {url}", url) == "failed: ***"
