"""``RunScope``: the mutable per-run holder shared by the node closures and ``run_job``.

It is never part of the LangGraph state. ``run_job`` creates it (``invio.graph.build.new_scope``)
before it builds the graph; ``finalize`` closes its work session and sets ``finalized``. If
building or invoking the graph ends with ``finalized`` still false, ``run_job`` rolls the session
back, records the failure and releases the lock (research R8). Defined here (and re-exported
by ``invio.graph.build``) so ``invio.graph.stages`` can use it without importing the builder.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final, Self

from sqlalchemy.orm import Session

from invio.config.job import JobConfig
from invio.domain import ItemType
from invio.graph.budget import BudgetTracker
from invio.graph.nodes.synthesize import SynthesisResult
from invio.graph.ports import DeliveryReport, ProgressCounts, ProviderBinding, RunObserver
from invio.graph.state import RunError, RunStage
from invio.sources.urls import redact_url, without_query
from invio.textsafe import strip_control

__all__ = ["MAX_TITLE_CHARS", "MAX_URL_CHARS", "ItemRef", "RunScope"]

# Caps of a stored item reference: up to 100 of them are kept in ``runs.stats["errors"]``.
MAX_TITLE_CHARS: Final = 300
MAX_URL_CHARS: Final = 500


@dataclass(frozen=True, slots=True)
class ItemRef:
    """Title and URL of a taken item, captured at deduplication (a dry run rolls the rows back).

    Both are untrusted: build it with :meth:`of`, which strips control characters from both,
    removes credentials, query string and fragment from the URL (they can hold signed tokens)
    and cuts both. Consumers need not sanitize again.
    """

    title: str
    url: str

    @classmethod
    def of(cls, title: str, url: str) -> Self:
        """The terminal-safe, bounded reference of an item with this raw title and URL."""
        return cls(
            title=strip_control(title, limit=MAX_TITLE_CHARS),
            url=strip_control(redact_url(without_query(url)), limit=MAX_URL_CHARS),
        )


@dataclass(slots=True)
class RunScope:
    """Per-run state that must not travel through the graph state (sessions, tracker, locks)."""

    job_id: int
    run_id: int
    token: datetime  # the claimed ``locked_until``: ownership token and run deadline
    dry_run: bool
    semaphore: asyncio.Semaphore  # bounds the in-flight source fetches and items
    job_name: str = ""
    config: JobConfig | None = None
    session: Session | None = None  # the run's single work session, opened lazily
    budget: BudgetTracker | None = None
    binding: ProviderBinding | None = None
    synthesis: SynthesisResult | None = None
    delivery: DeliveryReport | None = None
    failure: BaseException | None = None  # the original exception of the first fatal error
    fatal: RunError | None = None  # the recorded first fatal error; set together with ``failure``
    finalized: bool = False
    next_run_at: datetime | None = None  # written by ``release_lock`` (only when it released)
    retry_scheduled: bool = False  # ``next_run_at`` is a failure retry, not the regular slot
    stage: RunStage = "load_job"  # the run-level stage that runs now (items: ``extract_text``)
    errors_recorded: bool = False  # ``runs.stats["errors"]`` is written; the safety net keeps it
    item_types: dict[int, ItemType] = field(default_factory=dict)  # kind of every taken item
    item_stage: dict[int, RunStage] = field(default_factory=dict)  # last stage an item entered
    max_items: int | None = None  # per-run cap from ``--max-items``; applied in ``load_job``
    observer: RunObserver | None = None  # progress sink; dropped after its first failure
    progress: ProgressCounts = field(default_factory=ProgressCounts)
    item_refs: dict[int, ItemRef] = field(default_factory=dict)
