"""Progress events of ``run_job``: order, counts, item outcomes and a failing observer."""

import dataclasses
import logging
from typing import Any

import pytest
from sqlalchemy import Engine

from invio.domain import RunStatus
from invio.llm.base import LLMUnavailableError
from invio.pipeline import ProgressEvent
from invio.pipeline.run import run_job
from tests.conftest import FakeClock
from tests.pipeline_helpers import (
    RoutedFakeProvider,
    build_env,
    make_candidates,
    relevance_reply,
)

pytestmark = pytest.mark.usefixtures("clean_jobs")


class Recorder:
    def __init__(self) -> None:
        self.events: list[ProgressEvent] = []

    def __call__(self, event: ProgressEvent) -> None:
        self.events.append(event)

    @property
    def stages(self) -> list[str]:
        return [e.stage for e in self.events if e.kind == "stage"]

    @property
    def items(self) -> list[ProgressEvent]:
        return [e for e in self.events if e.kind == "item"]


def _route(title: str) -> Any:
    if title == "Article 2":
        return RuntimeError("boom")
    return relevance_reply(score=0.0) if title == "Article 3" else relevance_reply()


async def test_stage_events_arrive_in_order_with_the_counts(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    env = build_env(db_engine, fake_clock)
    recorder = Recorder()

    result = await run_job(env.job_id, dry_run=True, deps=env.deps, observer=recorder)

    assert recorder.stages == [
        "deduplicate",
        "keyword_prefilter",
        "synthesize_digest",
        "persist",
        "notify",
        "finalize",
    ]
    dedup = next(e for e in recorder.events if e.stage == "deduplicate")
    assert (dedup.counts.found, dedup.counts.new) == (result.stats["found"], result.stats["new"])


async def test_one_item_event_per_selected_item_and_final_counts_match_the_stats(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    provider = RoutedFakeProvider(relevance=_route)
    env = build_env(db_engine, fake_clock, provider=provider)
    recorder = Recorder()

    result = await run_job(env.job_id, deps=env.deps, observer=recorder)

    by_title = {e.title: e for e in recorder.items}
    assert set(by_title) == {"Article 1", "Article 2", "Article 3"}
    assert by_title["Article 1"].outcome == "relevant"
    assert by_title["Article 2"].outcome == "failed"
    assert by_title["Article 2"].message == "RuntimeError: item failed"
    assert by_title["Article 3"].outcome == "irrelevant"
    last = recorder.events[-1].counts
    assert (last.processed, last.relevant, last.failed) == (
        result.stats["processed"],
        result.stats["relevant"],
        result.stats["failed"],
    )
    assert last.selected == 3


async def test_counts_never_decrease(db_engine: Engine, fake_clock: FakeClock) -> None:
    env = build_env(db_engine, fake_clock, provider=RoutedFakeProvider(relevance=_route))
    recorder = Recorder()

    await run_job(env.job_id, deps=env.deps, observer=recorder)

    names = [f.name for f in dataclasses.fields(recorder.events[0].counts)]
    for before, after in zip(recorder.events, recorder.events[1:], strict=False):
        for name in names:
            assert getattr(after.counts, name) >= getattr(before.counts, name)


async def test_events_carry_a_frozen_snapshot(db_engine: Engine, fake_clock: FakeClock) -> None:
    env = build_env(db_engine, fake_clock)
    recorder = Recorder()

    await run_job(env.job_id, deps=env.deps, observer=recorder)

    first, last = recorder.events[0].counts, recorder.events[-1].counts
    assert first is not last
    assert first.processed == 0 or first.processed < last.processed
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.found = 99  # type: ignore[misc]


async def test_a_failing_observer_does_not_fail_the_run_and_is_dropped(
    db_engine: Engine, fake_clock: FakeClock, caplog: pytest.LogCaptureFixture
) -> None:
    calls = 0

    def broken(event: ProgressEvent) -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("secret display bug")

    env = build_env(db_engine, fake_clock)

    with caplog.at_level(logging.WARNING, logger="invio.graph"):
        result = await run_job(env.job_id, deps=env.deps, observer=broken)

    assert result.status == RunStatus.SUCCEEDED
    assert result.stats["relevant"] == 3
    assert result.errors == ()
    assert calls == 1  # dropped after its first failure
    failures = [r for r in caplog.records if r.getMessage() == "run.observer_failed"]
    assert len(failures) == 1
    assert "secret display bug" not in caplog.text


async def test_item_titles_are_cleaned_and_urls_lose_their_query(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    candidate = dataclasses.replace(
        make_candidates(1)[0],
        title="\x1b[31mEvil\ntitle\x07",
        url="https://example.com/post-1?token=SECRET#frag",
    )
    env = build_env(
        db_engine,
        fake_clock,
        candidates=[candidate],
        provider=RoutedFakeProvider(relevance=RuntimeError("boom")),
    )
    recorder = Recorder()

    result = await run_job(env.job_id, dry_run=True, deps=env.deps, observer=recorder)

    (item,) = recorder.items
    assert item.title == "Evil title"
    (entry,) = result.stats["errors"]
    assert entry["title"] == "Evil title"
    assert entry["url"] == "https://example.com/post-1"
    assert "SECRET" not in str(result.stats)


async def test_item_urls_lose_their_credentials(db_engine: Engine, fake_clock: FakeClock) -> None:
    candidate = dataclasses.replace(
        make_candidates(1)[0], url="https://user:SECRET@example.com/post-1?token=x"
    )
    env = build_env(
        db_engine,
        fake_clock,
        candidates=[candidate],
        provider=RoutedFakeProvider(relevance=RuntimeError("boom")),
    )

    result = await run_job(env.job_id, dry_run=True, deps=env.deps)

    (entry,) = result.stats["errors"]
    assert entry["url"] == "https://example.com/post-1"


async def test_a_relevant_item_that_fails_to_summarize_counts_as_relevant_and_failed(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    provider = RoutedFakeProvider(summarize=LLMUnavailableError("down"))
    env = build_env(db_engine, fake_clock, provider=provider)
    recorder = Recorder()

    result = await run_job(env.job_id, deps=env.deps, observer=recorder)

    assert {e.outcome for e in recorder.items} == {"failed"}
    last = recorder.events[-1].counts
    assert (last.relevant, last.failed) == (result.stats["relevant"], result.stats["failed"])
    assert last.relevant == 3
