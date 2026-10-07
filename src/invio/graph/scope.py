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

from sqlalchemy.orm import Session

from invio.config.job import JobConfig
from invio.domain import ItemType
from invio.graph.budget import BudgetTracker
from invio.graph.nodes.synthesize import SynthesisResult
from invio.graph.ports import DeliveryReport, ProgressCounts, ProviderBinding, RunObserver
from invio.graph.state import RunStage

__all__ = ["ItemRef", "RunScope"]


@dataclass(frozen=True, slots=True)
class ItemRef:
    """Title and URL of a taken item, captured at deduplication (a dry run rolls the rows back).

    Both are untrusted: the title has control characters stripped and is cut, the URL has no
    query string or fragment (they can hold signed tokens).
    """

    title: str
    url: str


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
    finalized: bool = False
    item_types: dict[int, ItemType] = field(default_factory=dict)  # kind of every taken item
    item_stage: dict[int, RunStage] = field(default_factory=dict)  # last stage an item entered
    max_items: int | None = None  # per-run cap from ``--max-items``; applied in ``load_job``
    observer: RunObserver | None = None  # progress sink; dropped after its first failure
    progress: ProgressCounts = field(default_factory=ProgressCounts)
    item_refs: dict[int, ItemRef] = field(default_factory=dict)
