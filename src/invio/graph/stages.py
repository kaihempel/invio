"""Stage functions of the research graph, one ``async`` function per stage.

Every stage takes ``(state, deps, scope)`` and returns a partial state update;
``invio.graph.build`` binds them to the graph as closures. All stages are ``async`` so that
LangGraph keeps them on the event loop thread: the run's single work session is only ever used
from that thread, and no repository call spans an ``await`` (research R5).

Logging: ``logging.getLogger("invio.graph")`` with ``extra={...}``; fields carry ids, source
keys and error classes only, never URLs with a query string or provider, database or document
text.
"""

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Final

from pydantic import ValidationError
from sqlalchemy.orm import Session

from invio.config.job import JobConfig, LimitsConfig, ScheduleConfig, SourceConfig
from invio.db.models import Item
from invio.db.repositories import (
    DigestRepository,
    ItemRepository,
    JobRepository,
    NotificationRepository,
    RunRepository,
    UsageRepository,
)
from invio.db.session import session_scope
from invio.domain import Candidate, ItemStatus, NotificationStatus, RunStatus
from invio.graph.budget import BudgetTracker
from invio.graph.errors import stored_errors
from invio.graph.nodes.deduplicate import deduplicate as run_deduplicate
from invio.graph.nodes.extract import extract_item
from invio.graph.nodes.keyword_filter import keyword_filter
from invio.graph.nodes.persist import (
    DigestDraft,
    RunDraft,
    StageCounts,
    decide_status,
    finalize_run,
    finish_dry_run,
    record_failed_run,
)
from invio.graph.nodes.relevance import RelevanceOutcome, ScoringContext, score_item
from invio.graph.nodes.summarize_item import SummaryContext, SummaryOutcome, summarize_item
from invio.graph.nodes.synthesize import (
    SynthesisContext,
    entries_from_items,
)
from invio.graph.nodes.synthesize import synthesize_digest as run_synthesis
from invio.graph.ports import DeliveryReport, ProgressEvent, RunDeps
from invio.graph.scope import ItemRef, RunScope
from invio.graph.state import (
    ItemResult,
    ItemState,
    ItemUpdate,
    RunError,
    RunStage,
    RunState,
    error_of,
)
from invio.llm.retry import RetryingProvider
from invio.retry import retrying
from invio.sources.errors import FetchError, is_transient_fetch
from invio.sources.urls import without_query
from invio.textsafe import strip_control

__all__ = [
    "LockExpiredError",
    "close_work_session",
    "deduplicate",
    "emit",
    "emit_stage",
    "ensure_lock_held",
    "extract_text",
    "fetch_sources",
    "finalize",
    "join",
    "keyword_prefilter",
    "load_job",
    "notify",
    "persist",
    "record_failure",
    "release_lock",
    "rollback_work_session",
    "score_relevance",
    "stage_event",
    "summarize",
    "synthesize_digest",
]

logger = logging.getLogger("invio.graph")

# Source types that have an adapter. The others are skipped, not failed (research R9).
SUPPORTED_SOURCES: Final = frozenset({"rss", "web"})


# --- Shared helpers -------------------------------------------------------------------------


def work_session(deps: RunDeps, scope: RunScope) -> Session:
    """The run's single work session, opened on first use."""
    if scope.session is None:
        scope.session = deps.session_factory()
    return scope.session


def close_work_session(scope: RunScope) -> None:
    """Close the work session (after commit or rollback) so no transaction stays open."""
    session, scope.session = scope.session, None
    if session is not None:
        session.close()


def _required[T](value: T | None, what: str) -> T:
    """``value``, which ``load_job`` (or an earlier stage) must have set; a bug otherwise."""
    if value is None:
        raise RuntimeError(f"{what} is not set: the stage ran out of order")
    return value


def _load_item(items: ItemRepository, item_id: int) -> Item:
    return _required(items.get(item_id), f"item {item_id}")


class LockExpiredError(RuntimeError):
    """The run outlived its job lock: another run may own the job now, so this one stops."""


def ensure_lock_held(deps: RunDeps, scope: RunScope) -> None:
    """Raise :class:`LockExpiredError` once the run's lock (its token) has expired.

    Checked before every stage that writes run data and before every item node, so a run that
    takes longer than ``lock_ttl`` stops at the next boundary instead of racing a run that
    claimed the expired lock (research R8). ``claim`` takes a lock over at ``locked_until <=
    now``, hence ``>=`` here.
    """
    if deps.clock() >= scope.token:
        raise LockExpiredError(f"the lock of job {scope.job_id} expired at {scope.token}")


def stage_event(scope: RunScope, stage: RunStage) -> ProgressEvent:
    """A ``stage`` event carrying a frozen copy of the run's counts so far."""
    return ProgressEvent(kind="stage", stage=stage, counts=scope.progress.snapshot())


def emit(scope: RunScope, event: ProgressEvent | Callable[[], ProgressEvent]) -> None:
    """Report progress to the run's observer; never raises and never fails the run.

    ``event`` may be a callable that builds the event, so building (the counts snapshot) is
    inside the guard too. The first failure is logged (class only) and drops the observer: a
    broken display is not called again.
    """
    observer = scope.observer
    if observer is None:
        return
    try:
        observer(event() if callable(event) else event)
    except Exception as err:  # not BaseException: Ctrl-C and cancellation must pass
        logger.warning("run.observer_failed", extra={"error": type(err).__name__})
        scope.observer = None


def emit_stage(scope: RunScope, stage: RunStage) -> None:
    """Report that ``stage`` completed."""
    emit(scope, lambda: stage_event(scope, stage))


def item_ref(title: str, url: str) -> ItemRef:
    """The terminal-safe reference of an item: no control characters, no query or fragment."""
    return ItemRef(title=strip_control(title, limit=300), url=strip_control(without_query(url)))


def source_key(index: int, source: SourceConfig) -> str:
    """The key of a configured source in ``candidates`` and ``RunError.source``."""
    return f"{index}:{source.type}"


# --- Run stages -----------------------------------------------------------------------------


async def load_job(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Validate the stored job config and bind the provider and the token budget.

    The run row and the run context already exist (created by ``run_job``). Context variables
    are not set here: LangGraph runs every node in a task with a copied context, so they would
    not reach later nodes (research R10). A ``ValidationError``, ``MissingSettingError`` or
    ``LLMConfigError`` propagates, so a broken config or a missing key fails the run before any
    LLM call.
    """
    with session_scope(deps.session_factory) as session:
        raw = _required(JobRepository(session).get(scope.job_id), f"job {scope.job_id}").config
    config = JobConfig.model_validate(raw)
    scope.config = config
    binding = deps.provider_for(config.llm)
    # Every provider request of the run is retried on transient errors (research R4).
    retrying_provider = RetryingProvider(binding.provider, policy=deps.retry, sleep=deps.sleep)
    scope.binding = dataclasses.replace(binding, provider=retrying_provider)
    scope.budget = BudgetTracker(config.limits.max_llm_tokens_per_run)
    if scope.max_items is not None:
        # In memory only: the stored job config is never written. The cap can only go down.
        limits = config.limits
        capped = min(scope.max_items, limits.max_items_per_run)
        scope.config = config.model_copy(
            update={"limits": limits.model_copy(update={"max_items_per_run": capped})}
        )
    return {}


@dataclasses.dataclass(frozen=True, slots=True)
class _Fetched:
    """The outcome of one source fetch: its candidates, or the ``FetchError`` that persisted."""

    candidates: list[Candidate]
    error: FetchError | None = None


async def _fetch_one(source: SourceConfig, deps: RunDeps, scope: RunScope) -> _Fetched:
    """Fetch one source, retrying transient errors; the slot is held per attempt, not per wait."""

    async def attempt() -> list[Candidate]:
        async with scope.semaphore:
            return await deps.fetch_source(source)

    try:
        found = await retrying(
            attempt,
            policy=deps.retry,
            retry_on=is_transient_fetch,
            what="source",
            sleep=deps.sleep,
        )
    except FetchError as err:
        return _Fetched([], err)
    return _Fetched(found)


async def fetch_sources(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Fetch the enabled sources in parallel (bounded); cut each to ``max_items_per_source``.

    Results are merged in configuration order. Only a ``FetchError`` that persists after the
    retries is a source failure; any other exception is a bug or a credential problem and
    reaches the stage guard (research R9), after the other fetches have finished.
    """
    config = _required(scope.config, "config")
    wanted: list[tuple[str, SourceConfig]] = []
    for index, source in enumerate(config.sources):
        if not source.enabled:
            continue
        key = source_key(index, source)
        if source.type in SUPPORTED_SOURCES:
            wanted.append((key, source))
        else:
            logger.warning("source.unsupported", extra={"source": key, "type": source.type})
    outcomes = await asyncio.gather(
        *(_fetch_one(source, deps, scope) for _, source in wanted), return_exceptions=True
    )
    unexpected = [
        (key, outcome)
        for (key, _), outcome in zip(wanted, outcomes, strict=True)
        if isinstance(outcome, BaseException)
    ]
    if unexpected:
        for key, other in unexpected[1:]:  # only the first is raised: log the others' classes
            logger.error(
                "source.fetch_dropped", extra={"source": key, "error": type(other).__name__}
            )
        raise unexpected[0][1]
    candidates: dict[str, list[Candidate]] = {}
    errors: list[RunError] = []
    for (key, _), outcome in zip(wanted, outcomes, strict=True):
        assert not isinstance(outcome, BaseException)
        if outcome.error is not None:
            logger.warning(
                "source.failed", extra={"source": key, "error": type(outcome.error).__name__}
            )
            errors.append(error_of("fetch_sources", outcome.error, source=key))
        else:
            candidates[key] = outcome.candidates[: config.limits.max_items_per_source]
    return {
        "candidates": candidates,
        "sources_total": len(wanted),
        "sources_failed": len(errors),
        "errors": errors,
    }


async def deduplicate(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Select this run's items. A normal run commits the take before the LLM stages begin."""
    config = _required(scope.config, "config")
    session = work_session(deps, scope)
    result = run_deduplicate(
        session,
        job_id=scope.job_id,
        run_id=scope.run_id,
        candidates=state.get("candidates", {}),
        limits=config.limits,
    )
    if not scope.dry_run:
        session.commit()
    scope.item_types.update({item.id: item.type for item in result.items})
    scope.item_refs.update({item.id: item_ref(item.title, item.url) for item in result.items})
    counts = StageCounts(found=result.stats.found, new=result.stats.new, after_keyword_filter=0)
    scope.progress.found, scope.progress.new = counts.found, counts.new
    emit_stage(scope, "deduplicate")
    return {"taken": [item.id for item in result.items], "counts": counts}


async def keyword_prefilter(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Apply the keyword rules to the taken items; a rejected item becomes ``skipped_keyword``."""
    config = _required(scope.config, "config")
    items = ItemRepository(work_session(deps, scope))
    selected: list[int] = []
    for item_id in state.get("taken", []):
        item = _load_item(items, item_id)
        if keyword_filter(item, config.search.keywords, items).matched:
            selected.append(item_id)
    counts = state.get("counts") or StageCounts(found=0, new=0, after_keyword_filter=0)
    scope.progress.after_keyword_filter = scope.progress.selected = len(selected)
    emit_stage(scope, "keyword_prefilter")
    return {
        "selected": selected,
        "counts": dataclasses.replace(counts, after_keyword_filter=len(selected)),
    }


async def join(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Barrier after the fan-out; nothing to do."""
    return {}


def _scoring_context(deps: RunDeps, scope: RunScope) -> ScoringContext:
    binding, session = _required(scope.binding, "binding"), work_session(deps, scope)
    return ScoringContext(
        job_id=scope.job_id,
        run_id=scope.run_id,
        search=_required(scope.config, "config").search,
        provider=binding.provider,
        provider_name=binding.provider_name,
        model=binding.fast_model,
        registry=binding.registry,
        items=ItemRepository(session),
        usage=UsageRepository(session),
        budget=_required(scope.budget, "budget"),
    )


def _summary_context(deps: RunDeps, scope: RunScope) -> SummaryContext:
    binding, session = _required(scope.binding, "binding"), work_session(deps, scope)
    config = _required(scope.config, "config")
    return SummaryContext(
        job_id=scope.job_id,
        run_id=scope.run_id,
        language=config.language,
        semantic_description=config.search.semantic_description,
        provider=binding.provider,
        provider_name=binding.provider_name,
        fast_model=binding.fast_model,
        smart_model=binding.smart_model,
        registry=binding.registry,
        items=ItemRepository(session),
        usage=UsageRepository(session),
        budget=_required(scope.budget, "budget"),
    )


# --- Per-item stages (nodes of the process_item subgraph) -------------------------------------


async def extract_text(state: ItemState, deps: RunDeps, scope: RunScope) -> ItemUpdate:
    """Fetch the item page and store its text; a too short page keeps title and teaser."""
    session = work_session(deps, scope)
    item = _load_item(ItemRepository(session), state["item_id"])
    await extract_item(
        item,
        fetch_page=deps.fetch_page,
        items=ItemRepository(session),
        retry=deps.retry,
        sleep=deps.sleep,
    )
    return {"stage": "extract_text"}


async def score_relevance(state: ItemState, deps: RunDeps, scope: RunScope) -> ItemUpdate:
    """Rate the item with the ``fast`` model."""
    item = _load_item(ItemRepository(work_session(deps, scope)), state["item_id"])
    outcome = await score_item(item, _scoring_context(deps, scope))
    return {"stage": "score_relevance", "relevance": outcome}


async def summarize(state: ItemState, deps: RunDeps, scope: RunScope) -> ItemUpdate:
    """Summarize a relevant item."""
    item = _load_item(ItemRepository(work_session(deps, scope)), state["item_id"])
    outcome = await summarize_item(item, _summary_context(deps, scope))
    return {"stage": "summarize_item", "summary": outcome}


def result_of(state: ItemState) -> ItemResult:
    """The :class:`ItemResult` of a finished item subgraph."""
    return ItemResult(
        item_id=state["item_id"],
        relevance=state.get("relevance"),
        summary=state.get("summary"),
    )


# --- Closing stages ---------------------------------------------------------------------------


def _outcomes(
    state: RunState,
) -> tuple[list[RelevanceOutcome], list[SummaryOutcome]]:
    results = state.get("items", [])
    relevance = [r.relevance for r in results if r.relevance is not None]
    summaries = [r.summary for r in results if r.summary is not None]
    return relevance, summaries


async def synthesize_digest(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Write the digest of the summarized items with one ``smart`` call (none without items)."""
    config, binding = _required(scope.config, "config"), _required(scope.binding, "binding")
    session = work_session(deps, scope)
    _, summaries = _outcomes(state)
    items = ItemRepository(session)
    rows = [
        row
        for outcome in summaries
        if outcome.status == ItemStatus.SUMMARIZED and (row := items.get(outcome.item_id))
    ]
    entries = entries_from_items(rows)
    context = SynthesisContext(
        job_id=scope.job_id,
        run_id=scope.run_id,
        language=config.language,
        semantic_description=config.search.semantic_description,
        provider=binding.provider,
        provider_name=binding.provider_name,
        smart_model=binding.smart_model,
        registry=binding.registry,
        usage=UsageRepository(session),
        budget=_required(scope.budget, "budget"),
    )
    result = await run_synthesis(entries, context)
    scope.synthesis = result
    emit_stage(scope, "synthesize_digest")
    if not entries:
        return {"digest": None}
    # The graph cannot import notify.render, so the digest title is the job's name.
    digest = DigestDraft(title=scope.job_name, body=result.body, item_ids=result.item_ids)
    return {"digest": digest}


def _update_run_status(deps: RunDeps, scope: RunScope, status: RunStatus) -> None:
    """Store ``status`` on the run in its own committed transaction, keeping stats and error."""
    with session_scope(deps.session_factory) as session:
        runs = RunRepository(session)
        run = _required(runs.get(scope.run_id), f"run {scope.run_id}")
        runs.finish(
            run,
            status,
            stats=dict(run.stats) if run.stats is not None else None,
            error=run.error,
            finished_at=run.finished_at,
        )


def _digest_id(deps: RunDeps, scope: RunScope) -> int | None:
    """The id of the digest this run stored, or ``None``."""
    with session_scope(deps.session_factory) as session:
        digest = DigestRepository(session).get_for_run(scope.run_id)
        return digest.id if digest is not None else None


async def persist(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Save the run all-or-nothing and return its status and the stored digest's id.

    A save that fails is recorded by ``finalize_run``/``finish_dry_run`` without raising; its
    error class still reaches ``RunResult.errors`` as a ``persist`` error.
    """
    config = _required(scope.config, "config")
    session = work_session(deps, scope)
    items = ItemRepository(session)
    taken = [row for item_id in state.get("taken", []) if (row := items.get(item_id)) is not None]
    relevance, summaries = _outcomes(state)
    counts = state.get("counts") or StageCounts(found=0, new=0, after_keyword_filter=0)
    counts = dataclasses.replace(
        counts,
        sources=state.get("sources_total", 0),
        sources_failed=state.get("sources_failed", 0),
    )
    digest = state.get("digest")
    draft = RunDraft(
        job_id=scope.job_id,
        run_id=scope.run_id,
        counts=counts,
        taken=taken,
        relevance=relevance,
        summaries=summaries,
        digest=digest or DigestDraft(title=scope.job_name, body="", item_ids=()),
        budget=_required(scope.budget, "budget"),
        dry_run=scope.dry_run,
        fallback_digest=scope.synthesis is not None and scope.synthesis.fallback,
    )
    if digest is None and config.notification.send_if_empty and not scope.dry_run:
        # No summarized item: an empty digest is stored only when the job asks for it, and
        # never for a failed run (nothing worth telling happened).
        draft = dataclasses.replace(
            draft, store_empty_digest=decide_status(draft) is not RunStatus.FAILED
        )
    save = finish_dry_run if scope.dry_run else finalize_run
    failures: list[Exception] = []
    status = save(deps.session_factory, session, draft, on_failure=failures.append)
    close_work_session(scope)  # committed or rolled back: free the connection for what follows
    update: RunState = {"status": status, "digest_id": _digest_id(deps, scope)}
    emit_stage(scope, "persist")
    if failures:
        update["errors"] = [error_of("persist", failures[0])]
    return update


async def notify(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Deliver the stored digest; a failed or skipped recipient lowers ``succeeded``."""
    digest_id = state.get("digest_id")
    status = state.get("status")
    if digest_id is None or status is None or status is RunStatus.FAILED or scope.dry_run:
        emit_stage(scope, "notify")
        return {}  # nothing stored, a failed run, or a dry run: nothing is sent
    try:
        report = await deps.notify(digest_id)
    except Exception:
        # Some recipients may have been mailed before the notifier broke: report what the
        # notification rows say, so ``RunResult`` does not claim that nothing was sent.
        scope.delivery = _delivery_from_rows(deps, scope)
        raise
    scope.delivery = report
    emit_stage(scope, "notify")
    if status is RunStatus.SUCCEEDED and (report.failed > 0 or report.error is not None):
        _update_run_status(deps, scope, RunStatus.PARTIAL)
        return {"status": RunStatus.PARTIAL}
    return {}


def _delivery_from_rows(deps: RunDeps, scope: RunScope) -> DeliveryReport | None:
    """The delivery counts stored for this run; ``None`` when they cannot be read.

    Counts like ``deliver_digest``: every row that is not ``sent`` failed (a ``pending`` row
    was never delivered).
    """
    try:
        with session_scope(deps.session_factory) as session:
            rows = NotificationRepository(session).list_for_run(scope.run_id)
            sent = sum(1 for row in rows if row.status == NotificationStatus.SENT)
            return DeliveryReport(sent=sent, failed=len(rows) - sent)
    except Exception as err:  # the notifier's error is the one that matters
        logger.error("run.delivery_unreadable", extra={"error": type(err).__name__})
        return None


def rollback_work_session(scope: RunScope) -> None:
    """Roll the work session back and close it; a failing rollback is logged, not raised."""
    session, scope.session = scope.session, None
    if session is None:
        return
    try:
        session.rollback()
    except Exception as err:
        logger.error(
            "run.rollback_failed",
            extra={"error": type(err).__name__, "db_run_id": scope.run_id},
        )
    finally:
        session.close()


def _stored_schedule(deps: RunDeps, scope: RunScope) -> ScheduleConfig | None:
    """The schedule of the stored job config, when that part alone is still valid.

    Used when the whole config failed validation: the job should still get its next run time.
    """
    with session_scope(deps.session_factory) as session:
        job = JobRepository(session).get(scope.job_id)
        raw = job.config if job is not None else None
    try:
        if not isinstance(raw, dict):
            raise TypeError("job config is not a mapping")
        return ScheduleConfig.model_validate(raw["schedule"])
    except (KeyError, TypeError, ValidationError):
        logger.warning("run.schedule_unreadable", extra={"job_id": scope.job_id})
        return None


def release_lock(deps: RunDeps, scope: RunScope) -> datetime | None:
    """Release this run's job lock and, outside a dry run, set the next run time.

    One atomic UPDATE that only succeeds while the lock still holds the run's token. A job whose
    schedule cannot be read keeps its ``next_run_at``. Returns the new ``next_run_at`` (``None``
    when it was kept); ``run.lock_lost`` is logged when the lock belonged to someone else.
    """
    schedule = None
    if not scope.dry_run:
        config = scope.config
        schedule = config.schedule if config is not None else _stored_schedule(deps, scope)
    next_run_at = None
    if schedule is not None:
        try:
            next_run_at = deps.next_run(schedule, deps.clock())
        except Exception as err:  # the lock must still be released; next_run_at stays as it is
            logger.warning(
                "run.next_run_failed", extra={"error": type(err).__name__, "job_id": scope.job_id}
            )
    with session_scope(deps.session_factory) as session:
        jobs = JobRepository(session)
        if next_run_at is not None:
            released = jobs.release(scope.job_id, until=scope.token, next_run_at=next_run_at)
        else:
            released = jobs.release(scope.job_id, until=scope.token)
    if not released:
        logger.warning("run.lock_lost", extra={"job_id": scope.job_id, "db_run_id": scope.run_id})
    return next_run_at


def record_failure(
    deps: RunDeps,
    scope: RunScope,
    error: BaseException,
    *,
    lower_succeeded: bool = False,
) -> RunStatus:
    """Fail the run after an error: roll back the work session, then ``record_failed_run`` (#19).

    A run that is no longer ``running`` (the error came after the save, e.g. from the notifier)
    keeps its stored status. With ``lower_succeeded`` (the notifier failed) ``succeeded`` becomes
    ``partial``: delivery went wrong. A failure to release the lock or compute the next run time
    does not touch a saved run. Used by ``finalize`` and by the safety net of ``run_job``.
    """
    rollback_work_session(scope)
    budget = scope.budget or BudgetTracker(LimitsConfig().max_llm_tokens_per_run)
    status = record_failed_run(
        deps.session_factory,
        job_id=scope.job_id,
        run_id=scope.run_id,
        budget=budget,
        error=error,
        replay_usage=not scope.dry_run,
    )
    if lower_succeeded and status is RunStatus.SUCCEEDED:
        _update_run_status(deps, scope, RunStatus.PARTIAL)
        return RunStatus.PARTIAL
    return status


def _record_errors(state: RunState, deps: RunDeps, scope: RunScope) -> None:
    """Store the run's error list in its stats; a failure is logged and never changes the run."""
    try:
        entries, omitted = stored_errors(
            state.get("errors", []), state.get("items", []), scope.item_refs, scope.failure
        )
        with session_scope(deps.session_factory) as session:
            RunRepository(session).record_errors(
                scope.run_id, [entry.to_json() for entry in entries], omitted=omitted
            )
    except Exception as err:
        logger.warning(
            "run.errors_not_recorded",
            extra={"error": type(err).__name__, "db_run_id": scope.run_id},
        )


async def finalize(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Close the run, set ``next_run_at`` and release the job lock; the only way to ``END``.

    After a guarded stage raised (``fatal``) the run is recorded failed first. Idempotent.
    """
    if scope.finalized:
        return {}
    fatal = state.get("fatal")
    status = state.get("status")
    if fatal is not None:
        error = scope.failure if scope.failure is not None else RuntimeError(fatal.error_class)
        status = record_failure(deps, scope, error, lower_succeeded=fatal.stage == "notify")
    if status is None:
        raise RuntimeError("finalize reached before persist set a status")
    _record_errors(state, deps, scope)
    next_run_at = release_lock(deps, scope)
    logger.info(
        "run.finalized",
        extra={
            "status": status.value,
            "dry_run": scope.dry_run,
            "next_run_at": next_run_at.isoformat() if next_run_at is not None else None,
            "db_run_id": scope.run_id,
        },
    )
    close_work_session(scope)
    scope.finalized = True
    emit_stage(scope, "finalize")
    return {"status": status}
