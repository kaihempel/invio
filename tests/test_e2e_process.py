"""End-to-end smoke test of the installed ``invio`` command in a separate process.

Selected with ``-m e2e``; see specs/022-gh-issue-26/contracts/e2e-harness.md. The process gets
a clean environment, the stub Ollama server and ``tests/e2e_bootstrap`` on ``PYTHONPATH``, which
lets it reach the loopback feed.
"""

import os
import subprocess
import sys
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, update

from invio.db.models import Digest, Item, Job, Run
from invio.db.session import session_factory, session_scope
from invio.domain import RunStatus
from tests.e2e_helpers import (
    RECIPIENTS,
    E2EFeed,
    FakeOllamaServer,
    database_url_for,
    initial_articles,
    mail_texts,
    scenario_engine,
    write_job_yaml,
)
from tests.smtp_helpers import _serve

pytestmark = pytest.mark.e2e

BOOTSTRAP = Path(__file__).parent / "e2e_bootstrap"


def invio_command() -> Path:
    """The ``invio`` console script next to the interpreter that runs the tests."""
    command = Path(sys.executable).parent / "invio"
    assert command.exists(), f"{command} not found: run `uv sync`"
    return command


def bootstrap_env(tmp_path: Path) -> dict[str, str]:
    """The clean base environment of every subprocess: bootstrap on, external network blocked."""
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(tmp_path),
        "PYTHONPATH": str(BOOTSTRAP),
        "INVIO_E2E_BLOCK_EXTERNAL_NETWORK": "1",
    }


def test_e2e_process_guard_blocks_external_hosts(tmp_path: Path) -> None:
    # Proves the subprocess guard is active: the smoke test below only checks that no refusal was
    # printed, which would also hold if the bootstrap silently installed nothing.
    done = subprocess.run(
        [sys.executable, "-c", "import socket; socket.getaddrinfo('example.com', 80)"],
        env=bootstrap_env(tmp_path),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert done.returncode != 0, done.stderr
    assert "external network access blocked: example.com" in done.stderr, done.stderr


def test_e2e_installed_command_runs_scenario_a(
    tmp_path: Path,
    _server_engine: Engine | None,
    clean_jobs: None,
) -> None:
    # ``_server_engine`` and ``clean_jobs`` matter on MariaDB only (a shared database); against
    # SQLite they are a no-op.
    now = datetime.now(UTC)
    database_url = database_url_for(tmp_path)
    archive_dir = tmp_path / "archive"
    with ExitStack() as stack:
        engine = scenario_engine(stack, _server_engine, database_url)
        feed = E2EFeed(initial_articles(now))
        stack.callback(feed.stop)
        feed.start()
        ollama = FakeOllamaServer(feed.articles)
        stack.callback(ollama.stop)
        ollama.start()
        mail = stack.enter_context(_serve())
        env = bootstrap_env(tmp_path) | {
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
            # The guard also prints every refusal, so a swallowed error shows up here.
            assert "external network access blocked" not in done.stderr, done.stderr
            return done

        if _server_engine is None:
            invio("db", "upgrade")
        invio("job", "create", "--from-file", str(job_yaml), "--name", "e2e-process")
        with session_scope(session_factory(engine)) as session:
            session.execute(
                update(Job)
                .where(Job.name == "e2e-process")
                .values(next_run_at=datetime.now(UTC) - timedelta(minutes=1))
            )
        invio("run-due")

        # Delivery is awaited inside the run, so the mails are in the sink once it has exited.
        relevant = {a.title for a in feed.articles if a.relevant}
        off_topic = {a.title for a in feed.articles if not a.relevant}
        with session_scope(session_factory(engine)) as session:
            job = session.scalars(select(Job).where(Job.name == "e2e-process")).one()
            runs = session.scalars(select(Run).where(Run.job_id == job.id)).all()
            items = {
                item.title: item.id
                for item in session.scalars(select(Item).where(Item.job_id == job.id))
            }
            digests = session.scalars(select(Digest).where(Digest.job_id == job.id)).all()
            next_run_at = job.next_run_at
        assert [run.status for run in runs] == [RunStatus.SUCCEEDED]
        assert set(items) == relevant | off_topic
        assert len(digests) == 1
        assert set(digests[0].item_ids) == {items[title] for title in relevant}
        assert next_run_at is not None and next_run_at > datetime.now(UTC)
        recipients = sorted(rcpt for m in mail.handler.received for rcpt in m.rcpt_tos)
        assert recipients == sorted(RECIPIENTS)
        for recipient in RECIPIENTS:
            (text,) = mail_texts(mail, recipient)
            assert all(title in text for title in relevant), text
            assert not any(title in text for title in off_topic), text
        pages = [p for p in archive_dir.glob("e2e-process/*.html") if p.name != "index.html"]
        assert len(pages) == 1
        assert {"relevance", "summarize", "synthesize"} <= ollama.purposes()
        assert "/feed.xml" in feed.requests
