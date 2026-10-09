"""One SDK client per running event loop, shared by the HTTP-based LLM providers.

An SDK client (and so its HTTP connection pool) is bound to the event loop that uses it, so a
provider keeps one client per running loop, built lazily on its first use there.
:meth:`LoopClients.aclose` closes the client of the running loop; clients of loops that were
closed meanwhile cannot be closed any more and are dropped on the next build. The table is
guarded by a lock, so threads running their own loops may share one provider.
"""

import asyncio
import threading
from collections.abc import Callable
from typing import Protocol


class AsyncCloseable(Protocol):
    async def aclose(self) -> None: ...


class LoopClients[C]:
    """Lazily built ``(sdk_client, http_client)`` pairs keyed by the running event loop."""

    def __init__(self, build: Callable[[], tuple[C, AsyncCloseable]]) -> None:
        self._build = build
        self.entries: dict[asyncio.AbstractEventLoop, tuple[C, AsyncCloseable]] = {}
        self._lock = threading.Lock()

    def get(self) -> C:
        """Return the SDK client of the running loop, building it on its first use."""
        loop = asyncio.get_running_loop()
        with self._lock:
            entry = self.entries.get(loop)
            if entry is None:
                # A closed loop's pool cannot be closed any more: drop it (sockets are released
                # on garbage collection). Clients of other live loops stay untouched.
                for closed in [other for other in self.entries if other.is_closed()]:
                    del self.entries[closed]
                entry = self._build()
                self.entries[loop] = entry
        return entry[0]

    async def aclose(self) -> None:
        """Close the HTTP client of the running loop; the next call builds a new one."""
        loop = asyncio.get_running_loop()
        with self._lock:
            entry = self.entries.pop(loop, None)
        if entry is not None:
            await entry[1].aclose()
