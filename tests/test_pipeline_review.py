"""Behaviour fixed after the code review of #21: boundaries, finalization and lock handling."""

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.exc import OperationalError, PendingRollbackError

from invio.config.job import ScheduleConfig
from invio.db.models import Digest, Item, Job, Run
from invio.db.repositories import DigestRepository, ItemRepository, RunRepository
from invio.db.session import session_factory, session_scope
from invio.domain import RunStatus
from invio.llm.base import LLMAuthError, LLMUnavailableError
from invio.pipeline.run import run_job
from invio.sources.errors import FetchError, TooLargeError
from tests.conftest import FakeClock
from tests.db_helpers import make_digest, make_job, make_run
from tests.pipeline_helpers import (
    Attempts,
    FakePorts,
    RoutedFakeProvider,
    build_env,
    make_candidates,
    make_job_config,
    relevance_reply,
    settle,
)

pytestmark = pytest.mark.usefixtures("clean_jobs")

NextRun = Callable[[ScheduleConfig, datetime], datetime]
FEED = "https://example.com/feed.xml"
FEED_2 = "https://example.com/second.xml"


def _rows[T](engine: Engine, model: type[T]) -> list[T]:
    with session_scope(session_factory(engine)) as session:
        rows = list(session.scalars(select(model).order_by(model.id)))  # type: ignore[attr-defined]
        session.expunge_all()
        return rows


def _job(engine: Engine, job_id: int) -> Job:
    with session_scope(session_factory(engine)) as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def _run(engine: Engine) -> Run:
    (run,) = _rows(engine, Run)
    return run


# --- 2: the item boundary survives a broken session ---


async def test_a_failing_mark_failed_makes_the_run_fail_instead_of_escaping(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken(*args: object, **kwargs: object) -> None:
        raise PendingRollbackError("session needs a rollback")

    monkeypatch.setattr(ItemRepository, "mark_failed", broken)
    pages = {"https://example.com/post-2": TooLargeError(url="https://example.com/post-2", limit=1)}
    env = build_env(db_engine, fake_clock, recording_next_run, pages=pages)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert _run(db_engine).error == "PendingRollbackError: run failed"
    assert _job(db_engine, env.job_id).locked_until is None
    assert any(e.stage == "extract_text" for e in result.errors)


async def test_an_item_failing_after_the_run_failed_is_not_marked(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_failed = asyncio.Event()
    marked: list[int] = []

    def db_error(*args: object, **kwargs: object) -> None:
        first_failed.set()
        raise OperationalError("UPDATE items", {}, Exception("x"))

    real_mark = ItemRepository.mark_failed

    def recording_mark(self: ItemRepository, item: Item, error: str) -> None:
        marked.append(item.id)
        real_mark(self, item, error)

    monkeypatch.setattr(ItemRepository, "set_extracted", db_error)
    monkeypatch.setattr(ItemRepository, "mark_failed", recording_mark)

    class Ordered(FakePorts):
        async def fetch_page(self, url: str) -> str:
            if url.endswith("post-2"):
                await first_failed.wait()
                await settle()  # let the first item's boundary finish
                raise TooLargeError(url=url, limit=1)
            return await super().fetch_page(url)

    ports = Ordered({FEED: make_candidates(2)})
    env = build_env(db_engine, fake_clock, recording_next_run, ports=ports, concurrency=2)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert marked == []


# --- 7: in-flight items stop once the run is failing ---


async def test_in_flight_items_stop_before_their_next_stage_when_the_run_fails(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    failed = asyncio.Event()

    def relevance(title: str) -> Any:
        failed.set()
        return LLMAuthError("bad key", provider="fakeco")

    class Waiting(FakePorts):
        async def fetch_page(self, url: str) -> str:
            if url.endswith("post-2"):
                await failed.wait()
                await settle()  # the failing item's boundary has recorded the failure
            return await super().fetch_page(url)

    provider = RoutedFakeProvider(
        relevance=lambda title: relevance(title) if title == "Article 1" else relevance_reply()
    )
    ports = Waiting({FEED: make_candidates(2)})
    env = build_env(
        db_engine, fake_clock, recording_next_run, ports=ports, provider=provider, concurrency=2
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert provider.titles_for("relevance") == {"Article 1": 1}


# --- 3: setup failures and release failures ---


async def test_a_failing_graph_build_fails_the_run_and_releases_the_lock(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from invio.pipeline import run as run_module

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cannot build")

    monkeypatch.setattr(run_module, "build_graph", boom)
    env = build_env(db_engine, fake_clock, recording_next_run)

    with pytest.raises(RuntimeError, match="cannot build"):
        await run_job(env.job_id, deps=env.deps)

    run = _run(db_engine)
    assert (run.status, run.error) == (RunStatus.FAILED, "RuntimeError: run failed")
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    # Like every other failure, the safety net schedules the next run from the stored config.
    assert job.next_run_at == fake_clock() + timedelta(hours=1)


async def test_a_failing_next_run_computation_keeps_the_run_succeeded(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(schedule: ScheduleConfig, after: datetime) -> datetime:
        raise ValueError("secret schedule detail")

    env = build_env(db_engine, fake_clock, broken)
    original = _job(db_engine, env.job_id).next_run_at

    with caplog.at_level(logging.WARNING, logger="invio.graph"):
        result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    assert _run(db_engine).status == RunStatus.SUCCEEDED
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == original
    (record,) = [r for r in caplog.records if r.getMessage() == "run.next_run_failed"]
    assert record.error == "ValueError"  # type: ignore[attr-defined]
    assert "secret schedule detail" not in caplog.text


async def test_a_failing_release_after_a_successful_save_does_not_downgrade_the_run(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from invio.graph import stages

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("db gone")

    monkeypatch.setattr(stages, "release_lock", boom)  # only the call inside finalize
    env = build_env(db_engine, fake_clock, recording_next_run)

    with pytest.raises(OSError):
        await run_job(env.job_id, deps=env.deps)

    assert _run(db_engine).status == RunStatus.SUCCEEDED
    assert _job(db_engine, env.job_id).locked_until is None


# --- 4: a failed run stores no empty digest and sends no mail ---


async def test_a_run_failed_by_its_sources_stores_no_empty_digest_and_sends_nothing(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config(notification__send_if_empty=True)
    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        config=config,
        sources={FEED: FetchError("timeout", url=FEED)},
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert _rows(db_engine, Digest) == []
    assert env.ports.notifier.calls == []


async def test_a_run_failed_by_its_sources_does_not_mail_a_digest_of_pending_items(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from tests.db_helpers import make_item

    env = build_env(
        db_engine, fake_clock, recording_next_run, sources={FEED: FetchError("timeout", url=FEED)}
    )
    with session_scope(env.factory) as session:
        job = session.get(Job, env.job_id)
        assert job is not None
        make_item(session, job, url="https://example.com/old", title="Old pending")

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert env.ports.notifier.calls == []


# --- 5: targeted digest lookup ---


def test_get_for_run_finds_the_digest_of_a_run(db_session: Any) -> None:
    job = make_job(db_session)
    run_a, run_b = make_run(db_session, job), make_run(db_session, job)
    digest = make_digest(db_session, job, run_id=run_b.id)
    repo = DigestRepository(db_session)

    assert repo.get_for_run(run_b.id) is digest
    assert repo.get_for_run(run_a.id) is None


# --- 6: the fallback-digest downgrade is part of the save ---


async def test_a_fallback_digest_is_saved_partial_in_one_write(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finishes: list[RunStatus] = []
    real = RunRepository.finish

    def counting(self: RunRepository, run: Run, status: RunStatus, **kw: Any) -> Run:
        finishes.append(status)
        return real(self, run, status, **kw)

    monkeypatch.setattr(RunRepository, "finish", counting)
    provider = RoutedFakeProvider(
        synthesize=LLMUnavailableError("down", provider="fakeco", model="m")
    )
    env = build_env(db_engine, fake_clock, recording_next_run, provider=provider)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.PARTIAL
    assert finishes == [RunStatus.PARTIAL]


def test_decide_status_lowers_a_fallback_digest_to_partial(db_session: Any) -> None:
    from invio.graph.budget import BudgetTracker
    from invio.graph.nodes.persist import RunDraft, StageCounts, decide_status

    job = make_job(db_session)
    base = RunDraft(
        job_id=job.id,
        run_id=1,
        counts=StageCounts(found=0, new=0, after_keyword_filter=0),
        taken=[],
        relevance=[],
        summaries=[],
        digest=None,
        budget=BudgetTracker(10),
    )

    assert decide_status(base) == RunStatus.SUCCEEDED
    assert decide_status(dataclasses.replace(base, fallback_digest=True)) == RunStatus.PARTIAL


# --- 8: the semaphore is not held while a retry waits ---


async def test_a_source_waiting_to_retry_does_not_hold_the_concurrency_slot(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config()
    config["sources"] = [{"type": "rss", "url": FEED}, {"type": "rss", "url": FEED_2}]
    sources = {
        FEED: Attempts(FetchError("timeout", url=FEED), make_candidates(1)),
        FEED_2: make_candidates(1, prefix="https://example.com/b"),
    }
    ports = FakePorts(sources)
    env = build_env(
        db_engine, fake_clock, recording_next_run, config=config, ports=ports, concurrency=1
    )

    async def sleep(seconds: float) -> None:
        # Only returns once the second source was fetched: needs the slot to be free.
        async with asyncio.timeout(2):
            while not any(c == ("source", FEED_2) for c in ports.calls):
                await asyncio.sleep(0.001)

    deps = dataclasses.replace(env.deps, sleep=sleep)

    result = await run_job(env.job_id, deps=deps)

    assert result.stats["sources_failed"] == 0
    assert result.stats["found"] == 2


# --- low: explicit error classes, cleanup ---


async def test_node_level_failures_carry_the_error_class_explicitly(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.llm.base import LLMInvalidRequestError

    provider = RoutedFakeProvider(
        relevance=LLMInvalidRequestError("bad", provider="fakeco", model="m", status=400)
    )
    env = build_env(db_engine, fake_clock, recording_next_run, provider=provider)

    result = await run_job(env.job_id, deps=env.deps)

    assert {e.error_class for e in result.errors} == {"LLMInvalidRequestError"}
    outcomes = [r for r in result.errors]
    assert len(outcomes) == 3
