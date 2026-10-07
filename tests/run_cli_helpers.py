"""Fixture for the ``invio job run`` / ``invio run`` CLI tests (not shipped).

The CLI runs the real pipeline over fake ports: a routed fake provider, fixture sources and
pages, a recording notifier and the fixed ``fake_clock``. No network, no SMTP.
"""

from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select
from typer.testing import CliRunner, Result

from invio.cli import main as cli_main
from invio.cli.commands import job as job_module
from invio.cli.commands import run as run_module
from invio.config.settings import get_settings
from invio.db.models import Base, Job, Run
from invio.db.session import create_db_engine, session_scope
from invio.graph.ports import RunDeps
from invio.services.jobs import JobService
from invio.services.runs import RunService
from invio.sources.errors import FetchError
from tests.conftest import FakeClock
from tests.pipeline_helpers import (
    Env,
    build_env,
    make_candidates,
    relevance_reply,
    source_key,
)

__all__ = ["FEED_URL", "RunCli", "run_cli"]

FEED_URL = "https://example.com/feed.xml"


@dataclass
class RunCli:
    """A CLI runner wired to the real pipeline over fakes."""

    env: Env
    engine: Engine
    runner: CliRunner
    monkeypatch: pytest.MonkeyPatch
    clock: FakeClock

    def invoke(self, args: Sequence[str]) -> Result:
        return self.runner.invoke(cli_main.app, list(args), env={"COLUMNS": "200"})

    def runs(self) -> list[Run]:
        """All runs of the job, newest first."""
        with session_scope(self.env.factory) as session:
            rows = list(
                session.scalars(
                    select(Run)
                    .where(Run.job_id == self.env.job_id)
                    .order_by(Run.started_at.desc(), Run.id.desc())
                )
            )
            session.expunge_all()
            return rows

    def job_row(self) -> Job:
        with session_scope(self.env.factory) as session:
            job = session.get(Job, self.env.job_id)
            assert job is not None
            session.expunge(job)
            return job

    @property
    def notifier_calls(self) -> list[int]:
        return self.env.ports.notifier.calls

    # --- scenario set-ups: each reconfigures the fakes the stored job runs against ------------

    def fail_item_at_relevance(self, title: str = "Article 2") -> None:
        """One item raises at the relevance stage (the run ends ``partial``)."""

        def route(item_title: str) -> Any:
            return RuntimeError("boom secret") if item_title == title else relevance_reply()

        self.env.provider._routes["relevance"] = route

    def heal_items(self) -> None:
        """Every item is rated normally again."""
        self.env.provider._routes["relevance"] = relevance_reply()

    def fail_every_source(self) -> None:
        """Every source raises ``FetchError`` (the run ends ``failed``)."""
        self.env.ports.sources[source_key(FEED_URL)] = FetchError(
            "http_error", url=FEED_URL, status=500
        )

    def nothing_relevant(self) -> None:
        """Every item is rated below the minimum (no digest)."""
        self.env.provider._routes["relevance"] = relevance_reply(score=0.0)

    def with_candidates(self, count: int) -> None:
        """The source reports ``count`` candidates."""
        self.env.ports.sources[source_key(FEED_URL)] = make_candidates(count)


@pytest.fixture
def run_cli(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_clock: FakeClock
) -> Iterator[RunCli]:
    url = f"sqlite:///{tmp_path}/run.sqlite"
    engine = create_db_engine(url)
    Base.metadata.create_all(engine)
    monkeypatch.setenv("INVIO_DATABASE_URL", url)
    get_settings.cache_clear()
    env = build_env(engine, fake_clock)
    service = JobService(env.factory)

    @asynccontextmanager
    async def deps() -> AsyncIterator[RunDeps]:
        yield env.deps

    monkeypatch.setattr(job_module, "_make_service", lambda: service)
    monkeypatch.setattr(job_module, "_run_deps", deps)
    # The fixture's engine (disposed below), not a new engine per ``invio run`` invocation.
    monkeypatch.setattr(run_module, "_make_service", lambda: RunService(env.factory))
    yield RunCli(env, engine, CliRunner(), monkeypatch, fake_clock)
    engine.dispose()
