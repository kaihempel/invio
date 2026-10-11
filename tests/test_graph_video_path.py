"""The ``video_path`` node: transcripts, permanent and transient failures, deferral (#29)."""

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, select

from invio.config.job import ScheduleConfig
from invio.db.models import Item, Job
from invio.db.session import session_factory, session_scope
from invio.domain import ItemStatus, RunStatus
from invio.pipeline.run import run_job
from invio.sources.errors import FetchError, TranscriptionDeferred, TranscriptUnavailable
from tests.conftest import FakeClock
from tests.db_helpers import make_item
from tests.pipeline_helpers import (
    Attempts,
    Env,
    FakePorts,
    FakeTranscripts,
    build_env,
    make_candidates,
    make_job_config,
    transcript_of,
    video,
    watch,
)

pytestmark = pytest.mark.usefixtures("clean_jobs")

NextRun = Callable[[ScheduleConfig, datetime], datetime]
TEXT = "spoken words of the video"


def _items(engine: Engine) -> dict[str, Item]:
    with session_scope(session_factory(engine)) as session:
        rows = list(session.scalars(select(Item)))
        session.expunge_all()
        return {row.title: row for row in rows}


def _env(
    engine: Engine,
    clock: FakeClock,
    next_run: NextRun,
    transcripts: FakeTranscripts,
    candidates: list[Any],
    **kwargs: Any,
) -> Env:
    ports = FakePorts({"https://example.com/feed.xml": candidates}, transcripts=transcripts)
    return build_env(engine, clock, next_run, ports=ports, **kwargs)


def _unavailable(video_id: str, description: str | None) -> TranscriptUnavailable:
    return TranscriptUnavailable(
        "whisper_not_installed", url=watch(video_id), description=description
    )


# --- (a) a transcript becomes the content ---------------------------------------------------


async def test_a_transcript_is_the_content_and_the_page_is_not_fetched(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    transcripts = FakeTranscripts({watch("vid1"): transcript_of(TEXT)})
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, [video(1)])

    result = await run_job(env.job_id, deps=env.deps)

    item = _items(db_engine)["Video 1"]
    assert item.raw_content == TEXT
    assert item.status == ItemStatus.SUMMARIZED
    assert env.ports.page_calls == {}
    (rating,) = env.provider.requests_for("relevance")
    assert TEXT in rating.user
    assert result.status == RunStatus.SUCCEEDED
    assert result.stats["summarized"] == 1


# --- (b) permanent unavailability: rated from title + description ---------------------------


async def test_a_permanent_failure_rates_from_the_description(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    caplog: pytest.LogCaptureFixture,
) -> None:
    transcripts = FakeTranscripts({watch("vid1"): _unavailable("vid1", "About X")})
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, [video(1)])

    with caplog.at_level(logging.INFO, logger="invio.graph"):
        result = await run_job(env.job_id, deps=env.deps)

    item = _items(db_engine)["Video 1"]
    assert item.raw_content is None
    assert item.teaser == "About X"
    assert item.status != ItemStatus.FAILED
    assert result.status == RunStatus.SUCCEEDED
    (rating,) = env.provider.requests_for("relevance")
    assert "About X" in rating.user
    (record,) = [r for r in caplog.records if r.getMessage() == "extract.transcript_unavailable"]
    assert record.__dict__["reason"] == "whisper_not_installed"
    assert record.__dict__["item_id"] == item.id


async def test_an_existing_teaser_is_not_overwritten(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    transcripts = FakeTranscripts({watch("vid1"): _unavailable("vid1", "About X")})
    env = _env(
        db_engine, fake_clock, recording_next_run, transcripts, [video(1, teaser="Own teaser")]
    )

    await run_job(env.job_id, deps=env.deps)

    assert _items(db_engine)["Video 1"].teaser == "Own teaser"


async def test_a_long_description_is_collapsed_and_cut(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    description = "word  \n\n" * 1000
    transcripts = FakeTranscripts({watch("vid1"): _unavailable("vid1", description)})
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, [video(1)])

    await run_job(env.job_id, deps=env.deps)

    teaser = _items(db_engine)["Video 1"].teaser
    assert teaser is not None
    assert len(teaser) == 2000
    assert "  " not in teaser and "\n" not in teaser


async def test_a_permanent_failure_without_a_description_keeps_the_teaser_empty(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = _env(db_engine, fake_clock, recording_next_run, FakeTranscripts(), [video(1)])

    result = await run_job(env.job_id, deps=env.deps)

    item = _items(db_engine)["Video 1"]
    assert item.teaser is None and item.raw_content is None
    assert item.status != ItemStatus.FAILED
    assert result.status == RunStatus.SUCCEEDED


# --- (c) transient failures fail the item ---------------------------------------------------


async def test_a_transient_failure_is_retried_then_fails_only_that_item(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    transcripts = FakeTranscripts(
        {
            watch("vid1"): TranscriptUnavailable("timeout", url=watch("vid1")),
            watch("vid2"): transcript_of(TEXT),
        }
    )
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, [video(1), video(2)])

    result = await run_job(env.job_id, deps=env.deps)

    items = _items(db_engine)
    assert items["Video 1"].status == ItemStatus.FAILED
    assert (items["Video 1"].last_error or "").startswith("TranscriptUnavailable: timeout")
    assert items["Video 2"].status == ItemStatus.SUMMARIZED
    assert transcripts.call_counts[watch("vid1")] == 3  # RetrySettings default
    assert len(env.sleep.calls) == 2
    assert result.status == RunStatus.PARTIAL


async def test_a_transient_error_that_recovers_is_a_success(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    transcripts = FakeTranscripts(
        {
            watch("vid1"): Attempts(
                TranscriptUnavailable("timeout", url=watch("vid1")), transcript_of(TEXT)
            )
        }
    )
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, [video(1)])

    result = await run_job(env.job_id, deps=env.deps)

    assert _items(db_engine)["Video 1"].raw_content == TEXT
    assert result.status == RunStatus.SUCCEEDED


# --- (d) deferral releases the item ---------------------------------------------------------


async def test_a_deferred_item_is_released_and_not_counted(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    caplog: pytest.LogCaptureFixture,
) -> None:
    deferral = TranscriptionDeferred("cap_reached")
    transcripts = FakeTranscripts({watch("vid1"): deferral})
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, [video(1)])

    with caplog.at_level(logging.INFO, logger="invio.graph"):
        result = await run_job(env.job_id, deps=env.deps)

    item = _items(db_engine)["Video 1"]
    assert (item.status, item.attempts, item.run_id) == (ItemStatus.NEW, 0, None)
    assert result.status == RunStatus.SUCCEEDED  # nothing failed: an all-deferred run is no failure
    assert result.stats["processed"] == 0 and result.stats["failed"] == 0
    assert result.errors == ()
    assert env.provider.calls == []
    (record,) = [r for r in caplog.records if r.getMessage() == "item.transcription_deferred"]
    assert record.__dict__["reason"] == "cap_reached"
    assert record.__dict__["item_id"] == item.id


async def test_a_deferred_item_is_picked_up_again_by_the_next_run(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    deferral = TranscriptionDeferred("not_enough_time")
    transcripts = FakeTranscripts({watch("vid1"): Attempts(deferral, transcript_of(TEXT))})
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, [video(1)])

    await run_job(env.job_id, deps=env.deps)
    assert _items(db_engine)["Video 1"].status == ItemStatus.NEW
    with session_scope(env.factory) as session:
        job = session.get(Job, env.job_id)
        assert job is not None
        job.next_run_at = fake_clock() - timedelta(minutes=1)
        job.locked_until = None
    await run_job(env.job_id, deps=env.deps)

    item = _items(db_engine)["Video 1"]
    assert item.status == ItemStatus.SUMMARIZED and item.raw_content == TEXT
    assert item.attempts == 1


async def test_a_budget_stop_and_a_deferral_in_one_run_release_the_item_once(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config(limits__max_llm_tokens_per_run=20)
    deferral = TranscriptionDeferred("cap_reached")
    transcripts = FakeTranscripts({watch("vid9"): deferral})
    env = _env(
        db_engine,
        fake_clock,
        recording_next_run,
        transcripts,
        make_candidates(3),
        config=config,
        concurrency=1,
    )
    with session_scope(env.factory) as session:
        job = session.get(Job, env.job_id)
        assert job is not None
        # an interrupted earlier run left this video with one attempt; it is retaken (attempts 2)
        make_item(
            session,
            job,
            url=watch("vid9"),
            title="Video 9",
            type="video",
            status=ItemStatus.NEW,
            attempts=1,
            published_at=datetime(2026, 10, 1, tzinfo=UTC) - timedelta(minutes=90),
        )

    result = await run_job(env.job_id, deps=env.deps)

    items = _items(db_engine)
    assert result.stats["budget_exceeded"] is True
    assert transcripts.call_counts[watch("vid9")] == 1
    assert items["Video 9"].attempts == 1  # taken (2), released once (1): not twice
    assert items["Video 9"].status == ItemStatus.NEW
    assert items["Article 3"].attempts == 0  # released once by the budget stop
    assert result.stats["skipped_budget"] == 2  # Article 2 and 3; the deferred video is not one


# --- (e), (f), (g) ----------------------------------------------------------------------------


async def test_the_port_gets_the_job_language_and_the_run_deadline(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    transcripts = FakeTranscripts({watch("vid1"): transcript_of(TEXT)})
    config = make_job_config(language="de")
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, [video(1)], config=config)

    await run_job(env.job_id, deps=env.deps)

    ((url, language, deadline),) = transcripts.calls
    assert (url, language) == (watch("vid1"), "de")
    assert deadline is not None
    assert deadline.replace(tzinfo=UTC) == (env.deps.clock() + env.deps.lock_ttl).replace(
        tzinfo=UTC
    )


async def test_an_article_still_uses_the_page_fetcher(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    transcripts = FakeTranscripts()
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, make_candidates(1))

    await run_job(env.job_id, deps=env.deps)

    assert transcripts.calls == []
    assert env.ports.page_calls["https://example.com/post-1"] == 1


async def test_a_cookies_problem_fails_the_item_without_a_retry(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    error = FetchError("cookies_unavailable", url=watch("vid1"))
    transcripts = FakeTranscripts({watch("vid1"): error})
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, [video(1)])

    await run_job(env.job_id, deps=env.deps)

    item = _items(db_engine)["Video 1"]
    assert item.status == ItemStatus.FAILED
    assert (item.last_error or "").startswith("FetchError: cookies_unavailable")
    assert transcripts.call_counts[watch("vid1")] == 1


# --- the per-run transcription cap ------------------------------------------------------------


def _fallback_env(
    engine: Engine,
    clock: FakeClock,
    next_run: NextRun,
    count: int,
    *,
    cap: int,
    outcomes: dict[str, Any] | None = None,
    concurrency: int = 4,
) -> tuple[Env, FakeTranscripts]:
    urls = [watch(f"vid{n}") for n in range(1, count + 1)]
    resolved = {url: transcript_of(f"words of {url[-4:]}") for url in urls}
    resolved.update(outcomes or {})
    transcripts = FakeTranscripts(resolved, needs_fallback=urls)
    env = _env(
        engine,
        clock,
        next_run,
        transcripts,
        [video(n) for n in range(1, count + 1)],
        max_transcriptions_per_run=cap,
        concurrency=concurrency,
    )
    return env, transcripts


async def test_the_cap_transcribes_three_of_five_and_releases_the_rest(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env, transcripts = _fallback_env(db_engine, fake_clock, recording_next_run, 5, cap=3)

    result = await run_job(env.job_id, deps=env.deps)

    items = _items(db_engine).values()
    done = [i for i in items if i.raw_content]
    released = [i for i in items if i.status == ItemStatus.NEW]
    assert len(done) == 3 and len(released) == 2
    assert all(i.attempts == 0 and i.run_id is None for i in released)
    assert len(transcripts.transcribed) == 3
    assert result.status == RunStatus.SUCCEEDED
    assert result.stats["failed"] == 0 and result.errors == ()

    # the released videos are picked up by the next run
    with session_scope(env.factory) as session:
        job = session.get(Job, env.job_id)
        assert job is not None
        job.next_run_at = fake_clock() - timedelta(minutes=1)
        job.locked_until = None
    await run_job(env.job_id, deps=env.deps)

    assert all(i.raw_content for i in _items(db_engine).values())


async def test_a_retried_item_uses_one_slot_only(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    flaky = Attempts(TranscriptUnavailable("timeout", url=watch("vid1")), transcript_of(TEXT))
    env, transcripts = _fallback_env(
        db_engine,
        fake_clock,
        recording_next_run,
        2,
        cap=1,
        outcomes={watch("vid1"): flaky},
        concurrency=1,
    )

    await run_job(env.job_id, deps=env.deps)

    items = _items(db_engine)
    assert items["Video 1"].raw_content == TEXT
    assert items["Video 2"].status == ItemStatus.NEW  # the one slot belonged to video 1
    assert transcripts.call_counts[watch("vid1")] == 2


async def test_a_deferral_for_lack_of_time_gives_the_slot_back(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    late = TranscriptionDeferred("not_enough_time")
    env, _ = _fallback_env(
        db_engine,
        fake_clock,
        recording_next_run,
        2,
        cap=1,
        outcomes={watch("vid1"): late},
        concurrency=1,
    )

    await run_job(env.job_id, deps=env.deps)

    items = _items(db_engine)
    assert items["Video 1"].status == ItemStatus.NEW
    assert items["Video 2"].raw_content  # it got the slot video 1 gave back


async def test_the_node_refuses_every_slot_request_at_a_cap_of_zero(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    # Node level only: the real adapter never asks for a slot at cap 0 (it raises the permanent
    # ``transcription_disabled``); this fake port asks, and the node must refuse.
    env, transcripts = _fallback_env(db_engine, fake_clock, recording_next_run, 2, cap=0)

    result = await run_job(env.job_id, deps=env.deps)

    assert all(i.status == ItemStatus.NEW for i in _items(db_engine).values())
    assert transcripts.transcribed == []
    assert result.status == RunStatus.SUCCEEDED


# --- D1: the deferral boundary when the database or the run fails -----------------------------


async def test_a_database_error_while_releasing_a_deferred_item_is_run_fatal(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sqlalchemy.exc import OperationalError

    from invio.db.repositories import ItemRepository

    def boom(*args: object, **kwargs: object) -> None:
        raise OperationalError("UPDATE items", {}, Exception("x"))

    monkeypatch.setattr(ItemRepository, "release", boom)
    deferral = TranscriptionDeferred("cap_reached")
    transcripts = FakeTranscripts({watch("vid1"): deferral})
    env = _env(db_engine, fake_clock, recording_next_run, transcripts, [video(1)])

    with caplog.at_level(logging.INFO, logger="invio.graph"):
        result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert {e.stage for e in result.errors} == {"extract_text"}
    assert [r for r in caplog.records if r.getMessage() == "item.transcription_deferred"] == []
    assert _items(db_engine)["Video 1"].status != ItemStatus.FAILED
    with session_scope(env.factory) as session:
        job = session.get(Job, env.job_id)
        assert job is not None and job.locked_until is None


async def test_a_deferral_after_the_run_failed_is_not_released_by_the_boundary(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import asyncio

    from sqlalchemy.exc import OperationalError

    from invio.db.repositories import ItemRepository

    run_failed = asyncio.Event()
    released: list[int] = []
    deferred: list[str] = []
    real_release = ItemRepository.release

    def db_error(*args: object, **kwargs: object) -> None:
        run_failed.set()
        raise OperationalError("UPDATE items", {}, Exception("x"))

    def recording_release(self: ItemRepository, items: Any) -> Any:
        released.extend(item.id for item in items)
        return real_release(self, items)

    monkeypatch.setattr(ItemRepository, "set_extracted", db_error)
    monkeypatch.setattr(ItemRepository, "release", recording_release)

    class LateDeferral(FakeTranscripts):
        async def __call__(self, url: str, /, **kwargs: Any) -> Any:  # type: ignore[override]
            await run_failed.wait()
            for _ in range(5):
                await asyncio.sleep(0)  # let the article's boundary record the failure
            deferred.append(url)
            raise TranscriptionDeferred("cap_reached")

    env = _env(
        db_engine,
        fake_clock,
        recording_next_run,
        LateDeferral(),
        [video(1), *make_candidates(1)],
        concurrency=2,
    )

    with caplog.at_level(logging.INFO, logger="invio.graph"):
        result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert deferred == [watch("vid1")]  # the deferral did reach the boundary
    assert [r for r in caplog.records if r.getMessage() == "item.transcription_deferred"] == []
    assert _items(db_engine)["Video 1"].id not in released  # the boundary left it alone
