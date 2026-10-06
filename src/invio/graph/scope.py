"""``RunScope``: the mutable per-run holder shared by the node closures and ``run_job``.

It is never part of the LangGraph state. ``build_graph`` creates it and returns it with the
compiled graph; ``finalize`` closes its work session and sets ``finalized``. If ``ainvoke`` ends
with ``finalized`` still false, ``run_job`` rolls the session back, records the failure and
releases the lock (research R8). Defined here (and re-exported by ``invio.graph.build``) so
``invio.graph.stages`` can use it without importing the builder.
"""

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy.orm import Session

from invio.config.job import JobConfig
from invio.domain import ItemType
from invio.graph.budget import BudgetTracker
from invio.graph.nodes.synthesize import SynthesisResult
from invio.graph.ports import DeliveryReport, ProviderBinding
from invio.graph.state import RunStage

__all__ = ["RunScope"]


@dataclass(slots=True)
class RunScope:
    """Per-run state that must not travel through the graph state (sessions, tracker, locks)."""

    job_id: int
    run_id: int
    token: datetime  # the claimed ``locked_until``: ownership token for ``release``
    dry_run: bool
    job_name: str = ""
    config: JobConfig | None = None
    session: Session | None = None  # the run's single work session, opened lazily
    budget: BudgetTracker | None = None
    binding: ProviderBinding | None = None
    semaphore: asyncio.Semaphore = field(default_factory=asyncio.Semaphore)
    synthesis: SynthesisResult | None = None
    delivery: DeliveryReport | None = None
    failure: BaseException | None = None  # the original exception of the first fatal error
    finalized: bool = False
    item_types: dict[int, ItemType] = field(default_factory=dict)  # kind of every taken item
    item_stage: dict[int, RunStage] = field(default_factory=dict)  # last stage an item entered
