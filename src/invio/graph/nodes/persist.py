"""Run persistence: save a run's results all-or-nothing, with statistics and a final status.

The run's work session holds every write of the run: item updates of the nodes, their
``llm_usage`` rows and, at the end, the digest and the run's status and statistics. Nodes only
flush; :func:`finalize_run` is the one place that commits (:func:`record_failed_run` the other,
in its own transaction). If the save fails, the work session is rolled back, so no item change
and no digest survives, and the recovery step writes the usage rows again from the budget
tracker's ledger and marks the run ``failed`` (research R1, R7).

Preconditions for the caller (#21):

* the run row and the deduplication take (``mark_taken``) are committed *before* the LLM stages
  open the work session, so a rolled-back save keeps the attempt increment and an item that
  always fails to save still reaches ``max_attempts``;
* no ``llm_usage`` row is committed before :func:`finalize_run`, because
  :func:`record_failed_run` replays the whole ledger and would otherwise write duplicates;
* a stage that raises before the save is handled with :func:`record_failed_run`, after the work
  session was rolled back (its open transaction would otherwise hold locks the recovery waits
  on, and its flushed usage rows would be replayed twice if it ever committed).

``runs.error`` never carries database or provider text: SQLAlchemy messages quote the statement
and its parameters, which can contain document text or the database URL (research R8).
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy.orm import Session, sessionmaker

from invio.db.models import Item, Run
from invio.db.repositories import (
    DigestRepository,
    ItemRepository,
    RunRepository,
    UsageRepository,
)
from invio.db.session import session_scope
from invio.domain import ItemStatus, RunStatus
from invio.graph.budget import BudgetTracker
from invio.graph.nodes.llm_calls import failure_message
from invio.graph.nodes.relevance import RelevanceOutcome
from invio.graph.nodes.summarize_item import SummaryOutcome
from invio.llm.base import LLMError

__all__ = [
    "STATS_VERSION",
    "DigestDraft",
    "RunResult",
    "StageCounts",
    "build_stats",
    "decide_status",
    "finalize_run",
    "persist_run",
    "record_failed_run",
    "unprocessed",
]

logger = logging.getLogger("invio.graph")

STATS_VERSION: Final = 1  # layout of runs.stats, see contracts/run-stats.md


@dataclass(frozen=True, slots=True, kw_only=True)
class StageCounts:
    """Counts the orchestrator collected before the LLM stages."""

    found: int
    new: int
    after_keyword_filter: int


@dataclass(frozen=True, slots=True, kw_only=True)
class DigestDraft:
    """The digest step's result; ``item_ids`` must all be taken by the run, without repeats."""

    title: str
    body: str
    item_ids: Sequence[int]


@dataclass(frozen=True, slots=True, kw_only=True)
class RunResult:
    """Everything the save step needs: what the run took, what the stages produced, the budget."""

    job_id: int
    run_id: int
    counts: StageCounts
    taken: Sequence[Item]
    relevance: Sequence[RelevanceOutcome]
    summaries: Sequence[SummaryOutcome]
    digest: DigestDraft | None
    budget: BudgetTracker


def unprocessed(result: RunResult) -> list[Item]:
    """Return the taken items that reached no final state in this run (pure).

    Final: skipped by the keyword filter, rated ``skipped_irrelevant`` or ``failed``, or
    summarized or failed in the summary stage. An item rated ``relevant`` but not summarized, or
    not rated at all, is unprocessed (a budget stop left it). Call it before the items are
    released: it reads their status.
    """
    rated = {
        o.item_id
        for o in result.relevance
        if o.status in (ItemStatus.SKIPPED_IRRELEVANT, ItemStatus.FAILED)
    }
    summarized = {
        o.item_id
        for o in result.summaries
        if o.status in (ItemStatus.SUMMARIZED, ItemStatus.FAILED)
    }
    return [
        item
        for item in result.taken
        if item.status != ItemStatus.SKIPPED_KEYWORD
        and item.id not in rated
        and item.id not in summarized
    ]


def _ledger_stats(budget: BudgetTracker) -> dict[str, Any]:
    """The token, cost and budget figures; with ``version`` the layout of a recovered run."""
    return {
        "llm_calls": budget.calls,
        "input_tokens": budget.input_tokens,
        "output_tokens": budget.output_tokens,
        "tokens": budget.used,
        "estimated_cost_usd": format(budget.cost_usd, "f"),
        "cost_complete": budget.cost_complete,
        "budget_limit": budget.limit,
        "budget_exceeded": budget.exceeded,
    }


def _failed_ids(result: RunResult) -> set[int]:
    """Ids of the items that failed in either stage."""
    outcomes: list[RelevanceOutcome | SummaryOutcome] = [*result.relevance, *result.summaries]
    return {o.item_id for o in outcomes if o.status == ItemStatus.FAILED}


# Any: runs.stats is a JSON column (dict[str, Any] on the model); the layout is documented in
# contracts/run-stats.md.
def build_stats(result: RunResult) -> dict[str, Any]:
    """Return the ``runs.stats`` object for ``result`` (pure).

    Stage counts come from the orchestrator and the outcomes, token and cost figures from the
    budget tracker's ledger, which holds exactly the rows written for the run. ``skipped_budget``
    counts the items a budget stop left unprocessed.
    """
    return {
        "version": STATS_VERSION,
        "found": result.counts.found,
        "new": result.counts.new,
        "after_keyword_filter": result.counts.after_keyword_filter,
        "relevant": sum(o.status == ItemStatus.RELEVANT for o in result.relevance),
        "summarized": sum(o.status == ItemStatus.SUMMARIZED for o in result.summaries),
        "failed": len(_failed_ids(result)),
        "skipped_budget": len(unprocessed(result)) if result.budget.exceeded else 0,
        **_ledger_stats(result.budget),
    }


def decide_status(result: RunResult) -> RunStatus:
    """Return the final status of a run that reached the save (pure; FR-010 in this order).

    ``failed`` when the budget was not exceeded and every attempted item failed (an attempted
    item has a relevance or summary outcome); ``partial`` when the budget stopped the run or any
    item failed; otherwise ``succeeded``, also for a run without items. A run that fails before
    or while saving is ``failed`` through :func:`record_failed_run`, not through this function.
    """
    attempted = {o.item_id for o in result.relevance} | {o.item_id for o in result.summaries}
    failed = _failed_ids(result)
    if not result.budget.exceeded and attempted and failed == attempted:
        return RunStatus.FAILED
    if result.budget.exceeded or failed:
        return RunStatus.PARTIAL
    return RunStatus.SUCCEEDED


def _validate_digest(result: RunResult) -> None:
    """Raise ``ValueError`` unless the digest's item ids are taken by the run, each once.

    After a budget stop the ids must also exclude the unprocessed items: they are released and
    will be processed again, so a digest must not reference them yet.
    """
    if result.digest is None:
        return
    ids = result.digest.item_ids
    if len(set(ids)) != len(ids):
        raise ValueError("digest item_ids contain duplicate ids")
    taken = {item.id for item in result.taken}
    missing = sorted(set(ids) - taken)
    if missing:
        raise ValueError(f"digest item_ids not taken by the run: {missing}")
    if result.budget.exceeded:
        released = sorted(set(ids) & {item.id for item in unprocessed(result)})
        if released:
            raise ValueError(f"digest item_ids not processed by the run: {released}")


def persist_run(session: Session, result: RunResult) -> Run:
    """Write the digest and finish the run in ``session``; flush only, the caller commits.

    When the budget was exceeded, the unprocessed items are released first (attempt undone, back
    to ``new``, research R4). Raises ``ValueError`` for an invalid digest and ``LookupError`` for
    an unknown run. Status and statistics are computed before the first write, from the items as
    the stages left them.
    """
    runs = RunRepository(session)
    run = runs.get(result.run_id)
    if run is None:
        raise LookupError(f"run {result.run_id} not found")
    _validate_digest(result)
    status, stats = decide_status(result), build_stats(result)
    if result.budget.exceeded:
        ItemRepository(session).release(unprocessed(result))
    if result.digest is not None and result.digest.item_ids:
        DigestRepository(session).add(
            result.job_id,
            result.digest.title,
            result.digest.body,
            list(result.digest.item_ids),
            run_id=result.run_id,
        )
    return runs.finish(run, status, stats=stats)


def _sanitized_error(error: BaseException) -> str:
    """Error text for ``runs.error``: LLM errors by their facts, anything else by class only."""
    if isinstance(error, LLMError):
        return failure_message(error)
    return f"{type(error).__name__}: run failed"


def record_failed_run(
    factory: sessionmaker[Session],
    *,
    job_id: int,
    run_id: int,
    budget: BudgetTracker,
    error: BaseException,
) -> None:
    """Mark the run ``failed`` in a new transaction and keep its LLM usage.

    Re-inserts every ledger entry as an ``llm_usage`` row of the run (the caller's work session
    was rolled back, taking its rows along), then finishes the run with the ledger statistics
    (stage counts are omitted, the results were discarded) and a sanitized error.

    If this transaction fails, ``run.record_failed_error`` is logged with the error class only
    and the error is re-raised: the run stays ``running`` and the caller must exit non-zero.
    """
    message = _sanitized_error(error)
    try:
        with session_scope(factory) as session:
            run = RunRepository(session).get(run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            usage = UsageRepository(session)
            for entry in budget.ledger:
                usage.add(
                    job_id,
                    entry.provider,
                    entry.model,
                    entry.input_tokens,
                    entry.output_tokens,
                    run_id=run_id,
                    purpose=entry.purpose,
                    cost_usd=entry.cost_usd,
                    created_at=entry.created_at,
                )
            RunRepository(session).finish(
                run,
                RunStatus.FAILED,
                stats={"version": STATS_VERSION, **_ledger_stats(budget)},
                error=message,
            )
    except Exception as recovery_error:
        logger.error("run.record_failed_error", extra={"error": type(recovery_error).__name__})
        raise


def _recover(
    factory: sessionmaker[Session], session: Session, result: RunResult, error: BaseException
) -> None:
    """Roll the work session back and record the failed run; only ints and classes are logged.

    The ORM objects of the session are expired or detached after the rollback, so nothing of
    them is read here. A rollback that itself fails is logged and the recovery goes on.
    """
    error_class = type(error).__name__
    try:
        session.rollback()
    except Exception as rollback_error:
        logger.error("run.rollback_failed", extra={"error": type(rollback_error).__name__})
    logger.error("run.persist_failed", extra={"error": error_class})
    record_failed_run(
        factory, job_id=result.job_id, run_id=result.run_id, budget=result.budget, error=error
    )


def finalize_run(factory: sessionmaker[Session], session: Session, result: RunResult) -> RunStatus:
    """Save the run in ``session`` with one commit and return its final status.

    On success logs ``run.persisted`` (status and every statistic as fields). If the save fails
    the work session is rolled back and :func:`record_failed_run` runs; the save error itself is
    not re-raised and the status is ``failed``. An error of that recovery transaction *is*
    re-raised (see :func:`record_failed_run`), as is ``KeyboardInterrupt`` or ``SystemExit``
    after the recovery. A commit that fails after the database applied it (lost connection)
    leads to duplicate usage rows from the replay; that case is accepted.
    """
    try:
        run = persist_run(session, result)
        status, stats = RunStatus(run.status), dict(run.stats or {})  # read before the commit
        session.commit()
    except Exception as error:
        _recover(factory, session, result, error)
        return RunStatus.FAILED
    except BaseException as error:
        _recover(factory, session, result, error)
        raise
    logger.info("run.persisted", extra={"status": status.value, **stats})
    return status
