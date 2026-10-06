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
    RunRepository,
    UsageRepository,
)
from invio.db.session import session_scope
from invio.domain import Candidate, ItemStatus, RunStatus
from invio.graph.budget import BudgetTracker
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
from invio.graph.ports import ProviderBinding, RunDeps
from invio.graph.scope import RunScope
from invio.graph.state import ItemResult, ItemState, ItemUpdate, RunError, RunState, error_of
from invio.llm.retry import RetryingProvider
from invio.retry import is_transient_fetch, retrying
from invio.sources.errors import FetchError

__all__ = [
    "close_work_session",
    "deduplicate",
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


def _config(state: RunState) -> JobConfig:
    config = state.get("config")
    assert config is not None, "load_job has not run"
    return config


def _binding(scope: RunScope) -> ProviderBinding:
    assert scope.binding is not None, "load_job has not run"
    return scope.binding


def _budget(scope: RunScope) -> BudgetTracker:
    assert scope.budget is not None, "load_job has not run"
    return scope.budget


def _load_item(session: Session, item_id: int) -> Item:
    item = ItemRepository(session).get(item_id)
    assert item is not None, f"item {item_id} vanished"
    return item


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
        job = JobRepository(session).get(scope.job_id)
        assert job is not None, f"job {scope.job_id} vanished"
        raw = job.config
    config = JobConfig.model_validate(raw)
    scope.config = config
    binding = deps.provider_for(config.llm)
    # Every provider request of the run is retried on transient errors (research R4).
    retrying_provider = RetryingProvider(binding.provider, policy=deps.retry, sleep=deps.sleep)
    scope.binding = dataclasses.replace(binding, provider=retrying_provider)
    scope.budget = BudgetTracker(config.limits.max_llm_tokens_per_run)
    return {"config": config}


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
    config = _config(state)
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
    config = _config(state)
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
    counts = StageCounts(found=result.stats.found, new=result.stats.new, after_keyword_filter=0)
    return {"taken": [item.id for item in result.items], "counts": counts}


async def keyword_prefilter(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Apply the keyword rules to the taken items; a rejected item becomes ``skipped_keyword``."""
    config = _config(state)
    items = ItemRepository(work_session(deps, scope))
    selected: list[int] = []
    for item_id in state.get("taken", []):
        item = items.get(item_id)
        assert item is not None
        if keyword_filter(item, config.search.keywords, items).matched:
            selected.append(item_id)
    counts = state.get("counts") or StageCounts(found=0, new=0, after_keyword_filter=0)
    return {
        "selected": selected,
        "counts": dataclasses.replace(counts, after_keyword_filter=len(selected)),
    }


async def join(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Barrier after the fan-out; nothing to do."""
    return {}


def _scoring_context(deps: RunDeps, scope: RunScope) -> ScoringContext:
    binding, session = _binding(scope), work_session(deps, scope)
    return ScoringContext(
        job_id=scope.job_id,
        run_id=scope.run_id,
        search=_scope_config(scope).search,
        provider=binding.provider,
        provider_name=binding.provider_name,
        model=binding.fast_model,
        registry=binding.registry,
        items=ItemRepository(session),
        usage=UsageRepository(session),
        budget=_budget(scope),
    )


def _summary_context(deps: RunDeps, scope: RunScope) -> SummaryContext:
    binding, session, config = _binding(scope), work_session(deps, scope), _scope_config(scope)
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
        budget=_budget(scope),
    )


def _scope_config(scope: RunScope) -> JobConfig:
    assert scope.config is not None, "load_job has not run"
    return scope.config


# --- Per-item stages (nodes of the process_item subgraph) -------------------------------------


async def extract_text(state: ItemState, deps: RunDeps, scope: RunScope) -> ItemUpdate:
    """Fetch the item page and store its text; a too short page keeps title and teaser."""
    session = work_session(deps, scope)
    item = _load_item(session, state["item_id"])
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
    item = _load_item(work_session(deps, scope), state["item_id"])
    outcome = await score_item(item, _scoring_context(deps, scope))
    return {"stage": "score_relevance", "relevance": outcome}


async def summarize(state: ItemState, deps: RunDeps, scope: RunScope) -> ItemUpdate:
    """Summarize a relevant item."""
    item = _load_item(work_session(deps, scope), state["item_id"])
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
    config, binding = _config(state), _binding(scope)
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
        budget=_budget(scope),
    )
    result = await run_synthesis(entries, context)
    scope.synthesis = result
    if not entries:
        return {"digest": None}
    # The graph cannot import notify.render, so the digest title is the job's name.
    digest = DigestDraft(title=scope.job_name, body=result.body, item_ids=result.item_ids)
    return {"digest": digest}


def _update_run_status(deps: RunDeps, scope: RunScope, status: RunStatus) -> None:
    """Store ``status`` on the run in its own committed transaction, keeping stats and error."""
    with session_scope(deps.session_factory) as session:
        runs = RunRepository(session)
        run = runs.get(scope.run_id)
        assert run is not None
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
    """Save the run all-or-nothing and return its status and the stored digest's id."""
    config = _config(state)
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
        budget=_budget(scope),
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
    status = save(deps.session_factory, session, draft)
    close_work_session(scope)  # committed or rolled back: free the connection for what follows
    return {"status": status, "digest_id": _digest_id(deps, scope)}


async def notify(state: RunState, deps: RunDeps, scope: RunScope) -> RunState:
    """Deliver the stored digest; a failed or skipped recipient lowers ``succeeded``."""
    digest_id = state.get("digest_id")
    status = state.get("status")
    if digest_id is None or status is None or status is RunStatus.FAILED or scope.dry_run:
        return {}  # nothing stored, a failed run, or a dry run: nothing is sent
    report = await deps.notify(digest_id)
    scope.delivery = report
    if status is RunStatus.SUCCEEDED and (report.failed > 0 or report.error is not None):
        _update_run_status(deps, scope, RunStatus.PARTIAL)
        return {"status": RunStatus.PARTIAL}
    return {}


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


def release_lock(deps: RunDeps, scope: RunScope, config: JobConfig | None) -> datetime | None:
    """Release this run's job lock and, outside a dry run, set the next run time.

    One atomic UPDATE that only succeeds while the lock still holds the run's token. A job whose
    schedule cannot be read keeps its ``next_run_at``. Returns the new ``next_run_at`` (``None``
    when it was kept); ``run.lock_lost`` is logged when the lock belonged to someone else.
    """
    schedule = None
    if not scope.dry_run:
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
    next_run_at = release_lock(deps, scope, state.get("config"))
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
    return {"status": status}
