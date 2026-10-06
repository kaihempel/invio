"""Call-level retry with exponential backoff (dependency-free leaf module).

Transient failures are retried around the *single external call* (source fetch, item page
fetch, provider request), not by re-running a graph node: the nodes catch these errors
themselves and a node re-run would repeat side effects (spec 014, research R4).

The policy field names mirror LangGraph's ``RetryPolicy`` so the issue's vocabulary carries
over without making ``invio.llm`` import LangGraph. This module imports nothing from ``invio``
except the error types the predicates classify.
"""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from invio.llm.base import LLMRateLimitError, LLMUnavailableError
from invio.sources.errors import (
    BlockedError,
    FetchError,
    RenderUnavailableError,
    TooLargeError,
)

__all__ = [
    "RetrySettings",
    "Sleep",
    "is_transient_fetch",
    "is_transient_llm",
    "retrying",
]

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]

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


def is_transient_llm(err: BaseException) -> bool:
    """Rate limits and provider outages are worth another try; everything else is not."""
    return isinstance(err, LLMRateLimitError | LLMUnavailableError)


_TRANSIENT_FETCH_REASONS = frozenset(
    {"timeout", "connection_failed", "dns_failed", "invalid_response", "render_failed"}
)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 425, 429})


def is_transient_fetch(err: BaseException) -> bool:
    """Allow-list: only network trouble and retryable HTTP statuses are worth another try.

    Everything else (4xx other than 408/425/429, invalid URL, not HTML, malformed feed, missing
    selector, too many redirects, policy refusals, size limits, an unknown reason) is permanent.
    """
    if not isinstance(err, FetchError) or isinstance(
        err, BlockedError | TooLargeError | RenderUnavailableError
    ):
        return False
    if err.reason == "http_status":
        status = err.status
        return status is not None and (status in _TRANSIENT_HTTP_STATUSES or 500 <= status <= 599)
    return err.reason in _TRANSIENT_FETCH_REASONS


def _wait_before_retry(
    policy: RetrySettings, attempt: int, err: BaseException, rand: Callable[[], float]
) -> float:
    base = policy.initial_interval * policy.backoff_factor ** (attempt - 1)
    retry_after = 0.0
    if isinstance(err, LLMRateLimitError) and err.retry_after is not None:
        retry_after = err.retry_after
    wait = min(policy.max_interval, max(base, retry_after))
    if policy.jitter:
        wait = min(policy.max_interval, wait * (1 + _JITTER_FRACTION * rand()))
    return wait


async def retrying[T](
    call: Callable[[], Awaitable[T]],
    *,
    policy: RetrySettings,
    retry_on: Callable[[BaseException], bool],
    what: str,
    sleep: Sleep = asyncio.sleep,
    rand: Callable[[], float] = random.random,
) -> T:
    """Await ``call()``, retrying while ``retry_on(err)`` holds and attempts remain.

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
            wait = _wait_before_retry(policy, attempt, err, rand)
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
