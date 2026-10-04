"""Tests for the engine factory and the declared schema."""

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.dialects import mysql
from sqlalchemy.pool import StaticPool
from sqlalchemy.schema import CreateTable

from invio.db.models import Base, Item
from invio.db.session import create_db_engine, session_factory

pytestmark = pytest.mark.db

TABLES = {"jobs", "runs", "items", "digests", "notifications", "llm_usage"}


def _sqlite_engine() -> Engine:
    return create_db_engine(
        "sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )


def test_sqlite_engine_enables_foreign_keys() -> None:
    engine = _sqlite_engine()
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
    engine.dispose()


def test_session_factory_does_not_expire_on_commit() -> None:
    engine = _sqlite_engine()

    with session_factory(engine)() as session:
        assert session.expire_on_commit is False
    engine.dispose()


def test_mysql_engine_uses_pre_ping_and_recycle() -> None:
    engine = create_db_engine("mysql+pymysql://u:p@127.0.0.1:1/db")

    assert engine.pool._pre_ping is True
    assert engine.pool._recycle == 3600
    assert engine.url.query["charset"] == "utf8mb4"
    engine.dispose()


def test_metadata_has_exactly_the_six_tables() -> None:
    assert set(Base.metadata.tables) == TABLES


@pytest.mark.parametrize("table", sorted(TABLES))
def test_every_table_has_innodb_utf8mb4_options(table: str) -> None:
    options = Base.metadata.tables[table].dialect_options["mysql"]

    assert options["engine"] == "InnoDB"
    assert options["charset"] == "utf8mb4"
    assert options["collate"] == "utf8mb4_unicode_ci"


def test_foreign_key_column_types_match_primary_keys() -> None:
    for table in Base.metadata.tables.values():
        for fk in table.foreign_keys:
            assert type(fk.parent.type) is type(fk.column.type)
            assert fk.parent.type.compile(mysql.dialect()) == fk.column.type.compile(
                mysql.dialect()
            )


def test_mysql_ddl_for_items() -> None:
    ddl = str(CreateTable(Item.__table__).compile(dialect=mysql.dialect()))  # type: ignore[arg-type]

    assert "CHAR(64)" in ddl
    assert "MEDIUMTEXT" in ddl
    assert "NUMERIC(3, 2)" in ddl  # MariaDB treats NUMERIC as DECIMAL
    assert "ENUM('new','extracted'" in ddl
    assert "ON DELETE CASCADE" in ddl


def test_url_hash_uses_binary_collation_on_mysql() -> None:
    ddl = str(CreateTable(Item.__table__).compile(dialect=mysql.dialect()))  # type: ignore[arg-type]

    assert "url_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL" in ddl
