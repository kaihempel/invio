"""``run_job`` end to end over fakes: digest, stage order, filters, empty digest, run context."""

import logging
from collections import Counter
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
import pytest
from sqlalchemy import Engine, select
from yt_dlp.utils import DownloadError

from invio.config.job import ScheduleConfig
from invio.db.models import Digest, Item, Job, LlmUsage, Run
from invio.db.repositories import JobRepository
from invio.db.session import session_factory, session_scope
from invio.domain import ItemStatus, RunStatus
from invio.log import job_var, run_id_var
from invio.pipeline.run import run_job
from tests.conftest import FakeClock
from tests.db_helpers import uses_sqlite
from tests.http_helpers import FakeResolver, RecordingTransport
from tests.pipeline_helpers import (
    Env,
    FakePorts,
    RoutedFakeProvider,
    build_env,
    make_candidates,
    make_job_config,
    relevance_reply,
)

pytestmark = pytest.mark.usefixtures("clean_jobs")

NextRun = Callable[[ScheduleConfig, datetime], datetime]


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


def _schedule(env: Env) -> ScheduleConfig:
    return ScheduleConfig.model_validate(env.config["schedule"])


# --- S1 -------------------------------------------------------------------------------------


async def test_s1_a_run_produces_a_stored_notified_digest(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    (run,) = _rows(db_engine, Run)
    assert (run.id, run.status) == (result.run_id, RunStatus.SUCCEEDED)
    assert run.finished_at is not None
    items = _rows(db_engine, Item)
    (digest,) = _rows(db_engine, Digest)
    assert sorted(digest.item_ids) == sorted(item.id for item in items)
    assert len(digest.item_ids) == 3
    assert all(i.status == ItemStatus.SUMMARIZED and i.raw_content for i in items)
    expected = {
        "found": 3,
        "new": 3,
        "after_keyword_filter": 3,
        "relevant": 3,
        "summarized": 3,
        "failed": 0,
        "sources": 1,
        "sources_failed": 0,
    }
    assert {k: result.stats[k] for k in expected} == expected
    assert env.ports.notifier.calls == [digest.id]
    job = _job(db_engine, env.job_id)
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())
    assert job.locked_until is None
    assert result.errors == ()
    assert (result.notifications_sent, result.notifications_failed) == (1, 0)
    assert result.digest is not None and list(result.digest.item_ids) == digest.item_ids


# --- S2 -------------------------------------------------------------------------------------


async def test_s2_stages_run_in_order_and_each_page_is_fetched_once(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)

    await run_job(env.job_id, deps=env.deps)

    trace = env.trace
    assert trace[0] == "source"
    assert trace[-1] == "notify"
    assert trace.count("page") == 3
    assert trace.index("synthesize") > max(i for i, t in enumerate(trace) if t == "summarize")
    assert trace.index("synthesize") == len(trace) - 2
    assert set(env.ports.page_calls.values()) == {1}
    assert len(env.ports.page_calls) == 3


async def test_s2_graph_nodes_run_in_stage_order_and_each_item_once(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from invio.graph import stages

    visited: list[tuple[str, int | None]] = []
    run_stages = (
        "load_job",
        "fetch_sources",
        "deduplicate",
        "keyword_prefilter",
        "join",
        "synthesize_digest",
        "persist",
        "notify",
        "finalize",
    )
    item_stages = ("extract_text", "score_relevance", "summarize")

    def spy(name: str) -> Any:
        original = getattr(stages, name)

        async def recorded(state: Any, deps: Any, scope: Any) -> Any:
            visited.append((name, state.get("item_id") if name in item_stages else None))
            return await original(state, deps, scope)

        return recorded

    for name in run_stages + item_stages:
        monkeypatch.setattr(stages, name, spy(name))
    env = build_env(db_engine, fake_clock, recording_next_run)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    names = [name for name, _ in visited]
    assert [n for n in names if n in run_stages] == list(run_stages)
    first_item, last_item = names.index("extract_text"), len(names) - names[::-1].index("summarize")
    assert names.index("keyword_prefilter") < first_item
    assert last_item <= names.index("join")
    item_ids = {item.id for item in _rows(db_engine, Item)}
    for stage in item_stages:  # every selected item went through every item stage exactly once
        assert Counter(i for n, i in visited if n == stage) == Counter(item_ids)
    for item_id in item_ids:  # and in order: extract, then score, then summarize
        assert [n for n, i in visited if i == item_id] == list(item_stages)


# --- S3 -------------------------------------------------------------------------------------


async def test_s3_a_second_run_finds_nothing_new(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)
    await run_job(env.job_id, deps=env.deps)
    requests = len(env.provider.calls)
    fake_clock.advance(timedelta(hours=2))

    second = await run_job(env.job_id, deps=env.deps)

    assert len(env.provider.calls) == requests
    assert second.status == RunStatus.SUCCEEDED
    assert (second.stats["found"], second.stats["new"]) == (3, 0)
    zero = ("after_keyword_filter", "relevant", "summarized", "failed", "llm_calls")
    assert {key: second.stats[key] for key in zero} == dict.fromkeys(zero, 0)
    assert second.digest is None
    assert len(_rows(db_engine, Digest)) == 1  # only the first run's; no empty digest by default
    assert len(env.ports.notifier.calls) == 1
    assert _job(db_engine, env.job_id).next_run_at == recording_next_run(
        _schedule(env), fake_clock()
    )


# --- S4 -------------------------------------------------------------------------------------


async def test_s4_an_irrelevant_item_is_skipped_and_not_in_the_digest(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    provider = RoutedFakeProvider(
        relevance=lambda title: relevance_reply(0.1 if title == "Article 2" else 0.9)
    )
    env = build_env(db_engine, fake_clock, recording_next_run, provider=provider)

    result = await run_job(env.job_id, deps=env.deps)

    by_title = {i.title: i for i in _rows(db_engine, Item)}
    assert by_title["Article 2"].status == ItemStatus.SKIPPED_IRRELEVANT
    assert "Article 2" not in provider.titles_for("summarize")
    (digest,) = _rows(db_engine, Digest)
    assert by_title["Article 2"].id not in digest.item_ids
    assert len(digest.item_ids) == 2
    assert result.status == RunStatus.SUCCEEDED


# --- Keyword filter -------------------------------------------------------------------------


async def test_a_keyword_rejected_item_is_skipped_and_its_page_is_never_fetched(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config(search__keywords={"exclude": ["Article 3"]})
    env = build_env(db_engine, fake_clock, recording_next_run, config=config)

    result = await run_job(env.job_id, deps=env.deps)

    by_title = {i.title: i for i in _rows(db_engine, Item)}
    assert by_title["Article 3"].status == ItemStatus.SKIPPED_KEYWORD
    assert env.ports.page_calls["https://example.com/post-3"] == 0
    assert result.stats["after_keyword_filter"] == 2
    assert result.status == RunStatus.SUCCEEDED


# --- YouTube sources (#28) ---------------------------------------------------------------------

CHANNEL_LISTING = "https://www.youtube.com/channel/UC123/videos"


def _youtube_config(*extra: dict[str, Any]) -> dict[str, Any]:
    config = make_job_config()
    config["sources"].append({"type": "youtube_channel", "channel_id": "UC123"})
    config["sources"].extend(extra)
    return config


async def test_youtube_videos_reach_the_pipeline_as_video_items(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    listing = {"entries": [{"id": "vid1", "title": "Video 1"}, {"id": "vid2", "title": "Video 2"}]}
    config = make_job_config(sources=[{"type": "youtube_channel", "channel_id": "UC123"}])
    ports = FakePorts(youtube={CHANNEL_LISTING: listing})
    env = build_env(db_engine, fake_clock, recording_next_run, config=config, ports=ports)

    result = await run_job(env.job_id, deps=env.deps)

    items = _rows(db_engine, Item)
    assert {(i.title, i.type, i.url) for i in items} == {
        ("Video 1", "video", "https://www.youtube.com/watch?v=vid1"),
        ("Video 2", "video", "https://www.youtube.com/watch?v=vid2"),
    }
    assert (result.stats["sources"], result.stats["sources_failed"]) == (1, 0)
    assert result.status == RunStatus.SUCCEEDED


async def test_a_blocked_youtube_source_does_not_stop_the_other_sources(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    blocked = DownloadError("ERROR: Sign in to confirm you're not a bot")
    ports = FakePorts(
        {"https://example.com/feed.xml": make_candidates(2)}, youtube={CHANNEL_LISTING: blocked}
    )
    env = build_env(
        db_engine, fake_clock, recording_next_run, config=_youtube_config(), ports=ports
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.stats["sources_failed"] == 1
    assert result.stats["found"] == 2
    (entry,) = result.stats["errors"]
    assert entry["source"] == "1:youtube_channel"
    assert "not a bot" not in str(entry)
    assert len(_rows(db_engine, Item)) == 2
    # 429 is transient: retried by the existing retry path (3 attempts, 2 recorded fake sleeps)
    assert [c for c in ports.calls if c == ("source", "UC123")] == [("source", "UC123")] * 3
    assert len(env.sleep.calls) == 2


async def test_a_missing_youtube_cookies_file_does_not_stop_the_other_sources(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun, tmp_path: Path
) -> None:
    ports = FakePorts(
        {"https://example.com/feed.xml": make_candidates(2)},
        youtube={CHANNEL_LISTING: {"entries": [{"id": "vid1", "title": "Never listed"}]}},
        youtube_cookies_file=tmp_path / "missing-cookies.txt",
    )
    env = build_env(
        db_engine, fake_clock, recording_next_run, config=_youtube_config(), ports=ports
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert (result.stats["sources_failed"], result.stats["found"]) == (1, 2)
    (entry,) = result.stats["errors"]
    assert entry["source"] == "1:youtube_channel"
    assert "missing-cookies" not in str(entry)
    assert {i.title for i in _rows(db_engine, Item)} == {"Article 1", "Article 2"}
    # a configuration problem is permanent: one attempt, no retry sleep
    assert [c for c in ports.calls if c == ("source", "UC123")] == [("source", "UC123")]
    assert env.sleep.calls == []


async def test_a_missing_youtube_channel_is_attempted_once_without_sleeping(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    gone = DownloadError("ERROR: HTTP Error 404: Not Found")
    ports = FakePorts(
        {"https://example.com/feed.xml": make_candidates(1)}, youtube={CHANNEL_LISTING: gone}
    )
    env = build_env(
        db_engine, fake_clock, recording_next_run, config=_youtube_config(), ports=ports
    )

    result = await run_job(env.job_id, deps=env.deps)

    assert result.stats["sources_failed"] == 1
    assert [c for c in ports.calls if c == ("source", "UC123")] == [("source", "UC123")]
    assert env.sleep.calls == []


# --- S25 / S24 are in test_graph_extract and test_graph_build ---------------------------------


async def test_s25_a_too_short_page_does_not_fail_the_item(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    pages = {"https://example.com/post-1": "<html><body><p>tiny</p></body></html>"}
    env = build_env(db_engine, fake_clock, recording_next_run, pages=pages)

    result = await run_job(env.job_id, deps=env.deps)

    by_title = {i.title: i for i in _rows(db_engine, Item)}
    assert by_title["Article 1"].raw_content is None
    assert by_title["Article 1"].status == ItemStatus.SUMMARIZED
    assert result.status == RunStatus.SUCCEEDED
    assert result.errors == ()


# --- S30 ------------------------------------------------------------------------------------

PAGE_URL = "https://example.com/spotlight"


async def test_s30_a_page_mode_web_item_is_fetched_unconditionally(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun, tmp_path: Any
) -> None:
    from invio.config.settings import Settings
    from invio.pipeline.deps import default_deps
    from tests.pipeline_helpers import article_html

    html = article_html("Spotlight")

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("if-none-match"):
            return httpx2.Response(304)
        return httpx2.Response(
            200, headers={"ETag": '"v1"', "Content-Type": "text/html"}, content=html.encode()
        )

    config = make_job_config()
    config["sources"] = [{"type": "web", "url": PAGE_URL, "mode": "page"}]
    env = build_env(db_engine, fake_clock, recording_next_run, config=config, sources={})
    settings = Settings(
        _env_file=None,
        database_url="sqlite+pysqlite://",  # type: ignore[arg-type]
        http_respect_robots=False,
        http_host_interval_seconds=0.01,
    )

    transport = RecordingTransport(handler)
    resolver = FakeResolver({"example.com": ["93.184.216.34"]})
    async with default_deps(settings, transport=transport, resolver=resolver) as real:
        deps = env.deps.__class__(
            session_factory=env.factory,
            fetch_source=real.fetch_source,
            fetch_page=real.fetch_page,
            provider_for=env.deps.provider_for,
            notify=env.deps.notify,
            next_run=env.deps.next_run,
            clock=env.deps.clock,
            retry_delay=env.deps.retry_delay,
            sleep=env.deps.sleep,
        )
        result = await run_job(env.job_id, deps=deps)

    (item,) = _rows(db_engine, Item)
    assert item.url == PAGE_URL
    assert item.status == ItemStatus.SUMMARIZED
    assert result.status == RunStatus.SUCCEEDED
    seen = transport.requests
    assert len(seen) == 2  # the source fetch and the item page
    assert "if-none-match" not in seen[1].headers


# --- S31 ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("send_if_empty", "stored"), [(True, 1), (False, 0)])
async def test_s31_an_empty_run_stores_and_notifies_an_empty_digest_only_when_asked(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    send_if_empty: bool,
    stored: int,
) -> None:
    config = make_job_config(notification__send_if_empty=send_if_empty)
    env = build_env(db_engine, fake_clock, recording_next_run, config=config, candidates=[])

    result = await run_job(env.job_id, deps=env.deps)

    digests = _rows(db_engine, Digest)
    assert len(digests) == stored
    assert result.status == RunStatus.SUCCEEDED
    if send_if_empty:
        assert digests[0].item_ids == []
        assert env.ports.notifier.calls == [digests[0].id]
    else:
        assert env.ports.notifier.calls == []
    assert env.provider.calls == []


# --- S28 ------------------------------------------------------------------------------------


class _ContextCapture(logging.Handler):
    """Reads the log context variables at emit time, like the JSON formatter does."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[tuple[str, str | None, str | None]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append((record.getMessage(), job_var.get(), run_id_var.get()))


async def test_s28_every_record_of_the_run_carries_job_and_run_id(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)
    capture = _ContextCapture()
    root = logging.getLogger()
    previous = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        result = await run_job(env.job_id, deps=env.deps)
    finally:
        root.removeHandler(capture)
        root.setLevel(previous)

    names = [name for name, _, _ in capture.records]
    start, end = names.index("run.started"), names.index("run.finalized")
    inside = capture.records[start : end + 1]
    assert len(inside) > 5  # the nodes logged too
    assert {(job, run) for _, job, run in inside} == {("research", str(result.run_id))}
    assert job_var.get() is None  # the context ends with the run


# --- S27: invalid config, missing key, refusals ----------------------------------------------


async def test_s27_an_invalid_stored_config_fails_the_run_with_field_paths_only(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config(search__min_relevance=2, llm__provider="nope-provider")
    env = build_env(db_engine, fake_clock, recording_next_run, config=config)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    (run,) = _rows(db_engine, Run)
    assert run.error == "ValidationError: invalid fields search.min_relevance, llm.provider"
    assert "nope-provider" not in (run.error or "")
    assert env.provider.calls == []
    assert env.ports.calls == []
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == recording_next_run(_schedule(env), fake_clock())


async def test_s27_many_invalid_fields_list_five_paths_and_an_ellipsis(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config(
        search__min_relevance=2,
        llm__provider="nope-provider",
        limits={
            "max_items_per_source": 0,
            "max_items_per_run": 0,
            "baseline_items": 0,
            "max_items_in_notification": 0,
            "max_llm_tokens_per_run": 0,
        },
    )
    env = build_env(db_engine, fake_clock, recording_next_run, config=config)

    await run_job(env.job_id, deps=env.deps)

    (run,) = _rows(db_engine, Run)
    assert run.error is not None
    prefix = "ValidationError: invalid fields "
    assert run.error.startswith(prefix) and run.error.endswith(", …")
    assert len(run.error.removeprefix(prefix).removesuffix(", …").split(", ")) == 5


async def test_s27_a_missing_api_key_fails_the_run_before_any_llm_call(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.config.settings import MissingSettingError

    def no_key(config: Any) -> Any:
        raise MissingSettingError("mistral_api_key")

    env = build_env(db_engine, fake_clock, recording_next_run, provider_for=no_key)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.FAILED
    (run,) = _rows(db_engine, Run)
    assert run.error == "MissingSettingError: run failed"
    assert env.provider.calls == []
    assert _job(db_engine, env.job_id).locked_until is None


async def test_s27_an_unknown_job_id_raises_and_creates_no_run(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.services.jobs import JobNotFoundError

    env = build_env(db_engine, fake_clock, recording_next_run)

    with pytest.raises(JobNotFoundError):
        await run_job(env.job_id + 1000, deps=env.deps)

    assert _rows(db_engine, Run) == []


async def test_s27_a_disabled_job_is_refused_without_a_run_or_lock_change(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.pipeline.run import JobDisabledError

    env = build_env(db_engine, fake_clock, recording_next_run, enabled=False)

    with pytest.raises(JobDisabledError):
        await run_job(env.job_id, deps=env.deps)

    assert _rows(db_engine, Run) == []
    assert _job(db_engine, env.job_id).locked_until is None


@pytest.mark.parametrize("concurrency", [0, -2])
async def test_a_concurrency_below_one_is_rejected_before_any_write(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun, concurrency: int
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)

    with pytest.raises(ValueError, match="concurrency"):
        await run_job(env.job_id, deps=env.deps, concurrency=concurrency)

    assert _rows(db_engine, Run) == []
    assert _job(db_engine, env.job_id).locked_until is None


# --- US6: dry run -------------------------------------------------------------------------------


async def test_s19_a_dry_run_changes_nothing_but_its_run_row(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from tests.pipeline_helpers import snapshot

    env = build_env(db_engine, fake_clock, recording_next_run)
    before = snapshot(env.factory, env.job_id)

    result = await run_job(env.job_id, dry_run=True, deps=env.deps)

    assert snapshot(env.factory, env.job_id) == before
    assert result.dry_run is True
    assert result.status == RunStatus.SUCCEEDED
    assert env.ports.notifier.calls == []
    assert result.digest is not None and len(result.digest.item_ids) == 3
    assert (result.notifications_sent, result.notifications_failed) == (0, 0)
    assert _rows(db_engine, Item) == []  # the candidates did not even create rows
    (run,) = _rows(db_engine, Run)
    assert (run.id, run.status) == (result.run_id, RunStatus.SUCCEEDED)
    assert run.stats is not None and run.stats["dry_run"] is True
    assert run.stats["found"] == 3 and run.stats["summarized"] == 3
    assert run.finished_at is not None
    job = _job(db_engine, env.job_id)
    assert job.locked_until is None
    assert job.next_run_at == before["next_run_at"]
    assert _rows(db_engine, LlmUsage) == []
    assert result.stats["llm_calls"] == 7  # the ledger still counts the calls it made


async def test_s20_a_normal_run_after_a_dry_run_processes_the_same_items_once(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)
    await run_job(env.job_id, dry_run=True, deps=env.deps)

    result = await run_job(env.job_id, deps=env.deps)

    items = _rows(db_engine, Item)
    assert len(items) == 3
    assert all(i.status == ItemStatus.SUMMARIZED and i.attempts == 1 for i in items)
    assert len(_rows(db_engine, Digest)) == 1
    assert result.status == RunStatus.SUCCEEDED
    assert result.stats["new"] == 3
    assert env.ports.notifier.calls != []
    assert "dry_run" not in result.stats


async def test_a_dry_run_with_a_stage_failure_is_failed_and_changes_nothing(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from tests.pipeline_helpers import snapshot

    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        sources={"https://example.com/feed.xml": RuntimeError("boom")},
    )
    before = snapshot(env.factory, env.job_id)

    result = await run_job(env.job_id, dry_run=True, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert snapshot(env.factory, env.job_id) == before
    (run,) = _rows(db_engine, Run)
    assert run.error == "RuntimeError: run failed"
    assert run.stats is not None and run.stats["dry_run"] is True
    assert _job(db_engine, env.job_id).locked_until is None


async def test_a_dry_run_that_fails_while_items_ran_replays_no_usage(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.llm.base import LLMAuthError

    provider = RoutedFakeProvider(
        relevance=lambda title: (
            LLMAuthError("no key") if title == "Article 3" else relevance_reply()
        )
    )
    env = build_env(db_engine, fake_clock, recording_next_run, provider=provider, concurrency=1)

    result = await run_job(env.job_id, dry_run=True, deps=env.deps)

    assert result.status == RunStatus.FAILED
    assert _rows(db_engine, LlmUsage) == []
    assert _rows(db_engine, Item) == []


async def test_a_dry_run_respects_a_busy_lock(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.pipeline.run import JobBusyError

    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        locked_until=fake_clock() + timedelta(hours=1),
    )

    with pytest.raises(JobBusyError):
        await run_job(env.job_id, dry_run=True, deps=env.deps)

    assert _rows(db_engine, Run) == []


async def test_a_dry_run_takes_over_an_expired_lock(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(
        db_engine,
        fake_clock,
        recording_next_run,
        locked_until=fake_clock() - timedelta(seconds=1),
    )

    result = await run_job(env.job_id, dry_run=True, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    assert _job(db_engine, env.job_id).locked_until is None


async def test_a_dry_run_after_a_budget_stop_reports_partial_without_releasing_rows(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config(limits__max_llm_tokens_per_run=20)
    env = build_env(db_engine, fake_clock, recording_next_run, config=config, concurrency=1)

    result = await run_job(env.job_id, dry_run=True, deps=env.deps)

    assert result.status == RunStatus.PARTIAL
    assert result.stats["budget_exceeded"] is True
    assert result.stats["skipped_budget"] == 2
    assert _rows(db_engine, Item) == []


async def test_s19_a_dry_run_releases_pending_items_it_took_with_attempts_unchanged(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from tests.db_helpers import make_item
    from tests.pipeline_helpers import snapshot

    env = build_env(db_engine, fake_clock, recording_next_run, candidates=[])
    with session_scope(env.factory) as session:
        job = session.get(Job, env.job_id)
        assert job is not None
        make_item(session, job, url="https://example.com/old", title="Old pending", attempts=1)
    before = snapshot(env.factory, env.job_id)
    assert [i["status"] for i in before["items"]] == [ItemStatus.NEW]

    result = await run_job(env.job_id, dry_run=True, deps=env.deps)

    # The dry run really processed the pending item ...
    assert env.ports.page_calls["https://example.com/old"] == 1
    assert env.provider.titles_for("summarize") == {"Old pending": 1}
    assert result.status == RunStatus.SUCCEEDED
    assert result.digest is not None and len(result.digest.item_ids) == 1
    # ... and left it exactly as it was: pending, attempts 1, no content, summary or run id.
    assert snapshot(env.factory, env.job_id) == before

    real = await run_job(env.job_id, deps=env.deps)

    (item,) = _rows(db_engine, Item)
    assert (item.status, item.attempts) == (ItemStatus.SUMMARIZED, 2)
    assert real.status == RunStatus.SUCCEEDED


# --- run-due: the due-aware claim (#23) ------------------------------------------------------


async def test_a_job_that_is_no_longer_due_is_refused_without_a_run_or_lock_change(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.pipeline.run import JobNotDueError

    future = fake_clock() + timedelta(days=1)
    env = build_env(db_engine, fake_clock, recording_next_run, next_run_at=future)

    with pytest.raises(JobNotDueError) as excinfo:
        await run_job(env.job_id, deps=env.deps, due_by=fake_clock())

    assert (excinfo.value.job_id, excinfo.value.next_run_at) == (env.job_id, future)
    assert _rows(db_engine, Run) == []
    job = _job(db_engine, env.job_id)
    assert (job.locked_until, job.next_run_at) == (None, future)


async def test_an_unexpired_lock_is_busy_even_with_due_by(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.pipeline.run import JobBusyError

    env = build_env(
        db_engine, fake_clock, recording_next_run, locked_until=fake_clock() + timedelta(hours=1)
    )

    with pytest.raises(JobBusyError):
        await run_job(env.job_id, deps=env.deps, due_by=fake_clock())

    assert _rows(db_engine, Run) == []


async def test_a_failed_claim_without_due_by_is_busy_as_before(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.pipeline.run import JobBusyError

    env = build_env(
        db_engine, fake_clock, recording_next_run, locked_until=fake_clock() + timedelta(hours=1)
    )

    with pytest.raises(JobBusyError):
        await run_job(env.job_id, deps=env.deps)


async def test_a_due_job_runs_with_due_by(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)

    result = await run_job(env.job_id, deps=env.deps, due_by=fake_clock())

    assert result.status == RunStatus.SUCCEEDED


async def test_an_error_between_claim_and_graph_releases_the_lock(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = build_env(db_engine, fake_clock, recording_next_run)

    def boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("scope")

    monkeypatch.setattr("invio.pipeline.run.new_scope", boom)

    with pytest.raises(RuntimeError, match="scope"):
        await run_job(env.job_id, deps=env.deps)

    assert _job(db_engine, env.job_id).locked_until is None


@pytest.mark.skipif(uses_sqlite(), reason="needs two connections; SQLite tests share one")
async def test_a_lock_taken_by_another_connection_is_classified_busy(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    from invio.pipeline.run import JobBusyError

    env = build_env(db_engine, fake_clock, recording_next_run)
    with session_scope(session_factory(db_engine)) as session:
        assert JobRepository(session).claim(
            env.job_id, now=fake_clock(), until=fake_clock() + timedelta(hours=1)
        )

    with pytest.raises(JobBusyError):
        await run_job(env.job_id, deps=env.deps, due_by=fake_clock())
