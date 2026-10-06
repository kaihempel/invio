"""Call-level retry with exponential backoff (leaf module: imports nothing from ``invio``).

Transient failures are retried around the *single external call* (source fetch, item page
fetch, provider request), not by re-running a graph node: the nodes catch these errors
themselves and a node re-run would repeat side effects (spec 014, research R4).

The policy field names mirror LangGraph's ``RetryPolicy`` so the issue's vocabulary carries
over without making ``invio.llm`` import LangGraph. Which errors are transient, and how long a
server asks to wait, is decided by the caller: ``invio.llm.retry`` and ``invio.sources.errors``
hold those predicates next to the error types they classify.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

__all__ = ["RetryAfter", "RetrySettings", "Sleep", "retrying"]

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
RetryAfter = Callable[[BaseException], float | None]  # the wait a server asked for, if any

_JITTER_FRACTION = 0.1


@dataclass(frozen=True)
class RetrySettings:
    """Backoff: wait ``initial_interval * backoff_factor**(n-1)``, at most ``max_interval``."""

    initial_interval: float = 1.0
    backoff_factor: float = 2.0
    max_interval: float = 30.0
    max_attempts: int = 3
    jitter: bool = True

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if self.initial_interval <= 0:
            raise ValueError("initial_interval must be > 0")
        if self.backoff_factor < 1:
            raise ValueError("backoff_factor must be >= 1")
        if self.max_interval < self.initial_interval:
            raise ValueError("max_interval must be >= initial_interval")


def _wait_before_retry(
    policy: RetrySettings, attempt: int, requested: float | None, rand: Callable[[], float]
) -> float | None:
    """The wait before the next attempt, or ``None`` when retrying is pointless.

    A server that asks for a longer wait than ``max_interval`` would only answer the early
    retry with the same error again, so the call gives up instead of burning its attempts.
    """
    if requested is not None and requested > policy.max_interval:
        return None
    base = policy.initial_interval * policy.backoff_factor ** (attempt - 1)
    wait = min(policy.max_interval, max(base, requested or 0.0))
    if policy.jitter:
        wait = min(policy.max_interval, wait * (1 + _JITTER_FRACTION * rand()))
    return wait


async def retrying[T](
    call: Callable[[], Awaitable[T]],
    *,
    policy: RetrySettings,
    retry_on: Callable[[BaseException], bool],
    what: str,
    retry_after: RetryAfter | None = None,
    sleep: Sleep = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Await ``call()``, retrying while ``retry_on(err)`` holds and attempts remain.

    ``retry_after(err)`` is the wait the server asked for: it is used when it is longer than the
    computed backoff, and the error is re-raised at once when it exceeds ``max_interval``.
    The last error is re-raised unchanged. Each retry logs ``retry.attempt`` with the error
    class only, never its message.
    """
    attempt = 1
    while True:
        try:
            return await call()
        except Exception as err:
            if attempt >= policy.max_attempts or not retry_on(err):
                raise
            requested = retry_after(err) if retry_after is not None else None
            wait = _wait_before_retry(policy, attempt, requested, rand)
            if wait is None:
                logger.info(
                    "retry.gave_up",
                    extra={"what": what, "attempt": attempt, "error": type(err).__name__},
                )
                raise
            logger.info(
                "retry.attempt",
                extra={
                    "what": what,
                    "attempt": attempt,
                    "wait_s": wait,
                    "error": type(err).__name__,
                },
            )
            await sleep(wait)
            attempt += 1
