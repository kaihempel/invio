"""Stored run errors: the pure builder, the repository merge and the end-to-end persistence."""

import dataclasses
import logging
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import Engine, select
from sqlalchemy.exc import SQLAlchemyError

from invio.config.job import JobConfig
from invio.db.models import Job, Run
from invio.db.repositories import JobRepository, RunRepository
from invio.db.session import session_factory, session_scope
from invio.domain import ItemStatus, RunStatus
from invio.graph.errors import (
    DELIVERY_ERROR,
    MAX_MESSAGE_CHARS,
    MAX_STORED_ERRORS,
    StoredError,
    ordered_errors,
    stored_errors,
)
from invio.graph.nodes.persist import sanitized_error
from invio.graph.nodes.relevance import RelevanceOutcome
from invio.graph.nodes.summarize_item import SummaryOutcome
from invio.graph.ports import DeliveryReport
from invio.graph.scope import MAX_TITLE_CHARS, MAX_URL_CHARS, ItemRef
from invio.graph.state import ItemResult, RunError
from invio.pipeline.run import run_job
from invio.sources.errors import FetchError
from tests.conftest import FakeClock
from tests.pipeline_helpers import (
    RoutedFakeProvider,
    add_run,
    build_env,
    relevance_reply,
)

FEED = "https://example.com/feed.xml"


def _failed_relevance(item_id: int, error: str) -> ItemResult:
    outcome = RelevanceOutcome(
        item_id=item_id,
        status=ItemStatus.FAILED,
        relevance=None,
        result=None,
        error=error,
        error_class=error.split(":")[0],
    )
    return ItemResult(item_id=item_id, relevance=outcome)


def _failed_summary(item_id: int, error: str) -> ItemResult:
    outcome = SummaryOutcome(
        item_id=item_id,
        status=ItemStatus.FAILED,
        summary=None,
        error=error,
        calls=0,
        chunks=0,
        truncated=False,
        error_class=error.split(":")[0],
    )
    return ItemResult(item_id=item_id, summary=outcome)


# --- stored_errors -------------------------------------------------------------------------


def test_an_item_error_takes_the_outcome_text_title_and_url() -> None:
    errors = [RunError(stage="score_relevance", error_class="LLMInvalidOutputError", item_id=4)]
    items = [_failed_relevance(4, "LLMInvalidOutputError: bad answer")]
    refs = {4: ItemRef(title="A title", url="https://example.org/a")}

    entries, omitted = stored_errors(errors, items, refs)

    assert omitted == 0
    assert entries == [
        StoredError(
            stage="score_relevance",
            error_class="LLMInvalidOutputError",
            message="LLMInvalidOutputError: bad answer",
            item_id=4,
            title="A title",
            url="https://example.org/a",
        )
    ]


def test_a_summary_error_takes_the_summary_outcome_text() -> None:
    errors = [RunError(stage="summarize_item", error_class="LLMUnavailableError", item_id=2)]
    items = [_failed_summary(2, "LLMUnavailableError: call failed")]

    (entry,), _ = stored_errors(errors, items, {})

    assert entry.message == "LLMUnavailableError: call failed"
    assert entry.title is None and entry.url is None


def test_an_item_error_without_an_outcome_falls_back_to_the_class() -> None:
    errors = [RunError(stage="extract_text", error_class="KeyError", item_id=2)]

    (entry,), _ = stored_errors(errors, [], {})

    assert entry.message == "KeyError"


def test_an_item_error_at_another_stage_never_borrows_an_outcome_text() -> None:
    """A ``persist`` error of an item that also failed rating keeps its own class only."""
    errors = [RunError(stage="persist", error_class="OperationalError", item_id=4)]
    items = [_failed_relevance(4, "LLMInvalidOutputError: bad answer")]

    (entry,), _ = stored_errors(errors, items, {})

    assert entry.message == "OperationalError"


def test_an_item_error_whose_outcome_did_not_fail_falls_back_to_the_class() -> None:
    rated = RelevanceOutcome(
        item_id=5,
        status=ItemStatus.RELEVANT,
        relevance=None,
        result=None,
        error=None,
        error_class=None,
    )
    errors = [RunError(stage="score_relevance", error_class="TimeoutError", item_id=5)]

    (entry,), _ = stored_errors(errors, [ItemResult(item_id=5, relevance=rated)], {})

    assert entry.message == "TimeoutError"


def test_a_source_error_names_the_key_but_no_url() -> None:
    errors = [RunError(stage="fetch_sources", error_class="FetchError", source="0:rss")]

    (entry,), _ = stored_errors(errors, [], {})

    assert entry.message == "FetchError: source failed"
    assert entry.source == "0:rss"
    assert entry.url is None and entry.item_id is None


def test_a_fatal_error_uses_the_sanitized_failure() -> None:
    with pytest.raises(ValidationError) as caught:
        JobConfig.model_validate({})
    failure = caught.value
    errors = [RunError(stage="load_job", error_class="ValidationError")]

    (entry,), _ = stored_errors(errors, [], {}, fatal=errors[0], failure=failure)

    assert entry.message == sanitized_error(failure)
    assert entry.stage == "load_job"


def test_entries_are_sorted_by_stage_then_item_and_deduplicated() -> None:
    errors = [
        RunError(stage="summarize_item", error_class="KeyError", item_id=1),
        RunError(stage="score_relevance", error_class="KeyError", item_id=9),
        RunError(stage="score_relevance", error_class="KeyError", item_id=3),
        RunError(stage="score_relevance", error_class="KeyError", item_id=3),
        RunError(stage="fetch_sources", error_class="FetchError", source="0:rss"),
    ]

    entries, _ = stored_errors(errors, [], {})

    assert [(e.stage, e.item_id) for e in entries] == [
        ("fetch_sources", None),
        ("score_relevance", 3),
        ("score_relevance", 9),
        ("summarize_item", 1),
    ]


def test_the_list_is_capped_and_the_rest_counted() -> None:
    errors = [RunError(stage="score_relevance", error_class="E", item_id=n) for n in range(150)]

    entries, omitted = stored_errors(errors, [], {})

    assert len(entries) == MAX_STORED_ERRORS == 100
    assert omitted == 50


def test_the_message_is_truncated() -> None:
    errors = [RunError(stage="score_relevance", error_class="E", item_id=1)]
    items = [_failed_relevance(1, "E: " + "x" * 900)]

    (entry,), _ = stored_errors(errors, items, {})

    assert len(entry.message) == MAX_MESSAGE_CHARS == 500


def test_an_item_ref_is_sanitized_and_bounded() -> None:
    ref = ItemRef.of(
        "\x1b[31mRed\x1b[0m\ntitle " + "t" * 900,
        "https://user:pw@example.org/" + "p" * 900 + "?token=secret#frag",
    )

    assert ref.title.startswith("Red title ")
    assert len(ref.title) == MAX_TITLE_CHARS == 300
    assert len(ref.url) == MAX_URL_CHARS == 500
    assert "secret" not in ref.url and "pw" not in ref.url and "#" not in ref.url


def test_the_fatal_text_goes_to_the_fatal_record_only() -> None:
    """A non-fatal error of the same class as the fatal one keeps its own message."""
    with pytest.raises(ValidationError) as caught:
        JobConfig.model_validate({})
    failure = caught.value
    earlier = RunError(stage="score_relevance", error_class="ValidationError", item_id=3)
    fatal = RunError(stage="synthesize_digest", error_class="ValidationError")
    items = [_failed_relevance(3, "ValidationError: item text")]

    first, second = stored_errors([fatal, earlier], items, {}, fatal=fatal, failure=failure)[0]

    assert (first.stage, first.message) == ("score_relevance", "ValidationError: item text")
    assert (second.stage, second.message) == ("synthesize_digest", sanitized_error(failure))


@pytest.mark.parametrize(
    ("delivery", "message"),
    [
        (DeliveryReport(sent=2, failed=1), "1 of 3 notification(s) not sent"),
        (DeliveryReport(sent=0, failed=0, error="smtp: secret"), "delivery did not start"),
        (None, "delivery failed"),
    ],
)
def test_a_delivery_error_is_described_by_its_counts(
    delivery: DeliveryReport | None, message: str
) -> None:
    errors = [RunError(stage="notify", error_class=DELIVERY_ERROR)]

    (entry,), _ = stored_errors(errors, [], {}, delivery=delivery)

    assert entry.message == f"{DELIVERY_ERROR}: {message}"
    assert "secret" not in entry.message


def test_ordered_errors_deduplicates_and_sorts() -> None:
    late = RunError(stage="persist", error_class="E")
    early = RunError(stage="score_relevance", error_class="E", item_id=2)

    assert ordered_errors([late, early, late, early]) == [early, late]


def test_to_json_omits_none_fields() -> None:
    entry = StoredError(stage="persist", error_class="E", message="E")

    assert entry.to_json() == {"stage": "persist", "error_class": "E", "message": "E"}


def test_stored_error_validation() -> None:
    with pytest.raises(ValueError, match="error_class"):
        StoredError(stage="persist", error_class=" ", message="m")
    with pytest.raises(ValueError, match="not both"):
        StoredError(stage="persist", error_class="E", message="m", item_id=1, source="0:rss")


# --- RunRepository.record_errors -----------------------------------------------------------


def _finished_run(engine: Engine, stats: dict[str, Any] | None, status: RunStatus) -> int:
    factory = session_factory(engine)
    with session_scope(factory) as session:
        job_id = JobRepository(session).add(Job(name="j", enabled=True, config={})).id
    return add_run(factory, job_id, status, stats=stats, error="boom")


def _run(engine: Engine, run_id: int) -> Run:
    with session_scope(session_factory(engine)) as session:
        run = session.get(Run, run_id)
        assert run is not None
        session.expunge(run)
        return run


@pytest.mark.usefixtures("clean_jobs")
class TestRecordErrors:
    def test_merges_into_the_existing_stats(self, db_engine: Engine) -> None:
        run_id = _finished_run(db_engine, {"version": 1, "found": 3}, RunStatus.PARTIAL)
        before = _run(db_engine, run_id)

        with session_scope(session_factory(db_engine)) as session:
            done = RunRepository(session).record_errors(run_id, [{"stage": "persist"}])

        after = _run(db_engine, run_id)
        assert done is True
        assert after.stats == {"version": 1, "found": 3, "errors": [{"stage": "persist"}]}
        assert (after.status, after.error, after.finished_at) == (
            before.status,
            before.error,
            before.finished_at,
        )

    def test_null_stats_become_a_minimal_object(self, db_engine: Engine) -> None:
        run_id = _finished_run(db_engine, None, RunStatus.FAILED)

        with session_scope(session_factory(db_engine)) as session:
            RunRepository(session).record_errors(run_id, [])

        assert _run(db_engine, run_id).stats == {"version": 1, "errors": []}

    def test_omitted_is_written_only_when_positive(self, db_engine: Engine) -> None:
        run_id = _finished_run(db_engine, {"version": 1}, RunStatus.PARTIAL)

        with session_scope(session_factory(db_engine)) as session:
            RunRepository(session).record_errors(run_id, [], omitted=0)
        assert "errors_omitted" not in (_run(db_engine, run_id).stats or {})

        with session_scope(session_factory(db_engine)) as session:
            RunRepository(session).record_errors(run_id, [], omitted=7)
        assert (_run(db_engine, run_id).stats or {})["errors_omitted"] == 7

    def test_a_running_or_unknown_run_is_left_alone(self, db_engine: Engine) -> None:
        run_id = _finished_run(db_engine, None, RunStatus.RUNNING)

        with session_scope(session_factory(db_engine)) as session:
            repo = RunRepository(session)
            assert repo.record_errors(run_id, []) is False
            assert repo.record_errors(run_id + 99, []) is False

        assert _run(db_engine, run_id).stats is None


# --- End to end through run_job ------------------------------------------------------------


def _stats(engine: Engine) -> dict[str, Any]:
    with session_scope(session_factory(engine)) as session:
        return dict(session.scalars(select(Run.stats)).one() or {})


def _failing_title(title: str) -> RoutedFakeProvider:
    def route(item_title: str) -> Any:
        return RuntimeError("boom secret") if item_title == title else relevance_reply()

    return RoutedFakeProvider(relevance=route)


@pytest.mark.usefixtures("clean_jobs")
@pytest.mark.parametrize("dry_run", [False, True])
async def test_a_failing_item_is_stored_with_title_and_url(
    db_engine: Engine, fake_clock: FakeClock, dry_run: bool
) -> None:
    env = build_env(db_engine, fake_clock, provider=_failing_title("Article 2"))

    result = await run_job(env.job_id, dry_run=dry_run, deps=env.deps)

    assert result.status == RunStatus.PARTIAL
    (entry,) = _stats(db_engine)["errors"]
    assert entry["stage"] == "score_relevance"
    assert entry["error_class"] == "RuntimeError"
    assert entry["message"] == "RuntimeError: item failed"
    assert entry["title"] == "Article 2"
    assert entry["url"] == "https://example.com/post-2"
    assert "secret" not in str(entry)


@pytest.mark.usefixtures("clean_jobs")
async def test_a_failing_source_is_stored(db_engine: Engine, fake_clock: FakeClock) -> None:
    env = build_env(db_engine, fake_clock, sources={FEED: FetchError("http", url=FEED, status=500)})

    await run_job(env.job_id, deps=env.deps)

    errors = _stats(db_engine)["errors"]
    assert [(e["stage"], e["source"]) for e in errors] == [("fetch_sources", "0:rss")]
    assert FEED not in str(errors)


@pytest.mark.usefixtures("clean_jobs")
async def test_a_stage_that_raises_is_stored(db_engine: Engine, fake_clock: FakeClock) -> None:
    env = build_env(db_engine, fake_clock)

    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("secret detail")

    deps = dataclasses.replace(env.deps, fetch_source=boom)

    await run_job(env.job_id, deps=deps)

    errors = _stats(db_engine)["errors"]
    assert errors[0]["stage"] == "fetch_sources"
    assert "secret detail" not in str(errors)


@pytest.mark.usefixtures("clean_jobs")
async def test_a_failed_delivery_is_stored_and_makes_the_run_partial(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    env = build_env(db_engine, fake_clock)
    env.ports.notifier.report = DeliveryReport(sent=1, failed=1, error="smtp said secret")

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.PARTIAL
    assert result.errors == (RunError(stage="notify", error_class=DELIVERY_ERROR),)
    (entry,) = _stats(db_engine)["errors"]
    assert entry == {
        "stage": "notify",
        "error_class": DELIVERY_ERROR,
        "message": f"{DELIVERY_ERROR}: 1 of 2 notification(s) not sent",
    }


@pytest.mark.usefixtures("clean_jobs")
async def test_a_succeeded_run_stores_an_empty_list(
    db_engine: Engine, fake_clock: FakeClock
) -> None:
    env = build_env(db_engine, fake_clock)

    result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    assert _stats(db_engine)["errors"] == []
    assert _stats(db_engine)["version"] == 1


@pytest.mark.usefixtures("clean_jobs")
async def test_a_failing_error_write_does_not_change_the_run(
    db_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def boom(*args: object, **kwargs: object) -> bool:
        raise SQLAlchemyError("secret sql")

    monkeypatch.setattr(RunRepository, "record_errors", boom)
    env = build_env(db_engine, fake_clock)

    with caplog.at_level(logging.WARNING, logger="invio.graph"):
        result = await run_job(env.job_id, deps=env.deps)

    assert result.status == RunStatus.SUCCEEDED
    assert "errors" not in _stats(db_engine)
    assert any(r.getMessage() == "run.errors_not_recorded" for r in caplog.records)
    assert "secret sql" not in caplog.text
    with session_scope(session_factory(db_engine)) as session:
        assert session.scalars(select(Job.locked_until)).one() is None
