"""Per-origin rate limiting for the safe HTTP client.

For every origin at most one request is in flight, and request starts are spaced by the
effective interval. Different origins never wait for each other. There are no background
tasks: a caller that has to wait simply sleeps inside :meth:`HostRateLimiter.slot`.
"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final

from invio.sources.netguard import Origin

__all__ = ["CRAWL_DELAY_CAP", "HostRateLimiter", "effective_interval"]

CRAWL_DELAY_CAP: Final = 30.0


def effective_interval(configured: float, crawl_delay: float | None) -> float:
    """The spacing to use: ``Crawl-delay`` can only raise the configured interval, up to 30 s.

    A configured interval is never capped; only the part that comes from the site's own
    ``Crawl-delay`` is, so a hostile robots.txt cannot stall a run for long.
    """
    return max(configured, min(crawl_delay or 0.0, CRAWL_DELAY_CAP))


@dataclass
class _OriginState:
    # asyncio.Lock wakes waiters in FIFO order, and a cancelled waiter just drops out.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_start: float | None = None


class HostRateLimiter:
    """Hands out one slot at a time per origin, spaced by the interval of the caller."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        self._states: dict[Origin, _OriginState] = {}

    @asynccontextmanager
    async def slot(self, origin: Origin, interval: float) -> AsyncIterator[None]:
        """Hold the origin's slot; waits until ``interval`` has passed since the last start."""
        # No await between lookup and insert, so concurrent callers share one state.
        state = self._states.setdefault(origin, _OriginState())
        async with state.lock:
            if state.last_start is not None:
                wait = state.last_start + interval - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            state.last_start = self._clock()
            yield
