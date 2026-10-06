"""Migration tests: upgrade/downgrade, idempotence, foreign tables and model drift."""

import io
import re
from collections.abc import Iterator
from pathlib import Path
from typing import get_args

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.util import CommandError
from sqlalchemy import JSON, Boolean, Engine, Enum, inspect, text
from sqlalchemy.engine import Connection

from invio.config.settings import get_settings
from invio.db.migrate import alembic_config, current_revision, downgrade, upgrade
from invio.db.models import Base
from invio.db.session import create_db_engine
from invio.domain import ItemStatus, ItemType, NotificationStatus, RunStatus
from tests.db_helpers import uses_sqlite

pytestmark = pytest.mark.db

REPO_ROOT = Path(__file__).resolve().parents[1]
APP_TABLES = {"jobs", "runs", "items", "digests", "notifications", "llm_usage"}


@pytest.fixture
def migration_engine(tmp_path: Path, _server_engine: Engine | None) -> Iterator[Engine]:
    """An empty database for migration tests.

    SQLite: a fresh temp-file database. MariaDB: the test server, downgraded to base first and
    left at head afterwards so the savepoint-isolated tests can rely on the schema.
    """
    if _server_engine is None:
        engine = create_db_engine(f"sqlite:///{tmp_path}/m.sqlite")
        yield engine
        engine.dispose()
        return
    _downgrade(_server_engine)
    try:
        yield _server_engine
    finally:
        _upgrade(_server_engine)


def _upgrade(engine: Engine, revision: str = "head") -> str:
    with engine.begin() as conn:
        return upgrade(alembic_config(connection=conn), revision)


def _downgrade(engine: Engine, revision: str = "base") -> None:
    with engine.begin() as conn:
        downgrade(alembic_config(connection=conn), revision)


def _tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_upgrade_creates_all_tables_and_reports_revision(migration_engine: Engine) -> None:
    assert _upgrade(migration_engine) == "0004"

    assert _tables(migration_engine) == APP_TABLES | {"alembic_version"}
    with migration_engine.begin() as conn:
        assert current_revision(conn) == "0004"


def test_second_upgrade_is_a_noop(migration_engine: Engine) -> None:
    _upgrade(migration_engine)

    assert _upgrade(migration_engine) == "0004"
    assert _tables(migration_engine) == APP_TABLES | {"alembic_version"}


def test_downgrade_base_leaves_only_version_table(migration_engine: Engine) -> None:
    _upgrade(migration_engine)

    _downgrade(migration_engine)

    assert _tables(migration_engine) == {"alembic_version"}
    with migration_engine.begin() as conn:
        assert current_revision(conn) is None


def test_upgrade_leaves_foreign_tables_untouched(migration_engine: Engine) -> None:
    with migration_engine.begin() as conn:
        conn.execute(text("CREATE TABLE other (id INTEGER PRIMARY KEY)"))

    try:
        _upgrade(migration_engine)
        assert "other" in _tables(migration_engine)
        _downgrade(migration_engine)

        assert _tables(migration_engine) == {"alembic_version", "other"}
    finally:
        with migration_engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS other"))


def _item_rows(engine: Engine) -> list[tuple[str, str, str | None, int, str | None]]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT url_hash, status, last_error, attempts, run_id FROM items ORDER BY id")
        )
        return [(r[0], r[1], r[2], r[3], None if r[4] is None else str(r[4])) for r in rows]


def test_0003_moves_baseline_items_to_own_status_and_back(migration_engine: Engine) -> None:
    _upgrade(migration_engine, "0002")
    now = "2026-10-05 10:00:00"
    with migration_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (id, name, enabled, created_at, updated_at)"
                " VALUES (1, 'j', 1, :now, :now)"
            ),
            {"now": now},
        )
        conn.execute(
            text(
                "INSERT INTO runs (id, job_id, status, started_at) VALUES (7, 1, 'succeeded', :now)"
            ),
            {"now": now},
        )
        for n, status, error in (
            (1, "skipped_irrelevant", "baseline"),
            (2, "skipped_irrelevant", None),
            (3, "failed", "baseline"),
        ):
            conn.execute(
                text(
                    "INSERT INTO items (job_id, run_id, url, url_hash, type, title, status,"
                    " attempts, last_error, created_at, updated_at)"
                    " VALUES (1, 7, :url, :hash, 'article', 't', :status, 2, :error, :now, :now)"
                ),
                {
                    "url": f"https://e.com/{n}",
                    "hash": str(n) * 64,
                    "status": status,
                    "error": error,
                    "now": now,
                },
            )
    before = _item_rows(migration_engine)

    _upgrade(migration_engine)

    assert _item_rows(migration_engine) == [
        ("1" * 64, "skipped_baseline", None, 2, "7"),
        ("2" * 64, "skipped_irrelevant", None, 2, "7"),
        ("3" * 64, "failed", "baseline", 2, "7"),
    ]
    _downgrade(migration_engine, "0002")
    assert _item_rows(migration_engine) == before
    assert _db_enum_values(migration_engine, "items", "status") == (
        {s.value for s in ItemStatus} - {"skipped_baseline"}
    )
    with migration_engine.begin() as conn:
        conn.execute(text("DELETE FROM items"))
        conn.execute(text("DELETE FROM runs"))
        conn.execute(text("DELETE FROM jobs"))


def _is_reflection_noise(diff: object) -> bool:
    """On MariaDB, JSON reflects as LONGTEXT and Boolean as TINYINT(1); ignore those diffs."""
    if uses_sqlite():
        return False
    entries = diff if isinstance(diff, list) else [diff]
    for entry in entries:
        if not (isinstance(entry, tuple) and entry[0] == "modify_type"):
            return False
        _op, _schema, _table, _column, _kw, _existing_type, model_type = entry
        if not isinstance(model_type, JSON | Boolean):
            return False
    return True


def test_migration_matches_models(migration_engine: Engine) -> None:
    _upgrade(migration_engine)

    with migration_engine.connect() as conn:
        context = MigrationContext.configure(conn, opts={"compare_type": True})
        diff = compare_metadata(context, Base.metadata)

    assert [d for d in diff if not _is_reflection_noise(d)] == []


def test_migration_has_the_same_checks_and_foreign_key_rules_as_models(
    migration_engine: Engine,
) -> None:
    _upgrade(migration_engine)

    insp = inspect(migration_engine)
    db_checks: set[str] = set()
    for table in Base.metadata.tables.values():
        db_checks |= {str(c["name"]) for c in insp.get_check_constraints(table.name)}
        model_fks = {(fk.parent.name, fk.ondelete) for fk in table.foreign_keys}
        db_fks = {
            (fk["constrained_columns"][0], fk["options"].get("ondelete"))
            for fk in insp.get_foreign_keys(table.name)
        }
        assert db_fks == model_fks, table.name
    explicit = {
        "ck_items_relevance_range",
        "ck_items_attempts_non_negative",
        "ck_llm_usage_input_tokens_non_negative",
        "ck_llm_usage_output_tokens_non_negative",
    }
    enum_checks = {
        "ck_runs_run_status",
        "ck_items_item_status",
        "ck_items_item_type",
        "ck_notifications_notification_status",
    }
    if uses_sqlite():
        assert db_checks == explicit | enum_checks
    else:
        # Native ENUMs emit no CHECK and MariaDB adds auto-named json_valid() checks.
        assert explicit <= db_checks


def _db_enum_values(engine: Engine, table: str, column: str) -> set[str]:
    """Return the values the migrated database accepts for an enum column."""
    insp = inspect(engine)
    if uses_sqlite():
        enum_type = Base.metadata.tables[table].c[column].type
        assert isinstance(enum_type, Enum)
        (check,) = (
            c
            for c in insp.get_check_constraints(table)
            if c["name"] == f"ck_{table}_{enum_type.name}"
        )
        return set(re.findall(r"'([^']*)'", check["sqltext"]))
    (col,) = (c for c in insp.get_columns(table) if c["name"] == column)
    assert isinstance(col["type"], Enum)
    return set(col["type"].enums)


@pytest.mark.parametrize(
    ("table", "column", "values"),
    [
        ("items", "type", set(get_args(ItemType))),
        ("items", "status", {s.value for s in ItemStatus}),
        ("runs", "status", {s.value for s in RunStatus}),
        ("notifications", "status", {s.value for s in NotificationStatus}),
    ],
)
def test_migration_enum_values_match_domain(
    migration_engine: Engine, table: str, column: str, values: set[str]
) -> None:
    """compare_metadata ignores enum value lists; check them against the domain types."""
    _upgrade(migration_engine)

    assert _db_enum_values(migration_engine, table, column) == values


def test_upgrade_to_unknown_revision_fails(migration_engine: Engine) -> None:
    with pytest.raises(CommandError):
        _upgrade(migration_engine, "nope")


def test_upgrade_with_url_creates_its_own_engine(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/own.sqlite"

    assert upgrade(alembic_config(url=url)) == "0004"
    assert upgrade(alembic_config(url=url)) == "0004"
    downgrade(alembic_config(url=url))

    engine = create_db_engine(url)
    assert _tables(engine) == {"alembic_version"}
    engine.dispose()


def test_env_falls_back_to_database_url_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/env.sqlite")
    get_settings.cache_clear()

    command.upgrade(alembic_config(), "head")

    engine = create_db_engine(f"sqlite:///{tmp_path}/env.sqlite")
    assert _tables(engine) == APP_TABLES | {"alembic_version"}
    engine.dispose()


def test_alembic_ini_points_at_packaged_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/ini.sqlite")
    get_settings.cache_clear()

    command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")

    engine = create_db_engine(f"sqlite:///{tmp_path}/ini.sqlite")
    assert _tables(engine) >= APP_TABLES
    engine.dispose()


def test_offline_mode_renders_sql_from_url(tmp_path: Path) -> None:
    config = alembic_config(url=f"sqlite:///{tmp_path}/offline.sqlite")
    config.output_buffer = io.StringIO()

    command.upgrade(config, "head", sql=True)

    sql = config.output_buffer.getvalue()
    assert "CREATE TABLE jobs" in sql
    assert not (tmp_path / "offline.sqlite").exists()


def test_offline_mode_takes_url_from_connection(migration_engine: Engine) -> None:
    with migration_engine.connect() as conn:
        config = alembic_config(connection=conn)
        config.output_buffer = io.StringIO()

        command.upgrade(config, "head", sql=True)

    assert "CREATE TABLE items" in config.output_buffer.getvalue()


def test_offline_mode_falls_back_to_setting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/o.sqlite")
    get_settings.cache_clear()
    config = alembic_config()
    config.output_buffer = io.StringIO()

    command.upgrade(config, "head", sql=True)

    assert "CREATE TABLE runs" in config.output_buffer.getvalue()


def test_connection_type_is_sqlalchemy_connection(migration_engine: Engine) -> None:
    with migration_engine.connect() as conn:
        assert isinstance(conn, Connection)
        assert current_revision(conn) is None


def _notification_columns(engine: Engine) -> dict[str, dict[str, object]]:
    return {str(c["name"]): dict(c) for c in inspect(engine).get_columns("notifications")}


def _notification_checks(engine: Engine) -> set[str]:
    return {str(c["name"]) for c in inspect(engine).get_check_constraints("notifications")}


def test_0004_adds_attempt_columns_and_backfills_existing_rows(migration_engine: Engine) -> None:
    _upgrade(migration_engine, "0003")
    with migration_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO jobs (name, enabled, created_at, updated_at) "
                "VALUES ('j', 1, '2026-10-04 12:00:00', '2026-10-04 12:00:00')"
            )
        )
        conn.execute(
            text(
                "INSERT INTO notifications (job_id, channel, recipient, status, created_at) "
                "VALUES ((SELECT id FROM jobs WHERE name = 'j'), 'email', 'a@example.org', "
                "'pending', '2026-10-04 12:00:00')"
            )
        )
    assert "attempts" not in _notification_columns(migration_engine)

    _upgrade(migration_engine, "0004")

    columns = _notification_columns(migration_engine)
    assert columns["attempts"]["nullable"] is False
    assert str(columns["attempts"]["default"]).strip("'()") == "0"
    assert columns["last_attempt_at"]["nullable"] is True
    with migration_engine.connect() as conn:
        row = conn.execute(text("SELECT attempts, last_attempt_at FROM notifications")).one()
    assert tuple(row) == (0, None)


def test_0004_downgrade_removes_columns_and_keeps_status_check(migration_engine: Engine) -> None:
    _upgrade(migration_engine)

    _downgrade(migration_engine, "0003")

    columns = _notification_columns(migration_engine)
    assert "attempts" not in columns
    assert "last_attempt_at" not in columns
    if uses_sqlite():
        assert "ck_notifications_notification_status" in _notification_checks(migration_engine)

    _upgrade(migration_engine)

    assert {"attempts", "last_attempt_at"} <= set(_notification_columns(migration_engine))
    if uses_sqlite():
        assert "ck_notifications_notification_status" in _notification_checks(migration_engine)
