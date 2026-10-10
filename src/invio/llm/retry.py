"""Retry decorator for LLM providers.

``RetryingProvider`` wraps any :class:`~invio.llm.base.LLMProvider` and retries a request that
failed with a transient error (rate limit, provider unavailable) with exponential backoff.
Retrying the single request, not re-running the node that made it, keeps the nodes' contracts:
a node counts a request once it succeeded, writes its usage row once and never repeats earlier
chunk calls of a summary (spec 014, research R4).

Permanent errors (credentials, a rejected request, an invalid structured answer, which the
inner provider already repaired once) are raised after one attempt, and so is a rate limit
whose ``retry_after`` is longer than the policy's ``max_interval``. A
:class:`~invio.llm.fallback.FallbackProvider` wraps retrying providers, so a primary provider is
retried before the fallback is asked. The module only depends on ``invio.retry`` and
``invio.llm.base``; it knows nothing of the graph.
"""

from pydantic import BaseModel

from invio.llm.base import (
    LLMProvider,
    LLMQuotaError,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
)
from invio.retry import RetrySettings, Sleep, retrying

__all__ = ["RetryingProvider", "is_transient_llm", "llm_retry_after"]


def is_transient_llm(err: BaseException) -> bool:
    """Rate limits and provider outages are worth another try; everything else is not.

    An exhausted quota is a rate-limit error too, but waiting never resolves it.
    """
    if isinstance(err, LLMQuotaError):
        return False
    return isinstance(err, LLMRateLimitError | LLMUnavailableError)


def llm_retry_after(err: BaseException) -> float | None:
    """The wait a rate-limited provider asked for, if it named one."""
    return err.retry_after if isinstance(err, LLMRateLimitError) else None


class RetryingProvider:
    """An :class:`~invio.llm.base.LLMProvider` that retries transient failures of ``inner``."""

    def __init__(self, inner: LLMProvider, *, policy: RetrySettings, sleep: Sleep) -> None:
        self._inner = inner
        self._policy = policy
        self._sleep = sleep

    async def complete(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int
    ) -> tuple[str, Usage]:
        """Return the model's text answer, retrying transient errors."""
        return await retrying(
            lambda: self._inner.complete(
                system, user, model=model, temperature=temperature, max_tokens=max_tokens
            ),
            policy=self._policy,
            retry_on=is_transient_llm,
            what="llm",
            retry_after=llm_retry_after,
            sleep=self._sleep,
        )

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        """Return a validated structured answer, retrying transient errors."""
        return await retrying(
            lambda: self._inner.complete_structured(
                system, user, schema, model=model, temperature=temperature
            ),
            policy=self._policy,
            retry_on=is_transient_llm,
            what="llm",
            retry_after=llm_retry_after,
            sleep=self._sleep,
        )

    async def aclose(self) -> None:
        """Close the inner provider."""
        await self._inner.aclose()
