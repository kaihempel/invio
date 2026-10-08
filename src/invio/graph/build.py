"""Assemble the research graph of one run.

Topology (contracts/graph.md)::

    START -> load_job -> fetch_sources -> deduplicate -> keyword_prefilter
          -> Send x N process_item (or -> join without items) -> join
          -> synthesize_digest -> persist -> notify -> finalize -> END

``process_item`` runs the item subgraph (:func:`build_item_graph`)::

    route_kind -> extract_text | video_path -> score_relevance -> summarize_item | END

The graph is compiled once per run, without a checkpointer, because its nodes close over the
per-run dependencies (:class:`~invio.graph.ports.RunDeps`) and the per-run
:class:`~invio.graph.scope.RunScope`. Every node is ``async`` so LangGraph keeps it on the
event loop thread, where the run's single work session lives. Transient failures are retried at
the call boundary (source fetch, page fetch, provider request), not by re-running nodes
(research R1, R4).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Literal, Protocol, cast

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Send
from sqlalchemy.exc import SQLAlchemyError

from invio.config.settings import MissingSettingError
from invio.db.repositories import ItemRepository
from invio.domain import ItemStatus
from invio.graph import stages
from invio.graph.budget import BudgetExceeded
from invio.graph.nodes.extract import video_path
from invio.graph.nodes.llm_calls import failure_message
from invio.graph.nodes.relevance import RelevanceOutcome
from invio.graph.nodes.summarize_item import SummaryOutcome
from invio.graph.ports import ProgressEvent, RunDeps, RunObserver
from invio.graph.scope import RunScope
from invio.graph.state import (
    ItemResult,
    ItemState,
    ItemTask,
    ItemUpdate,
    RunError,
    RunStage,
    RunState,
    error_of,
)
from invio.llm.base import LLMAuthError, LLMConfigError, LLMError
from invio.sources.errors import FetchError
from invio.sources.extract import ExtractionError

__all__ = ["build_graph", "build_item_graph", "item_failure_message", "new_scope"]

logger = logging.getLogger("invio.graph")

# Errors that no item can recover from: the run fails instead of failing every item. A database
# error leaves the shared work session unusable, and an expired lock means another run may own
# the job, so both are run-fatal too.
RUN_FATAL_ITEM_ERRORS = (
    LLMAuthError,
    LLMConfigError,
    MissingSettingError,
    SQLAlchemyError,
    stages.LockExpiredError,
)


class _RunAborted(Exception):
    """Private: an item stops before its next stage because the run is already failing."""


ItemGraph = CompiledStateGraph[ItemState, None, ItemState, ItemState]
RunGraph = CompiledStateGraph[RunState, None, RunState, RunState]
StageFn = Callable[[RunState, RunDeps, RunScope], Awaitable[RunState]]


class _RunNode(Protocol):
    """A node of the run graph (LangGraph passes the state by the name ``state``)."""

    async def __call__(self, state: RunState) -> RunState: ...


class _ItemNode(Protocol):
    """A node of the item subgraph."""

    async def __call__(self, state: ItemState) -> ItemUpdate: ...


def _recording(stage: RunStage, deps: RunDeps, scope: RunScope, node: _ItemNode) -> _ItemNode:
    """Wrap an item node so it records its stage on the scope before it runs.

    The error boundary reads it: the subgraph's state is discarded when a node raises, so the
    scope is the only place that still knows which node an item was in. Once the run is failing
    the wrapper stops the item instead of starting the node; once the run's lock has expired it
    raises :class:`~invio.graph.stages.LockExpiredError`, which fails the run.
    """

    async def entered(state: ItemState) -> ItemUpdate:
        if scope.failure is not None:
            raise _RunAborted  # do not keep spending LLM budget on a run that is failing
        scope.item_stage[state["item_id"]] = stage
        stages.ensure_lock_held(deps, scope)
        return await node(state)

    return entered


def build_item_graph(deps: RunDeps, scope: RunScope) -> ItemGraph:
    """Compile the ``process_item`` subgraph over ``deps`` and ``scope``."""

    async def route_kind(state: ItemState) -> ItemUpdate:
        scope.item_stage[state["item_id"]] = "extract_text"
        return {"stage": "extract_text"}

    async def extract(state: ItemState) -> ItemUpdate:
        return await stages.extract_text(state, deps, scope)

    async def score(state: ItemState) -> ItemUpdate:
        return await stages.score_relevance(state, deps, scope)

    async def summarize(state: ItemState) -> ItemUpdate:
        return await stages.summarize(state, deps, scope)

    def by_kind(state: ItemState) -> str:
        return "video_path" if state["kind"] == "video" else "extract_text"

    def if_relevant(state: ItemState) -> str:
        outcome = state.get("relevance")
        return "summarize_item" if outcome and outcome.status == ItemStatus.RELEVANT else END

    graph = StateGraph(ItemState)
    graph.add_node("route_kind", route_kind)
    graph.add_node("extract_text", _recording("extract_text", deps, scope, extract))
    graph.add_node("video_path", video_path)
    graph.add_node("score_relevance", _recording("score_relevance", deps, scope, score))
    graph.add_node("summarize_item", _recording("summarize_item", deps, scope, summarize))
    graph.add_edge(START, "route_kind")
    graph.add_conditional_edges("route_kind", by_kind, ["extract_text", "video_path"])
    graph.add_edge("video_path", "extract_text")
    graph.add_edge("extract_text", "score_relevance")
    graph.add_conditional_edges("score_relevance", if_relevant, ["summarize_item", END])
    graph.add_edge("summarize_item", END)
    return graph.compile()


def item_failure_message(err: Exception) -> str:
    """The ``items.last_error`` text of an item failed by the error boundary.

    LLM, fetch and extraction errors are described by ``failure_message`` (class and structured
    facts; their messages carry no query string or document text). Anything else is reduced to
    its class: a bug's message can quote anything.
    """
    if isinstance(err, LLMError | FetchError | ExtractionError):
        return failure_message(err)
    return f"{type(err).__name__}: item failed"


def _failed_outcomes(
    item_id: int, stage: RunStage, message: str, error_class: str
) -> tuple[RelevanceOutcome | None, SummaryOutcome | None]:
    """Outcomes that make ``decide_status`` count an item failed by the boundary as attempted.

    The nodes return outcomes for the failures they handle themselves; an error that escaped
    them leaves none, so one is synthesized for the stage that raised.
    """
    if stage == "summarize_item":
        summary = SummaryOutcome(
            item_id=item_id,
            status=ItemStatus.FAILED,
            summary=None,
            error=message,
            calls=0,
            chunks=0,
            truncated=False,
            error_class=error_class,
        )
        return None, summary
    relevance = RelevanceOutcome(
        item_id=item_id,
        status=ItemStatus.FAILED,
        relevance=None,
        result=None,
        error=message,
        error_class=error_class,
    )
    return relevance, None


def _node_failures(result: ItemResult) -> list[RunError]:
    """``RunError``s for outcomes a node already returned as failed (per-item LLM errors)."""
    errors: list[RunError] = []
    stages_and_outcomes: tuple[tuple[RunStage, RelevanceOutcome | SummaryOutcome | None], ...] = (
        ("score_relevance", result.relevance),
        ("summarize_item", result.summary),
    )
    for stage, outcome in stages_and_outcomes:
        if outcome is not None and outcome.status == ItemStatus.FAILED and outcome.error_class:
            errors.append(
                RunError(stage=stage, error_class=outcome.error_class, item_id=result.item_id)
            )
    return errors


def guarded(
    stage: RunStage, node: _RunNode, scope: RunScope, *, before: Callable[[], None] | None = None
) -> _RunNode:
    """Wrap a stage so an exception becomes a ``fatal`` state update instead of aborting the run.

    ``before`` runs first, inside the same guard (the lock check). The original exception is
    kept on the scope (``RunError`` holds only its class) for ``record_failed_run``; the first
    one wins. ``BaseException`` (cancellation, interrupts) is not caught: the safety net of
    ``run_job`` handles it (research R8).
    """

    async def run(state: RunState) -> RunState:
        scope.stage = stage
        try:
            if before is not None:
                before()
            return await node(state)
        except Exception as err:
            return _fatal(stage, err, scope)

    return run


def _fatal(
    stage: RunStage, err: Exception, scope: RunScope, *, item_id: int | None = None
) -> RunState:
    logger.error("run.stage_failed", extra={"stage": stage, "error": type(err).__name__})
    recorded = error_of(stage, err, item_id=item_id)
    if scope.failure is None:
        scope.failure, scope.fatal = err, recorded
    return {"fatal": recorded, "errors": [recorded]}


def route_after(next_stage: str) -> Callable[[RunState], str]:
    """Router of the edge after a guarded stage: ``finalize`` on ``fatal``, else ``next_stage``."""

    def route(state: RunState) -> str:
        return "finalize" if state.get("fatal") is not None else next_stage

    return route


def _bind(fn: StageFn, deps: RunDeps, scope: RunScope) -> _RunNode:
    async def node(state: RunState) -> RunState:
        return await fn(state, deps, scope)

    return node


def new_scope(
    deps: RunDeps,
    *,
    job_id: int,
    run_id: int,
    token: datetime,
    dry_run: bool,
    max_items: int | None = None,
    observer: RunObserver | None = None,
) -> RunScope:
    """The :class:`RunScope` of a run that ``run_job`` has claimed and started."""
    return RunScope(
        job_id=job_id,
        run_id=run_id,
        token=token,
        dry_run=dry_run,
        semaphore=asyncio.Semaphore(deps.concurrency),
        max_items=max_items,
        observer=observer,
    )


def build_graph(deps: RunDeps, scope: RunScope) -> RunGraph:
    """Compile the research graph of the run that ``scope`` belongs to."""
    item_graph = build_item_graph(deps, scope)

    async def process_item(state: ItemTask) -> RunState:
        """Run the item subgraph inside the item's error boundary (research R6)."""
        scope.stage = "extract_text"
        async with scope.semaphore:
            return await _process(state)

    async def _process(state: ItemTask) -> RunState:
        item_id = state.item_id
        if scope.failure is not None:
            return {}  # the run is already failing: items that have not started do nothing
        try:
            final = cast(
                ItemState,
                await item_graph.ainvoke({"item_id": item_id, "kind": state.kind}),
            )
        except BudgetExceeded:
            # The item is left unchanged; persist releases it (#19 R4).
            logger.info("item.budget_stopped", extra={"item_id": item_id})
            return {}
        except _RunAborted:
            return {}
        except RUN_FATAL_ITEM_ERRORS as err:
            # No item can succeed: the run fails (guarded stages route to finalize).
            return _fatal(
                scope.item_stage.get(item_id, "extract_text"), err, scope, item_id=item_id
            )
        except Exception as err:
            return _fail_item(item_id, err)
        result = stages.result_of(final)
        _report_item(result)
        return {"items": [result], "errors": _node_failures(result)}

    def _report_item(result: ItemResult) -> None:
        """Count the finished item and tell the observer (an item the boundary failed too)."""
        failed = [
            o
            for o in (result.relevance, result.summary)
            if o is not None and o.status == ItemStatus.FAILED
        ]
        relevant = result.relevance is not None and result.relevance.status == ItemStatus.RELEVANT
        outcome: Literal["relevant", "irrelevant", "failed"] = (
            "failed" if failed else "relevant" if relevant else "irrelevant"
        )
        _count_item(result.item_id, outcome, failed[0].error if failed else None, relevant=relevant)

    def _count_item(
        item_id: int,
        outcome: Literal["relevant", "irrelevant", "failed"],
        message: str | None,
        *,
        relevant: bool = False,
    ) -> None:
        """Update the live counts like ``build_stats`` does: an item rated relevant that then
        failed to summarize counts as relevant and as failed."""
        progress = scope.progress
        progress.processed += 1
        if relevant:
            progress.relevant += 1
        if outcome == "failed":
            progress.failed += 1
        ref = scope.item_refs.get(item_id)
        stage = scope.item_stage.get(item_id, "extract_text")
        stages.emit(
            scope,
            lambda: ProgressEvent(
                kind="item",
                stage=stage,
                counts=progress.snapshot(),
                item_id=item_id,
                title=ref.title if ref is not None else None,
                outcome=outcome,
                message=message,
            ),
        )

    def _fail_item(item_id: int, err: Exception) -> RunState:
        stage = scope.item_stage.get(item_id, "extract_text")
        if scope.failure is not None:
            return {}  # the run is failing already: leave the (possibly broken) session alone
        message = item_failure_message(err)
        try:
            if scope.session is None:
                raise LookupError("the work session is not open")
            items = ItemRepository(scope.session)
            item = items.get(item_id)
            if item is None:
                raise LookupError(f"item {item_id} vanished")
            items.mark_failed(item, message)
        except (SQLAlchemyError, LookupError) as db_err:
            # The item cannot be marked: the session is unusable, so the run fails.
            return _fatal(stage, db_err, scope, item_id=item_id)
        logger.warning(
            "item.failed",
            extra={"item_id": item_id, "stage": stage, "error": type(err).__name__},
        )
        relevance, summary = _failed_outcomes(item_id, stage, message, type(err).__name__)
        result = ItemResult(item_id=item_id, relevance=relevance, summary=summary)
        _count_item(item_id, "failed", message)
        return {"items": [result], "errors": [error_of(stage, err, item_id=item_id)]}

    def fan_out(state: RunState) -> list[Send] | str:
        if state.get("fatal") is not None:
            return "finalize"
        selected = state.get("selected", [])
        if not selected:
            return "join"
        return [
            Send("process_item", ItemTask(item_id=item_id, kind=scope.item_types[item_id]))
            for item_id in selected
        ]

    graph = StateGraph(RunState)

    def lock_held() -> None:
        stages.ensure_lock_held(deps, scope)

    def stage(name: RunStage, fn: StageFn, *, fenced: bool = True) -> None:
        check = lock_held if fenced else None
        graph.add_node(name, guarded(name, _bind(fn, deps, scope), scope, before=check))

    def continue_to(current: str, following: str) -> None:
        graph.add_conditional_edges(current, route_after(following), [following, "finalize"])

    stage("load_job", stages.load_job)
    stage("fetch_sources", stages.fetch_sources)
    stage("deduplicate", stages.deduplicate)
    stage("keyword_prefilter", stages.keyword_prefilter)
    graph.add_node("process_item", process_item, input_schema=ItemTask)
    graph.add_node("join", _bind(stages.join, deps, scope))
    stage("synthesize_digest", stages.synthesize_digest)
    stage("persist", stages.persist)
    stage("notify", stages.notify, fenced=False)  # the run is saved: delivery is not racing
    graph.add_node("finalize", _bind(stages.finalize, deps, scope))

    graph.add_edge(START, "load_job")
    continue_to("load_job", "fetch_sources")
    continue_to("fetch_sources", "deduplicate")
    continue_to("deduplicate", "keyword_prefilter")
    graph.add_conditional_edges("keyword_prefilter", fan_out, ["process_item", "join", "finalize"])
    graph.add_edge("process_item", "join")
    continue_to("join", "synthesize_digest")
    continue_to("synthesize_digest", "persist")
    continue_to("persist", "notify")
    graph.add_edge("notify", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile()
