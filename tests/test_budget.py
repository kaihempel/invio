"""Tests for the per-run token budget tracker (arithmetic, check/latch, cost completeness)."""

import copy
import pickle
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from invio.graph.budget import BudgetExceeded, BudgetTracker, UsageEntry


def _entry(
    input_tokens: int = 10,
    output_tokens: int = 5,
    cost: str | None = "0.000100",
    purpose: str = "relevance",
) -> UsageEntry:
    return UsageEntry(
        provider="mistral",
        model="fast-model",
        purpose=purpose,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=Decimal(cost) if cost is not None else None,
        created_at=datetime(2026, 10, 6, tzinfo=UTC),
    )


@pytest.mark.parametrize("limit", [0, -1])
def test_limit_below_one_is_rejected(limit: int) -> None:
    with pytest.raises(ValueError, match="limit"):
        BudgetTracker(limit)


def test_new_tracker_is_empty() -> None:
    tracker = BudgetTracker(100)
    assert (tracker.limit, tracker.used, tracker.calls) == (100, 0, 0)
    assert (tracker.input_tokens, tracker.output_tokens) == (0, 0)
    assert tracker.ledger == ()
    assert tracker.exceeded is False
    assert tracker.cost_usd == Decimal("0.000000")
    assert tracker.cost_complete is True


def test_record_sums_tokens_and_keeps_call_order() -> None:
    tracker = BudgetTracker(1000)
    first, second = _entry(10, 5, purpose="a"), _entry(20, 1, purpose="b")
    tracker.record(first)
    tracker.record(second)
    assert tracker.used == 36
    assert (tracker.input_tokens, tracker.output_tokens, tracker.calls) == (30, 6, 2)
    assert tracker.ledger == (first, second)
    assert isinstance(tracker.ledger, tuple)


def test_check_passes_up_to_and_including_the_limit() -> None:
    tracker = BudgetTracker(15)
    tracker.check()
    tracker.record(_entry(10, 5))
    tracker.check()  # used == limit is still within the budget
    assert tracker.exceeded is False


def test_check_raises_above_the_limit_and_latches() -> None:
    tracker = BudgetTracker(15)
    tracker.record(_entry(10, 6))
    with pytest.raises(BudgetExceeded) as caught:
        tracker.check()
    assert (caught.value.used, caught.value.limit) == (16, 15)
    assert tracker.exceeded is True
    with pytest.raises(BudgetExceeded):
        tracker.check()
    assert tracker.exceeded is True


def test_exceeded_stays_true_without_further_checks() -> None:
    tracker = BudgetTracker(1)
    tracker.record(_entry(5, 5))
    with pytest.raises(BudgetExceeded):
        tracker.check()
    tracker.record(_entry(1, 1))
    assert tracker.exceeded is True


def test_exceeded_is_false_until_a_check_fails() -> None:
    tracker = BudgetTracker(1)
    tracker.record(_entry(5, 5))
    assert tracker.used > tracker.limit
    assert tracker.exceeded is False


def test_record_never_raises_above_the_limit() -> None:
    tracker = BudgetTracker(1)
    tracker.record(_entry(100, 100))
    tracker.record(_entry(100, 100))
    assert tracker.used == 400


def test_budget_exceeded_is_a_plain_exception() -> None:
    from invio.llm.base import LLMError

    assert issubclass(BudgetExceeded, Exception)
    assert not issubclass(BudgetExceeded, LLMError)


def test_cost_sums_known_costs_quantized() -> None:
    tracker = BudgetTracker(1000)
    tracker.record(_entry(cost="0.0000004"))
    tracker.record(_entry(cost="0.000002"))
    assert tracker.cost_usd == Decimal("0.000002")
    assert tracker.cost_usd.as_tuple().exponent == -6
    assert tracker.cost_complete is True


def test_unknown_cost_makes_the_total_incomplete() -> None:
    tracker = BudgetTracker(1000)
    tracker.record(_entry(cost="0.5"))
    tracker.record(_entry(cost=None))
    assert tracker.cost_complete is False
    assert tracker.cost_usd == Decimal("0.500000")


def test_budget_exceeded_args_and_message() -> None:
    error = BudgetExceeded(1200, 1000)
    assert error.args == (1200, 1000)
    assert (error.used, error.limit) == (1200, 1000)
    assert str(error) == "token budget exceeded: 1200 used, limit 1000"


@pytest.mark.parametrize(
    "clone",
    [copy.copy, copy.deepcopy, lambda e: pickle.loads(pickle.dumps(e))],
    ids=["copy", "deepcopy", "pickle"],
)
def test_budget_exceeded_survives_copy_and_pickle(
    clone: Callable[[BudgetExceeded], BudgetExceeded],
) -> None:
    error = BudgetExceeded(16, 15)
    cloned = clone(error)
    assert type(cloned) is BudgetExceeded
    assert (cloned.used, cloned.limit, cloned.args) == (16, 15, (16, 15))
    assert str(cloned) == str(error)


def test_raised_budget_exceeded_round_trips_through_pickle() -> None:
    tracker = BudgetTracker(15)
    tracker.record(_entry(10, 6))
    with pytest.raises(BudgetExceeded) as caught:
        tracker.check()
    restored = pickle.loads(pickle.dumps(caught.value))
    assert (restored.used, restored.limit) == (16, 15)
