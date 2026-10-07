"""Tests for run persistence: all-or-nothing save, recovery after a failed save, run status.

Tests that call ``finalize_run`` or ``record_failed_run`` commit, so they do not use the
``db_session`` fixture (a savepoint session on MariaDB): they seed through ``session_scope``,
work in a session of their own as a real run would, and assert in a fresh session. The seed
commits the run row and the taken items first, as the orchestrator does (research R7).
"""

import dataclasses
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel
from sqlalchemy import Engine, select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session, sessionmaker

from invio.config.job import SearchConfig
from invio.db.models import Digest, Item, LlmUsage, Run
from invio.db.repositories import (
    DigestRepository,
    ItemRepository,
    RunRepository,
    UsageRepository,
)
from invio.db.session import session_factory, session_scope
from invio.domain import ItemStatus, RunStatus
from invio.graph.budget import BudgetExceeded, BudgetTracker, UsageEntry
from invio.graph.nodes.llm_calls import call_structured, failure_message
from invio.graph.nodes.persist import (
    ALL_FAILED_ERROR,
    ALL_SOURCES_FAILED_ERROR,
    DigestDraft,
    RunDraft,
    StageCounts,
    build_stats,
    decide_status,
    finalize_run,
    finish_dry_run,
    persist_run,
    record_failed_run,
    unprocessed,
)
from invio.graph.nodes.relevance import RelevanceOutcome, ScoringContext, score_items
from invio.graph.nodes.summarize_item import (
    SummaryContext,
    SummaryOutcome,
    summarize_items,
)
from invio.llm.base import LLMProvider, LLMUnavailableError, Usage
from invio.llm.fake import FakeProvider, FakeReply, FakeStep
from invio.llm.registry import ModelInfo, ModelRegistry
from tests.db_helpers import make_item, make_job, make_run

pytestmark = pytest.mark.db

_REGISTRY = ModelRegistry(
    {
        "fast-model": ModelInfo("fast-model", "mistral", Decimal("1"), Decimal("2"), 32000),
        "smart-model": ModelInfo("smart-model", "mistral", Decimal("3"), Decimal("6"), 32000),
    }
)
_COUNTS = StageCounts(found=10, new=4, after_keyword_filter=3)
# Text that must never reach runs.error or the logs when a database error is injected.
_SQL_MARKER = "SECRET_SQL_MARKER"
_PARAM_MARKER = "SECRET_PARAM_MARKER"

# --- Helpers -------------------------------------------------------------------------------


def _relevance_answer(score: float = 0.9) -> str:
    return json.dumps({"score": score, "reason": "matches", "key_points": ["a"]})


def _summary_answer() -> str:
    return json.dumps({"headline": "Head", "bullets": ["a", "b", "c"], "why_relevant": "Matters."})


@dataclass(frozen=True, slots=True)
class _Seed:
    job_id: int
    run_id: int
    item_ids: list[int]


def _seed(factory: sessionmaker[Session], count: int = 2, **item_kw: Any) -> _Seed:
    """Commit a job, a running run and ``count`` items taken by it (attempts 1)."""
    with session_scope(factory) as session:
        job = make_job(session)
        run = RunRepository(session).start(job.id)
        item_kw.setdefault("raw_content", "A short body about agents.")
        items = [
            make_item(session, job, url=f"https://example.com/{n}", **item_kw) for n in range(count)
        ]
        ItemRepository(session).mark_taken(items, run.id)
        return _Seed(job.id, run.id, [item.id for item in items])


def _load_items(session: Session, ids: Sequence[int]) -> list[Item]:
    items = [session.get(Item, item_id) for item_id in ids]
    assert all(item is not None for item in items)
    return [item for item in items if item is not None]


def _contexts(
    session: Session, seed: _Seed, fake: LLMProvider, budget: BudgetTracker
) -> tuple[ScoringContext, SummaryContext]:
    items, usage = ItemRepository(session), UsageRepository(session)
    scoring = ScoringContext(
        job_id=seed.job_id,
        run_id=seed.run_id,
        search=SearchConfig(semantic_description="LLM agents", min_relevance=0.6),
        provider=fake,
        provider_name="mistral",
        model="fast-model",
        registry=_REGISTRY,
        items=items,
        usage=usage,
        budget=budget,
    )
    summary = SummaryContext(
        job_id=seed.job_id,
        run_id=seed.run_id,
        language="en",
        semantic_description="LLM agents",
        provider=fake,
        provider_name="mistral",
        fast_model="fast-model",
        smart_model="smart-model",
        registry=_REGISTRY,
        items=items,
        usage=usage,
        budget=budget,
    )
    return scoring, summary


def _stage_script(count: int) -> list[FakeStep]:
    """Relevance then summary replies for ``count`` items (110 and 250 tokens per call)."""
    return [FakeReply(_relevance_answer(), Usage(100, 10))] * count + [
        FakeReply(_summary_answer(), Usage(200, 50))
    ] * count


async def _run_stages(
    factory: sessionmaker[Session],
    seed: _Seed,
    *,
    budget_limit: int = 1_000_000,
    digest: bool = True,
) -> tuple[Session, RunDraft]:
    """Open the work session and run relevance and summarization like the pipeline would."""
    session = factory()
    items = _load_items(session, seed.item_ids)
    budget = BudgetTracker(budget_limit)
    fake = FakeProvider(_stage_script(len(items)))
    scoring, summary = _contexts(session, seed, fake, budget)
    relevance = await score_items(items, scoring)
    summaries = await summarize_items(items, summary)
    result = RunDraft(
        job_id=seed.job_id,
        run_id=seed.run_id,
        counts=_COUNTS,
        taken=items,
        relevance=relevance,
        summaries=summaries,
        digest=DigestDraft(title="Weekly", body="Body", item_ids=list(seed.item_ids))
        if digest
        else None,
        budget=budget,
    )
    return session, result


def _injected_error(kind: type[Exception] = IntegrityError) -> Exception:
    """A database error whose statement, parameters and driver text carry marker text."""
    return kind(
        f"INSERT INTO digests VALUES ({_SQL_MARKER})",
        {"title": _PARAM_MARKER},
        Exception(f"{_SQL_MARKER} {_PARAM_MARKER}"),
    )


def _raise(error: Exception) -> Any:
    def _boom(*args: object, **kwargs: object) -> None:
        raise error

    return _boom


def _run_row(factory: sessionmaker[Session], run_id: int) -> Run:
    with factory() as session:
        run = session.get(Run, run_id)
        assert run is not None
        session.expunge(run)
        return run


def _usage_rows(factory: sessionmaker[Session], run_id: int) -> list[LlmUsage]:
    with factory() as session:
        rows = list(
            session.scalars(select(LlmUsage).where(LlmUsage.run_id == run_id).order_by(LlmUsage.id))
        )
        session.expunge_all()
        return rows


def _digests(factory: sessionmaker[Session]) -> list[Digest]:
    with factory() as session:
        rows = list(session.scalars(select(Digest)))
        session.expunge_all()
        return rows


def _item_rows(factory: sessionmaker[Session], ids: Sequence[int]) -> list[Item]:
    with factory() as session:
        rows = _load_items(session, ids)
        session.expunge_all()
        return rows


def _events(caplog: pytest.LogCaptureFixture, name: str) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.getMessage() == name]


def _assert_no_markers(caplog: pytest.LogCaptureFixture, error: str | None) -> None:
    assert error is not None
    for marker in (_SQL_MARKER, _PARAM_MARKER, "INSERT"):
        assert marker not in error
        assert marker not in caplog.text
        assert all(marker not in str(record.__dict__) for record in caplog.records)


def _entry(
    input_tokens: int = 100,
    output_tokens: int = 10,
    cost: str | None = "0.000120",
    purpose: str = "relevance",
    created_at: datetime | None = None,
) -> UsageEntry:
    return UsageEntry(
        provider="mistral",
        model="fast-model",
        purpose=purpose,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=Decimal(cost) if cost is not None else None,
        created_at=created_at or datetime(2026, 10, 6, 12, 0, 0, 123456, tzinfo=UTC),
    )


# --- finalize_run: the all-or-nothing save (US1) -------------------------------------------


async def test_finalize_run_commits_items_digest_usage_and_run(
    db_engine: Engine, clean_jobs: None, caplog: pytest.LogCaptureFixture
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed)
    with caplog.at_level(logging.INFO, logger="invio.graph"):
        status = finalize_run(factory, session, result)
    session.close()
    assert status == RunStatus.SUCCEEDED

    (digest,) = _digests(factory)
    assert (digest.run_id, digest.job_id) == (seed.run_id, seed.job_id)
    assert (digest.title, digest.body, digest.item_ids) == ("Weekly", "Body", seed.item_ids)
    assert all(i.status == ItemStatus.SUMMARIZED for i in _item_rows(factory, seed.item_ids))
    assert len(_usage_rows(factory, seed.run_id)) == 4
    run = _run_row(factory, seed.run_id)
    assert run.status == status
    assert run.finished_at is not None
    assert run.error is None
    assert run.stats is not None
    assert run.stats["version"] == 1

    (event,) = _events(caplog, "run.persisted")
    assert event.status == status.value
    assert (event.job_id, event.db_run_id) == (seed.job_id, seed.run_id)
    assert event.tokens == 720
    assert event.estimated_cost_usd == "0.000840"


async def test_failed_save_rolls_everything_back_and_keeps_usage(
    db_engine: Engine,
    clean_jobs: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed)
    ledger = result.budget.ledger
    assert len(ledger) == 4
    monkeypatch.setattr(DigestRepository, "add", _raise(_injected_error()))
    with caplog.at_level(logging.INFO, logger="invio.graph"):
        status = finalize_run(factory, session, result)
    session.close()
    assert status == RunStatus.FAILED

    assert _digests(factory) == []
    for item in _item_rows(factory, seed.item_ids):  # back to the committed pre-run state
        assert (item.status, item.relevance, item.summary) == (ItemStatus.NEW, None, None)
        assert (item.attempts, item.run_id) == (1, seed.run_id)
    rows = _usage_rows(factory, seed.run_id)
    assert [(r.input_tokens, r.output_tokens, r.cost_usd, r.created_at) for r in rows] == [
        (e.input_tokens, e.output_tokens, e.cost_usd, e.created_at) for e in ledger
    ]
    run = _run_row(factory, seed.run_id)
    assert run.status == RunStatus.FAILED
    assert run.finished_at is not None
    assert run.error is not None
    assert run.error.startswith("IntegrityError:")
    _assert_no_markers(caplog, run.error)
    assert run.stats == {
        "version": 1,
        "llm_calls": 4,
        "llm_calls_unpriced": 0,
        "input_tokens": 600,
        "output_tokens": 120,
        "tokens": 720,
        "estimated_cost_usd": "0.000840",
        "cost_complete": True,
        "budget_limit": 1_000_000,
        "budget_exceeded": False,
        "over_budget": False,
    }
    (event,) = _events(caplog, "run.persist_failed")
    assert (event.error, event.job_id, event.db_run_id) == (
        "IntegrityError",
        seed.job_id,
        seed.run_id,
    )
    assert _events(caplog, "run.persisted") == []


async def test_failed_commit_is_recovered_like_a_failed_write(
    db_engine: Engine, clean_jobs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed)
    monkeypatch.setattr(session, "commit", _raise(_injected_error(OperationalError)))
    assert finalize_run(factory, session, result) == RunStatus.FAILED
    session.close()
    run = _run_row(factory, seed.run_id)
    assert (run.status, run.error) == (RunStatus.FAILED, "OperationalError: run failed")
    assert len(_usage_rows(factory, seed.run_id)) == 4


async def test_a_commit_applied_before_it_failed_is_not_replayed(
    db_engine: Engine,
    clean_jobs: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lost connection after the database applied the commit: no duplicate rows, status kept."""
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed)
    real_commit = session.commit

    def _commit_then_fail() -> None:
        real_commit()
        raise _injected_error(OperationalError)

    monkeypatch.setattr(session, "commit", _commit_then_fail)
    with caplog.at_level(logging.INFO, logger="invio.graph"):
        status = finalize_run(factory, session, result)
    session.close()
    assert status == RunStatus.SUCCEEDED
    run = _run_row(factory, seed.run_id)
    assert (run.status, run.error) == (RunStatus.SUCCEEDED, None)
    assert run.stats is not None
    assert "found" in run.stats  # the saved layout, not the recovery layout
    assert len(_usage_rows(factory, seed.run_id)) == 4
    assert len(_digests(factory)) == 1
    (event,) = _events(caplog, "run.already_finished")
    assert (event.status, event.job_id, event.db_run_id) == (
        RunStatus.SUCCEEDED,
        seed.job_id,
        seed.run_id,
    )


def test_record_failed_run_leaves_a_finished_run_alone(db_engine: Engine, clean_jobs: None) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=1)
    with session_scope(factory) as session:
        run = RunRepository(session).get(seed.run_id)
        assert run is not None
        RunRepository(session).finish(run, RunStatus.PARTIAL, stats={"version": 1})
    budget = BudgetTracker(100)
    budget.record(_entry())
    status = record_failed_run(
        factory, job_id=seed.job_id, run_id=seed.run_id, budget=budget, error=RuntimeError("x")
    )
    assert status == RunStatus.PARTIAL
    finished = _run_row(factory, seed.run_id)
    assert (finished.status, finished.error, finished.stats) == (
        RunStatus.PARTIAL,
        None,
        {"version": 1},
    )
    assert _usage_rows(factory, seed.run_id) == []


async def test_a_failing_rollback_is_logged_and_recovery_continues(
    db_engine: Engine, clean_jobs: None, caplog: pytest.LogCaptureFixture
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed, digest=False)

    def _fail_commit() -> None:
        raise _injected_error()

    def _fail_rollback() -> None:
        session.close()  # release the connection so the recovery can write
        raise RuntimeError(_SQL_MARKER)

    session.commit = _fail_commit  # type: ignore[method-assign]
    session.rollback = _fail_rollback  # type: ignore[method-assign]
    with caplog.at_level(logging.INFO, logger="invio.graph"):
        status = finalize_run(factory, session, result)
    assert status == RunStatus.FAILED
    assert _events(caplog, "run.rollback_failed")
    run = _run_row(factory, seed.run_id)
    assert run.status == RunStatus.FAILED
    _assert_no_markers(caplog, run.error)


async def test_keyboard_interrupt_is_recovered_and_re_raised(
    db_engine: Engine, clean_jobs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed)

    def _interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(DigestRepository, "add", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        finalize_run(factory, session, result)
    session.close()
    run = _run_row(factory, seed.run_id)
    assert (run.status, run.error) == (RunStatus.FAILED, "KeyboardInterrupt: run failed")
    assert len(_usage_rows(factory, seed.run_id)) == 4
    assert _digests(factory) == []


async def test_finalize_run_turns_an_invalid_digest_into_a_failed_run(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed)
    bad = _with(result, DigestDraft(title="t", body="b", item_ids=[seed.item_ids[0], 999_999]))
    assert finalize_run(factory, session, bad) == RunStatus.FAILED
    session.close()
    run = _run_row(factory, seed.run_id)
    assert run.error is not None
    assert run.error.startswith("ValueError:")
    assert _digests(factory) == []


# --- record_failed_run (US1) ---------------------------------------------------------------


def test_record_failed_run_replays_usage_and_marks_the_run_failed(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=1)
    budget = BudgetTracker(100)
    first = _entry(100, 10, "0.000120", created_at=datetime(2026, 1, 1, 8, tzinfo=UTC))
    second = _entry(5, 5, None, purpose="summarize", created_at=datetime(2026, 1, 1, 9, tzinfo=UTC))
    budget.record(first)
    budget.record(second)
    error = LLMUnavailableError("down: SECRET", provider="mistral", model="fast-model")
    record_failed_run(factory, job_id=seed.job_id, run_id=seed.run_id, budget=budget, error=error)

    run = _run_row(factory, seed.run_id)
    assert run.status == RunStatus.FAILED
    assert run.finished_at is not None
    assert run.error == failure_message(error)
    assert "SECRET" not in run.error
    assert run.stats == {
        "version": 1,
        "llm_calls": 2,
        "llm_calls_unpriced": 1,
        "input_tokens": 105,
        "output_tokens": 15,
        "tokens": 120,
        "estimated_cost_usd": "0.000120",
        "cost_complete": False,
        "budget_limit": 100,
        "budget_exceeded": False,
        "over_budget": True,
    }
    rows = _usage_rows(factory, seed.run_id)
    assert [(r.purpose, r.input_tokens, r.cost_usd, r.created_at) for r in rows] == [
        ("relevance", 100, Decimal("0.000120"), first.created_at),
        ("summarize", 5, None, second.created_at),
    ]
    assert all(r.job_id == seed.job_id for r in rows)


def test_record_failed_run_keeps_a_non_llm_error_text_out_of_the_run(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=1)
    record_failed_run(
        factory,
        job_id=seed.job_id,
        run_id=seed.run_id,
        budget=BudgetTracker(100),
        error=_injected_error(),
    )
    run = _run_row(factory, seed.run_id)
    assert run.error == "IntegrityError: run failed"
    assert run.stats is not None
    assert (run.stats["llm_calls"], run.stats["tokens"]) == (0, 0)
    assert run.stats["estimated_cost_usd"] == "0.000000"
    assert _usage_rows(factory, seed.run_id) == []


def test_record_failed_run_reports_a_budget_stop_in_the_stats(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=1)
    budget = BudgetTracker(10)
    budget.record(_entry(50, 5))
    with pytest.raises(BudgetExceeded):
        budget.check()
    record_failed_run(
        factory,
        job_id=seed.job_id,
        run_id=seed.run_id,
        budget=budget,
        error=RuntimeError("stage broke"),
    )
    run = _run_row(factory, seed.run_id)
    assert run.stats is not None
    assert (run.stats["budget_limit"], run.stats["budget_exceeded"]) == (10, True)


def test_a_failing_recovery_is_logged_with_its_class_only_and_re_raised(
    db_engine: Engine,
    clean_jobs: None,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=1)
    budget = BudgetTracker(100)
    budget.record(_entry())
    monkeypatch.setattr(RunRepository, "finish", _raise(_injected_error(OperationalError)))
    with caplog.at_level(logging.INFO, logger="invio.graph"), pytest.raises(OperationalError):
        record_failed_run(
            factory,
            job_id=seed.job_id,
            run_id=seed.run_id,
            budget=budget,
            error=RuntimeError("stage broke"),
        )
    (event,) = _events(caplog, "run.record_failed_error")
    assert (event.error, event.job_id, event.db_run_id) == (
        "OperationalError",
        seed.job_id,
        seed.run_id,
    )
    _assert_no_markers(caplog, "")
    monkeypatch.undo()
    run = _run_row(factory, seed.run_id)  # the recovery transaction was rolled back as a whole
    assert run.status == RunStatus.RUNNING
    assert _usage_rows(factory, seed.run_id) == []


def test_record_failed_run_for_a_missing_run_raises_lookup_error(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=1)
    with pytest.raises(LookupError):
        record_failed_run(
            factory,
            job_id=seed.job_id,
            run_id=seed.run_id + 1000,
            budget=BudgetTracker(1),
            error=RuntimeError("x"),
        )


# --- persist_run: digest validation and storage (US1) --------------------------------------


def _bare_result(
    db_session: Session,
    *,
    digest: DigestDraft | None,
    item_count: int = 2,
    budget: BudgetTracker | None = None,
) -> RunDraft:
    job = make_job(db_session)
    run = make_run(db_session, job)
    items = [make_item(db_session, job, url=f"https://example.com/{n}") for n in range(item_count)]
    ItemRepository(db_session).mark_taken(items, run.id)
    return RunDraft(
        job_id=job.id,
        run_id=run.id,
        counts=_COUNTS,
        taken=items,
        relevance=[],
        summaries=[],
        digest=digest,
        budget=budget or BudgetTracker(1000),
    )


def test_persist_run_rejects_a_digest_item_that_was_not_taken(db_session: Session) -> None:
    result = _bare_result(db_session, digest=None)
    draft = DigestDraft(title="t", body="b", item_ids=[result.taken[0].id, 999_999])
    with pytest.raises(ValueError, match="not taken"):
        persist_run(db_session, _with(result, draft))


def test_persist_run_rejects_duplicate_digest_item_ids(db_session: Session) -> None:
    result = _bare_result(db_session, digest=None)
    item_id = result.taken[0].id
    draft = DigestDraft(title="t", body="b", item_ids=[item_id, item_id])
    with pytest.raises(ValueError, match="duplicate"):
        persist_run(db_session, _with(result, draft))


def _with(result: RunDraft, digest: DigestDraft | None) -> RunDraft:
    return RunDraft(
        job_id=result.job_id,
        run_id=result.run_id,
        counts=result.counts,
        taken=result.taken,
        relevance=result.relevance,
        summaries=result.summaries,
        digest=digest,
        budget=result.budget,
    )


@pytest.mark.parametrize("draft", [None, DigestDraft(title="t", body="b", item_ids=[])])
def test_persist_run_stores_no_digest_without_items(
    db_session: Session, draft: DigestDraft | None
) -> None:
    result = _bare_result(db_session, digest=draft)
    run = persist_run(db_session, result)
    assert db_session.scalars(select(Digest)).all() == []
    assert run.status == RunStatus.SUCCEEDED
    assert run.finished_at is not None


@pytest.mark.parametrize(("store_empty", "expected"), [(False, 0), (True, 1)])
def test_persist_run_stores_an_empty_digest_only_when_asked(
    db_session: Session, store_empty: bool, expected: int
) -> None:
    base = _bare_result(
        db_session, digest=DigestDraft(title="Daily", body="", item_ids=()), item_count=0
    )
    result = dataclasses.replace(base, store_empty_digest=store_empty)

    persist_run(db_session, result)

    digests = db_session.scalars(select(Digest)).all()
    assert len(digests) == expected
    if store_empty:
        assert (digests[0].title, digests[0].item_ids) == ("Daily", [])


def test_run_draft_defaults_keep_existing_callers_valid(db_session: Session) -> None:
    result = _bare_result(db_session, digest=None)

    assert (result.dry_run, result.store_empty_digest) == (False, False)
    assert (result.counts.sources, result.counts.sources_failed) == (0, 0)


def test_persist_run_adds_the_digest_with_the_run_id(db_session: Session) -> None:
    base = _bare_result(db_session, digest=None)
    ids = [item.id for item in base.taken]
    result = RunDraft(
        job_id=base.job_id,
        run_id=base.run_id,
        counts=base.counts,
        taken=base.taken,
        relevance=[_rel(item, ItemStatus.RELEVANT) for item in base.taken],
        summaries=[_sum(item, ItemStatus.SUMMARIZED) for item in base.taken],
        digest=DigestDraft(title="T", body="B", item_ids=ids),
        budget=base.budget,
    )
    persist_run(db_session, result)
    (digest,) = db_session.scalars(select(Digest)).all()
    assert (digest.run_id, digest.job_id, digest.item_ids) == (result.run_id, result.job_id, ids)


def test_persist_run_for_a_missing_run_raises_lookup_error(db_session: Session) -> None:
    result = _bare_result(db_session, digest=None)
    missing = RunDraft(
        job_id=result.job_id,
        run_id=result.run_id + 1000,
        counts=result.counts,
        taken=result.taken,
        relevance=[],
        summaries=[],
        digest=None,
        budget=result.budget,
    )
    with pytest.raises(LookupError):
        persist_run(db_session, missing)


# Kept for the status and statistics tests below.
def _rel(item: Item, status: ItemStatus, error: str | None = None) -> RelevanceOutcome:
    return RelevanceOutcome(
        item_id=item.id,
        status=status,
        relevance=None if error else Decimal("0.90"),
        result=None,
        error=error,
    )


def _sum(item: Item, status: ItemStatus, error: str | None = None) -> SummaryOutcome:
    return SummaryOutcome(
        item_id=item.id,
        status=status,
        summary=None,
        error=error,
        calls=1,
        chunks=0,
        truncated=False,
    )


# --- Token budget runs (US2) ---------------------------------------------------------------


class _DigestAnswer(BaseModel):
    title: str


async def _run_pipeline(
    factory: sessionmaker[Session],
    seed: _Seed,
    script: list[FakeStep],
    *,
    budget_limit: int,
    digest_call: bool = False,
) -> tuple[Session, RunDraft]:
    """Relevance for every item, summaries for the relevant ones, an optional digest call.

    The digest call passes ``per_item=False`` like the digest node (#18) and, when an item was
    summarized, the draft lists the summarized items.
    """
    session = factory()
    items = _load_items(session, seed.item_ids)
    budget = BudgetTracker(budget_limit)
    scoring, summary = _contexts(session, seed, FakeProvider(script), budget)
    relevance = await score_items(items, scoring)
    relevant = [i for i in items if i.status == ItemStatus.RELEVANT]
    summaries = await summarize_items(relevant, summary)
    digest = None
    if digest_call:
        await call_structured(
            scoring,
            _DigestAnswer,
            model="smart-model",
            purpose="digest",
            system="s",
            user="u",
            per_item=False,
        )
        done = [o.item_id for o in summaries if o.status == ItemStatus.SUMMARIZED]
        digest = DigestDraft(title="Weekly", body="Body", item_ids=done) if done else None
    result = RunDraft(
        job_id=seed.job_id,
        run_id=seed.run_id,
        counts=_COUNTS,
        taken=items,
        relevance=relevance,
        summaries=summaries,
        digest=digest,
        budget=budget,
    )
    return session, result


def _scored(score: float, tokens: int = 600) -> FakeReply:
    return FakeReply(_relevance_answer(score), Usage(tokens, 0))


def _status_of(factory: sessionmaker[Session], ids: Sequence[int]) -> list[ItemStatus]:
    return [item.status for item in _item_rows(factory, ids)]


def test_unprocessed_lists_taken_items_without_a_final_state(db_session: Session) -> None:
    job = make_job(db_session)
    names = [
        "keyword",
        "irrelevant",
        "rel_failed",
        "done",
        "unsummarized",
        "untouched",
        "sum_failed",
    ]
    items = {
        name: make_item(db_session, job, url=f"https://example.com/{name}", status=status)
        for name, status in zip(
            names,
            [
                ItemStatus.SKIPPED_KEYWORD,
                ItemStatus.SKIPPED_IRRELEVANT,
                ItemStatus.FAILED,
                ItemStatus.SUMMARIZED,
                ItemStatus.RELEVANT,
                ItemStatus.NEW,
                ItemStatus.FAILED,
            ],
            strict=True,
        )
    }
    result = RunDraft(
        job_id=job.id,
        run_id=1,
        counts=_COUNTS,
        taken=list(items.values()),
        relevance=[
            _rel(items["irrelevant"], ItemStatus.SKIPPED_IRRELEVANT),
            _rel(items["rel_failed"], ItemStatus.FAILED, "LLMUnavailableError: x"),
            _rel(items["done"], ItemStatus.RELEVANT),
            _rel(items["unsummarized"], ItemStatus.RELEVANT),
            _rel(items["sum_failed"], ItemStatus.RELEVANT),
        ],
        summaries=[
            _sum(items["done"], ItemStatus.SUMMARIZED),
            _sum(items["sum_failed"], ItemStatus.FAILED, "LLMUnavailableError: x"),
        ],
        digest=None,
        budget=BudgetTracker(10),
    )
    assert unprocessed(result) == [items["unsummarized"], items["untouched"]]


async def test_budget_stop_releases_the_unprocessed_items_and_ends_partial(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=5)
    script: list[FakeStep] = [_scored(0.1), _scored(0.9), _scored(0.9)]
    session, result = await _run_pipeline(factory, seed, script, budget_limit=1000)
    assert len(result.relevance) == 2  # the third call was blocked
    assert result.budget.exceeded is True
    status = finalize_run(factory, session, result)
    session.close()

    assert status == RunStatus.PARTIAL
    first, *released = _item_rows(factory, seed.item_ids)
    assert (first.status, first.attempts, first.run_id) == (
        ItemStatus.SKIPPED_IRRELEVANT,
        1,
        seed.run_id,
    )
    for item in released:  # the relevant item rated before the stop is released too
        assert (item.status, item.attempts, item.run_id) == (ItemStatus.NEW, 0, None)
    assert len(_usage_rows(factory, seed.run_id)) == 2
    assert _digests(factory) == []


async def test_digest_call_after_the_budget_stop_is_counted(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=2)
    script: list[FakeStep] = [
        _scored(0.9, 100),
        _scored(0.9, 100),
        FakeReply(_summary_answer(), Usage(1000, 0)),
        FakeReply(json.dumps({"title": "Weekly"}), Usage(50, 0)),
    ]
    session, result = await _run_pipeline(
        factory, seed, script, budget_limit=1000, digest_call=True
    )
    status = finalize_run(factory, session, result)
    session.close()

    assert status == RunStatus.PARTIAL
    run = _run_row(factory, seed.run_id)
    assert run.stats is not None
    assert run.stats["tokens"] == 1250  # the digest call ran past the limit and is counted
    assert run.stats["llm_calls"] == 4
    assert run.stats["budget_exceeded"] is True
    (digest,) = _digests(factory)
    assert digest.item_ids == seed.item_ids[:1]
    done, released = _item_rows(factory, seed.item_ids)
    assert done.status == ItemStatus.SUMMARIZED
    assert (released.status, released.attempts, released.run_id) == (ItemStatus.NEW, 0, None)


async def test_budget_exceeded_before_any_item_finished_stores_no_digest(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=3)
    script: list[FakeStep] = [
        _scored(0.9, 600),
        _scored(0.9, 600),
        FakeReply(json.dumps({"title": "Weekly"}), Usage(50, 0)),
    ]
    session, result = await _run_pipeline(
        factory, seed, script, budget_limit=1000, digest_call=True
    )
    assert result.digest is None
    assert finalize_run(factory, session, result) == RunStatus.PARTIAL
    session.close()
    assert _digests(factory) == []
    assert _status_of(factory, seed.item_ids) == [ItemStatus.NEW] * 3
    assert _run_row(factory, seed.run_id).status == RunStatus.PARTIAL


# --- Run statistics (US3) ------------------------------------------------------------------

_STATS_KEYS = [
    "version",
    "found",
    "new",
    "after_keyword_filter",
    "sources",
    "sources_failed",
    "relevant",
    "summarized",
    "failed",
    "processed",
    "skipped_budget",
    "llm_calls",
    "llm_calls_unpriced",
    "input_tokens",
    "output_tokens",
    "tokens",
    "estimated_cost_usd",
    "cost_complete",
    "budget_limit",
    "budget_exceeded",
    "over_budget",
]


def _call_entry(
    model: str, input_tokens: int, output_tokens: int, registry: ModelRegistry = _REGISTRY
) -> UsageEntry:
    """A ledger entry priced by ``registry``, like ``call_structured`` builds it."""
    usage = Usage(input_tokens, output_tokens)
    return UsageEntry(
        provider="mistral",
        model=model,
        purpose="test",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=registry.cost(model, usage),
        created_at=datetime(2026, 10, 6, tzinfo=UTC),
    )


def _mixed_result(db_session: Session, budget: BudgetTracker) -> RunDraft:
    """Five taken items: three relevant (one unsummarized later), one irrelevant, one failed."""
    job = make_job(db_session)
    names = ["a", "b", "c", "d", "e", "f"]
    items = [make_item(db_session, job, url=f"https://example.com/{n}") for n in names]
    a, b, c, d, e, _ = items
    return RunDraft(
        job_id=job.id,
        run_id=1,
        counts=_COUNTS,
        taken=items,
        relevance=[
            _rel(a, ItemStatus.RELEVANT),
            _rel(b, ItemStatus.RELEVANT),
            _rel(c, ItemStatus.RELEVANT),
            _rel(d, ItemStatus.SKIPPED_IRRELEVANT),
            _rel(e, ItemStatus.FAILED, "LLMUnavailableError: x"),
        ],
        summaries=[
            _sum(a, ItemStatus.SUMMARIZED),
            _sum(b, ItemStatus.SUMMARIZED),
            _sum(c, ItemStatus.FAILED, "LLMUnavailableError: x"),
        ],
        digest=None,
        budget=budget,
    )


def test_build_stats_counts_outcomes_and_ledger_figures(db_session: Session) -> None:
    budget = BudgetTracker(1_000_000)
    budget.record(_call_entry("fast-model", 1000, 500))  # 0.002000
    budget.record(_call_entry("smart-model", 200, 100))  # 0.001200
    stats = build_stats(_mixed_result(db_session, budget))
    assert list(stats) == _STATS_KEYS
    assert stats == {
        "version": 1,
        "found": 10,
        "new": 4,
        "after_keyword_filter": 3,
        "sources": 0,
        "sources_failed": 0,
        "relevant": 3,
        "summarized": 2,
        "failed": 2,
        "processed": 5,
        "skipped_budget": 0,  # the unrated item is only counted after a budget stop
        "llm_calls": 2,
        "llm_calls_unpriced": 0,
        "input_tokens": 1200,
        "output_tokens": 600,
        "tokens": 1800,
        "estimated_cost_usd": "0.003200",
        "cost_complete": True,
        "budget_limit": 1_000_000,
        "budget_exceeded": False,
        "over_budget": False,
    }


def test_build_stats_estimated_cost_is_the_sum_of_the_registry_prices(
    db_session: Session,
) -> None:
    budget = BudgetTracker(1_000_000)
    calls = [("fast-model", 123, 45), ("smart-model", 678, 90), ("fast-model", 1, 1)]
    for model, tokens_in, tokens_out in calls:
        budget.record(_call_entry(model, tokens_in, tokens_out))
    expected = sum(
        (_REGISTRY.cost(model, Usage(i, o)) or Decimal(0) for model, i, o in calls), Decimal(0)
    )
    stats = build_stats(_mixed_result(db_session, budget))
    assert Decimal(stats["estimated_cost_usd"]) == expected
    assert stats["tokens"] == stats["input_tokens"] + stats["output_tokens"]


def test_build_stats_counts_skipped_budget_only_after_a_budget_stop(db_session: Session) -> None:
    budget = BudgetTracker(10)
    budget.record(_call_entry("fast-model", 50, 5))
    with pytest.raises(BudgetExceeded):
        budget.check()
    result = _mixed_result(db_session, budget)
    stats = build_stats(result)
    assert stats["skipped_budget"] == len(unprocessed(result)) == 1  # item "f" was never rated
    assert stats["budget_exceeded"] is True


def test_build_stats_counts_an_item_failing_in_both_stages_once(db_session: Session) -> None:
    result = _mixed_result(db_session, BudgetTracker(100))
    a = result.taken[0]
    both = RunDraft(
        job_id=result.job_id,
        run_id=result.run_id,
        counts=result.counts,
        taken=result.taken,
        relevance=[_rel(a, ItemStatus.FAILED, "e")],
        summaries=[_sum(a, ItemStatus.FAILED, "e")],
        digest=None,
        budget=result.budget,
    )
    assert build_stats(both)["failed"] == 1


def test_build_stats_with_an_unpriced_model_is_incomplete(db_session: Session) -> None:
    budget = BudgetTracker(1_000_000)
    budget.record(_call_entry("fast-model", 1000, 500))
    budget.record(_call_entry("mystery-model", 1000, 500, ModelRegistry({})))
    stats = build_stats(_mixed_result(db_session, budget))
    assert stats["cost_complete"] is False
    assert stats["estimated_cost_usd"] == "0.002000"  # the priced call only


def test_build_stats_without_taken_items_is_all_zero(db_session: Session) -> None:
    job = make_job(db_session)
    result = RunDraft(
        job_id=job.id,
        run_id=1,
        counts=StageCounts(found=0, new=0, after_keyword_filter=0),
        taken=[],
        relevance=[],
        summaries=[],
        digest=None,
        budget=BudgetTracker(5),
    )
    stats = build_stats(result)
    assert (stats["tokens"], stats["llm_calls"], stats["skipped_budget"]) == (0, 0, 0)
    assert stats["estimated_cost_usd"] == "0.000000"
    assert stats["cost_complete"] is True


async def test_stored_stats_equal_the_usage_totals_of_the_run(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed)
    finalize_run(factory, session, result)
    session.close()
    run = _run_row(factory, seed.run_id)
    assert run.stats is not None
    assert list(run.stats) == _STATS_KEYS
    assert (run.stats["found"], run.stats["new"], run.stats["after_keyword_filter"]) == (10, 4, 3)
    assert (run.stats["relevant"], run.stats["summarized"], run.stats["failed"]) == (2, 2, 0)
    with factory() as check:
        totals = UsageRepository(check).totals_for_run(seed.run_id)
    assert run.stats["input_tokens"] == totals.input_tokens
    assert run.stats["output_tokens"] == totals.output_tokens
    assert Decimal(run.stats["estimated_cost_usd"]) == totals.cost_usd


def test_digest_calls_after_a_budget_stop_may_overshoot_without_the_flag(
    db_session: Session,
) -> None:
    """``budget_exceeded`` is set by a failed check only; ``over_budget`` shows the overshoot."""
    budget = BudgetTracker(10)
    budget.record(_call_entry("fast-model", 50, 5))  # no check afterwards, like the digest call
    stats = build_stats(_mixed_result(db_session, budget))
    assert (stats["tokens"], stats["budget_limit"], stats["budget_exceeded"]) == (55, 10, False)
    assert stats["over_budget"] is True


def test_a_run_landing_exactly_on_the_limit_is_not_over_budget(db_session: Session) -> None:
    budget = BudgetTracker(55)
    budget.record(_call_entry("fast-model", 50, 5))
    assert build_stats(_mixed_result(db_session, budget))["over_budget"] is False


# --- Run status (US4) ----------------------------------------------------------------------

_OK = (ItemStatus.RELEVANT, ItemStatus.SUMMARIZED)
_IRRELEVANT = (ItemStatus.SKIPPED_IRRELEVANT, None)
_REL_FAILED = (ItemStatus.FAILED, None)
_SUM_FAILED = (ItemStatus.RELEVANT, ItemStatus.FAILED)


@pytest.mark.parametrize(
    ("outcomes", "exceeded", "expected"),
    [
        pytest.param([_OK, _OK], False, RunStatus.SUCCEEDED, id="all-succeeded"),
        pytest.param([_OK, _IRRELEVANT], False, RunStatus.SUCCEEDED, id="irrelevant-is-fine"),
        pytest.param([], False, RunStatus.SUCCEEDED, id="no-items"),
        pytest.param([_OK, _OK, _REL_FAILED], False, RunStatus.PARTIAL, id="one-of-three-failed"),
        pytest.param([_OK, _SUM_FAILED], False, RunStatus.PARTIAL, id="summary-failed"),
        pytest.param([_REL_FAILED, _REL_FAILED], False, RunStatus.FAILED, id="all-failed"),
        pytest.param([_SUM_FAILED], False, RunStatus.FAILED, id="only-attempt-failed-late"),
        pytest.param([_REL_FAILED, _IRRELEVANT], False, RunStatus.PARTIAL, id="failed-and-skipped"),
        pytest.param([], True, RunStatus.PARTIAL, id="budget-zero-successes"),
        pytest.param([_OK], True, RunStatus.PARTIAL, id="budget-with-success"),
        pytest.param([_OK, _REL_FAILED], True, RunStatus.PARTIAL, id="budget-and-a-failure"),
        pytest.param([_REL_FAILED], True, RunStatus.PARTIAL, id="budget-and-all-failed"),
    ],
)
def test_decide_status_table(
    db_session: Session,
    outcomes: list[tuple[ItemStatus, ItemStatus | None]],
    exceeded: bool,
    expected: RunStatus,
) -> None:
    job = make_job(db_session)
    items = [
        make_item(db_session, job, url=f"https://example.com/{n}") for n in range(len(outcomes))
    ]
    relevance, summaries = [], []
    for item, (rated, summarized) in zip(items, outcomes, strict=True):
        relevance.append(
            _rel(item, rated, "LLMUnavailableError: x" if rated == ItemStatus.FAILED else None)
        )
        if summarized is not None:
            summaries.append(
                _sum(
                    item,
                    summarized,
                    "LLMUnavailableError: x" if summarized == ItemStatus.FAILED else None,
                )
            )
    budget = BudgetTracker(1)
    if exceeded:
        budget.record(_entry(5, 5))
        with pytest.raises(BudgetExceeded):
            budget.check()
    result = RunDraft(
        job_id=job.id,
        run_id=1,
        counts=_COUNTS,
        taken=items,
        relevance=relevance,
        summaries=summaries,
        digest=None,
        budget=budget,
    )
    assert decide_status(result) == expected


# --- Source outcomes in the status rule (#21, US2) ---------------------------------------------


def _source_result(
    db_session: Session,
    *,
    sources: int,
    sources_failed: int,
    outcomes: list[tuple[ItemStatus, ItemStatus | None]] = (),  # type: ignore[assignment]
    exceeded: bool = False,
) -> RunDraft:
    job = make_job(db_session)
    run = make_run(db_session, job)
    items = [
        make_item(db_session, job, url=f"https://example.com/{n}") for n in range(len(outcomes))
    ]
    ItemRepository(db_session).mark_taken(items, run.id)
    relevance, summaries = [], []
    for item, (rated, summarized) in zip(items, outcomes, strict=True):
        relevance.append(
            _rel(item, rated, "LLMUnavailableError: x" if rated == ItemStatus.FAILED else None)
        )
        if summarized is not None:
            summaries.append(_sum(item, summarized))
    budget = BudgetTracker(1)
    if exceeded:
        budget.record(_entry(5, 5))
        with pytest.raises(BudgetExceeded):
            budget.check()
    return RunDraft(
        job_id=job.id,
        run_id=run.id,
        counts=StageCounts(
            found=1, new=1, after_keyword_filter=1, sources=sources, sources_failed=sources_failed
        ),
        taken=items,
        relevance=relevance,
        summaries=summaries,
        digest=None,
        budget=budget,
    )


def test_all_sources_failing_fails_the_run_with_its_own_error(db_session: Session) -> None:
    result = _source_result(db_session, sources=2, sources_failed=2)

    assert decide_status(result) == RunStatus.FAILED
    run = persist_run(db_session, result)
    assert (run.status, run.error) == (RunStatus.FAILED, ALL_SOURCES_FAILED_ERROR)
    assert ALL_SOURCES_FAILED_ERROR == "all sources failed"


def test_all_sources_failing_wins_over_items_that_succeeded(db_session: Session) -> None:
    result = _source_result(
        db_session,
        sources=1,
        sources_failed=1,
        outcomes=[(ItemStatus.RELEVANT, ItemStatus.SUMMARIZED)],
    )

    assert decide_status(result) == RunStatus.FAILED
    assert persist_run(db_session, result).error == ALL_SOURCES_FAILED_ERROR


def test_some_sources_failing_makes_the_run_partial(db_session: Session) -> None:
    result = _source_result(db_session, sources=2, sources_failed=1)

    assert decide_status(result) == RunStatus.PARTIAL
    assert persist_run(db_session, result).error is None


def test_no_adapter_sources_do_not_fail_the_run(db_session: Session) -> None:
    result = _source_result(db_session, sources=0, sources_failed=0)

    assert decide_status(result) == RunStatus.SUCCEEDED


def test_the_item_rules_still_apply_when_the_sources_are_healthy(db_session: Session) -> None:
    all_failed = _source_result(
        db_session, sources=1, sources_failed=0, outcomes=[(ItemStatus.FAILED, None)]
    )
    assert decide_status(all_failed) == RunStatus.FAILED
    assert persist_run(db_session, all_failed).error == ALL_FAILED_ERROR


def test_a_budget_stop_is_partial_with_healthy_sources(db_session: Session) -> None:
    result = _source_result(db_session, sources=1, sources_failed=0, exceeded=True)

    assert decide_status(result) == RunStatus.PARTIAL


def test_build_stats_reports_the_source_counts(db_session: Session) -> None:
    stats = build_stats(_source_result(db_session, sources=3, sources_failed=2))

    assert (stats["sources"], stats["sources_failed"]) == (3, 2)


async def test_all_items_failing_is_saved_through_the_normal_path(
    db_engine: Engine, clean_jobs: None, caplog: pytest.LogCaptureFixture
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    script: list[FakeStep] = [LLMUnavailableError("down", provider="mistral", model="m")] * 2
    session, result = await _run_pipeline(factory, seed, script, budget_limit=1_000_000)
    with caplog.at_level(logging.INFO, logger="invio.graph"):
        status = finalize_run(factory, session, result)
    session.close()

    assert status == RunStatus.FAILED
    assert _events(caplog, "run.persist_failed") == []
    run = _run_row(factory, seed.run_id)
    assert run.status == RunStatus.FAILED
    assert run.error == ALL_FAILED_ERROR  # no save error: the items failed, the save worked
    assert run.stats is not None
    assert (run.stats["found"], run.stats["failed"], run.stats["relevant"]) == (10, 2, 0)
    for item in _item_rows(factory, seed.item_ids):  # the item failures are committed
        assert item.status == ItemStatus.FAILED
        assert item.last_error is not None
        assert item.last_error.startswith("LLMUnavailableError:")


@pytest.mark.parametrize(
    ("path", "limit", "expected"),
    [
        ("succeeded", 1_000_000, RunStatus.SUCCEEDED),
        ("partial", 1000, RunStatus.PARTIAL),
        ("failed", 1_000_000, RunStatus.FAILED),
        ("failed-save", 1_000_000, RunStatus.FAILED),
    ],
)
async def test_every_finalize_path_leaves_the_run_finished(
    db_engine: Engine,
    clean_jobs: None,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    limit: int,
    expected: RunStatus,
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    scripts: dict[str, list[FakeStep]] = {
        "succeeded": _stage_script(2),
        "partial": [_scored(0.9, 2000)],  # the first call exhausts the budget
        "failed": [LLMUnavailableError("down")] * 2,
        "failed-save": _stage_script(2),
    }
    session, result = await _run_pipeline(factory, seed, scripts[path], budget_limit=limit)
    if path == "failed-save":
        real_finish, calls = RunRepository.finish, []

        def _first_call_fails(self: RunRepository, *args: Any, **kwargs: Any) -> Run:
            calls.append(1)
            if len(calls) == 1:  # the save fails, the recovery's own finish goes through
                raise _injected_error()
            return real_finish(self, *args, **kwargs)

        monkeypatch.setattr(RunRepository, "finish", _first_call_fails)
    assert finalize_run(factory, session, result) == expected
    session.close()
    run = _run_row(factory, seed.run_id)
    assert run.status == expected
    assert run.finished_at is not None


# --- QA additions: quickstart scenarios end to end, released items in the digest -----------

# 11,049 chars = 2,763 tokens per paragraph: one summary chunk each (as in test_summarize_item).
_LONG_PARAGRAPH = ("lorem ipsum. " * 850).strip()


def _exceeded_budget(limit: int = 10) -> BudgetTracker:
    budget = BudgetTracker(limit)
    budget.record(_entry(limit + 1, 0))
    with pytest.raises(BudgetExceeded):
        budget.check()
    return budget


def test_persist_run_rejects_a_digest_naming_an_unrated_item_after_a_budget_stop(
    db_session: Session,
) -> None:
    result = _bare_result(db_session, digest=None, budget=_exceeded_budget())
    draft = DigestDraft(title="t", body="b", item_ids=[result.taken[0].id])
    with pytest.raises(ValueError, match="not summarized by the run") as caught:
        persist_run(db_session, _with(result, draft))
    assert str(result.taken[0].id) in str(caught.value)
    assert db_session.scalars(select(Digest)).all() == []


def test_persist_run_rejects_a_digest_naming_a_relevant_unsummarized_item(
    db_session: Session,
) -> None:
    base = _bare_result(db_session, digest=None, budget=_exceeded_budget())
    done, pending = base.taken
    result = RunDraft(
        job_id=base.job_id,
        run_id=base.run_id,
        counts=base.counts,
        taken=base.taken,
        relevance=[_rel(done, ItemStatus.RELEVANT), _rel(pending, ItemStatus.RELEVANT)],
        summaries=[_sum(done, ItemStatus.SUMMARIZED)],
        digest=DigestDraft(title="t", body="b", item_ids=[done.id, pending.id]),
        budget=base.budget,
    )
    with pytest.raises(ValueError, match=rf"not summarized by the run: \[{pending.id}\]"):
        persist_run(db_session, result)


def test_persist_run_accepts_processed_items_after_a_budget_stop(db_session: Session) -> None:
    base = _bare_result(db_session, digest=None, budget=_exceeded_budget())
    done, pending = base.taken
    result = RunDraft(
        job_id=base.job_id,
        run_id=base.run_id,
        counts=base.counts,
        taken=base.taken,
        relevance=[_rel(done, ItemStatus.RELEVANT)],
        summaries=[_sum(done, ItemStatus.SUMMARIZED)],
        digest=DigestDraft(title="t", body="b", item_ids=(done.id,)),  # any Sequence
        budget=base.budget,
    )
    run = persist_run(db_session, result)
    assert run.status == RunStatus.PARTIAL
    (digest,) = db_session.scalars(select(Digest)).all()
    assert digest.item_ids == [done.id]
    assert (pending.status, pending.run_id, pending.attempts) == (ItemStatus.NEW, None, 0)


@pytest.mark.parametrize(
    ("rated", "summarized"),
    [
        pytest.param(None, None, id="unprocessed"),
        pytest.param(ItemStatus.SKIPPED_IRRELEVANT, None, id="irrelevant"),
        pytest.param(ItemStatus.FAILED, None, id="relevance-failed"),
        pytest.param(ItemStatus.RELEVANT, ItemStatus.FAILED, id="summary-failed"),
    ],
)
def test_persist_run_without_budget_stop_rejects_a_digest_of_unsummarized_items(
    db_session: Session, rated: ItemStatus | None, summarized: ItemStatus | None
) -> None:
    """A digest uses successfully processed items only (FR-005), with or without a budget stop."""
    base = _bare_result(db_session, digest=None, item_count=1)
    (item,) = base.taken
    result = RunDraft(
        job_id=base.job_id,
        run_id=base.run_id,
        counts=base.counts,
        taken=base.taken,
        relevance=[_rel(item, rated)] if rated else [],
        summaries=[_sum(item, summarized)] if summarized else [],
        digest=DigestDraft(title="t", body="b", item_ids=[item.id]),
        budget=base.budget,
    )
    with pytest.raises(ValueError, match=rf"not summarized by the run: \[{item.id}\]"):
        persist_run(db_session, result)
    assert db_session.scalars(select(Digest)).all() == []


async def test_finalize_run_turns_a_digest_with_a_released_item_into_a_failed_run(
    db_engine: Engine, clean_jobs: None, caplog: pytest.LogCaptureFixture
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=2)
    # Both rated relevant; the budget stops the summary stage before any summary.
    script: list[FakeStep] = [_scored(0.9, 600), _scored(0.9, 600)]
    session, result = await _run_pipeline(factory, seed, script, budget_limit=1000)
    assert result.budget.exceeded is True
    assert result.summaries == []
    bad = _with(result, DigestDraft(title="t", body="b", item_ids=[seed.item_ids[0]]))
    with caplog.at_level(logging.INFO, logger="invio.graph"):
        assert finalize_run(factory, session, bad) == RunStatus.FAILED
    session.close()

    run = _run_row(factory, seed.run_id)
    assert run.status == RunStatus.FAILED
    assert run.finished_at is not None
    assert run.error == "ValueError: run failed"
    assert run.stats is not None
    assert (run.stats["llm_calls"], run.stats["tokens"]) == (2, 1200)
    assert run.stats["budget_exceeded"] is True
    assert "found" not in run.stats  # recovery layout
    assert _digests(factory) == []
    for item in _item_rows(factory, seed.item_ids):  # the release was rolled back too
        assert (item.status, item.attempts, item.run_id) == (ItemStatus.NEW, 1, seed.run_id)
    assert len(_usage_rows(factory, seed.run_id)) == 2
    (event,) = _events(caplog, "run.persist_failed")
    assert event.error == "ValueError"


async def test_scenario_1_three_items_two_in_digest_succeeds(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=3)
    session, result = await _run_stages(factory, seed)
    draft = DigestDraft(title="Weekly", body="Body", item_ids=seed.item_ids[:2])
    assert finalize_run(factory, session, _with(result, draft)) == RunStatus.SUCCEEDED
    session.close()

    (digest,) = _digests(factory)
    assert (digest.run_id, digest.item_ids) == (seed.run_id, seed.item_ids[:2])
    assert _status_of(factory, seed.item_ids) == [ItemStatus.SUMMARIZED] * 3
    rows = _usage_rows(factory, seed.run_id)
    assert [r.purpose for r in rows] == ["relevance"] * 3 + ["summarize"] * 3
    assert all((r.job_id, r.run_id) == (seed.job_id, seed.run_id) for r in rows)
    run = _run_row(factory, seed.run_id)
    assert (run.status, run.error) == (RunStatus.SUCCEEDED, None)
    assert run.stats == {
        "version": 1,
        "found": 10,
        "new": 4,
        "after_keyword_filter": 3,
        "sources": 0,
        "sources_failed": 0,
        "relevant": 3,
        "summarized": 3,
        "failed": 0,
        "processed": 3,
        "skipped_budget": 0,
        "llm_calls": 6,
        "llm_calls_unpriced": 0,
        "input_tokens": 900,
        "output_tokens": 180,
        "tokens": 1080,
        "estimated_cost_usd": "0.001260",
        "cost_complete": True,
        "budget_limit": 1_000_000,
        "budget_exceeded": False,
        "over_budget": False,
    }


async def test_scenario_2_a_real_database_rejection_rolls_the_save_back(
    db_engine: Engine, clean_jobs: None
) -> None:
    """No monkeypatch: the database itself rejects the digest (NOT NULL title)."""
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed)
    draft = DigestDraft(title=None, body="Body", item_ids=seed.item_ids)  # type: ignore[arg-type]
    assert finalize_run(factory, session, _with(result, draft)) == RunStatus.FAILED
    session.close()

    assert _digests(factory) == []
    for item in _item_rows(factory, seed.item_ids):
        assert (item.status, item.summary, item.relevance) == (ItemStatus.NEW, None, None)
    assert len(_usage_rows(factory, seed.run_id)) == 4
    run = _run_row(factory, seed.run_id)
    assert run.status == RunStatus.FAILED
    assert run.finished_at is not None
    assert run.error == "IntegrityError: run failed"


async def test_scenario_3_budget_stop_blocks_calls_and_counts_skipped_items(
    db_engine: Engine, clean_jobs: None, caplog: pytest.LogCaptureFixture
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=5)
    session = factory()
    items = _load_items(session, seed.item_ids)
    budget = BudgetTracker(1000)
    fake = FakeProvider([_scored(0.1), _scored(0.9), _scored(0.9), _scored(0.9)])
    scoring, summary = _contexts(session, seed, fake, budget)
    with caplog.at_level(logging.INFO, logger="invio.graph"):
        relevance = await score_items(items, scoring)
        summaries = await summarize_items(
            [i for i in items if i.status == ItemStatus.RELEVANT], summary
        )
        result = RunDraft(
            job_id=seed.job_id,
            run_id=seed.run_id,
            counts=_COUNTS,
            taken=items,
            relevance=relevance,
            summaries=summaries,
            digest=None,
            budget=budget,
        )
        assert finalize_run(factory, session, result) == RunStatus.PARTIAL
    session.close()

    assert len(fake.requests) == 2  # exactly two relevance calls, no summary call
    assert summaries == []
    (event,) = _events(caplog, "budget.exceeded")  # logged once, by the relevance stage
    assert (event.stage, event.used, event.limit) == ("relevance", 1200, 1000)
    run = _run_row(factory, seed.run_id)
    assert run.stats is not None
    assert run.stats["skipped_budget"] == 4
    assert (run.stats["failed"], run.stats["relevant"], run.stats["summarized"]) == (0, 1, 0)
    assert (run.stats["budget_exceeded"], run.stats["tokens"]) == (True, 1200)
    expected = [ItemStatus.SKIPPED_IRRELEVANT] + [ItemStatus.NEW] * 4
    assert _status_of(factory, seed.item_ids) == expected


async def test_scenario_4_budget_stop_during_chunks_releases_the_item_and_keeps_usage(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=1, raw_content="\n\n".join([_LONG_PARAGRAPH] * 3))
    chunk = FakeReply(json.dumps({"bullets": ["a"]}), Usage(300, 100))
    # relevance 100, chunk 1 -> 500, chunk 2 -> 900, chunk 3 blocked (900 > 700)
    script: list[FakeStep] = [_scored(0.9, 100), chunk, chunk, chunk]
    session, result = await _run_pipeline(factory, seed, script, budget_limit=700)
    assert result.summaries == []
    assert finalize_run(factory, session, result) == RunStatus.PARTIAL
    session.close()

    (item,) = _item_rows(factory, seed.item_ids)
    assert (item.status, item.attempts, item.run_id) == (ItemStatus.NEW, 0, None)
    assert (item.summary, item.last_error) == (None, None)
    rows = _usage_rows(factory, seed.run_id)
    assert [r.purpose for r in rows] == ["relevance", "summarize_chunk", "summarize_chunk"]
    run = _run_row(factory, seed.run_id)
    assert run.stats is not None
    assert (run.stats["failed"], run.stats["skipped_budget"], run.stats["tokens"]) == (0, 1, 900)


async def test_scenario_6_budget_stop_before_any_item_counts_every_item_skipped(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=3)
    session, result = await _run_pipeline(factory, seed, [_scored(0.9, 2000)], budget_limit=1000)
    assert finalize_run(factory, session, result) == RunStatus.PARTIAL
    session.close()
    run = _run_row(factory, seed.run_id)
    assert run.stats is not None
    assert (run.stats["skipped_budget"], run.stats["failed"]) == (3, 0)
    assert _digests(factory) == []


async def test_scenario_8_one_of_three_items_failing_ends_partial(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=3)
    summary = FakeReply(_summary_answer(), Usage(200, 50))
    script: list[FakeStep] = [
        _scored(0.9, 100),
        LLMUnavailableError("down", provider="mistral", model="fast-model"),
        _scored(0.9, 100),
        summary,
        summary,
    ]
    session, result = await _run_pipeline(factory, seed, script, budget_limit=1_000_000)
    assert finalize_run(factory, session, result) == RunStatus.PARTIAL
    session.close()

    assert _status_of(factory, seed.item_ids) == [
        ItemStatus.SUMMARIZED,
        ItemStatus.FAILED,
        ItemStatus.SUMMARIZED,
    ]
    run = _run_row(factory, seed.run_id)
    assert (run.status, run.error) == (RunStatus.PARTIAL, None)
    assert run.stats is not None
    assert (run.stats["failed"], run.stats["summarized"], run.stats["skipped_budget"]) == (1, 2, 0)
    assert run.stats["budget_exceeded"] is False


async def test_scenario_9_unpriced_model_is_stored_as_incomplete_cost(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=1)
    session = factory()
    items = _load_items(session, seed.item_ids)
    budget = BudgetTracker(1_000_000)
    fake = FakeProvider(
        [
            FakeReply(_relevance_answer(), Usage(100, 10)),
            FakeReply(_summary_answer(), Usage(200, 50)),
            FakeReply(json.dumps({"title": "Weekly"}), Usage(1000, 1000)),
        ]
    )
    scoring, summary = _contexts(session, seed, fake, budget)
    relevance = await score_items(items, scoring)
    summaries = await summarize_items(items, summary)
    await call_structured(
        scoring,
        _DigestAnswer,
        model="unpriced-model",
        purpose="digest",
        system="s",
        user="u",
        per_item=False,
    )
    result = RunDraft(
        job_id=seed.job_id,
        run_id=seed.run_id,
        counts=_COUNTS,
        taken=items,
        relevance=relevance,
        summaries=summaries,
        digest=None,
        budget=budget,
    )
    assert finalize_run(factory, session, result) == RunStatus.SUCCEEDED
    session.close()

    run = _run_row(factory, seed.run_id)
    assert run.stats is not None
    assert run.stats["cost_complete"] is False
    # relevance (100*1 + 10*2) + short summary (200*1 + 50*2), both fast-model, per million
    # tokens; the unpriced digest call adds nothing
    assert run.stats["estimated_cost_usd"] == "0.000420"
    assert run.stats["tokens"] == 2360
    assert _usage_rows(factory, seed.run_id)[-1].cost_usd is None
    with factory() as check:
        totals = UsageRepository(check).totals_for_run(seed.run_id)
    assert Decimal(run.stats["estimated_cost_usd"]) == totals.cost_usd


async def test_scenario_11_a_run_without_items_succeeds_with_zero_usage(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory, count=0)
    session = factory()
    result = RunDraft(
        job_id=seed.job_id,
        run_id=seed.run_id,
        counts=StageCounts(found=0, new=0, after_keyword_filter=0),
        taken=[],
        relevance=[],
        summaries=[],
        digest=None,
        budget=BudgetTracker(200_000),
    )
    assert finalize_run(factory, session, result) == RunStatus.SUCCEEDED
    session.close()
    run = _run_row(factory, seed.run_id)
    assert (run.status, run.error) == (RunStatus.SUCCEEDED, None)
    assert run.finished_at is not None
    assert run.stats is not None
    assert (run.stats["tokens"], run.stats["llm_calls"]) == (0, 0)
    assert run.stats["estimated_cost_usd"] == "0.000000"
    assert _usage_rows(factory, seed.run_id) == []
    assert _digests(factory) == []


async def test_budget_stop_keeps_an_untouched_retry_item_failed_and_undoes_its_attempt(
    db_engine: Engine, clean_jobs: None
) -> None:
    """FR-006: a retried ``failed`` item left untouched stays ``failed``; a rated one is ``new``."""
    factory = session_factory(db_engine)
    seed = _seed(factory, count=2, status=ItemStatus.FAILED, attempts=1)
    session, result = await _run_pipeline(factory, seed, [_scored(0.9, 2000)], budget_limit=1000)
    assert finalize_run(factory, session, result) == RunStatus.PARTIAL
    session.close()
    rated, untouched = _item_rows(factory, seed.item_ids)
    assert (rated.status, rated.attempts, rated.run_id) == (ItemStatus.NEW, 1, None)
    assert (untouched.status, untouched.attempts, untouched.run_id) == (ItemStatus.FAILED, 1, None)
    run = _run_row(factory, seed.run_id)
    assert run.stats is not None
    assert (run.stats["failed"], run.stats["skipped_budget"]) == (0, 2)


# --- Dry run finish (#21, US6) ---------------------------------------------------------------


async def test_finish_dry_run_stores_stats_and_rolls_the_work_back(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed)
    result = dataclasses.replace(
        result,
        counts=StageCounts(found=10, new=4, after_keyword_filter=3, sources=2, sources_failed=1),
        dry_run=True,
    )

    status = finish_dry_run(factory, session, result)
    session.close()

    assert status == RunStatus.PARTIAL
    run = _run_row(factory, seed.run_id)
    assert run.status == RunStatus.PARTIAL
    assert run.stats is not None
    assert run.stats["dry_run"] is True
    assert (run.stats["sources"], run.stats["sources_failed"]) == (2, 1)
    assert run.stats["llm_calls"] == 4 and run.stats["tokens"] == 720
    assert run.finished_at is not None
    assert _usage_rows(factory, seed.run_id) == []  # rolled back, the ledger is not replayed
    with factory() as check:
        assert UsageRepository(check).totals_for_run(seed.run_id).input_tokens == 0
    assert _digests(factory) == []
    assert _status_of(factory, seed.item_ids) == [ItemStatus.NEW] * 2  # committed take only


async def test_finish_dry_run_failing_all_sources_stores_the_sources_error(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed, digest=False)
    result = dataclasses.replace(
        result,
        counts=StageCounts(found=0, new=0, after_keyword_filter=0, sources=1, sources_failed=1),
    )

    status = finish_dry_run(factory, session, result)
    session.close()

    run = _run_row(factory, seed.run_id)
    assert status == RunStatus.FAILED
    assert run.error == ALL_SOURCES_FAILED_ERROR


async def test_finish_dry_run_that_cannot_write_falls_back_to_a_failed_run(
    db_engine: Engine, clean_jobs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, drafted = await _run_stages(factory, seed)
    result = dataclasses.replace(drafted, dry_run=True)
    calls: list[int] = []
    real_finish = RunRepository.finish

    def flaky_finish(self: RunRepository, run: Run, status: RunStatus, **kw: Any) -> Run:
        calls.append(1)
        if len(calls) == 1:
            raise _injected_error(OperationalError)
        return real_finish(self, run, status, **kw)

    monkeypatch.setattr(RunRepository, "finish", flaky_finish)

    status = finish_dry_run(factory, session, result)
    session.close()

    assert status == RunStatus.FAILED
    run = _run_row(factory, seed.run_id)
    assert (run.status, run.error) == (RunStatus.FAILED, "OperationalError: run failed")
    assert _usage_rows(factory, seed.run_id) == []  # a dry run never replays usage rows
    assert run.stats is not None and run.stats["dry_run"] is True


async def test_finish_dry_run_rejects_an_invalid_digest_as_a_failed_run(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, result = await _run_stages(factory, seed)
    bad = dataclasses.replace(result, digest=DigestDraft(title="t", body="b", item_ids=[999_999]))

    status = finish_dry_run(factory, session, bad)
    session.close()

    assert status == RunStatus.FAILED
    assert _run_row(factory, seed.run_id).error == "ValueError: run failed"


def test_record_failed_run_without_replay_keeps_no_usage_rows(
    db_engine: Engine, clean_jobs: None
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    budget = BudgetTracker(1000)
    budget.record(_call_entry("fast-model", 100, 10))

    status = record_failed_run(
        factory,
        job_id=seed.job_id,
        run_id=seed.run_id,
        budget=budget,
        error=RuntimeError("x"),
        replay_usage=False,
    )

    assert status == RunStatus.FAILED
    assert _usage_rows(factory, seed.run_id) == []
    run = _run_row(factory, seed.run_id)
    assert run.stats is not None
    assert run.stats["dry_run"] is True and run.stats["tokens"] == 110


async def test_finish_dry_run_recovers_and_reraises_a_keyboard_interrupt(
    db_engine: Engine, clean_jobs: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = session_factory(db_engine)
    seed = _seed(factory)
    session, drafted = await _run_stages(factory, seed)
    result = dataclasses.replace(drafted, dry_run=True)

    def _interrupt(*args: object, **kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("invio.graph.nodes.persist.build_stats", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        finish_dry_run(factory, session, result)
    session.close()

    run = _run_row(factory, seed.run_id)
    assert (run.status, run.error) == (RunStatus.FAILED, "KeyboardInterrupt: run failed")
    assert _usage_rows(factory, seed.run_id) == []
