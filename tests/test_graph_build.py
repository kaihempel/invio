"""Topology and routing of the research graph, and the video path (S24)."""

import dataclasses
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine

from invio.config.job import ScheduleConfig
from invio.db.models import Item, Job
from invio.db.repositories import RunRepository
from invio.db.session import session_scope
from invio.domain import ItemStatus, RunStatus
from invio.graph import stages
from invio.graph.build import RunScope, build_graph, build_item_graph
from invio.graph.state import ItemState
from invio.pipeline.run import run_job
from tests.conftest import FakeClock
from tests.db_helpers import make_candidate, make_item
from tests.pipeline_helpers import Env, build_env, make_job_config

pytestmark = pytest.mark.usefixtures("clean_jobs")

NextRun = Callable[[ScheduleConfig, datetime], datetime]
RUN_NODES = {
    "load_job",
    "fetch_sources",
    "deduplicate",
    "keyword_prefilter",
    "process_item",
    "join",
    "synthesize_digest",
    "persist",
    "notify",
    "finalize",
}
ITEM_NODES = {"route_kind", "extract_text", "video_path", "score_relevance", "summarize_item"}


def _env(engine: Engine, clock: FakeClock, next_run: NextRun, **kwargs: Any) -> Env:
    return build_env(engine, clock, next_run, **kwargs)


def _graph(env: Env) -> Any:
    graph, _ = build_graph(
        env.deps,
        job_id=env.job_id,
        run_id=1,
        token=env.deps.clock() + timedelta(hours=1),
        dry_run=False,
    )
    return graph.get_graph()


def test_the_run_graph_has_the_documented_nodes_and_ends_only_in_finalize(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    drawn = _graph(_env(db_engine, fake_clock, recording_next_run))

    assert {n for n in drawn.nodes} - {"__start__", "__end__"} == RUN_NODES
    assert {e.source for e in drawn.edges if e.target == "__end__"} == {"finalize"}
    successors = {(e.source, e.target) for e in drawn.edges}
    assert ("keyword_prefilter", "process_item") in successors
    assert ("keyword_prefilter", "join") in successors
    assert ("process_item", "join") in successors
    assert ("notify", "finalize") in successors


def test_the_item_subgraph_has_the_documented_nodes(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = _env(db_engine, fake_clock, recording_next_run)
    scope = RunScope(job_id=env.job_id, run_id=1, token=fake_clock(), dry_run=False)

    drawn = build_item_graph(env.deps, scope).get_graph()

    assert set(drawn.nodes) - {"__start__", "__end__"} == ITEM_NODES
    successors = {(e.source, e.target) for e in drawn.edges}
    assert {("route_kind", "extract_text"), ("route_kind", "video_path")} <= successors
    assert ("video_path", "extract_text") in successors
    assert ("extract_text", "score_relevance") in successors
    assert ("score_relevance", "summarize_item") in successors
    assert ("summarize_item", "__end__") in successors
    assert ("score_relevance", "__end__") in successors


async def test_without_selected_items_the_prefilter_routes_straight_to_join(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    config = make_job_config(search__keywords={"exclude": ["Article"]})
    env = _env(db_engine, fake_clock, recording_next_run, config=config)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    assert env.ports.page_calls == {}
    assert env.provider.calls == []  # no relevance, no summary and no synthesis request
    assert result.stats["after_keyword_filter"] == 0


# --- S24: the video path --------------------------------------------------------------------


async def _stream_nodes(env: Env, kind: str, number: int) -> tuple[list[str], dict[str, Any]]:
    """Run the item subgraph for one stored item of ``kind``; return the nodes it visited."""
    with session_scope(env.factory) as session:
        run = RunRepository(session).start(env.job_id)
        job = session.get(Job, env.job_id)
        assert job is not None
        item = make_item(
            session,
            job,
            url=f"https://example.com/post-{number}",
            title=f"Article {number}",
            type=kind,
            teaser="a teaser",
        )
        run_id, item_id = run.id, item.id
    scope = RunScope(job_id=env.job_id, run_id=run_id, token=env.deps.clock(), dry_run=False)
    await stages.load_job({}, env.deps, scope)
    graph = build_item_graph(env.deps, scope)
    visited: list[str] = []
    final: dict[str, Any] = {}
    task: ItemState = {"item_id": item_id, "kind": "video" if kind == "video" else "article"}
    async for update in graph.astream(task, stream_mode="updates"):
        for name, value in update.items():
            visited.append(name)
            final.update(value)
    stages.close_work_session(scope)
    return visited, final


async def test_s24_a_video_item_takes_the_video_path_and_ends_like_an_article(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    env = _env(db_engine, fake_clock, recording_next_run, sources={})
    article_nodes, article = await _stream_nodes(env, "article", 1)
    video_nodes, video = await _stream_nodes(env, "video", 2)

    assert article_nodes == ["route_kind", "extract_text", "score_relevance", "summarize_item"]
    assert video_nodes == [
        "route_kind",
        "video_path",
        "extract_text",
        "score_relevance",
        "summarize_item",
    ]
    assert dataclasses.replace(video["relevance"], item_id=0) == dataclasses.replace(
        article["relevance"], item_id=0
    )
    assert video["summary"].status == article["summary"].status == ItemStatus.SUMMARIZED


async def test_s24_a_video_candidate_is_summarized_from_its_page_text(
    db_engine: Engine, fake_clock: FakeClock, recording_next_run: NextRun
) -> None:
    candidates = [make_candidate("https://example.com/watch-1", title="Talk 1", type="video")]
    env = _env(db_engine, fake_clock, recording_next_run, candidates=candidates)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    assert env.ports.page_calls["https://example.com/watch-1"] == 1
    assert result.stats["summarized"] == 1
    with session_scope(env.factory) as session:
        (item,) = session.query(Item).all()
        # No special status for a video: summarized from the page text like an article.
        assert (item.type, item.status) == ("video", ItemStatus.SUMMARIZED)
        assert item.raw_content and "paragraph 1" in item.raw_content
        assert result.digest is not None and list(result.digest.item_ids) == [item.id]
