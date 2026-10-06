"""``run_job`` failure paths: bad items, bad sources, error hygiene, budget stops, finalization."""

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import Engine, select

from invio.config.job import JobConfig, ScheduleConfig
from invio.db.models import Digest, Item, Job, LlmUsage, Run
from invio.db.session import session_factory, session_scope
from invio.domain import ItemStatus, RunStatus
from invio.graph.state import RunError, RunStage
from invio.llm.base import LLMInvalidRequestError, LLMUnavailableError
from invio.pipeline.run import run_job
from invio.sources.errors import FetchError, TooLargeError
from invio.sources.extract import MAX_INPUT_CHARS
from tests.conftest import FakeClock
from tests.db_helpers import make_item
from tests.pipeline_helpers import (
    Env,
    RoutedFakeProvider,
    build_env,
    make_candidates,
    make_job_config,
    relevance_reply,
)

pytestmark = pytest.mark.usefixtures("clean_jobs")

NextRun = Callable[[ScheduleConfig, datetime], datetime]
FEED_1 = "https://example.com/feed.xml"
FEED_2 = "https://example.com/second.xml"


def _rows[T](engine: Engine, model: type[T]) -> list[T]:
    with session_scope(session_factory(engine)) as session:
        rows = list(session.scalars(select(model).order_by(model.id)))  # type: ignore[attr-defined]
        session.expunge_all()
        return rows


def _by_title(engine: Engine) -> dict[str, Item]:
    return {item.title: item for item in _rows(engine, Item)}


def _job(engine: Engine, job_id: int) -> Job:
    with session_scope(session_factory(engine)) as session:
        job = session.get(Job, job_id)
        assert job is not None
        session.expunge(job)
        return job


def _run_row(engine: Engine) -> Run:
    (run,) = _rows(engine, Run)
    return run


def _schedule(env: Env) -> ScheduleConfig:
    return JobConfig.model_validate(env.config).schedule


# --- US2: one bad item never aborts the run -----------------------------------------------------


async def test_s5_one_failing_item_leaves_the_others_in_the_digest(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    pages = {"https://example.com/post-2": TooLargeError(url="https://example.com/post-2", limit=1)}
    env = build_env(db_engine, fake_clock, recording_next_run, pages=pages)

    result = await run_job(env.job_id, deps=env.deps)

    items = _by_title(db_engine)
    assert items["Article 1"].status == ItemStatus.SUMMARIZED
    assert items["Article 3"].status == ItemStatus.SUMMARIZED
    assert items["Article 2"].status == ItemStatus.FAILED
    assert (items["Article 2"].last_error or "").startswith("TooLargeError:")
    (digest,) = _rows(db_engine, Digest)
    assert sorted(digest.item_ids) == sorted([items["Article 1"].id, items["Article 3"].id])
    assert result.status == RunStatus.PARTIAL
    assert result.errors == (
        RunError(stage="extract_text", error_class="TooLargeError", item_id=items["Article 2"].id),
    )
    assert _run_row(db_engine).status == RunStatus.PARTIAL


async def test_s5b_an_unexpected_error_fails_only_that_item(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    def route(title: str) -> Any:
        return RuntimeError("boom secret") if title == "Article 2" else relevance_reply()

    env = build_env(
        db_engine, fake_clock, recording_next_run, provider=RoutedFakeProvider(relevance=route)
    )

    result = await run_job(env.job_id, deps=env.deps)

    items = _by_title(db_engine)
    assert items["Article 2"].status == ItemStatus.FAILED
    assert (items["Article 2"].last_error or "").startswith("RuntimeError:")
    assert "boom secret" not in (items["Article 2"].last_error or "")
    assert items["Article 1"].status == ItemStatus.SUMMARIZED
    assert items["Article 3"].status == ItemStatus.SUMMARIZED
    assert result.status == RunStatus.PARTIAL
    assert result.errors == (
        RunError(
            stage="score_relevance", error_class="RuntimeError", item_id=items["Article 2"].id
        ),
    )


async def test_an_error_while_summarizing_is_reported_for_that_stage(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    def route(title: str) -> Any:
        return KeyError("x") if title == "Article 3" else None

    provider = RoutedFakeProvider(summarize=lambda title: route(title) or _summary())
    env = build_env(db_engine, fake_clock, recording_next_run, provider=provider)

    result = await run_job(env.job_id, deps=env.deps)

    failed = _by_title(db_engine)["Article 3"]
    assert failed.status == ItemStatus.FAILED
    assert result.errors == (
        RunError(stage="summarize_item", error_class="KeyError", item_id=failed.id),
    )
    assert result.stats["failed"] == 1
    assert result.status == RunStatus.PARTIAL


def _summary() -> Any:
    from tests.pipeline_helpers import summary_reply

    return summary_reply()


async def test_s6_every_item_failing_fails_the_run(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    provider = RoutedFakeProvider(
        relevance=LLMInvalidRequestError("bad", provider="fakeco", model="m", status=400)
    )
    env = build_env(db_engine, fake_clock, recording_next_run, provider=provider)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    run = _run_row(db_engine)
    assert (run.status, run.error) == (RunStatus.FAILED, "all attempted items failed")
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())
    assert {e.stage for e in result.errors} == {"score_relevance"}
    assert {e.error_class for e in result.errors} == {"LLMInvalidRequestError"}
    assert len(result.errors) == 3
    assert _rows(db_engine, Digest) == []
    assert env.ports.notifier.calls == []


async def test_s7_no_error_text_leaks_secrets_query_strings_or_document_text(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret_feed = f"{FEED_2}?token=abc"
    config = make_job_config()
    config["sources"] = [
        {"type": "rss", "url": FEED_1},
        {"type": "rss", "url": secret_feed},
    ]
    page_url = "https://example.com/post-2"
    leaky_page = "CONFIDENTIAL BODY " + "x" * MAX_INPUT_CHARS
    pages = {
        page_url: leaky_page,
        "https://example.com/post-3": FetchError(
            "timeout", url="https://example.com/post-3?token=abc"
        ),
    }
    sources = {
        FEED_1: make_candidates(4),  # Article 4 stays healthy, so the run is partial, not failed
        secret_feed: FetchError("http_error", url=secret_feed, status=500),
    }

    def route(title: str) -> Any:
        if title == "Article 1":
            return LLMUnavailableError("secret-token-123 leaked by the provider")
        return relevance_reply()

    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        config=config,
        sources=sources,
        pages=pages,
        provider=RoutedFakeProvider(relevance=route),
    )

    with caplog.at_level(logging.DEBUG):
        result = await run_job(env.job_id, deps=env.deps)

    texts = [repr(result.errors), _run_row(db_engine).error or ""]
    texts += [item.last_error or "" for item in _rows(db_engine, Item)]
    texts.append(caplog.text)
    assert any(item.status == ItemStatus.FAILED for item in _rows(db_engine, Item))
    for text in texts:
        for marker in ("secret-token-123", "token=abc", "CONFIDENTIAL BODY"):
            assert marker not in text
    assert result.status == RunStatus.PARTIAL


async def test_s26_a_budget_stop_releases_the_unprocessed_items(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config(limits__max_llm_tokens_per_run=20)
    env = build_env(db_engine, fake_clock, recording_next_run, config=config, concurrency=1)

    result = await run_job(env.job_id, deps=env.deps)

    items = _by_title(db_engine)
    assert items["Article 1"].status == ItemStatus.SUMMARIZED
    for title in ("Article 2", "Article 3"):
        assert items[title].status == ItemStatus.NEW
        assert items[title].attempts == 0
    assert result.status == RunStatus.PARTIAL
    assert result.stats["budget_exceeded"] is True
    (digest,) = _rows(db_engine, Digest)
    assert digest.item_ids == [items["Article 1"].id]
    assert result.errors == ()


# --- US2: source failures -----------------------------------------------------------------------


def _two_sources() -> dict[str, Any]:
    config = make_job_config()
    config["sources"] = [{"type": "rss", "url": FEED_1}, {"type": "rss", "url": FEED_2}]
    return config


async def test_s21_one_failing_source_is_partial_and_the_other_is_used(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    sources = {
        FEED_1: make_candidates(2),
        FEED_2: FetchError("http_error", url=FEED_2, status=500),
    }
    env = build_env(
        db_engine, fake_clock, recording_next_run, config=_two_sources(), sources=sources
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.PARTIAL
    assert (result.stats["sources"], result.stats["sources_failed"]) == (2, 1)
    (digest,) = _rows(db_engine, Digest)
    assert len(digest.item_ids) == 2
    assert result.errors == (
        RunError(stage="fetch_sources", error_class="FetchError", source="1:rss"),
    )


async def test_s22_all_sources_failing_fails_the_run_but_finalizes(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    sources = {
        FEED_1: FetchError("http_error", url=FEED_1, status=500),
        FEED_2: FetchError("timeout", url=FEED_2),
    }
    env = build_env(
        db_engine, fake_clock, recording_next_run, config=_two_sources(), sources=sources
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    run = _run_row(db_engine)
    assert (run.status, run.error) == (RunStatus.FAILED, "all sources failed")
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())
    assert len(result.errors) == 2


async def test_pending_items_are_processed_even_when_every_source_fails(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    sources = {FEED_1: FetchError("timeout", url=FEED_1)}
    env = build_env(db_engine, fake_clock, recording_next_run, sources=sources)
    with session_scope(env.factory) as session:
        job = session.get(Job, env.job_id)
        assert job is not None
        make_item(session, job, url="https://example.com/old", title="Old pending")

    result = await run_job(env.job_id, deps=env.deps)

    assert _by_title(db_engine)["Old pending"].status == ItemStatus.SUMMARIZED
    assert result.status == RunStatus.FAILED
    assert _run_row(db_engine).error == "all sources failed"


# --- US5: finalization always happens -----------------------------------------------------------

_SQL_MARKER = "SECRET_SQL_MARKER"


def _stats_keys_of_a_recovered_run() -> set[str]:
    return {
        "version",
        "llm_calls",
        "input_tokens",
        "output_tokens",
        "tokens",
        "estimated_cost_usd",
        "cost_complete",
        "budget_limit",
        "budget_exceeded",
        "over_budget",
    }


async def test_s13_a_raising_stage_fails_the_run_and_still_finalizes(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        sources={FEED_1: RuntimeError("boom secret")},
    )

    with caplog.at_level(logging.DEBUG):
        result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    run = _run_row(db_engine)
    assert (run.status, run.error) == (RunStatus.FAILED, "RuntimeError: run failed")
    assert set(run.stats or {}) == _stats_keys_of_a_recovered_run()
    job = _job(db_engine, env.job_id)
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())
    assert job.locked_until is None
    assert result.errors == (RunError(stage="fetch_sources", error_class="RuntimeError"),)
    assert result.errors[0].source is None
    assert env.ports.notifier.calls == []
    assert "boom secret" not in caplog.text


async def test_s14_a_failed_save_is_recovered_with_the_usage_replayed(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sqlalchemy.exc import IntegrityError

    from invio.db.repositories import DigestRepository

    def boom(*args: object, **kwargs: object) -> None:
        raise IntegrityError(f"INSERT {_SQL_MARKER}", {"body": _SQL_MARKER}, Exception(_SQL_MARKER))

    monkeypatch.setattr(DigestRepository, "add", boom)
    env = build_env(db_engine, fake_clock, recording_next_run)

    with caplog.at_level(logging.DEBUG):
        result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert result.errors == (RunError(stage="persist", error_class="IntegrityError"),)
    run = _run_row(db_engine)
    assert (run.status, run.error) == (RunStatus.FAILED, "IntegrityError: run failed")
    assert len(_rows(db_engine, LlmUsage)) == len(env.provider.calls) == 7
    assert _rows(db_engine, Digest) == []
    for item in _rows(db_engine, Item):  # back to the committed state after deduplication
        assert (item.status, item.attempts, item.raw_content) == (ItemStatus.NEW, 1, None)
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())
    assert env.ports.notifier.calls == []
    assert _SQL_MARKER not in caplog.text


async def test_s15_a_failed_delivery_lowers_the_run_to_partial(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.graph.ports import DeliveryReport
    from tests.pipeline_helpers import FakePorts, RecordingNotifier

    ports = FakePorts(
        {FEED_1: make_candidates(3)},
        notifier=RecordingNotifier(DeliveryReport(sent=0, failed=1, error=None)),
    )
    env = build_env(db_engine, fake_clock, recording_next_run, ports=ports)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.PARTIAL
    assert _run_row(db_engine).status == RunStatus.PARTIAL
    assert (result.notifications_sent, result.notifications_failed) == (0, 1)
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())


async def test_a_notifier_that_raises_is_a_partial_run_not_a_lost_one(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from tests.pipeline_helpers import FakePorts, RecordingNotifier

    class Exploding(RecordingNotifier):
        async def __call__(self, digest_id: int) -> Any:
            raise RuntimeError("smtp exploded")

    ports = FakePorts({FEED_1: make_candidates(3)}, notifier=Exploding())
    env = build_env(db_engine, fake_clock, recording_next_run, ports=ports)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.PARTIAL
    assert _run_row(db_engine).status == RunStatus.PARTIAL
    assert result.errors == (RunError(stage="notify", error_class="RuntimeError"),)
    assert len(_rows(db_engine, Digest)) == 1
    assert _job(db_engine, env.job_id).locked_until is None


async def test_a_notifier_that_raises_midway_reports_the_mails_already_sent(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.domain import NotificationStatus
    from tests.db_helpers import make_notification
    from tests.pipeline_helpers import FakePorts, RecordingNotifier

    class HalfWay(RecordingNotifier):
        async def __call__(self, digest_id: int) -> Any:
            with session_scope(session_factory(db_engine)) as session:
                digest = session.get(Digest, digest_id)
                assert digest is not None
                job = session.get(Job, digest.job_id)
                assert job is not None
                for status in (NotificationStatus.SENT, NotificationStatus.PENDING):
                    make_notification(
                        session, job, run_id=digest.run_id, digest_id=digest_id, status=status
                    )
            raise RuntimeError("smtp died after the first recipient")

    ports = FakePorts({FEED_1: make_candidates(3)}, notifier=HalfWay())
    env = build_env(db_engine, fake_clock, recording_next_run, ports=ports)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.PARTIAL
    assert (result.notifications_sent, result.notifications_failed) == (1, 1)


async def test_a_run_that_outlives_its_lock_stops_before_the_next_stage(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)
    fetch_source = env.deps.fetch_source

    async def slow_fetch(config: Any) -> Any:
        fake_clock.advance(env.deps.lock_ttl)  # the lock expires while the source loads
        return await fetch_source(config)

    deps = dataclasses.replace(env.deps, fetch_source=slow_fetch)

    result = await run_job(env.job_id, deps=deps)

    assert result.status == RunStatus.FAILED
    assert result.errors == (RunError(stage="deduplicate", error_class="LockExpiredError"),)
    assert (_run_row(db_engine).error or "").startswith("LockExpiredError:")
    assert _rows(db_engine, Item) == []  # deduplicate never ran
    assert env.provider.calls == []
    assert env.ports.notifier.calls == []
    assert _job(db_engine, env.job_id).locked_until is None  # nobody took it: still ours


async def test_a_lock_that_expires_during_the_items_fails_the_run(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)
    fetch_page = env.deps.fetch_page

    async def slow_page(url: str) -> str:
        fake_clock.advance(env.deps.lock_ttl)
        return await fetch_page(url)

    deps = dataclasses.replace(env.deps, fetch_page=slow_page)

    result = await run_job(env.job_id, deps=deps)

    assert result.status == RunStatus.FAILED
    assert {e.error_class for e in result.errors} == {"LockExpiredError"}
    assert env.provider.calls == []  # no item reached score_relevance
    assert _rows(db_engine, Digest) == []
    assert env.ports.notifier.calls == []
    assert _job(db_engine, env.job_id).locked_until is None


async def test_an_auth_error_is_run_fatal_and_marks_no_item_failed(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.llm.base import LLMAuthError

    provider = RoutedFakeProvider(relevance=LLMAuthError("bad key", provider="fakeco"))
    env = build_env(db_engine, fake_clock, recording_next_run, provider=provider)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    run = _run_row(db_engine)
    assert (run.error or "").startswith("LLMAuthError:")
    assert all(item.status != ItemStatus.FAILED for item in _rows(db_engine, Item))
    assert {e.error_class for e in result.errors} == {"LLMAuthError"}
    assert _job(db_engine, env.job_id).locked_until is None
    assert env.ports.notifier.calls == []


async def test_s16_cancelling_a_run_fails_it_releases_the_lock_and_reraises(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    import asyncio

    from tests.pipeline_helpers import FakePorts

    ports = FakePorts({FEED_1: make_candidates(3)}, gate=asyncio.Event())
    env = build_env(db_engine, fake_clock, recording_next_run, ports=ports)
    task = asyncio.create_task(run_job(env.job_id, deps=env.deps))
    await asyncio.wait_for(ports.entered.wait(), timeout=10)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    run = _run_row(db_engine)
    assert run.status == RunStatus.FAILED
    assert run.error == "CancelledError: run failed"
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())
    for item in _rows(db_engine, Item):  # the work session was rolled back
        assert (item.status, item.raw_content) == (ItemStatus.NEW, None)


# --- Safety net and entry point edges -----------------------------------------------------------


async def test_a_failing_run_start_releases_the_lock_and_reraises(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from invio.db.repositories import RunRepository

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("cannot start")

    monkeypatch.setattr(RunRepository, "start", boom)
    env = build_env(db_engine, fake_clock, recording_next_run)
    original_next_run = _job(db_engine, env.job_id).next_run_at

    with pytest.raises(RuntimeError, match="cannot start"):
        await run_job(env.job_id, deps=env.deps)

    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == original_next_run
    assert _rows(db_engine, Run) == []


async def test_an_error_inside_finalize_is_caught_by_the_safety_net(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from invio.graph import stages

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("release exploded")

    monkeypatch.setattr(stages, "release_lock", boom)  # only the call inside finalize
    env = build_env(db_engine, fake_clock, recording_next_run)

    with pytest.raises(RuntimeError, match="release exploded"):
        await run_job(env.job_id, deps=env.deps)

    job = _job(db_engine, env.job_id)
    assert job.locked_until is None  # the safety net released it
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())
    # The run was saved ``succeeded``; failing to release the lock does not downgrade it.
    assert _run_row(db_engine).status == RunStatus.SUCCEEDED


async def test_a_failing_release_in_the_safety_net_is_logged_and_the_original_error_wins(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio

    from invio.pipeline import run as run_module
    from tests.pipeline_helpers import FakePorts

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("db gone")

    monkeypatch.setattr(run_module, "release_lock", boom)
    monkeypatch.setattr(run_module, "record_failure", boom)
    ports = FakePorts({FEED_1: make_candidates(3)}, gate=asyncio.Event())
    env = build_env(db_engine, fake_clock, recording_next_run, ports=ports)
    task = asyncio.create_task(run_job(env.job_id, deps=env.deps))
    await asyncio.wait_for(ports.entered.wait(), timeout=10)

    with caplog.at_level(logging.ERROR, logger="invio.pipeline"):
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    records = [r for r in caplog.records if r.getMessage() == "run.release_failed"]
    assert len(records) == 1
    assert records[0].error == "OSError"  # type: ignore[attr-defined]
    assert "db gone" not in caplog.text


async def test_run_job_builds_the_production_deps_when_none_are_given(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contextlib import asynccontextmanager

    from invio.pipeline import run as run_module

    env = build_env(db_engine, fake_clock, recording_next_run)
    built: list[object] = []

    @asynccontextmanager
    async def fake_default_deps(settings: object) -> Any:
        built.append(settings)
        yield env.deps

    monkeypatch.setattr(run_module, "default_deps", fake_default_deps)

    result = await run_job(env.job_id, concurrency=2)

    assert result.status == RunStatus.SUCCEEDED
    assert len(built) == 1


# --- More failure edges -------------------------------------------------------------------------


async def test_a_disabled_source_is_not_fetched_or_counted(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config()
    config["sources"] = [
        {"type": "rss", "url": FEED_1},
        {"type": "rss", "url": FEED_2, "enabled": False},
    ]
    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        config=config,
        sources={FEED_1: make_candidates(1), FEED_2: make_candidates(1, prefix="https://x.y/p")},
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert [c[1] for c in env.ports.calls if c[0] == "source"] == [FEED_1]
    assert result.stats["sources"] == 1


async def test_a_fallback_digest_lowers_the_run_to_partial(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    provider = RoutedFakeProvider(
        synthesize=LLMUnavailableError("down", provider="fakeco", model="m")
    )
    env = build_env(db_engine, fake_clock, recording_next_run, provider=provider)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.PARTIAL
    assert _run_row(db_engine).status == RunStatus.PARTIAL
    (digest,) = _rows(db_engine, Digest)
    assert len(digest.item_ids) == 3
    assert env.ports.notifier.calls == [digest.id]


async def test_an_unreadable_schedule_keeps_next_run_at_and_still_releases_the_lock(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config(schedule={"frequency": "sometimes"})
    env = build_env(db_engine, fake_clock, recording_next_run, config=config)
    original = _job(db_engine, env.job_id).next_run_at

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == original


async def test_a_config_that_is_not_a_mapping_still_releases_the_lock(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run, config={})
    with session_scope(env.factory) as session:
        job = session.get(Job, env.job_id)
        assert job is not None
        job.config = None  # type: ignore[assignment]

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert _run_row(db_engine).error is not None
    assert _job(db_engine, env.job_id).locked_until is None


async def test_a_raising_prefilter_routes_to_finalize_without_starting_items(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from invio.graph import stages

    async def boom(state: Any, deps: Any, scope: Any) -> Any:
        raise RuntimeError("prefilter bug")

    monkeypatch.setattr(stages, "keyword_prefilter", boom)
    env = build_env(db_engine, fake_clock, recording_next_run)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert result.errors == (RunError(stage="keyword_prefilter", error_class="RuntimeError"),)
    assert env.ports.page_calls == {}
    assert env.provider.calls == []
    assert _job(db_engine, env.job_id).locked_until is None


async def test_items_that_have_not_started_do_nothing_once_the_run_is_failing(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.llm.base import LLMAuthError

    provider = RoutedFakeProvider(relevance=LLMAuthError("bad key", provider="fakeco"))
    env = build_env(db_engine, fake_clock, recording_next_run, provider=provider, concurrency=1)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert len(provider.requests_for("relevance")) == 1
    assert sum(env.ports.page_calls.values()) == 1
    assert len(result.errors) == 1


async def test_a_database_error_inside_an_item_is_run_fatal(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sqlalchemy.exc import OperationalError

    from invio.db.repositories import ItemRepository

    def boom(*args: object, **kwargs: object) -> None:
        raise OperationalError("UPDATE items", {}, Exception(_SQL_MARKER))

    monkeypatch.setattr(ItemRepository, "set_extracted", boom)
    env = build_env(db_engine, fake_clock, recording_next_run, concurrency=1)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert _run_row(db_engine).error == "OperationalError: run failed"
    assert {e.stage for e in result.errors} == {"extract_text"}
    assert all(item.status != ItemStatus.FAILED for item in _rows(db_engine, Item))
    assert _job(db_engine, env.job_id).locked_until is None


def test_a_failing_rollback_of_the_work_session_is_logged_not_raised(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from datetime import UTC

    from invio.graph.scope import RunScope
    from invio.graph.stages import rollback_work_session

    class Broken:
        closed = False

        def rollback(self) -> None:
            raise OSError("connection lost")

        def close(self) -> None:
            self.closed = True

    session = Broken()
    scope = RunScope(
        job_id=1,
        run_id=2,
        token=datetime(2026, 1, 1, tzinfo=UTC),
        dry_run=False,
        semaphore=asyncio.Semaphore(1),
    )
    scope.session = session  # type: ignore[assignment]

    with caplog.at_level(logging.ERROR, logger="invio.graph"):
        rollback_work_session(scope)

    assert session.closed and scope.session is None
    assert [r for r in caplog.records if r.getMessage() == "run.rollback_failed"]
    rollback_work_session(scope)  # nothing left to roll back


# --- SC-003: every raising stage leaves ``running``, releases the lock and sets the next run ---

GUARDED_STAGES: list[tuple[RunStage, RunStatus]] = [
    ("load_job", RunStatus.FAILED),
    ("fetch_sources", RunStatus.FAILED),
    ("deduplicate", RunStatus.FAILED),
    ("keyword_prefilter", RunStatus.FAILED),
    ("synthesize_digest", RunStatus.FAILED),
    ("persist", RunStatus.FAILED),
    ("notify", RunStatus.PARTIAL),  # after the save: the stored run is lowered, not failed
]


def _raising_stage(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    from invio.graph import stages

    async def boom(state: Any, deps: Any, scope: Any) -> Any:
        raise RuntimeError(f"{stage} bug with secret text")

    monkeypatch.setattr(stages, stage, boom)


@pytest.mark.parametrize(("stage", "status"), GUARDED_STAGES, ids=[s for s, _ in GUARDED_STAGES])
async def test_sc003_any_raising_stage_is_finalized(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    stage: RunStage,
    status: RunStatus,
) -> None:
    _raising_stage(monkeypatch, stage)
    env = build_env(db_engine, fake_clock, recording_next_run)

    with caplog.at_level(logging.DEBUG):
        result = await run_job(env.job_id, deps=env.deps)

    run = _run_row(db_engine)
    assert run.status != RunStatus.RUNNING
    assert (result.status, run.status) == (status, status)
    assert run.finished_at is not None
    assert RunError(stage=stage, error_class="RuntimeError") in result.errors
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())
    assert "secret text" not in caplog.text
    assert "secret text" not in (run.error or "")
    if status is RunStatus.FAILED:
        assert run.error == "RuntimeError: run failed"
        assert env.ports.notifier.calls == []


@pytest.mark.parametrize(("stage", "status"), GUARDED_STAGES, ids=[s for s, _ in GUARDED_STAGES])
async def test_sc003_a_raising_stage_in_a_dry_run_keeps_next_run_at(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
    stage: RunStage,
    status: RunStatus,
) -> None:
    from tests.pipeline_helpers import snapshot

    _raising_stage(monkeypatch, stage)
    env = build_env(db_engine, fake_clock, recording_next_run)
    before = snapshot(env.factory, env.job_id)

    result = await run_job(env.job_id, dry_run=True, deps=env.deps)

    run = _run_row(db_engine)
    assert run.status != RunStatus.RUNNING
    assert result.status == run.status
    assert snapshot(env.factory, env.job_id) == before  # incl. next_run_at and locked_until
    assert env.ports.notifier.calls == []


async def test_an_auth_error_during_synthesis_fails_the_run_without_a_fallback(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.llm.base import LLMAuthError

    provider = RoutedFakeProvider(synthesize=LLMAuthError("bad key", provider="fakeco"))
    env = build_env(db_engine, fake_clock, recording_next_run, provider=provider)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    run = _run_row(db_engine)
    assert run.status == RunStatus.FAILED
    assert (run.error or "").startswith("LLMAuthError:")
    assert result.errors == (RunError(stage="synthesize_digest", error_class="LLMAuthError"),)
    assert _rows(db_engine, Digest) == []
    assert env.ports.notifier.calls == []
    assert len(provider.requests_for("synthesize")) == 1  # not retried
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())


async def test_one_failed_source_and_all_items_failed_is_failed_not_partial(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    sources = {
        FEED_1: make_candidates(2),
        FEED_2: FetchError("http_error", url=FEED_2, status=500),
    }
    provider = RoutedFakeProvider(
        relevance=LLMInvalidRequestError("bad", provider="fakeco", model="m", status=400)
    )
    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        config=_two_sources(),
        sources=sources,
        provider=provider,
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert _run_row(db_engine).error == "all attempted items failed"
    assert (result.stats["sources"], result.stats["sources_failed"]) == (2, 1)
    assert RunError(stage="fetch_sources", error_class="FetchError", source="1:rss") in (
        result.errors
    )
    assert _job(db_engine, env.job_id).locked_until is None
