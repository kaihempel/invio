"""Usage and cost report: the read model behind ``invio usage`` (contract: specs/018-gh-issue-35).

``UsageService`` sums the ``llm_usage`` rows per group and prices the summed tokens per
(group, provider, model) with the model registry, so current prices apply (the stored
``cost_usd`` column is not read). A model the registry does not price for its provider is
*unpriced*: its tokens count, its cost does not, and the group's cost becomes a lower bound
(``cost_complete=False``), or ``None`` when no part of the group is priced. Only frozen records
leave the service.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Self

from sqlalchemy.orm import Session, sessionmaker

from invio.config.settings import Settings, get_settings
from invio.db.repositories import JobRepository, UsageBucket, UsageRepository
from invio.db.session import create_db_engine, session_factory, session_scope
from invio.domain import COST_PRECISION
from invio.llm.base import Usage
from invio.llm.registry import ModelRegistry
from invio.services.jobs import JobNotFoundError

__all__ = ["UsageGroup", "UsageGrouping", "UsageReport", "UsageService", "UsageTotal"]


class UsageGrouping(StrEnum):
    """What one report row stands for."""

    PROVIDER = "provider"
    MODEL = "model"
    JOB = "job"
    DAY = "day"


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageTotal:
    """Summed usage and cost; ``unpriced_models`` names each ``provider/model`` without price."""

    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    cost_complete: bool
    unpriced_models: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageGroup(UsageTotal):
    """One report row: ``key`` is a provider, ``provider/model``, job name or ``YYYY-MM-DD``."""

    key: str


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageReport:
    """The whole report: the groups ordered by key, their total and every unpriced model."""

    by: UsageGrouping
    job: str | None
    since: date | None
    groups: tuple[UsageGroup, ...]
    total: UsageTotal

    @property
    def unpriced_models(self) -> tuple[str, ...]:
        """Every ``provider/model`` without a price, sorted."""
        return self.total.unpriced_models


@dataclass(frozen=True, slots=True)
class _Part:
    """The summed usage of one (group, provider, model) and its price, ``None`` if unpriced."""

    model: str
    calls: int
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None


def _key(bucket: UsageBucket, by: UsageGrouping) -> str:
    match by:
        case UsageGrouping.PROVIDER:
            return bucket.provider
        case UsageGrouping.MODEL:
            return f"{bucket.provider}/{bucket.model}"
        case UsageGrouping.JOB:
            return bucket.job_name
        case _:  # UsageGrouping.DAY
            assert bucket.day is not None  # by-day buckets always carry their day
            return bucket.day.isoformat()


def _price(registry: ModelRegistry, provider: str, model: str, usage: Usage) -> Decimal | None:
    """The cost of ``usage``, or ``None`` if ``model`` is not registered for ``provider``."""
    info = registry.get(model)
    if info is None or info.provider != provider:
        return None
    return registry.cost(model, usage)


def _sum(parts: Iterable[UsageTotal]) -> UsageTotal:
    """Add up totals: a cost is the sum of the priced ones, ``None`` if none is priced."""
    items = list(parts)
    costs = [part.cost_usd for part in items if part.cost_usd is not None]
    unpriced = sorted({model for part in items for model in part.unpriced_models})
    return UsageTotal(
        calls=sum(part.calls for part in items),
        input_tokens=sum(part.input_tokens for part in items),
        output_tokens=sum(part.output_tokens for part in items),
        cost_usd=sum(costs, Decimal(0)).quantize(COST_PRECISION) if costs or not items else None,
        cost_complete=all(part.cost_complete for part in items),
        unpriced_models=tuple(unpriced),
    )


def _group(key: str, parts: Iterable[_Part]) -> UsageGroup:
    total = _sum(
        UsageTotal(
            calls=part.calls,
            input_tokens=part.input_tokens,
            output_tokens=part.output_tokens,
            cost_usd=part.cost_usd,
            cost_complete=part.cost_usd is not None,
            unpriced_models=() if part.cost_usd is not None else (part.model,),
        )
        for part in parts
    )
    return UsageGroup(
        key=key,
        calls=total.calls,
        input_tokens=total.input_tokens,
        output_tokens=total.output_tokens,
        cost_usd=total.cost_usd,
        cost_complete=total.cost_complete,
        unpriced_models=total.unpriced_models,
    )


class UsageService:
    """Report LLM usage and cost."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Self:
        """Build a service on the database from ``INVIO_DATABASE_URL`` (``MissingSettingError``)."""
        url = (settings or get_settings()).require_secret("database_url")
        return cls(session_factory(create_db_engine(url)))

    def report(
        self,
        registry: ModelRegistry,
        *,
        by: UsageGrouping = UsageGrouping.MODEL,
        job: str | None = None,
        since: date | None = None,
    ) -> UsageReport:
        """Sum the usage per ``by`` group, of one ``job`` and from ``since`` 00:00 UTC on.

        An unknown ``job`` raises ``JobNotFoundError``.
        """
        with session_scope(self._session_factory) as session:
            job_id: int | None = None
            if job is not None:
                row = JobRepository(session).get_by_name(job)
                if row is None:
                    raise JobNotFoundError(job)
                job_id = row.id
            start = datetime.combine(since, time.min, tzinfo=UTC) if since is not None else None
            buckets = UsageRepository(session).grouped(
                job_id=job_id, since=start, by_day=by is UsageGrouping.DAY
            )
        tokens: defaultdict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
        for bucket in buckets:
            sums = tokens[_key(bucket, by), bucket.provider, bucket.model]
            sums[0] += bucket.calls
            sums[1] += bucket.input_tokens
            sums[2] += bucket.output_tokens
        parts: defaultdict[str, list[_Part]] = defaultdict(list)
        for (key, provider, model), (calls, given, produced) in tokens.items():
            cost = _price(registry, provider, model, Usage(given, produced))
            parts[key].append(_Part(f"{provider}/{model}", calls, given, produced, cost))
        groups = tuple(_group(key, parts[key]) for key in sorted(parts))
        return UsageReport(by=by, job=job, since=since, groups=groups, total=_sum(groups))
