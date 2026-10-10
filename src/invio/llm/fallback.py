"""Fallback decorator for LLM providers.

``FallbackProvider`` sends a request to its primary provider and, when that provider is down or
rate-limited (:class:`~invio.llm.base.LLMUnavailableError`,
:class:`~invio.llm.base.LLMRateLimitError` including :class:`~invio.llm.base.LLMQuotaError`),
sends the same request to the fallback provider with the fallback model. Every other error
(credentials, a rejected request, an invalid structured answer, non-LLM errors) and every error of
the fallback propagates unchanged: a fallback must not hide a configuration problem.

Compose it around retrying providers, ``FallbackProvider(RetryingProvider(primary),
RetryingProvider(fallback))``, so the primary's transient errors are retried before falling
back. The provider and model that actually answered are stamped on the returned
:class:`~invio.llm.base.Usage` (and on ``LLMInvalidOutputError.usage``) so usage rows name and
price them. When the fallback fails too, ``llm.fallback_failed`` is logged and its error
propagates.

Each call is independent (no state), so concurrent calls are safe. The flip side: there is no
circuit breaker, so during a long outage every call runs the primary's full retry policy before
it reaches the fallback. Only ``invio.llm.base`` is imported; the module knows nothing of the
graph.
"""

import dataclasses
import logging
from collections.abc import Awaitable, Callable

from pydantic import BaseModel

from invio.llm.base import (
    LLMInvalidOutputError,
    LLMProvider,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
)

__all__ = ["FallbackProvider"]

logger = logging.getLogger("invio.llm")


def _is_fallback_error(err: BaseException) -> bool:
    """An outage or a rate limit (an exhausted quota too) of the primary provider."""
    return isinstance(err, LLMUnavailableError | LLMRateLimitError)


def _stamp(usage: Usage, provider: str, model: str) -> Usage:
    return dataclasses.replace(usage, provider=provider, model=model)


class FallbackProvider:
    """An :class:`~invio.llm.base.LLMProvider` that falls back to a second provider."""

    def __init__(
        self,
        primary: LLMProvider,
        fallback: LLMProvider,
        *,
        primary_name: str,
        fallback_name: str,
        fallback_model: str,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._primary_name = primary_name
        self._fallback_name = fallback_name
        self._fallback_model = fallback_model

    async def _call[R](
        self,
        model: str,
        send: Callable[[LLMProvider, str], Awaitable[tuple[R, Usage]]],
    ) -> tuple[R, Usage]:
        """Run ``send`` on the primary, on an outage or rate limit on the fallback."""
        try:
            result, usage = await self._attempt(self._primary, self._primary_name, model, send)
        except Exception as err:
            if not _is_fallback_error(err):
                raise
            logger.warning("llm.fallback", extra=self._log_fields(model, err))
            try:
                result, usage = await self._attempt(
                    self._fallback, self._fallback_name, self._fallback_model, send
                )
            except Exception as fallback_err:
                logger.warning("llm.fallback_failed", extra=self._log_fields(model, fallback_err))
                raise
        return result, usage

    def _log_fields(self, model: str, err: Exception) -> dict[str, str]:
        """Providers, models and the error class of a fallback; never the error message."""
        return {
            "provider": self._primary_name,
            "model": model,
            "fallback_provider": self._fallback_name,
            "fallback_model": self._fallback_model,
            "error": type(err).__name__,
        }

    @staticmethod
    async def _attempt[R](
        provider: LLMProvider,
        name: str,
        model: str,
        send: Callable[[LLMProvider, str], Awaitable[tuple[R, Usage]]],
    ) -> tuple[R, Usage]:
        """One call of ``provider``; its usage (also of an invalid answer) names it."""
        try:
            result, usage = await send(provider, model)
        except LLMInvalidOutputError as err:
            err.usage = _stamp(err.usage, name, model)
            raise
        return result, _stamp(usage, name, model)

    async def complete(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int
    ) -> tuple[str, Usage]:
        """Return the text answer of the primary or, after an outage, of the fallback."""
        return await self._call(
            model,
            lambda provider, chosen: provider.complete(
                system, user, model=chosen, temperature=temperature, max_tokens=max_tokens
            ),
        )

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        """Return the structured answer of the primary or, after an outage, of the fallback."""
        return await self._call(
            model,
            lambda provider, chosen: provider.complete_structured(
                system, user, schema, model=chosen, temperature=temperature
            ),
        )

    async def aclose(self) -> None:
        """Close both providers (the fallback also when closing the primary fails)."""
        try:
            await self._primary.aclose()
        finally:
            await self._fallback.aclose()
