"""CLI tests for ``invio db upgrade``."""

import json
import logging
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from invio.cli.main import app
from invio.db.migrate import current_revision
from invio.db.session import create_db_engine

APP_TABLES = {"jobs", "runs", "items", "digests", "notifications", "llm_usage"}

runner = CliRunner()


def test_upgrade_creates_schema_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/cli.sqlite")

    first = runner.invoke(app, ["db", "upgrade"])
    second = runner.invoke(app, ["db", "upgrade"])

    assert first.exit_code == 0, first.stderr
    assert first.stdout == "database at revision 0001\n"
    assert second.exit_code == 0
    assert second.stdout == "database at revision 0001\n"


def test_upgrade_accepts_explicit_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/cli.sqlite")

    result = runner.invoke(app, ["db", "upgrade", "0001"])

    assert result.exit_code == 0
    assert result.stdout == "database at revision 0001\n"


def test_missing_url_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("INVIO_DATABASE_URL", raising=False)

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 2
    assert "INVIO_DATABASE_URL is not set" in result.stderr


def test_unparsable_url_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "not-a-url")

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 2
    assert result.stderr.startswith("Configuration error:")
    assert "invalid database URL" in result.stderr


def test_unknown_dialect_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "nosuchdb://u:s3cret@h/x")

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 2
    assert "Configuration error:" in result.stderr
    assert "s3cret" not in result.stderr


def test_unwritable_sqlite_path_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "sqlite:////nonexistent-dir/x.sqlite")

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 1
    assert "Error:" in result.stderr


def test_unknown_revision_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/cli.sqlite")

    result = runner.invoke(app, ["db", "upgrade", "nope"])

    assert result.exit_code == 1
    assert "Error:" in result.stderr


def test_unreachable_server_exits_1_without_leaking_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "mysql+pymysql://invio:s3cret@127.0.0.1:1/x")
    caplog.set_level(logging.DEBUG)

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 1
    assert "s3cret" not in result.stdout
    assert "s3cret" not in result.stderr
    assert all("s3cret" not in record.getMessage() for record in caplog.records)
    assert all("s3cret" not in str(record.__dict__) for record in caplog.records)


def test_help_lists_db_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "db" in result.stdout


def test_unexpected_error_exits_1_and_is_redacted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "mysql+pymysql://invio:s3cret@127.0.0.1:1/x")

    def boom(*_args: object, **_kwargs: object) -> str:
        raise ValueError("kaputt for mysql+pymysql://invio:s3cret@127.0.0.1:1/x")

    monkeypatch.setattr("invio.db.migrate.upgrade", boom)

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 1
    assert "Error: ValueError: kaputt" in result.stderr
    assert "s3cret" not in result.stderr
    assert "s3cret" not in result.stdout


# --- QA additions: contract cli-db.md (exit codes, output streams, redaction) -------------


def _app_tables(url: str) -> set[str]:
    engine = create_db_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _error_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if not line.startswith("{")]


def test_upgrade_creates_every_table_at_latest_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path}/cli.sqlite"
    monkeypatch.setenv("INVIO_DATABASE_URL", url)

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 0, result.stderr
    assert _app_tables(url) == APP_TABLES | {"alembic_version"}
    engine = create_db_engine(url)
    with engine.connect() as conn:
        assert current_revision(conn) == "0001"
    engine.dispose()


def test_upgrade_logs_start_and_finish_as_json_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/cli.sqlite")

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 0, result.stderr
    records = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    messages = [r["message"] for r in records]
    assert messages[0] == "migration started"
    assert messages[-1] == "migration finished"
    assert records[-1]["revision"] == "0001"
    assert result.stdout.count("\n") == 1


def test_upgrade_leaves_foreign_tables_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path}/cli.sqlite"
    engine = create_db_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE other (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO other (id) VALUES (42)"))
    monkeypatch.setenv("INVIO_DATABASE_URL", url)

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 0, result.stderr
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM other")).scalar_one() == 42
    engine.dispose()


def test_empty_url_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "")

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 2
    assert "Configuration error: INVIO_DATABASE_URL is not set" in result.stderr
    assert result.stdout == ""


def test_unknown_driver_exits_2_without_leaking_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "mysql+nosuchdriver://u:s3cret@h/db")

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 2
    assert "Configuration error: invalid database URL" in result.stderr
    assert "s3cret" not in result.stderr + result.stdout


def test_url_with_invalid_port_exits_2(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "mysql+pymysql://invio:s3cret@db:badport/x")

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 2
    assert result.stderr.startswith("Configuration error: invalid database URL")
    assert "s3cret" not in result.stderr + result.stdout


def test_failure_writes_nothing_to_stdout_and_one_error_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "sqlite:////nonexistent-dir/x.sqlite")

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 1
    assert result.stdout == ""
    errors = _error_lines(result.stderr)
    assert len(errors) == 1
    assert errors[0].startswith("Error: OperationalError:")


def test_unreachable_server_does_not_leak_url_encoded_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "mysql+pymysql://invio:p%40ss%2Fw0rd@127.0.0.1:1/x")
    caplog.set_level(logging.DEBUG)

    result = runner.invoke(app, ["db", "upgrade"])

    output = result.stdout + result.stderr + "".join(str(r.__dict__) for r in caplog.records)
    assert result.exit_code == 1
    for secret in ("p%40ss%2Fw0rd", "p@ss/w0rd"):
        assert secret not in output


def test_success_with_password_url_never_shows_password(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "mysql+pymysql://invio:s3cret@127.0.0.1:1/x")
    caplog.set_level(logging.DEBUG)
    seen: list[URL] = []

    def fake_upgrade(config: Config, revision: str = "head") -> str:
        seen.append(config.attributes["url"])
        return "0001"

    monkeypatch.setattr("invio.db.migrate.upgrade", fake_upgrade)

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "database at revision 0001\n"
    output = result.stdout + result.stderr + "".join(str(r.__dict__) for r in caplog.records)
    assert "s3cret" not in output
    # FR-003: the production connection requests utf8mb4; the URL never enters config options.
    assert seen[0].query["charset"] == "utf8mb4"


def test_sqlalchemy_error_message_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = "mysql+pymysql://invio:s3cret@127.0.0.1:1/x"
    monkeypatch.setenv("INVIO_DATABASE_URL", raw)

    def boom(*_args: object, **_kwargs: object) -> str:
        raise OperationalError("SELECT 1", {}, Exception(f"auth failed for {raw} (s3cret)"))

    monkeypatch.setattr("invio.db.migrate.upgrade", boom)

    result = runner.invoke(app, ["db", "upgrade"])

    assert result.exit_code == 1
    assert "Error: OperationalError: auth failed for ***" in result.stderr
    assert "s3cret" not in result.stderr


def test_db_group_help_lists_upgrade() -> None:
    result = runner.invoke(app, ["db", "--help"])

    assert result.exit_code == 0
    assert "upgrade" in result.stdout
    assert "INVIO_DATABASE_URL" in result.stdout
