"""``run_job`` / ``run_job_by_name`` additions of #22: names, caps, result fields, stats keys.

Ctrl-C at pipeline level is covered by ``tests/test_pipeline_failures.py``
(``test_s16_cancelling_a_run_fails_it_releases_the_lock_and_reraises``).
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest
from sqlalchemy import Engine, select

from invio.db.models import Job, LlmUsage, Run
from invio.db.session import session_factory, session_scope
from invio.domain import RunStatus
from invio.pipeline import ProgressEvent, RunDeps
from invio.pipeline import run as run_module
from invio.pipeline.run import run_job, run_job_by_name
from invio.services.jobs import JobNotFoundError
from invio.sources.errors import FetchError
from tests.conftest import FakeClock
from tests.pipeline_helpers import (
    RoutedFakeProvider,
    build_env,
    make_candidates,
    make_job_config,
    relevance_reply,
    stored_runs,
)

pytestmark = pytest.mark.usefixtures("clean_jobs")

FEED = "https://example.com/feed.xml"


def _runs(engine: Engine) -> list[Run]:
    return stored_runs(session_factory(engine))


def _stored_config(engine: Engine) -> dict[str, Any]:
    with session_scope(session_factory(engine)) as session:
        return dict(session.scalars(select(Job.config)).one())


async def test_run_by_name_runs_the_stored_job(db_engine: Engine, fake_clock: FakeClock) -> None:
    env = build_env(db_engine, fake_clock)

    result = await run_job_by_name("research", dry_run=True, deps=env.deps)

    assert result.job_id == env.job_id
    assert result.dry_run is True
    assert len(_runs(db_engine)) == 1


async def test_run_by_name_without_deps_uses_the_production_wiring(
    db_engine: Engine, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = build_env(db_engine, fake_clock)
    built: list[object] = []
    seen: list[ProgressEvent] = []

    @asynccontextmanager
    async def fake_default_deps(settings: object) -> AsyncIterator[RunDeps]:
        built.append(settings)
        yield env.deps

    monkeypatch.setattr(run_module, "default_deps", fake_default_deps)

    result = await run_job_by_name("research", dry_run=True, max_items=1, observer=seen.append)

    assert len(built) == 1  # built once, not again by the inner run_job
    assert result.stats["processed"] == 1
    assert any(event.kind == "item" for event in seen)


async def test_run_by_name_rejects_an_unknown_job_without_a_run(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    env = build_env(db_engine, fake_clock)

    with pytest.raises(JobNotFoundError):
        await run_job_by_name("nope", deps=env.deps)

    assert _runs(db_engine) == []


async def test_a_non_positive_max_items_is_rejected_before_any_write(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    env = build_env(db_engine, fake_clock)

    with pytest.raises(ValueError, match="max_items must be >= 1, got 0"):
        await run_job(env.job_id, max_items=0, deps=env.deps)

    assert _runs(db_engine) == []


async def test_unpriced_calls_equal_ledger_entries_without_cost(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    env = build_env(db_engine, fake_clock)  # fast model is free, smart model priced

    result = await run_job(env.job_id, deps=env.deps)

    with session_scope(session_factory(db_engine)) as session:
        rows = list(session.scalars(select(LlmUsage)))
    unpriced = sum(1 for row in rows if row.cost_usd is None)
    assert result.stats["llm_calls_unpriced"] == unpriced
    assert result.stats["llm_calls"] == len(rows)


async def test_processed_counts_rated_and_failed_items(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    def route(title: str) -> Any:
        return RuntimeError("boom") if title == "Article 2" else relevance_reply()

    env = build_env(db_engine, fake_clock, provider=RoutedFakeProvider(relevance=route))

    result = await run_job(env.job_id, deps=env.deps)

    assert result.stats["processed"] == 3
    assert result.stats["failed"] == 1


async def test_a_run_where_every_source_fails_reports_its_error_and_times(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    env = build_env(db_engine, fake_clock, sources={FEED: FetchError("http", url=FEED, status=500)})

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert result.error == "all sources failed"
    (run,) = _runs(db_engine)
    assert result.started_at == run.started_at
    assert result.finished_at == run.finished_at
    assert result.finished_at is not None
    assert result.finished_at >= result.started_at


async def test_max_items_caps_the_taken_items_and_keeps_the_stored_config(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    env = build_env(db_engine, fake_clock, candidates=make_candidates(10))
    before = _stored_config(db_engine)

    result = await run_job(env.job_id, dry_run=True, max_items=3, deps=env.deps)

    assert 0 < result.stats["after_keyword_filter"] <= 3
    assert result.stats["processed"] <= 3
    assert _stored_config(db_engine) == before


async def test_max_items_cannot_raise_the_job_limit(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    config = make_job_config(limits__max_items_per_run=5)
    env = build_env(db_engine, fake_clock, config=config, candidates=make_candidates(10))
    before = _stored_config(db_engine)

    result = await run_job(env.job_id, dry_run=True, max_items=1000, deps=env.deps)

    assert 0 < result.stats["after_keyword_filter"] <= 5
    assert _stored_config(db_engine) == before
