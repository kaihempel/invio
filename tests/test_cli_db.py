"""CLI tests for ``invio db upgrade`` (contract: specs/002-gh-issue-4/contracts/cli-db.md)."""

import json
import logging
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner, Result

from invio.cli.main import app
from invio.db.migrate import current_revision
from invio.db.session import create_db_engine

APP_TABLES = {"jobs", "runs", "items", "digests", "notifications", "llm_usage"}

runner = CliRunner()


def _upgrade(*args: str) -> Result:
    return runner.invoke(app, ["db", "upgrade", *args])


def _app_tables(url: str) -> set[str]:
    engine = create_db_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _error_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if not line.startswith("{")]


def _all_output(result: Result, caplog: pytest.LogCaptureFixture | None = None) -> str:
    records = caplog.records if caplog is not None else []
    return result.stdout + result.stderr + "".join(str(r.__dict__) for r in records)


# --- success ------------------------------------------------------------------------------


def test_upgrade_creates_every_table_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{tmp_path}/cli.sqlite"
    monkeypatch.setenv("INVIO_DATABASE_URL", url)

    first = _upgrade()
    second = _upgrade()

    assert first.exit_code == 0, first.stderr
    assert first.stdout == "database at revision 0002\n"
    assert second.exit_code == 0, second.stderr
    assert second.stdout == "database at revision 0002\n"
    assert _app_tables(url) == APP_TABLES | {"alembic_version"}
    engine = create_db_engine(url)
    with engine.connect() as conn:
        assert current_revision(conn) == "0002"
    engine.dispose()


def test_upgrade_accepts_explicit_revision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/cli.sqlite")

    result = _upgrade("0001")

    assert result.exit_code == 0
    assert result.stdout == "database at revision 0001\n"


def test_upgrade_logs_start_and_finish_as_json_on_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/cli.sqlite")

    result = _upgrade()

    assert result.exit_code == 0, result.stderr
    records = [json.loads(line) for line in result.stderr.splitlines() if line.strip()]
    messages = [r["message"] for r in records]
    assert messages[0] == "migration started"
    assert messages[-1] == "migration finished"
    assert records[-1]["revision"] == "0002"
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

    result = _upgrade()

    assert result.exit_code == 0, result.stderr
    with engine.connect() as conn:
        assert conn.execute(text("SELECT id FROM other")).scalar_one() == 42
    engine.dispose()


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

    result = _upgrade()

    assert result.exit_code == 0, result.stderr
    assert result.stdout == "database at revision 0001\n"
    assert "s3cret" not in _all_output(result, caplog)
    # FR-003: the production connection requests utf8mb4; the URL never enters config options.
    assert seen[0].query["charset"] == "utf8mb4"


# --- configuration errors: exit 2 ---------------------------------------------------------


@pytest.mark.parametrize("value", [None, ""], ids=["unset", "empty"])
def test_missing_url_exits_2(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    if value is None:
        monkeypatch.delenv("INVIO_DATABASE_URL", raising=False)
    else:
        monkeypatch.setenv("INVIO_DATABASE_URL", value)

    result = _upgrade()

    assert result.exit_code == 2
    assert "Configuration error: INVIO_DATABASE_URL is not set" in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "nosuchdb://u:s3cret@h/x",
        "mysql+nosuchdriver://u:s3cret@h/db",
        "mysql+pymysql://invio:s3cret@db:badport/x",
    ],
    ids=["unparsable", "unknown-dialect", "unknown-driver", "invalid-port"],
)
def test_invalid_url_exits_2_without_leaking_password(
    monkeypatch: pytest.MonkeyPatch, url: str
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", url)

    result = _upgrade()

    assert result.exit_code == 2
    assert result.stderr.startswith("Configuration error: invalid database URL")
    assert "s3cret" not in _all_output(result)


def test_driver_not_installed_exits_2_without_leaking_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "mssql+pyodbc://u:s3cret@h/x")

    result = _upgrade()

    assert result.exit_code == 2
    assert result.stderr.startswith("Configuration error: database driver not installed")
    assert "s3cret" not in _all_output(result)


# --- runtime errors: exit 1 ---------------------------------------------------------------


def test_unwritable_sqlite_path_exits_1_with_one_error_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "sqlite:////nonexistent-dir/x.sqlite")

    result = _upgrade()

    assert result.exit_code == 1
    assert result.stdout == ""
    errors = _error_lines(result.stderr)
    assert len(errors) == 1
    assert errors[0].startswith("Error: OperationalError:")


def test_unknown_revision_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/cli.sqlite")

    result = _upgrade("nope")

    assert result.exit_code == 1
    assert "Error:" in result.stderr


@pytest.mark.parametrize(
    ("password", "secrets"),
    [("s3cret", ["s3cret"]), ("p%40ss%2Fw0rd", ["p%40ss%2Fw0rd", "p@ss/w0rd"])],
    ids=["plain", "url-encoded"],
)
def test_unreachable_server_exits_1_without_leaking_password(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    password: str,
    secrets: list[str],
) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", f"mysql+pymysql://invio:{password}@127.0.0.1:1/x")
    caplog.set_level(logging.DEBUG)

    result = _upgrade()

    assert result.exit_code == 1
    output = _all_output(result, caplog)
    for secret in secrets:
        assert secret not in output


def test_unexpected_error_exits_1_and_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_DATABASE_URL", "mysql+pymysql://invio:s3cret@127.0.0.1:1/x")

    def boom(*_args: object, **_kwargs: object) -> str:
        raise ValueError("kaputt for mysql+pymysql://invio:s3cret@127.0.0.1:1/x")

    monkeypatch.setattr("invio.db.migrate.upgrade", boom)

    result = _upgrade()

    assert result.exit_code == 1
    assert "Error: ValueError: kaputt" in result.stderr
    assert "s3cret" not in _all_output(result)


def test_sqlalchemy_error_message_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = "mysql+pymysql://invio:s3cret@127.0.0.1:1/x"
    monkeypatch.setenv("INVIO_DATABASE_URL", raw)

    def boom(*_args: object, **_kwargs: object) -> str:
        raise OperationalError("SELECT 1", {}, Exception(f"auth failed for {raw} (s3cret)"))

    monkeypatch.setattr("invio.db.migrate.upgrade", boom)

    result = _upgrade()

    assert result.exit_code == 1
    assert "Error: OperationalError: auth failed for ***" in result.stderr
    assert "s3cret" not in result.stderr


# --- help ---------------------------------------------------------------------------------


def test_help_lists_db_command() -> None:
    result = runner.invoke(app, ["--help"])

    assert result.exit_code == 0
    assert "db" in result.stdout


def test_db_group_help_lists_upgrade() -> None:
    result = runner.invoke(app, ["db", "--help"])

    assert result.exit_code == 0
    assert "upgrade" in result.stdout
    assert "INVIO_DATABASE_URL" in result.stdout
