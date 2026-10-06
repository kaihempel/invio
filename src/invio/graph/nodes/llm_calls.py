"""Shared plumbing for LLM nodes: one structured or free-text call with usage, and failure text.

Every node call records one ``llm_usage`` row, also when the answer stays invalid (the model
was called, so the tokens were spent). Failure text for ``items.last_error`` is built from the
error class and its structured fields only: provider messages can quote the request (and so
document text), and validation errors can carry key names chosen by the model.
"""

from typing import Final, Protocol

from pydantic import BaseModel

from invio.db.repositories import UsageRepository
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
    "call_text",
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


def _record_usage(ctx: CallContext, model: str, purpose: str, usage: Usage) -> None:
    """Store one ``llm_usage`` row for a call (a repair request is already summed in)."""
    ctx.usage.add(
        ctx.job_id,
        ctx.provider_name,
        model,
        usage.input_tokens,
        usage.output_tokens,
        run_id=ctx.run_id,
        purpose=purpose,
        cost_usd=ctx.registry.cost(model, usage),
    )


async def call_structured[T: BaseModel](
    ctx: CallContext, schema: type[T], *, model: str, purpose: str, system: str, user: str
) -> T:
    """Run one structured call and record its usage, also when the answer stays invalid."""
    try:
        result, usage = await ctx.provider.complete_structured(
            system, user, schema, model=model, temperature=0.0
        )
    except LLMInvalidOutputError as err:
        _record_usage(ctx, model, purpose, err.usage)
        raise
    _record_usage(ctx, model, purpose, usage)
    return result


async def call_text(
    ctx: CallContext, *, model: str, purpose: str, system: str, user: str, max_tokens: int
) -> str:
    """Run one free-text call and record its usage.

    A failed request raises and records nothing: the provider reports no usage for it.
    """
    text, usage = await ctx.provider.complete(
        system, user, model=model, temperature=0.0, max_tokens=max_tokens
    )
    _record_usage(ctx, model, purpose, usage)
    return text


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
