"""Shared plumbing for LLM nodes: one structured call with usage recording, and failure text.

Every node call records one ``llm_usage`` row, also when the answer stays invalid (the model
was called, so the tokens were spent). Failure text for ``items.last_error`` is built from the
error class and its structured fields only: provider messages can quote the request (and so
document text), and validation errors can carry key names chosen by the model.

Every call is also recorded on the run's :class:`~invio.graph.budget.BudgetTracker`, whose ledger
equals the rows written. A per-item call first checks the budget and raises ``BudgetExceeded``
when it is used up (no provider call, no row); the digest call passes ``per_item=False`` so it is
counted but never blocked. ``BudgetExceeded`` is not an ``LLMError`` and not in
``PER_ITEM_ERRORS``: it never fails an item, only the batch loops catch it.
"""

from typing import Final, Protocol

from pydantic import BaseModel

from invio.db.repositories import UsageRepository
from invio.db.types import utcnow
from invio.graph.budget import BudgetTracker, UsageEntry
from invio.llm.base import (
    LLMError,
    LLMInvalidOutputError,
    LLMInvalidRequestError,
    LLMProvider,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
)
from invio.llm.registry import ModelRegistry

__all__ = [
    "INVALID_OUTPUT_MESSAGE",
    "PER_ITEM_ERRORS",
    "CallContext",
    "call_structured",
    "failure_message",
]

# LLM failures that fail one item; credential and configuration errors propagate instead.
PER_ITEM_ERRORS: Final = (
    LLMInvalidOutputError,
    LLMUnavailableError,
    LLMRateLimitError,
    LLMInvalidRequestError,
)

INVALID_OUTPUT_MESSAGE: Final = "invalid structured answer after repair"


class CallContext(Protocol):
    """The parts of a node context a provider call needs."""

    @property
    def job_id(self) -> int: ...
    @property
    def run_id(self) -> int | None: ...
    @property
    def provider(self) -> LLMProvider: ...
    @property
    def provider_name(self) -> str: ...
    @property
    def registry(self) -> ModelRegistry: ...
    @property
    def usage(self) -> UsageRepository: ...
    @property
    def budget(self) -> BudgetTracker: ...


def _record_usage(ctx: CallContext, model: str, purpose: str, usage: Usage) -> None:
    """Count one call on the budget and store its ``llm_usage`` row (a repair is summed in).

    The tracker comes first: if the flush fails, the tokens are still in the ledger for the
    replay after the rollback.
    """
    entry = UsageEntry(
        provider=ctx.provider_name,
        model=model,
        purpose=purpose,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cost_usd=ctx.registry.cost(model, usage),
        created_at=utcnow(),
    )
    ctx.budget.record(entry)
    ctx.usage.add(
        ctx.job_id,
        entry.provider,
        entry.model,
        entry.input_tokens,
        entry.output_tokens,
        run_id=ctx.run_id,
        purpose=entry.purpose,
        cost_usd=entry.cost_usd,
        created_at=entry.created_at,
    )


async def call_structured[T: BaseModel](
    ctx: CallContext,
    schema: type[T],
    *,
    model: str,
    purpose: str,
    system: str,
    user: str,
    per_item: bool = True,
) -> T:
    """Run one structured call and record its usage, also when the answer stays invalid.

    A per-item call (the default) raises ``BudgetExceeded`` before the provider is called when
    the run's budget is used up. ``per_item=False`` (the digest call) skips that check; its
    usage is still counted.
    """
    if per_item:
        ctx.budget.check()
    try:
        result, usage = await ctx.provider.complete_structured(
            system, user, schema, model=model, temperature=0.0
        )
    except LLMInvalidOutputError as err:
        _record_usage(ctx, model, purpose, err.usage)
        raise
    _record_usage(ctx, model, purpose, usage)
    return result


def failure_message(err: Exception) -> str:
    """Return ``"<ErrorClass>: <message>"`` for ``last_error``; never request or document text.

    An invalid answer gets a fixed message. Other LLM errors are described by their provider,
    model and HTTP status only; the provider's own message is left out because it can echo the
    request.
    """
    name = type(err).__name__
    if isinstance(err, LLMInvalidOutputError):
        return f"{name}: {INVALID_OUTPUT_MESSAGE}"
    if not isinstance(err, LLMError):
        return f"{name}: {err}"
    facts = [f"provider {err.provider or 'unknown'}", f"model {err.model or 'unknown'}"]
    if isinstance(err, LLMInvalidRequestError) and err.status is not None:
        facts.append(f"HTTP {err.status}")
    if isinstance(err, LLMRateLimitError) and err.retry_after is not None:
        facts.append(f"retry after {err.retry_after:g} s")
    return f"{name}: call failed ({', '.join(facts)})"
