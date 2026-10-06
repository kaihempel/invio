"""Graph state and ports: ``RunError`` hygiene, reducers and ``RunDeps`` validation."""

import dataclasses
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from invio.config.job import LLMConfig, ScheduleConfig, SourceConfig
from invio.domain import Candidate
from invio.graph.ports import DeliveryReport, ProviderBinding, RunDeps
from invio.graph.state import (
    STAGE_ORDER,
    ItemResult,
    ItemTask,
    RunError,
    error_of,
    keep_first,
)
from invio.llm.base import LLMUnavailableError
from invio.retry import RetrySettings


def test_run_error_rejects_item_and_source_together() -> None:
    with pytest.raises(ValueError, match="not both"):
        RunError(stage="fetch_sources", error_class="FetchError", item_id=1, source="0:rss")


@pytest.mark.parametrize("blank", ["", "   "])
def test_run_error_rejects_a_blank_error_class(blank: str) -> None:
    with pytest.raises(ValueError, match="error_class"):
        RunError(stage="persist", error_class=blank)


def test_run_error_accepts_item_or_source_alone() -> None:
    assert RunError(stage="extract_text", error_class="X", item_id=3).source is None
    assert RunError(stage="fetch_sources", error_class="X", source="1:rss").item_id is None


def test_error_of_keeps_only_the_class_name() -> None:
    err = LLMUnavailableError("secret-token-123 leaked in provider text")

    recorded = error_of("score_relevance", err, item_id=7)

    assert recorded == RunError(
        stage="score_relevance", error_class="LLMUnavailableError", item_id=7
    )
    assert "secret" not in repr(recorded)


def test_stage_order_lists_every_stage_in_execution_order() -> None:
    assert STAGE_ORDER[0] == "load_job"
    assert STAGE_ORDER[-1] == "finalize"
    assert len(STAGE_ORDER) == len(set(STAGE_ORDER)) == 11


def test_item_task_and_result_are_frozen() -> None:
    task = ItemTask(item_id=1, kind="article")
    result = ItemResult(item_id=1)

    with pytest.raises(dataclasses.FrozenInstanceError):
        task.item_id = 2  # type: ignore[misc]
    assert (result.relevance, result.summary) == (None, None)


def test_keep_first_prefers_the_first_written_value() -> None:
    first = RunError(stage="load_job", error_class="A")
    second = RunError(stage="persist", error_class="B")

    assert keep_first(None, first) is first
    assert keep_first(first, second) is first
    assert keep_first(first, None) is first
    assert keep_first(None, None) is None


# --- RunDeps ------------------------------------------------------------------------------------


async def _fetch_source(config: SourceConfig) -> list[Candidate]:
    return []


async def _fetch_page(url: str) -> str:
    return ""


async def _notify(digest_id: int) -> DeliveryReport:
    return DeliveryReport(sent=1, failed=0)


def _provider_for(config: LLMConfig) -> ProviderBinding:
    raise AssertionError("not called")


def _next_run(schedule: ScheduleConfig, after: datetime) -> datetime:
    return after + timedelta(hours=1)


def _deps(**overrides: Any) -> RunDeps:
    values: dict[str, Any] = {
        "session_factory": sessionmaker(create_engine("sqlite://")),
        "fetch_source": _fetch_source,
        "fetch_page": _fetch_page,
        "provider_for": _provider_for,
        "notify": _notify,
        "next_run": _next_run,
        "clock": lambda: datetime(2026, 10, 4, tzinfo=UTC),
    }
    values.update(overrides)
    return RunDeps(**values)


def test_run_deps_defaults() -> None:
    deps = _deps()

    assert deps.concurrency == 4
    assert deps.lock_ttl == timedelta(hours=2)
    assert deps.retry == RetrySettings()


@pytest.mark.parametrize("concurrency", [0, -1])
def test_run_deps_rejects_concurrency_below_one(concurrency: int) -> None:
    with pytest.raises(ValueError, match="concurrency"):
        _deps(concurrency=concurrency)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_run_deps_rejects_a_non_positive_lock_ttl(ttl: timedelta) -> None:
    with pytest.raises(ValueError, match="lock_ttl"):
        _deps(lock_ttl=ttl)


# --- Reducers (FR-005) --------------------------------------------------------------------------


async def test_parallel_branches_finishing_in_reverse_order_lose_no_result() -> None:
    import asyncio

    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Send

    from invio.graph.state import ItemTask, RunState

    count = 10
    done = [asyncio.Event() for _ in range(count)]
    finished: list[int] = []

    async def start(state: RunState) -> RunState:
        return {}

    def fan_out(state: RunState) -> list[Send]:
        return [Send("work", ItemTask(item_id=n, kind="article")) for n in range(count)]

    async def work(state: ItemTask) -> RunState:
        n = state.item_id
        if n + 1 < count:
            await done[n + 1].wait()  # the last item finishes first, deterministically
        finished.append(n)
        done[n].set()
        error = RunError(stage="extract_text", error_class="X", item_id=n)
        return {"items": [ItemResult(item_id=n)], "errors": [error]}

    async def join(state: RunState) -> RunState:
        return {}

    graph = StateGraph(RunState)
    graph.add_node("start", start)
    graph.add_node("work", work, input_schema=ItemTask)
    graph.add_node("join", join)
    graph.add_edge(START, "start")
    graph.add_conditional_edges("start", fan_out, ["work"])
    graph.add_edge("work", "join")
    graph.add_edge("join", END)

    final = await asyncio.wait_for(graph.compile().ainvoke({"items": [], "errors": []}), timeout=10)

    assert finished == list(reversed(range(count)))
    assert sorted(r.item_id for r in final["items"]) == list(range(count))
    assert sorted(e.item_id for e in final["errors"] if e.item_id is not None) == list(range(count))


async def test_the_first_fatal_error_wins_in_parallel_branches() -> None:
    from langgraph.graph import END, START, StateGraph
    from langgraph.types import Send

    from invio.graph.state import ItemTask, RunState

    def fan_out(state: RunState) -> list[Send]:
        return [Send("work", ItemTask(item_id=n, kind="article")) for n in range(3)]

    async def start(state: RunState) -> RunState:
        return {}

    async def work(state: ItemTask) -> RunState:
        return {"fatal": RunError(stage="extract_text", error_class=f"E{state.item_id}")}

    graph = StateGraph(RunState)
    graph.add_node("start", start)
    graph.add_node("work", work, input_schema=ItemTask)
    graph.add_edge(START, "start")
    graph.add_conditional_edges("start", fan_out, ["work"])
    graph.add_edge("work", END)

    final = await graph.compile().ainvoke({})

    assert final["fatal"] is not None
    assert final["fatal"].error_class in {"E0", "E1", "E2"}
