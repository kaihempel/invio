"""End-to-end smoke test of the installed ``invio`` command in a separate process.

Selected with ``-m e2e``; see specs/022-gh-issue-26/contracts/e2e-harness.md. The process gets
a clean environment, the stub Ollama server and ``tests/e2e_bootstrap`` on ``PYTHONPATH``, which
lets it reach the loopback feed.
"""

import os
import subprocess
import sys
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, delete, select, update

from invio.db.models import Digest, Job, Run
from invio.db.session import create_db_engine, session_factory, session_scope
from invio.domain import RunStatus
from tests.e2e_helpers import (
    RECIPIENTS,
    E2EFeed,
    FakeOllamaServer,
    database_url_for,
    initial_articles,
    write_job_yaml,
)
from tests.smtp_helpers import _serve

pytestmark = pytest.mark.e2e

BOOTSTRAP = Path(__file__).parent / "e2e_bootstrap"
MAIL_DEADLINE_SECONDS = 5.0


def invio_command() -> Path:
    """The ``invio`` console script next to the interpreter that runs the tests."""
    command = Path(sys.executable).parent / "invio"
    assert command.exists(), f"{command} not found: run `uv sync`"
    return command


def wait_until(condition: Callable[[], bool], deadline_seconds: float) -> bool:
    """Poll ``condition`` every 50 ms until it holds or the deadline passes (no sleep)."""
    pause = threading.Event()
    remaining = int(deadline_seconds / 0.05)
    while not condition() and remaining > 0:
        pause.wait(0.05)
        remaining -= 1
    return bool(condition())


def test_e2e_installed_command_runs_scenario_a(
    tmp_path: Path,
    _server_engine: Engine | None,
    clean_jobs: None,
) -> None:
    now = datetime.now(UTC)
    database_url = database_url_for(tmp_path)
    archive_dir = tmp_path / "archive"
    engine = _server_engine if _server_engine is not None else create_db_engine(database_url)
    feed = E2EFeed(initial_articles(now))
    ollama = FakeOllamaServer()
    feed.start()
    ollama.start()
    try:
        with _serve() as mail:
            env = {
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(tmp_path),
                "PYTHONPATH": str(BOOTSTRAP),
                "INVIO_DATABASE_URL": database_url,
                "INVIO_ARCHIVE_DIR": str(archive_dir),
                "INVIO_SMTP_HOST": mail.host,
                "INVIO_SMTP_PORT": str(mail.port),
                "INVIO_SMTP_SECURITY": "none",
                "INVIO_SMTP_FROM": "invio@example.test",
                "INVIO_HTTP_HOST_INTERVAL_SECONDS": "0.01",
                "INVIO_OLLAMA_BASE_URL": ollama.base_url,
                "INVIO_LOG_LEVEL": "WARNING",
            }
            if "COVERAGE_PROCESS_START" in os.environ:
                env["COVERAGE_PROCESS_START"] = os.environ["COVERAGE_PROCESS_START"]
            assert not [key for key in env if key.endswith("_API_KEY")]
            assert "INVIO_SMTP_USER" not in env
            assert "INVIO_SMTP_PASSWORD" not in env

            job_yaml = write_job_yaml(tmp_path / "job.yaml", feed_url=feed.feed_url)

            def invio(*args: str) -> subprocess.CompletedProcess[str]:
                done = subprocess.run(
                    [str(invio_command()), *args],
                    env=env,
                    cwd=tmp_path,  # not the repository: its .env must not be read
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                assert done.returncode == 0, (
                    f"invio {' '.join(args)} exited {done.returncode}\n"
                    f"stdout: {done.stdout}\nstderr: {done.stderr}"
                )
                return done

            if _server_engine is None:
                invio("db", "upgrade")
            else:
                with session_scope(session_factory(engine)) as session:
                    session.execute(delete(Job))
            invio("job", "create", "--from-file", str(job_yaml), "--name", "e2e-process")
            with session_scope(session_factory(engine)) as session:
                session.execute(
                    update(Job)
                    .where(Job.name == "e2e-process")
                    .values(next_run_at=datetime.now(UTC) - timedelta(minutes=1))
                )
            invio("run-due")

            assert wait_until(
                lambda: len(mail.handler.received) >= len(RECIPIENTS), MAIL_DEADLINE_SECONDS
            )
            with session_scope(session_factory(engine)) as session:
                runs = session.scalars(select(Run)).all()
                digests = session.scalars(select(Digest)).all()
            assert [run.status for run in runs] == [RunStatus.SUCCEEDED]
            assert len(digests) == 1
            recipients = sorted(rcpt for m in mail.handler.received for rcpt in m.rcpt_tos)
            assert recipients == sorted(RECIPIENTS)
            pages = [p for p in archive_dir.glob("e2e-process/*.html") if p.name != "index.html"]
            assert len(pages) == 1
            assert {"relevance", "summarize", "synthesize"} <= ollama.purposes()
            assert "/feed.xml" in feed.requests
    finally:
        ollama.stop()
        feed.stop()
        if _server_engine is None:
            engine.dispose()
