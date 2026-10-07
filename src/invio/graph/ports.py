"""Ports of the research graph: what the composition root injects into ``invio.graph``.

``invio.graph`` must not import ``invio.notify`` or ``invio.scheduling``; notification, next-run
computation, source and page fetching and provider binding arrive through :class:`RunDeps`
(research R2, R3). The production wiring lives in ``invio.pipeline.deps``; tests pass fakes.
"""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from sqlalchemy.orm import Session, sessionmaker

from invio.config.job import LLMConfig, ScheduleConfig, SourceConfig
from invio.domain import Candidate
from invio.graph.state import RunStage
from invio.llm.base import LLMProvider
from invio.llm.registry import ModelRegistry
from invio.retry import RetrySettings, Sleep

__all__ = [
    "DeliveryReport",
    "Notifier",
    "PageFetcher",
    "ProgressCounts",
    "ProgressEvent",
    "ProgressSnapshot",
    "ProviderBinding",
    "RunDeps",
    "RunObserver",
    "SourceFetcher",
]


class SourceFetcher(Protocol):
    """Fetch one configured source; raises ``FetchError`` when it fails."""

    async def __call__(self, config: SourceConfig) -> list[Candidate]: ...


class PageFetcher(Protocol):
    """Fetch the HTML of an item page; raises ``FetchError`` when it fails."""

    async def __call__(self, url: str) -> str: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class DeliveryReport:
    """The outcome of notifying one digest."""

    sent: int
    failed: int
    error: str | None = None


class Notifier(Protocol):
    """Deliver the stored digest ``digest_id``."""

    async def __call__(self, digest_id: int) -> DeliveryReport: ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderBinding:
    """A provider with the model ids and registry a run uses with it."""

    provider: LLMProvider
    provider_name: str
    fast_model: str
    smart_model: str
    registry: ModelRegistry


@dataclass(frozen=True, slots=True, kw_only=True)
class RunDeps:
    """Everything a run needs from the outside world."""

    session_factory: sessionmaker[Session]
    fetch_source: SourceFetcher
    fetch_page: PageFetcher
    provider_for: Callable[[LLMConfig], ProviderBinding]
    notify: Notifier
    next_run: Callable[[ScheduleConfig, datetime], datetime]
    clock: Callable[[], datetime]
    retry_delay: Callable[[int], timedelta]  # delay after the n-th consecutive failed run
    concurrency: int = 4
    retry: RetrySettings = field(default_factory=RetrySettings)
    lock_ttl: timedelta = timedelta(hours=2)
    sleep: Sleep = asyncio.sleep

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError(f"concurrency must be >= 1, got {self.concurrency}")
        if self.lock_ttl <= timedelta(0):
            raise ValueError("lock_ttl must be > 0")


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:
    """The cumulative counts of a run at one moment; what a :class:`ProgressEvent` carries."""

    found: int = 0
    new: int = 0
    after_keyword_filter: int = 0
    selected: int = 0
    processed: int = 0
    relevant: int = 0
    failed: int = 0


@dataclass(slots=True)
class ProgressCounts:
    """The mutable cumulative counts the stages update (on the event-loop thread only)."""

    found: int = 0
    new: int = 0
    after_keyword_filter: int = 0
    selected: int = 0
    processed: int = 0
    relevant: int = 0
    failed: int = 0

    def snapshot(self) -> ProgressSnapshot:
        """A frozen copy; later updates do not change it."""
        return ProgressSnapshot(
            found=self.found,
            new=self.new,
            after_keyword_filter=self.after_keyword_filter,
            selected=self.selected,
            processed=self.processed,
            relevant=self.relevant,
            failed=self.failed,
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class ProgressEvent:
    """One progress report: a stage that completed, or an item that reached an outcome.

    ``title`` is the (sanitized) item title and ``message`` the sanitized failure text; both
    are set on ``item`` events only.
    """

    kind: Literal["stage", "item"]
    stage: RunStage
    counts: ProgressSnapshot
    item_id: int | None = None
    title: str | None = None
    outcome: Literal["relevant", "irrelevant", "failed"] | None = None
    message: str | None = None


class RunObserver(Protocol):
    """Receives progress events; must be quick. Its exceptions never fail a run."""

    def __call__(self, event: ProgressEvent) -> None: ...
