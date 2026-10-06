"""``run_job`` locking and parallelism: busy and expired locks, concurrent claims, bounds."""

import asyncio
import dataclasses
import logging
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select

from invio.config.job import ScheduleConfig, SourceConfig
from invio.db.models import Item, Job, Run
from invio.db.repositories import RunRepository
from invio.db.session import session_factory, session_scope
from invio.domain import Candidate, ItemStatus, RunStatus
from invio.graph import stages
from invio.graph.build import build_graph
from invio.graph.ports import RunDeps
from invio.pipeline.run import JobBusyError, RunResult, run_job
from tests.conftest import FakeClock
from tests.pipeline_helpers import (
    Env,
    FakePorts,
    RoutedFakeProvider,
    build_env,
    make_candidates,
    make_job_config,
    snapshot,
)

pytestmark = pytest.mark.usefixtures("clean_jobs")

NextRun = Callable[[ScheduleConfig, datetime], datetime]


def _runs(engine: Engine) -> list[Run]:
    with session_scope(session_factory(engine)) as session:
        rows = list(session.scalars(select(Run)))
        session.expunge_all()
        return rows


def _job(engine: Engine, job_id: int) -> Job:
    with session_scope(session_factory(engine)) as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


# --- S17: busy and expired locks --------------------------------------------------------------


async def test_s17_a_busy_job_is_refused_without_a_run_or_any_change(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    locked_until = fake_clock() + timedelta(hours=1)
    env = build_env(db_engine, fake_clock, recording_next_run, locked_until=locked_until)
    before = snapshot(env.factory, env.job_id)

    with pytest.raises(JobBusyError) as raised:
        await run_job(env.job_id, deps=env.deps)

    assert raised.value.job_id == env.job_id
    assert raised.value.locked_until == locked_until
    assert _runs(db_engine) == []
    assert snapshot(env.factory, env.job_id) == before
    assert env.ports.calls == []


async def test_s17_an_expired_lock_is_taken_over_and_released(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        locked_until=fake_clock() - timedelta(seconds=1),
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    assert _job(db_engine, env.job_id).locked_until is None


async def test_the_lock_is_held_while_the_run_is_in_flight(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    seen: list[datetime | None] = []

    class Spying(FakePorts):
        async def fetch_source(self, config: SourceConfig) -> list[Candidate]:
            seen.append(_job(db_engine, env.job_id).locked_until)
            return await super().fetch_source(config)

    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        ports=Spying({"https://example.com/feed.xml": make_candidates(1)}),
    )

    await run_job(env.job_id, deps=env.deps)

    assert seen == [fake_clock() + env.deps.lock_ttl]


# --- S18: concurrent claim --------------------------------------------------------------------


async def test_s18_two_concurrent_runs_of_one_job_run_exactly_once(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)

    outcomes = await asyncio.gather(
        run_job(env.job_id, deps=env.deps),
        run_job(env.job_id, deps=env.deps),
        return_exceptions=True,
    )

    results = [o for o in outcomes if isinstance(o, RunResult)]
    busy = [o for o in outcomes if isinstance(o, JobBusyError)]
    assert (len(results), len(busy)) == (1, 1)
    assert len(_runs(db_engine)) == 1
    # The first run is unaffected by the refused one: it completes and processes each item once.
    assert results[0].status == RunStatus.SUCCEEDED
    assert _summarized(db_engine) == 3
    assert set(env.ports.page_calls.values()) == {1}
    assert _job(db_engine, env.job_id).locked_until is None


# --- Lock lost ---------------------------------------------------------------------------------


async def test_a_foreign_lock_set_mid_run_is_kept_and_reported(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    caplog: pytest.LogCaptureFixture,
) -> None:
    foreign = fake_clock() + timedelta(days=1)

    class Overtaken(FakePorts):
        async def fetch_source(self, config: SourceConfig) -> list[Candidate]:
            # Before deduplication opens the work session: another writer takes the lock over.
            with session_scope(session_factory(db_engine)) as session:
                job = session.get(Job, env.job_id)
                assert job is not None
                job.locked_until = foreign
            return await super().fetch_source(config)

    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        ports=Overtaken({"https://example.com/feed.xml": make_candidates(2)}),
    )
    original_next_run = _job(db_engine, env.job_id).next_run_at

    with caplog.at_level(logging.WARNING, logger="invio.graph"):
        result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    job = _job(db_engine, env.job_id)
    assert job.locked_until == foreign
    assert job.next_run_at == original_next_run  # release is atomic: nothing written without it
    assert [r for r in caplog.records if r.getMessage() == "run.lock_lost"]


# --- S12: the concurrency bound ---------------------------------------------------------------
#
# Extraction runs in a worker thread, so whether N items overlap *by chance* depends on timing.
# The bound tests therefore hold every page fetch at a gate: the run is observed while all the
# items it admitted are blocked, which makes "reaches the limit" and "never more" deterministic.

PAGE_HOLD = 4


def _ten() -> dict[str, Any]:
    return {"https://example.com/feed.xml": make_candidates(10)}


class _ItemSpan:
    """Items in flight from their page fetch until their summary answer arrived."""

    def __init__(self) -> None:
        self.active: set[str] = set()
        self.max = 0

    def enter(self, key: str) -> None:
        self.active.add(key)
        self.max = max(self.max, len(self.active))

    def leave(self, key: str) -> None:
        self.active.discard(key)


class _SpanPorts(FakePorts):
    def __init__(self, span: _ItemSpan, **kwargs: Any) -> None:
        super().__init__(_ten(), **kwargs)
        self.span = span

    async def fetch_page(self, url: str) -> str:
        self.span.enter(url.rsplit("-", 1)[1])  # ".../post-7" -> "7"
        return await super().fetch_page(url)


class _SpanProvider(RoutedFakeProvider):
    def __init__(self, span: _ItemSpan) -> None:
        super().__init__(hold=PAGE_HOLD)
        self.span = span

    async def _request(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int | None
    ) -> tuple[str, Any]:
        answer = await super()._request(
            system, user, model=model, temperature=temperature, max_tokens=max_tokens
        )
        title = re.search(r"<title>Article (\d+)</title>", user)
        if self._purpose(system) == "summarize" and title is not None:
            self.span.leave(title.group(1))
        return answer


async def _run_ten(
    engine: Engine,
    clock: FakeClock,
    next_run: NextRun,
    *,
    limit: int,
    deps: Callable[[Env], RunDeps] | None = None,
    concurrency: int | None = None,
    override: int | None = None,
) -> tuple[Env, RunResult, _ItemSpan]:
    """Run 10 items with every page fetch held until ``limit`` fetches are in flight."""
    span = _ItemSpan()
    gate = asyncio.Event()
    ports = _SpanPorts(span, gate=gate, hold=PAGE_HOLD)
    reached = ports.in_flight_reached(limit)
    kwargs: dict[str, Any] = {} if concurrency is None else {"concurrency": concurrency}
    env = build_env(engine, clock, next_run, ports=ports, provider=_SpanProvider(span), **kwargs)
    run_deps = deps(env) if deps is not None else env.deps
    task = asyncio.create_task(run_job(env.job_id, deps=run_deps, concurrency=override))

    await asyncio.wait_for(reached.wait(), timeout=10)  # a hang guard, not a timing assumption
    for _ in range(50):  # give an item beyond the limit every chance to start
        await asyncio.sleep(0)
    assert ports.in_flight == limit
    assert len(span.active) == limit
    gate.set()
    result = await asyncio.wait_for(task, timeout=30)
    return env, result, span


def _summarized(engine: Engine) -> int:
    with session_scope(session_factory(engine)) as session:
        return len([i for i in session.scalars(select(Item)) if i.status == ItemStatus.SUMMARIZED])


def _without_concurrency(env: Env) -> RunDeps:
    """``env.deps`` rebuilt without ``concurrency``, so ``RunDeps``'s own default applies."""
    values = {f.name: getattr(env.deps, f.name) for f in dataclasses.fields(RunDeps)}
    del values["concurrency"]
    return RunDeps(**values)


async def test_s12_at_most_four_items_run_at_once_by_default(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env, result, span = await _run_ten(
        db_engine, fake_clock, recording_next_run, limit=4, deps=_without_concurrency
    )

    assert env.ports.max_in_flight == 4
    assert span.max == 4
    assert env.provider.max_in_flight <= 4
    assert result.status == RunStatus.SUCCEEDED
    assert _summarized(db_engine) == 10
    assert env.provider.titles_for("summarize") == {f"Article {n}": 1 for n in range(1, 11)}


async def test_s12_the_limit_is_the_configured_concurrency(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env, result, span = await _run_ten(
        db_engine, fake_clock, recording_next_run, limit=3, concurrency=3
    )

    assert env.ports.max_in_flight == 3
    assert span.max == 3
    assert env.provider.max_in_flight <= 3
    assert _summarized(db_engine) == 10
    assert result.status == RunStatus.SUCCEEDED


async def test_s12_a_concurrency_override_of_one_runs_items_one_at_a_time(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env, result, span = await _run_ten(
        db_engine, fake_clock, recording_next_run, limit=1, override=1
    )

    assert env.ports.max_in_flight == 1
    assert span.max == 1
    assert env.provider.max_in_flight == 1
    assert _summarized(db_engine) == 10
    assert result.status == RunStatus.SUCCEEDED


async def test_the_semaphore_bounds_items_without_langgraphs_max_concurrency(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    ports = FakePorts(_ten(), hold=PAGE_HOLD)
    env = build_env(db_engine, fake_clock, recording_next_run, ports=ports, concurrency=2)
    with session_scope(env.factory) as session:
        job = session.get(Job, env.job_id)
        assert job is not None
        run = RunRepository(session).start(env.job_id)
        run_id, token = run.id, fake_clock() + env.deps.lock_ttl
        job.locked_until = token
    graph, scope = build_graph(
        env.deps, job_id=env.job_id, run_id=run_id, token=token, dry_run=False
    )
    scope.job_name = "research"

    await graph.ainvoke(
        {"job_id": env.job_id, "run_id": run_id, "dry_run": False, "items": [], "errors": []}
    )

    assert ports.max_in_flight == 2
    assert _summarized(db_engine) == 10


async def test_sources_are_fetched_in_parallel_up_to_the_limit(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    urls = [f"https://example.com/feed-{n}.xml" for n in range(6)]
    config = make_job_config()
    config["sources"] = [{"type": "rss", "url": url} for url in urls]
    sources = {
        url: make_candidates(1, prefix=f"https://example.com/s{n}") for n, url in enumerate(urls)
    }
    ports = FakePorts(sources, hold=PAGE_HOLD)
    env = build_env(
        db_engine, fake_clock, recording_next_run, config=config, ports=ports, concurrency=3
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert ports.max_source_in_flight == 3
    assert result.stats["sources"] == 6
    assert result.stats["found"] == 6


async def test_source_results_keep_the_configuration_order(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    urls = [f"https://example.com/feed-{n}.xml" for n in range(4)]
    config = make_job_config()
    config["sources"] = [{"type": "rss", "url": url} for url in urls]
    sources = {
        url: make_candidates(1, prefix=f"https://example.com/s{n}") for n, url in enumerate(urls)
    }

    class Reversed(FakePorts):
        """Later sources answer first."""

        async def fetch_source(self, config: SourceConfig) -> list[Candidate]:
            index = urls.index(str(getattr(config, "url")))  # noqa: B009
            for _ in range((len(urls) - index) * 3):
                await asyncio.sleep(0)
            return await super().fetch_source(config)

    ports = Reversed(sources)
    env = build_env(
        db_engine, fake_clock, recording_next_run, config=config, ports=ports, concurrency=4
    )
    seen: list[list[str]] = []
    original = stages.deduplicate

    async def spy(state: Any, deps: Any, scope: Any) -> Any:
        seen.append(list(state["candidates"]))
        return await original(state, deps, scope)

    monkeypatch.setattr(stages, "deduplicate", spy)
    await run_job(env.job_id, deps=env.deps)

    assert seen == [["0:rss", "1:rss", "2:rss", "3:rss"]]
