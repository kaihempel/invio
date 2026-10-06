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

from pydantic import ValidationError
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
    "ALL_FAILED_ERROR",
    "ALL_SOURCES_FAILED_ERROR",
    "STATS_VERSION",
    "DigestDraft",
    "RunDraft",
    "StageCounts",
    "build_stats",
    "decide_status",
    "failure_error",
    "finalize_run",
    "finish_dry_run",
    "persist_run",
    "record_failed_run",
    "unprocessed",
]

logger = logging.getLogger("invio.graph")

STATS_VERSION: Final = 1  # layout of runs.stats, see contracts/run-stats.md
ALL_FAILED_ERROR: Final = "all attempted items failed"  # runs.error of a run failed by its items
ALL_SOURCES_FAILED_ERROR: Final = "all sources failed"  # runs.error when no source could be read


@dataclass(frozen=True, slots=True, kw_only=True)
class StageCounts:
    """Counts the orchestrator collected before the LLM stages."""

    found: int
    new: int
    after_keyword_filter: int
    sources: int = 0  # enabled sources with an adapter
    sources_failed: int = 0  # of those, still failing after retries


@dataclass(frozen=True, slots=True, kw_only=True)
class DigestDraft:
    """The digest step's result; ``item_ids`` must all be taken by the run, without repeats."""

    title: str
    body: str
    item_ids: Sequence[int]


@dataclass(frozen=True, slots=True, kw_only=True)
class RunDraft:
    """Everything the save step needs: what the run took, what the stages produced, the budget."""

    job_id: int
    run_id: int
    counts: StageCounts
    taken: Sequence[Item]
    relevance: Sequence[RelevanceOutcome]
    summaries: Sequence[SummaryOutcome]
    digest: DigestDraft | None
    budget: BudgetTracker
    dry_run: bool = False
    # The digest is the fallback text (the digest call failed): a ``succeeded`` run is ``partial``.
    fallback_digest: bool = False
    # Store the digest even when its ``item_ids`` are empty (``notification.send_if_empty``).
    store_empty_digest: bool = False


def unprocessed(result: RunDraft) -> list[Item]:
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
    """The token, cost and budget figures; with ``version`` the layout of a recovered run.

    ``budget_exceeded`` says the budget stopped per-item calls; ``over_budget`` says the tokens
    ended above the limit, which the last per-item call or the digest call can cause on its own.
    """
    return {
        "llm_calls": budget.calls,
        "input_tokens": budget.input_tokens,
        "output_tokens": budget.output_tokens,
        "tokens": budget.used,
        "estimated_cost_usd": format(budget.cost_usd, "f"),
        "cost_complete": budget.cost_complete,
        "budget_limit": budget.limit,
        "budget_exceeded": budget.exceeded,
        "over_budget": budget.used > budget.limit,
    }


def _failed_ids(result: RunDraft) -> set[int]:
    """Ids of the items that failed in either stage."""
    outcomes: list[RelevanceOutcome | SummaryOutcome] = [*result.relevance, *result.summaries]
    return {o.item_id for o in outcomes if o.status == ItemStatus.FAILED}


# Any: runs.stats is a JSON column (dict[str, Any] on the model); the layout is documented in
# contracts/run-stats.md.
def build_stats(result: RunDraft) -> dict[str, Any]:
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
        "sources": result.counts.sources,
        "sources_failed": result.counts.sources_failed,
        "relevant": sum(o.status == ItemStatus.RELEVANT for o in result.relevance),
        "summarized": sum(o.status == ItemStatus.SUMMARIZED for o in result.summaries),
        "failed": len(_failed_ids(result)),
        "skipped_budget": len(unprocessed(result)) if result.budget.exceeded else 0,
        **_ledger_stats(result.budget),
    }


def _all_sources_failed(result: RunDraft) -> bool:
    """Whether every source with an adapter failed (and there was at least one)."""
    return result.counts.sources >= 1 and result.counts.sources_failed == result.counts.sources


def _items_all_failed(result: RunDraft) -> bool:
    attempted = {o.item_id for o in result.relevance} | {o.item_id for o in result.summaries}
    return not result.budget.exceeded and bool(attempted) and _failed_ids(result) == attempted


def decide_status(result: RunDraft) -> RunStatus:
    """Return the final status of a run that reached the save (pure; FR-009/FR-010 in order).

    ``failed`` when every source with an adapter failed, or when the budget was not exceeded and
    every attempted item failed (an attempted item has a relevance or summary outcome);
    ``partial`` when a source failed, the budget stopped the run or any item failed; otherwise
    ``succeeded``, also for a run without items. A run that fails before or while saving is
    ``failed`` through :func:`record_failed_run`, not through this function.
    """
    if _all_sources_failed(result) or _items_all_failed(result):
        return RunStatus.FAILED
    if (
        result.counts.sources_failed > 0
        or result.budget.exceeded
        or result.fallback_digest
        or _failed_ids(result)
    ):
        return RunStatus.PARTIAL
    return RunStatus.SUCCEEDED


def failure_error(result: RunDraft) -> str | None:
    """The ``runs.error`` text of a run failed by its sources or items, else ``None`` (pure)."""
    if _all_sources_failed(result):
        return ALL_SOURCES_FAILED_ERROR
    if _items_all_failed(result):
        return ALL_FAILED_ERROR
    return None


def _validate_digest(result: RunDraft) -> None:
    """Raise ``ValueError`` unless the digest's item ids are taken and summarized by the run, once.

    A digest uses the items the run processed successfully (FR-005): not a failed or irrelevant
    item, and not an item a budget stop left unprocessed (it is released and processed again).
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
    summarized = {o.item_id for o in result.summaries if o.status == ItemStatus.SUMMARIZED}
    unsummarized = sorted(set(ids) - summarized)
    if unsummarized:
        raise ValueError(f"digest item_ids not summarized by the run: {unsummarized}")


def persist_run(session: Session, result: RunDraft) -> Run:
    """Write the digest and finish the run in ``session``; flush only, the caller commits.

    When the budget was exceeded, the unprocessed items are released first (attempt undone, back
    to ``new``, research R4). Raises ``ValueError`` for an invalid digest and ``LookupError`` for
    an unknown run. Status and statistics are computed before the first write, from the items as
    the stages left them; a run failed by its sources or items gets
    :data:`ALL_SOURCES_FAILED_ERROR` or :data:`ALL_FAILED_ERROR` as its error.
    """
    runs = RunRepository(session)
    run = runs.get(result.run_id)
    if run is None:
        raise LookupError(f"run {result.run_id} not found")
    _validate_digest(result)
    status, stats = decide_status(result), build_stats(result)
    if result.budget.exceeded:
        ItemRepository(session).release(unprocessed(result))
    if result.digest is not None and (result.digest.item_ids or result.store_empty_digest):
        DigestRepository(session).add(
            result.job_id,
            result.digest.title,
            result.digest.body,
            list(result.digest.item_ids),
            run_id=result.run_id,
        )
    return runs.finish(run, status, stats=stats, error=failure_error(result))


MAX_ERROR_PATHS: Final = 5  # field paths named in the error of an invalid stored job config


def _invalid_fields(error: ValidationError) -> str:
    """``"ValidationError: invalid fields <path>[, <path>…]"``: paths only, never values.

    pydantic's messages quote the offending input, so only the dotted ``loc`` of each error is
    used (repeats dropped, at most :data:`MAX_ERROR_PATHS`, then ``", …"``).
    """
    paths: list[str] = []
    for detail in error.errors(include_input=False, include_url=False):
        path = ".".join(str(part) for part in detail["loc"]) or "(root)"
        if path not in paths:
            paths.append(path)
    text = ", ".join(paths[:MAX_ERROR_PATHS])
    if len(paths) > MAX_ERROR_PATHS:
        text += ", …"
    return f"{type(error).__name__}: invalid fields {text or '(root)'}"


def _sanitized_error(error: BaseException) -> str:
    """Error text for ``runs.error``: LLM errors by their facts, a config ``ValidationError`` by
    its field paths, anything else by class only."""
    if isinstance(error, LLMError):
        return failure_message(error)
    if isinstance(error, ValidationError):
        return _invalid_fields(error)
    return f"{type(error).__name__}: run failed"


def record_failed_run(
    factory: sessionmaker[Session],
    *,
    job_id: int,
    run_id: int,
    budget: BudgetTracker,
    error: BaseException,
    replay_usage: bool = True,
) -> RunStatus:
    """Mark the run ``failed`` in a new transaction, keep its LLM usage and return its status.

    Re-inserts every ledger entry as an ``llm_usage`` row of the run (the caller's work session
    was rolled back, taking its rows along), then finishes the run with the ledger statistics
    (stage counts are omitted, the results were discarded) and a sanitized error.

    ``replay_usage=False`` is for a dry run: no usage row is written and the stats carry
    ``"dry_run": True``, so nothing but the run row remains.

    A run that is no longer ``running`` is left as it is and its status returned: a save whose
    commit failed after the database applied it has already stored the run, its usage rows and
    its final status, so a replay would duplicate the rows and overwrite the real status.

    If this transaction fails, ``run.record_failed_error`` is logged with the error class only
    and the error is re-raised: the run stays ``running`` and the caller must exit non-zero.
    """
    message = _sanitized_error(error)
    try:
        with session_scope(factory) as session:
            runs = RunRepository(session)
            run = runs.get(run_id)
            if run is None:
                raise LookupError(f"run {run_id} not found")
            if run.status != RunStatus.RUNNING:
                logger.warning(
                    "run.already_finished",
                    extra={"status": run.status, "job_id": job_id, "db_run_id": run_id},
                )
                return RunStatus(run.status)
            usage = UsageRepository(session)
            for entry in budget.ledger if replay_usage else ():
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
            runs.finish(
                run,
                RunStatus.FAILED,
                stats={
                    "version": STATS_VERSION,
                    **_ledger_stats(budget),
                    **({} if replay_usage else {"dry_run": True}),
                },
                error=message,
            )
    except Exception as recovery_error:
        logger.error(
            "run.record_failed_error",
            extra={"error": type(recovery_error).__name__, "job_id": job_id, "db_run_id": run_id},
        )
        raise
    return RunStatus.FAILED


def _recover(
    factory: sessionmaker[Session], session: Session, result: RunDraft, error: BaseException
) -> RunStatus:
    """Roll the work session back, record the failed run and return the run's stored status.

    Only ints and classes are logged.

    The ORM objects of the session are expired or detached after the rollback, so nothing of
    them is read here. A rollback that itself fails is logged and the recovery goes on.
    """
    ids = {"job_id": result.job_id, "db_run_id": result.run_id}
    try:
        session.rollback()
    except Exception as rollback_error:
        logger.error("run.rollback_failed", extra={"error": type(rollback_error).__name__, **ids})
    logger.error("run.persist_failed", extra={"error": type(error).__name__, **ids})
    return record_failed_run(
        factory,
        job_id=result.job_id,
        run_id=result.run_id,
        budget=result.budget,
        error=error,
        replay_usage=not result.dry_run,
    )


def finalize_run(factory: sessionmaker[Session], session: Session, result: RunDraft) -> RunStatus:
    """Save the run in ``session`` with one commit and return its final status.

    On success logs ``run.persisted`` (status and every statistic as fields). If the save fails
    the work session is rolled back and :func:`record_failed_run` runs; the save error itself is
    not re-raised and the status is ``failed``. An error of that recovery transaction *is*
    re-raised (see :func:`record_failed_run`), as is ``KeyboardInterrupt`` or ``SystemExit``
    after the recovery. A commit that fails after the database applied it (lost connection) is
    detected by the recovery (the run is no longer ``running``): nothing is replayed and the
    stored status is returned.
    """
    try:
        run = persist_run(session, result)
        status, stats = RunStatus(run.status), dict(run.stats or {})  # read before the commit
        session.commit()
    except Exception as error:
        return _recover(factory, session, result, error)
    except BaseException as error:
        _recover(factory, session, result, error)
        raise
    ids = {"job_id": result.job_id, "db_run_id": result.run_id}
    logger.info("run.persisted", extra={"status": status.value, **ids, **stats})
    return status


def finish_dry_run(factory: sessionmaker[Session], session: Session, result: RunDraft) -> RunStatus:
    """Finish a dry run: keep the run row with its statistics and discard everything else.

    The status, statistics and error are computed first, while the items are still readable;
    then the work session is rolled back (item inserts, attempts, statuses, usage rows and the
    digest all vanish) and the run is finished in a new transaction with ``stats["dry_run"]``
    set. If that fails, the run is recorded failed like a failed save, without replaying usage.
    Logs ``run.persisted`` with ``dry_run=True``; returns the final status.
    """
    try:
        _validate_digest(result)
        status, stats = decide_status(result), {**build_stats(result), "dry_run": True}
        error = failure_error(result)
        session.rollback()
        with session_scope(factory) as fresh:
            runs = RunRepository(fresh)
            run = runs.get(result.run_id)
            if run is None:
                raise LookupError(f"run {result.run_id} not found")
            runs.finish(run, status, stats=stats, error=error)
    except Exception as err:
        return _recover(factory, session, result, err)
    except BaseException as err:
        _recover(factory, session, result, err)
        raise
    ids = {"job_id": result.job_id, "db_run_id": result.run_id}
    logger.info("run.persisted", extra={"status": status.value, **ids, **stats})
    return status
