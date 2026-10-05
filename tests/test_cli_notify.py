"""CLI tests for ``invio notify retry`` (contract: contracts/cli-notify-retry.md).

The CLI builds its own engine from ``INVIO_DATABASE_URL``, so these tests use a SQLite file
database. The command runs ``asyncio.run`` itself, hence the tests are synchronous and the SMTP
server runs in its own thread.
"""

import sys
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner, Result

from invio.cli.main import create_app
from invio.config.settings import get_settings
from invio.db.migrate import alembic_config, upgrade
from invio.db.session import create_db_engine
from invio.domain import NotificationStatus
from tests.smtp_helpers import (
    SmtpServer,
    add_notification,
    closed_port,  # noqa: F401
    notification_by_id,
    rows,
    seed,
    smtp_server,  # noqa: F401
)

S = NotificationStatus
DB_PASSWORD = "db-s3cr3t"


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """A migrated file database that ``INVIO_DATABASE_URL`` points at."""
    url = f"sqlite:///{tmp_path}/notify.sqlite"
    upgrade(alembic_config(url=url))
    monkeypatch.setenv("INVIO_DATABASE_URL", url)
    engine = create_db_engine(url)
    yield engine
    engine.dispose()


def smtp_env(monkeypatch: pytest.MonkeyPatch, host: str = "127.0.0.1", port: int = 25) -> None:
    monkeypatch.setenv("INVIO_SMTP_HOST", host)
    monkeypatch.setenv("INVIO_SMTP_PORT", str(port))
    monkeypatch.setenv("INVIO_SMTP_SECURITY", "none")
    monkeypatch.setenv("INVIO_SMTP_FROM", "Invio <invio@localhost>")


def run_retry() -> Result:
    get_settings.cache_clear()
    return CliRunner().invoke(create_app(), ["notify", "retry"], env={"COLUMNS": "200"})


def failed_rows(engine: Engine, *recipients: str) -> list[int]:
    digest_id = seed(engine, to=["a@example.org"])
    return [
        add_notification(engine, digest_id, status=S.FAILED, attempts=1, recipient=recipient)
        for recipient in recipients
    ]


def test_summary_exit_0(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, smtp_server: SmtpServer
) -> None:
    failed_rows(engine, "a@example.org", "b@example.org")
    smtp_env(monkeypatch, port=smtp_server.port)

    result = run_retry()

    assert result.exit_code == 0, result.stderr
    lines = result.stdout.splitlines()
    assert len(lines) == 3
    assert lines[0].startswith("sent      #") and lines[0].endswith(" ai-news a@example.org")
    assert lines[1].startswith("sent      #") and lines[1].endswith(" ai-news b@example.org")
    assert lines[2] == "retried 2, sent 2, failed 0, given up 0"
    assert {r.status for r in rows(engine)} == {S.SENT}
    assert len(smtp_server.messages) == 2


def test_still_failing_exit_1(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    (row_id,) = failed_rows(engine, "a@example.org")
    smtp_env(monkeypatch, port=closed_port)

    result = run_retry()

    assert result.exit_code == 1
    first, summary = result.stdout.splitlines()
    assert first.startswith("failed    #")
    assert "ai-news a@example.org  " in first
    assert summary == "retried 1, sent 0, failed 1, given up 0"
    row = notification_by_id(engine, row_id)
    assert (row.status, row.attempts) == (S.FAILED, 2)


def test_given_up_exit_1(engine: Engine, monkeypatch: pytest.MonkeyPatch, closed_port: int) -> None:
    digest_id = seed(engine, to=["a@example.org"])
    add_notification(engine, digest_id, status=S.FAILED, attempts=4, recipient="c@example.org")
    smtp_env(monkeypatch, port=closed_port)

    result = run_retry()

    assert result.exit_code == 1
    first, summary = result.stdout.splitlines()
    assert first.startswith("given up  #")
    assert summary == "retried 1, sent 0, failed 0, given up 1"


def test_nothing_to_retry(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    smtp_env(monkeypatch)

    result = run_retry()

    assert result.exit_code == 0
    assert result.stdout == "nothing to retry\n"


def test_missing_smtp_exit_2(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    (row_id,) = failed_rows(engine, "a@example.org")
    monkeypatch.setenv("INVIO_SMTP_FROM", "invio@localhost")

    result = run_retry()

    assert result.exit_code == 2
    assert "Configuration error: smtp not configured: missing INVIO_SMTP_HOST" in result.stderr
    row = notification_by_id(engine, row_id)
    assert (row.status, row.attempts) == (S.FAILED, 1)


def test_missing_sender_exit_2(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_SMTP_HOST", "127.0.0.1")

    result = run_retry()

    assert result.exit_code == 2
    assert "Configuration error: smtp not configured: missing INVIO_SMTP_FROM" in result.stderr


def test_missing_database_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    smtp_env(monkeypatch)

    result = run_retry()

    assert result.exit_code == 2
    assert "Configuration error" in result.stderr


def test_unknown_security_exit_2(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    smtp_env(monkeypatch)
    monkeypatch.setenv("INVIO_SMTP_SECURITY", "tls")

    result = run_retry()

    assert result.exit_code == 2
    assert "Configuration error" in result.stderr


def test_password_not_printed(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    failed_rows(engine, "a@example.org")
    smtp_env(monkeypatch, port=closed_port)
    monkeypatch.setenv("INVIO_SMTP_USER", "bob@x")
    monkeypatch.setenv("INVIO_SMTP_PASSWORD", "s3cr3t-pw")

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("login bob@x s3cr3t-pw failed")

    result = run_retry()
    monkeypatch.setattr("invio.cli.commands.notify.retry_failed", boom)
    crashed = run_retry()

    for output in (result.stdout + result.stderr, crashed.stdout + crashed.stderr):
        assert "s3cr3t-pw" not in output
        assert "bob@x" not in output
    assert crashed.exit_code == 1
    assert "Error: RuntimeError: login *** *** failed" in crashed.stderr


def test_database_password_not_printed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{tmp_path}/{DB_PASSWORD}.sqlite"
    monkeypatch.setenv("INVIO_DATABASE_URL", f"mysql+pymysql://invio:{DB_PASSWORD}@db.invalid/x")
    smtp_env(monkeypatch)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise OperationalError("connect", {}, Exception(f"access denied {DB_PASSWORD} at {url}"))

    monkeypatch.setattr("invio.cli.commands.notify.retry_failed", boom)

    result = run_retry()

    assert result.exit_code == 1
    assert "Error: OperationalError" in result.stderr
    assert DB_PASSWORD not in result.stdout + result.stderr


def test_command_registered() -> None:
    result = CliRunner().invoke(create_app(), ["notify", "--help"], env={"COLUMNS": "200"})

    assert result.exit_code == 0
    assert "retry" in result.stdout


# --- configuration errors (exit 2) ---------------------------------------------------------------


def assert_handled_exit(result: Result, code: int) -> None:
    """The command ended through ``typer.Exit`` (no uncaught exception, so no traceback)."""
    assert result.exit_code == code, result.output
    assert isinstance(result.exception, SystemExit), repr(result.exception)
    assert "Traceback" not in result.stdout + result.stderr


@pytest.mark.parametrize(
    "url",
    [
        f"mysql+nope://u:{DB_PASSWORD}@h/db",
        f"mysql+pymysql://u:{DB_PASSWORD}@h:notaport/db",
        f"postgresql+psycopg2://u:{DB_PASSWORD}@h/db",
    ],
    ids=["unknown-dialect", "unparsable", "missing-driver"],
)
def test_unusable_database_url_exit_2(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    monkeypatch.setitem(sys.modules, "psycopg2", None)  # ImportError even if installed
    monkeypatch.setenv("INVIO_DATABASE_URL", url)
    smtp_env(monkeypatch)

    result = run_retry()

    assert_handled_exit(result, 2)
    assert result.stderr.startswith("Configuration error: ")
    assert result.stdout == ""
    assert DB_PASSWORD not in result.stdout + result.stderr


def test_settings_error_inside_the_command_exit_2(monkeypatch: pytest.MonkeyPatch) -> None:
    # The root callback normally reports invalid settings first; the command guards anyway.
    def broken() -> None:
        raise ValueError("smtp_security: Input should be 'starttls', 'ssl' or 'none'")

    monkeypatch.setattr("invio.cli.commands.notify.get_settings", broken)

    result = run_retry()

    assert_handled_exit(result, 2)
    assert result.stderr.startswith("Configuration error: smtp_security")


def test_missing_database_message(monkeypatch: pytest.MonkeyPatch) -> None:
    smtp_env(monkeypatch)

    result = run_retry()

    assert_handled_exit(result, 2)
    assert result.stderr == "Configuration error: INVIO_DATABASE_URL is not set\n"


def test_missing_smtp_exit_2_even_with_nothing_to_retry(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVIO_SMTP_FROM", "invio@localhost")

    result = run_retry()

    assert_handled_exit(result, 2)
    assert "nothing to retry" not in result.stdout


def test_blank_smtp_host_exit_2(engine: Engine, monkeypatch: pytest.MonkeyPatch) -> None:
    (row_id,) = failed_rows(engine, "a@example.org")
    smtp_env(monkeypatch, host="   ")

    result = run_retry()

    assert_handled_exit(result, 2)
    assert "missing INVIO_SMTP_HOST" in result.stderr
    assert notification_by_id(engine, row_id).attempts == 1


# --- outcomes (exit 0 / 1) ---------------------------------------------------------------------


def test_mixed_outcome_exit_1(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, smtp_server: SmtpServer
) -> None:
    smtp_server.handler.reject = {"b@example.org"}
    failed_rows(engine, "a@example.org", "b@example.org")
    smtp_env(monkeypatch, port=smtp_server.port)

    result = run_retry()

    assert_handled_exit(result, 1)
    sent, failed, summary = result.stdout.splitlines()
    assert sent.startswith("sent      #") and sent.endswith(" ai-news a@example.org")
    assert failed.startswith("failed    #")
    assert "ai-news b@example.org  SMTPRecipientsRefused" in failed
    assert "550" in failed
    assert summary == "retried 2, sent 1, failed 1, given up 0"


def test_interrupted_at_limit_given_up_then_nothing_to_retry(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, smtp_server: SmtpServer
) -> None:
    digest_id = seed(engine, to=["a@example.org"])
    row_id = add_notification(
        engine,
        digest_id,
        status=S.PENDING,
        attempts=5,
        created_at=datetime.now(UTC) - timedelta(hours=2),
        last_attempt_at=datetime.now(UTC) - timedelta(hours=2),
    )
    smtp_env(monkeypatch, port=smtp_server.port)

    first = run_retry()
    second = run_retry()

    assert_handled_exit(first, 1)
    line, summary = first.stdout.splitlines()
    assert line == (
        f"given up  #{row_id} ai-news c@example.org  interrupted: attempt limit reached"
    )
    assert summary == "retried 1, sent 0, failed 0, given up 1"
    assert smtp_server.messages == []
    assert second.exit_code == 0
    assert second.stdout == "nothing to retry\n"


def test_given_up_row_is_not_retried_by_the_next_run(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, closed_port: int
) -> None:
    digest_id = seed(engine, to=["a@example.org"])
    add_notification(engine, digest_id, status=S.FAILED, attempts=4)
    smtp_env(monkeypatch, port=closed_port)

    first = run_retry()
    second = run_retry()

    assert first.exit_code == 1
    assert (second.exit_code, second.stdout) == (0, "nothing to retry\n")


def test_unusable_payload_line_falls_back_to_job_id(
    engine: Engine, monkeypatch: pytest.MonkeyPatch, smtp_server: SmtpServer
) -> None:
    digest_id = seed(engine, to=["a@example.org"])
    row_id = add_notification(engine, digest_id, status=S.FAILED, attempts=1, payload=None)
    job_id = notification_by_id(engine, row_id).job_id
    smtp_env(monkeypatch, port=smtp_server.port)

    result = run_retry()

    assert_handled_exit(result, 1)
    line = result.stdout.splitlines()[0]
    assert line.startswith(f"failed    #{row_id} {job_id} c@example.org  ")
    assert "invalid notification payload: " in line


# --- secrets in unexpected errors ---------------------------------------------------------------


@pytest.mark.parametrize("wrapped", [False, True], ids=["plain", "sqlalchemy-orig"])
def test_database_url_in_internal_error_is_masked(
    monkeypatch: pytest.MonkeyPatch, wrapped: bool
) -> None:
    # Building the engine does not connect, so the (unreachable) server is never contacted.
    raw = f"mysql+pymysql://invio:{DB_PASSWORD}@db.invalid/x"
    normalized = f"{raw}?charset=utf8mb4"
    monkeypatch.setenv("INVIO_DATABASE_URL", raw)
    smtp_env(monkeypatch)
    text = f"cannot reach {raw} (as {normalized}), password {DB_PASSWORD}"

    def boom(*args: Any, **kwargs: Any) -> None:
        if wrapped:
            raise OperationalError("SELECT 1", {}, RuntimeError(text))
        raise RuntimeError(text)

    monkeypatch.setattr("invio.cli.commands.notify.retry_failed", boom)

    result = run_retry()

    assert_handled_exit(result, 1)
    output = result.stdout + result.stderr
    error_type = "OperationalError" if wrapped else "RuntimeError"
    assert result.stderr.startswith(f"Error: {error_type}: cannot reach *** (as ***)")
    assert "SELECT 1" not in output  # only the driver error is shown, not the statement
    assert DB_PASSWORD not in output
    assert "db.invalid" not in output
