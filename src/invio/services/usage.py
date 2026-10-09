"""Usage and cost report: the read model behind ``invio usage`` (contract: specs/018-gh-issue-35).

``UsageService`` sums the ``llm_usage`` rows per group and prices the summed tokens per
(group, provider, model) with the model registry, so current prices apply (the stored
``cost_usd`` column is not read). A model the registry does not price for its provider is
*unpriced*: its tokens count, its cost does not, and the group's cost becomes a lower bound
(``cost_complete=False``), or ``None`` when no part of the group is priced. Only frozen records
leave the service.
"""

import contextlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from enum import StrEnum
from typing import Self, assert_never

from sqlalchemy.orm import Session, sessionmaker

from invio.config.settings import Settings, get_settings
from invio.db.repositories import JobRepository, UsageBucket, UsageRepository
from invio.db.session import checked_session_factory, session_scope
from invio.domain import COST_PRECISION
from invio.llm.base import LLMConfigError, Usage
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


def _key(bucket: UsageBucket, by: UsageGrouping) -> str:
    match by:
        case UsageGrouping.PROVIDER:
            return bucket.provider
        case UsageGrouping.MODEL:
            return f"{bucket.provider}/{bucket.model}"
        case UsageGrouping.JOB:
            return bucket.job_name
        case UsageGrouping.DAY:
            if bucket.day is None:
                raise ValueError("by-day report on a bucket without a day")
            return bucket.day.isoformat()
        case _:
            assert_never(by)


def _part(registry: ModelRegistry, provider: str, model: str, sums: list[int]) -> UsageTotal:
    """The summed usage of one (group, provider, model), priced if the registry knows the model.

    A model registered under another provider counts as unpriced, like an unknown one.
    """
    calls, given, produced = sums
    cost: Decimal | None = None
    with contextlib.suppress(LLMConfigError):
        registry.require(model, provider)
        cost = registry.cost(model, Usage(given, produced))
    return UsageTotal(
        calls=calls,
        input_tokens=given,
        output_tokens=produced,
        cost_usd=cost,
        cost_complete=cost is not None,
        unpriced_models=() if cost is not None else (f"{provider}/{model}",),
    )


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


class UsageService:
    """Report LLM usage and cost."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Self:
        """Build a service on the database from ``INVIO_DATABASE_URL``.

        Raises ``MissingSettingError`` when it is not set and ``DatabaseConfigError`` when unusable.
        """
        url = (settings or get_settings()).require_secret("database_url")
        return cls(checked_session_factory(url))

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
        parts: defaultdict[str, list[UsageTotal]] = defaultdict(list)
        for (key, provider, model), sums in tokens.items():
            parts[key].append(_part(registry, provider, model, sums))
        groups = tuple(UsageGroup(key=key, **asdict(_sum(parts[key]))) for key in sorted(parts))
        return UsageReport(by=by, job=job, since=since, groups=groups, total=_sum(groups))
