"""robots.txt policy cache for the safe HTTP client.

One robots.txt is fetched per origin and the outcome is kept for the client's lifetime:

- 2xx: the first 500 KB are parsed;
- 4xx: no robots.txt, everything is allowed;
- 5xx or any failure (network, timeout, blocked target, too many redirects): everything is
  disallowed, which is the cautious reading of "could not find out".

The fetch itself is injected, so the cache knows nothing about HTTP and the client can run the
robots request through its normal guarded pipeline.
"""

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, Literal
from urllib.robotparser import RobotFileParser

import httpx

from invio.sources.errors import FetchError
from invio.sources.netguard import Origin

__all__ = ["ROBOTS_AGENT_TOKEN", "ROBOTS_MAX_BYTES", "RobotsCache", "RobotsPolicy"]

ROBOTS_AGENT_TOKEN: Final = "invio"
ROBOTS_MAX_BYTES: Final = 512_000
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}

RobotsFetch = Callable[[httpx.URL], Awaitable[tuple[int, bytes]]]


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """What robots.txt allows on one origin."""

    mode: Literal["parsed", "allow_all", "disallow_all"]
    parser: RobotFileParser | None = None
    crawl_delay: float | None = None

    def allows(self, url: str) -> bool:
        """True if the ``invio`` agent may fetch ``url``."""
        if self.mode == "allow_all":
            return True
        if self.parser is None:
            return False
        return self.parser.can_fetch(ROBOTS_AGENT_TOKEN, url)


class RobotsCache:
    """Per-origin robots.txt policies, fetched lazily and at most once."""

    def __init__(self, fetch: RobotsFetch) -> None:
        self._fetch = fetch
        self._policies: dict[Origin, RobotsPolicy] = {}
        self._locks: dict[Origin, asyncio.Lock] = {}

    def known(self, origin: Origin) -> RobotsPolicy | None:
        """The policy if it has already been fetched; never triggers a fetch."""
        return self._policies.get(origin)

    async def policy(self, origin: Origin) -> RobotsPolicy:
        """Return the policy of ``origin``; concurrent first callers share one fetch."""
        cached = self._policies.get(origin)
        if cached is not None:
            return cached
        # setdefault runs without an await in between, so every caller gets the same lock.
        async with self._locks.setdefault(origin, asyncio.Lock()):
            cached = self._policies.get(origin)
            if cached is None:
                cached = await self._load(origin)
                self._policies[origin] = cached
            return cached

    async def _load(self, origin: Origin) -> RobotsPolicy:
        try:
            status, body = await self._fetch(self._robots_url(origin))
        except FetchError:
            return RobotsPolicy("disallow_all")
        if 200 <= status < 300:
            return self._parse(body)
        if 400 <= status < 500:
            return RobotsPolicy("allow_all")
        return RobotsPolicy("disallow_all")

    @staticmethod
    def _robots_url(origin: Origin) -> httpx.URL:
        port = None if origin.port == _DEFAULT_PORTS[origin.scheme] else origin.port
        return httpx.URL(scheme=origin.scheme, host=origin.host, port=port, path="/robots.txt")

    @staticmethod
    def _parse(body: bytes) -> RobotsPolicy:
        parser = RobotFileParser()
        parser.parse(body[:ROBOTS_MAX_BYTES].decode("utf-8", errors="replace").splitlines())
        return RobotsPolicy("parsed", parser, _crawl_delay(parser))


def _crawl_delay(parser: RobotFileParser) -> float | None:
    # typeshed types the value as ``str | None``; at runtime it is an int or None.
    raw = parser.crawl_delay(ROBOTS_AGENT_TOKEN)
    try:
        delay = float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    if delay is None or not math.isfinite(delay) or delay < 0:
        return None
    return delay
