"""``UsageService.report`` over a fixture database (issue #35)."""

from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from invio.db.models import LlmUsage
from invio.db.repositories import UsageBucket, UsageRepository
from invio.db.session import session_factory, session_scope
from invio.services.jobs import JobNotFoundError
from invio.services.usage import UsageGroup, UsageGrouping, UsageReport, UsageService, _key
from tests.cli_helpers import make_registry
from tests.db_helpers import make_job, make_llm_usage

# (job, provider, model, input, output, created_at); prices: 1 USD in / 2 USD out per Mtok.
ROWS = [
    ("alpha", "openai", "gpt-small", 1_000, 500, datetime(2026, 10, 1, 10, 0, tzinfo=UTC)),
    ("alpha", "openai", "gpt-small", 2_000, 1_000, datetime(2026, 10, 2, 23, 59, tzinfo=UTC)),
    ("alpha", "mistral", "mistral-one", 3_000, 0, datetime(2026, 10, 2, 0, 0, tzinfo=UTC)),
    ("beta", "openai", "gpt-large", 1_000_000, 1_000_000, datetime(2026, 10, 3, tzinfo=UTC)),
    ("beta", "openai", "unknown-x", 500, 500, datetime(2026, 10, 3, 8, 0, tzinfo=UTC)),
    # Registered, but for another provider: unpriced as well.
    ("beta", "mistral", "gpt-small", 100, 100, datetime(2026, 10, 1, 9, 0, tzinfo=UTC)),
]


@pytest.fixture
def factory(db_engine: Engine, clean_jobs: None) -> Iterator[sessionmaker[Session]]:
    factory = session_factory(db_engine)
    with session_scope(factory) as session:
        jobs = {name: make_job(session, name) for name in ("alpha", "beta", "idle")}
        for name, provider, model, given, produced, created in ROWS:
            make_llm_usage(
                session,
                jobs[name],
                provider=provider,
                model=model,
                input_tokens=given,
                output_tokens=produced,
                created_at=created,
            )
    yield factory


def _report(factory: sessionmaker[Session], **kw: object) -> UsageReport:
    return UsageService(factory).report(make_registry(), **kw)  # type: ignore[arg-type]


def _by_key(report: UsageReport) -> dict[str, UsageGroup]:
    return {group.key: group for group in report.groups}


def test_totals_match_the_raw_rows(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        calls, given, produced = session.execute(
            select(
                func.count(LlmUsage.id),
                func.sum(LlmUsage.input_tokens),
                func.sum(LlmUsage.output_tokens),
            )
        ).one()

    for by in UsageGrouping:
        total = _report(factory, by=by).total
        assert (total.calls, total.input_tokens, total.output_tokens) == (
            int(calls),
            int(given),
            int(produced),
        )
        assert total.cost_usd == Decimal("3.009000")
        assert total.cost_complete is False
        assert total.unpriced_models == ("mistral/gpt-small", "openai/unknown-x")


def test_by_model_prices_each_model(factory: sessionmaker[Session]) -> None:
    report = _report(factory)

    assert report.by is UsageGrouping.MODEL
    assert [group.key for group in report.groups] == [
        "mistral/gpt-small",
        "mistral/mistral-one",
        "openai/gpt-large",
        "openai/gpt-small",
        "openai/unknown-x",
    ]
    groups = _by_key(report)
    small = groups["openai/gpt-small"]
    assert (small.calls, small.input_tokens, small.output_tokens) == (2, 3_000, 1_500)
    assert small.cost_usd == Decimal("0.006000")
    assert small.cost_complete is True
    assert small.unpriced_models == ()
    unknown = groups["openai/unknown-x"]
    assert unknown.cost_usd is None
    assert unknown.cost_complete is False
    assert unknown.unpriced_models == ("openai/unknown-x",)
    assert report.unpriced_models == ("mistral/gpt-small", "openai/unknown-x")


def test_by_provider_is_a_lower_bound_when_a_model_is_unpriced(
    factory: sessionmaker[Session],
) -> None:
    groups = _by_key(_report(factory, by=UsageGrouping.PROVIDER))

    assert list(groups) == ["mistral", "openai"]
    openai = groups["openai"]
    assert openai.calls == 4
    assert openai.cost_usd == Decimal("3.006000")
    assert openai.cost_complete is False
    assert openai.unpriced_models == ("openai/unknown-x",)
    mistral = groups["mistral"]
    assert mistral.cost_usd == Decimal("0.003000")
    assert mistral.unpriced_models == ("mistral/gpt-small",)


def test_by_job(factory: sessionmaker[Session]) -> None:
    groups = _by_key(_report(factory, by=UsageGrouping.JOB))

    assert list(groups) == ["alpha", "beta"]  # a job without usage has no row
    alpha = groups["alpha"]
    assert (alpha.calls, alpha.input_tokens, alpha.output_tokens) == (3, 6_000, 1_500)
    assert alpha.cost_usd == Decimal("0.009000")
    assert alpha.cost_complete is True
    assert groups["beta"].cost_usd == Decimal("3.000000")


def test_by_day_buckets_by_utc_day(factory: sessionmaker[Session]) -> None:
    groups = _by_key(_report(factory, by=UsageGrouping.DAY))

    assert list(groups) == ["2026-10-01", "2026-10-02", "2026-10-03"]
    assert groups["2026-10-01"].calls == 2
    assert groups["2026-10-01"].cost_usd == Decimal("0.002000")  # gpt-small 1000/500
    assert groups["2026-10-02"].input_tokens == 5_000
    assert groups["2026-10-02"].cost_usd == Decimal("0.007000")
    assert groups["2026-10-03"].unpriced_models == ("openai/unknown-x",)


def test_by_day_uses_the_utc_day_of_an_offset_timestamp(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        job = make_job(session, "gamma")
        # 2026-10-04 01:00 +02:00 is 2026-10-03 23:00 UTC.
        created = datetime(2026, 10, 4, 1, 0, tzinfo=timezone(timedelta(hours=2)))
        make_llm_usage(session, job, input_tokens=7, output_tokens=0, created_at=created)

    groups = _by_key(_report(factory, by=UsageGrouping.DAY, job="gamma"))

    assert list(groups) == ["2026-10-03"]
    assert groups["2026-10-03"].input_tokens == 7


def test_by_day_key_needs_a_day() -> None:
    bucket = UsageBucket(
        job_name="alpha",
        provider="openai",
        model="gpt-small",
        day=None,
        calls=1,
        input_tokens=1,
        output_tokens=1,
    )

    with pytest.raises(ValueError, match="without a day"):
        _key(bucket, UsageGrouping.DAY)


def test_since_keeps_rows_from_that_day_on(factory: sessionmaker[Session]) -> None:
    report = _report(factory, by=UsageGrouping.DAY, since=date(2026, 10, 2))

    assert report.since == date(2026, 10, 2)
    assert [group.key for group in report.groups] == ["2026-10-02", "2026-10-03"]
    assert report.total.calls == 4


def test_job_filter(factory: sessionmaker[Session]) -> None:
    report = _report(factory, job="alpha")

    assert report.job == "alpha"
    assert report.total.calls == 3
    assert report.total.cost_complete is True
    assert report.unpriced_models == ()


def test_unknown_job(factory: sessionmaker[Session]) -> None:
    with pytest.raises(JobNotFoundError, match="job 'nope' not found"):
        _report(factory, job="nope")


def test_no_usage_is_an_empty_complete_report(factory: sessionmaker[Session]) -> None:
    report = _report(factory, job="idle")

    assert report.groups == ()
    assert report.total.calls == 0
    assert report.total.cost_usd == Decimal("0.000000")
    assert report.total.cost_complete is True


def test_only_unpriced_usage_has_no_cost(factory: sessionmaker[Session]) -> None:
    with session_scope(factory) as session:
        # Only the unpriced row of that day is left.
        session.execute(LlmUsage.__table__.delete().where(LlmUsage.model == "gpt-large"))
    report = _report(factory, by=UsageGrouping.JOB, since=date(2026, 10, 3), job="beta")

    assert report.total.cost_usd is None
    assert report.total.cost_complete is False
    assert report.groups[0].cost_usd is None


def test_repository_grouped_without_filters(db_session: Session) -> None:
    job = make_job(db_session, "solo")
    make_llm_usage(db_session, job, input_tokens=3, output_tokens=4)
    make_llm_usage(db_session, job, input_tokens=5, output_tokens=6)

    (bucket,) = UsageRepository(db_session).grouped()

    assert (bucket.job_name, bucket.provider, bucket.model, bucket.day) == (
        "solo",
        "openai",
        "gpt-small",
        None,
    )
    assert (bucket.calls, bucket.input_tokens, bucket.output_tokens) == (2, 8, 10)
