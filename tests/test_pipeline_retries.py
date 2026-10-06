"""``run_job`` retries: transient failures are retried with backoff, permanent ones are not."""

from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import Engine, select

from invio.config.job import ScheduleConfig
from invio.db.models import Item, LlmUsage, Run
from invio.db.session import session_factory, session_scope
from invio.domain import ItemStatus, RunStatus
from invio.llm.base import (
    LLMAuthError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMUnavailableError,
)
from invio.pipeline.run import run_job
from invio.retry import RetrySettings
from invio.sources.errors import BlockedError, BlockReason, FetchError
from tests.conftest import FakeClock
from tests.pipeline_helpers import (
    Attempts,
    RoutedFakeProvider,
    build_env,
    make_candidates,
    relevance_reply,
)

pytestmark = pytest.mark.usefixtures("clean_jobs")

NextRun = Callable[[ScheduleConfig, datetime], datetime]
FEED = "https://example.com/feed.xml"
PAGE_1 = "https://example.com/post-1"


def _rows[T](engine: Engine, model: type[T]) -> list[T]:
    with session_scope(session_factory(engine)) as session:
        rows = list(session.scalars(select(model).order_by(model.id)))  # type: ignore[attr-defined]
        session.expunge_all()
        return rows


def _by_title(engine: Engine) -> dict[str, Item]:
    return {item.title: item for item in _rows(engine, Item)}


def _one_item(count: int = 1) -> dict[str, Any]:
    return {FEED: make_candidates(count)}


def _relevance_script(*errors: Exception) -> Callable[[str], Any]:
    """Raise ``errors`` in order for ``Article 1``, then answer; other items answer at once."""
    queue = list(errors)

    def route(title: str) -> Any:
        if title == "Article 1" and queue:
            return queue.pop(0)
        return relevance_reply()

    return route


async def test_s8_a_rate_limited_request_is_retried_with_the_provider_wait(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    errors = [LLMRateLimitError("slow", retry_after=2), LLMRateLimitError("slow", retry_after=2)]
    provider = RoutedFakeProvider(relevance=_relevance_script(*errors))
    env = build_env(
        db_engine, fake_clock, recording_next_run, sources=_one_item(), provider=provider
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    assert _by_title(db_engine)["Article 1"].status == ItemStatus.SUMMARIZED
    assert provider.titles_for("relevance")["Article 1"] == 3
    assert env.sleep.calls == [2.0, 2.0]
    usage = [u for u in _rows(db_engine, LlmUsage) if u.purpose == "relevance"]
    assert len(usage) == 1  # failed requests record no usage


async def test_s9_a_transient_source_failure_is_retried(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    flaky = Attempts(FetchError("timeout", url=FEED), make_candidates(2))
    env = build_env(db_engine, fake_clock, recording_next_run, sources={FEED: flaky})

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    assert result.stats["sources_failed"] == 0
    assert result.stats["found"] == 2
    assert env.sleep.calls == [1.0]
    assert [c for c in env.ports.calls if c[0] == "source"] == [("source", FEED)] * 2


async def test_s10_exhausted_retries_fail_only_that_item(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    errors = [LLMUnavailableError("down")] * 3
    provider = RoutedFakeProvider(relevance=_relevance_script(*errors))
    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        sources=_one_item(3),
        provider=provider,
        concurrency=1,
    )

    result = await run_job(env.job_id, deps=env.deps)

    items = _by_title(db_engine)
    assert items["Article 1"].status == ItemStatus.FAILED
    assert items["Article 2"].status == ItemStatus.SUMMARIZED
    assert items["Article 3"].status == ItemStatus.SUMMARIZED
    assert result.status == RunStatus.PARTIAL
    assert env.sleep.calls == [1.0, 2.0]
    assert provider.titles_for("relevance")["Article 1"] == 3


async def test_s11_permanent_errors_are_attempted_exactly_once(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    provider = RoutedFakeProvider(relevance=LLMAuthError("no key", provider="fakeco"))
    env = build_env(
        db_engine, fake_clock, recording_next_run, sources=_one_item(), provider=provider
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert len(provider.requests_for("relevance")) == 1
    assert env.sleep.calls == []


async def test_s11_an_invalid_request_fails_the_item_after_one_request(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    error = LLMInvalidRequestError("bad", provider="fakeco", model="m", status=400)
    provider = RoutedFakeProvider(relevance=_relevance_script(error))
    env = build_env(
        db_engine, fake_clock, recording_next_run, sources=_one_item(2), provider=provider
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert provider.titles_for("relevance")["Article 1"] == 1
    assert _by_title(db_engine)["Article 1"].status == ItemStatus.FAILED
    assert result.status == RunStatus.PARTIAL
    assert env.sleep.calls == []


async def test_s11_a_blocked_page_is_fetched_once_and_fails_the_item(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    pages = {PAGE_1: BlockedError(BlockReason.BLOCKED_BY_ROBOTS, url=PAGE_1)}
    env = build_env(db_engine, fake_clock, recording_next_run, sources=_one_item(2), pages=pages)

    result = await run_job(env.job_id, deps=env.deps)

    assert env.ports.page_calls[PAGE_1] == 1
    assert _by_title(db_engine)["Article 1"].status == ItemStatus.FAILED
    assert result.status == RunStatus.PARTIAL
    assert env.sleep.calls == []


async def test_s11_a_blocked_source_is_fetched_once_and_fails(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    blocked = BlockedError(BlockReason.NON_PUBLIC_ADDRESS, url=FEED)
    env = build_env(db_engine, fake_clock, recording_next_run, sources={FEED: blocked})

    result = await run_job(env.job_id, deps=env.deps)

    assert [c for c in env.ports.calls if c[0] == "source"] == [("source", FEED)]
    assert result.stats["sources_failed"] == 1
    assert result.status == RunStatus.FAILED
    (run,) = _rows(db_engine, Run)
    assert run.error == "all sources failed"
    assert env.sleep.calls == []


async def test_an_item_page_is_retried_on_a_transient_fetch_error_and_then_extracted(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from tests.pipeline_helpers import article_html

    pages = {PAGE_1: Attempts(FetchError("timeout", url=PAGE_1), article_html("Article 1"))}
    env = build_env(db_engine, fake_clock, recording_next_run, sources=_one_item(), pages=pages)

    result = await run_job(env.job_id, deps=env.deps)

    item = _by_title(db_engine)["Article 1"]
    assert item.status == ItemStatus.SUMMARIZED
    assert item.raw_content
    assert env.ports.page_calls[PAGE_1] == 2
    assert env.sleep.calls == [1.0]
    assert result.status == RunStatus.SUCCEEDED


async def test_the_retry_policy_of_the_deps_is_used(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    flaky = Attempts(FetchError("timeout", url=FEED), FetchError("timeout", url=FEED))
    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        sources={FEED: flaky},
        retry=RetrySettings(max_attempts=2, jitter=False),
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.stats["sources_failed"] == 1
    assert [c for c in env.ports.calls if c[0] == "source"] == [("source", FEED)] * 2
    assert env.sleep.calls == [1.0]
