"""Per-run token budget: a tracker with a usage ledger, and the exception that stops a stage.

``BudgetTracker.record`` adds one provider call (input plus output tokens) and never raises, so
every call is counted, the digest call included. ``check`` is called before each per-item call
and raises :class:`BudgetExceeded` once ``used`` is *above* the limit (``>``, not ``>=``: a run
that lands exactly on the limit is within its budget). The first failing ``check`` latches
``exceeded``; the flag stays true for the rest of the run. It can be false while ``used`` is above
the limit: the last call may overshoot, and the digest call is never checked.

The ledger keeps one :class:`UsageEntry` per call, in call order. It is the source for replaying
the ``llm_usage`` rows after a rolled-back save, and the source of the run's token and cost totals
(research R1, R2). ``BudgetExceeded`` is deliberately not an ``LLMError``: it must never fail an
item, so it is not part of the per-item error handling.
"""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from invio.domain import COST_PRECISION

__all__ = ["BudgetExceeded", "BudgetTracker", "UsageEntry"]


@dataclass(frozen=True, slots=True, kw_only=True)
class UsageEntry:
    """Immutable copy of one ``llm_usage`` row (``cost_usd`` is ``None`` for an unpriced model)."""

    provider: str
    model: str
    purpose: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal | None
    created_at: datetime


class BudgetExceeded(Exception):
    """The run's token budget is used up; no further per-item call may start."""

    def __init__(self, used: int, limit: int) -> None:
        super().__init__(used, limit)  # args match __init__, so copy and pickle work
        self.used = used
        self.limit = limit

    def __str__(self) -> str:
        return f"token budget exceeded: {self.used} used, limit {self.limit}"


class BudgetTracker:
    """Token budget and usage ledger of one run (calls are sequential; not thread-safe)."""

    def __init__(self, limit: int) -> None:
        if limit < 1:
            raise ValueError(f"budget limit must be >= 1, got {limit}")
        self.limit = limit
        self._ledger: list[UsageEntry] = []
        self._exceeded = False

    @property
    def ledger(self) -> tuple[UsageEntry, ...]:
        """Every recorded call, in call order."""
        return tuple(self._ledger)

    @property
    def used(self) -> int:
        """Input plus output tokens of every recorded call."""
        return self.input_tokens + self.output_tokens

    @property
    def exceeded(self) -> bool:
        """Whether a ``check`` has failed (latched)."""
        return self._exceeded

    @property
    def input_tokens(self) -> int:
        """Input tokens of every recorded call."""
        return sum(entry.input_tokens for entry in self._ledger)

    @property
    def output_tokens(self) -> int:
        """Output tokens of every recorded call."""
        return sum(entry.output_tokens for entry in self._ledger)

    @property
    def calls(self) -> int:
        """Number of recorded calls."""
        return len(self._ledger)

    @property
    def cost_usd(self) -> Decimal:
        """Sum of the known costs (6 places); unpriced calls add nothing."""
        total = sum((e.cost_usd for e in self._ledger if e.cost_usd is not None), Decimal(0))
        return total.quantize(COST_PRECISION)

    @property
    def cost_complete(self) -> bool:
        """``False`` as soon as one call used a model without a registered price."""
        return all(entry.cost_usd is not None for entry in self._ledger)

    def check(self) -> None:
        """Raise :class:`BudgetExceeded` when ``used`` is above the limit; latch ``exceeded``."""
        if self.used > self.limit:
            self._exceeded = True
            raise BudgetExceeded(self.used, self.limit)

    def record(self, entry: UsageEntry) -> None:
        """Add one call to the ledger; never raises, whatever the budget."""
        self._ledger.append(entry)
