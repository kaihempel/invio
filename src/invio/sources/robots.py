"""robots.txt policy cache for the safe HTTP client.

One robots.txt is fetched per origin and the outcome is kept for the client's lifetime:

- 2xx: the first 500 KB are parsed;
- 4xx: no robots.txt, everything is allowed;
- 5xx, a blocked target or a malformed answer: everything is disallowed, which is the cautious
  reading of "could not find out";
- a transient failure (timeout, connection or DNS error): everything is disallowed too, but
  only for :data:`ROBOTS_RETRY_AFTER` seconds, so one network blip does not block an origin
  for a whole long run.

Rules are matched as RFC 9309 describes, independent of the Python version (the standard
library's ``urllib.robotparser`` ignores ``*`` and ``$`` before Python 3.14): the ``invio``
groups apply, else the ``*`` groups; the longest matching pattern wins and ``Allow`` wins a tie.

The fetch itself is injected, so the cache knows nothing about HTTP and the client can run the
robots request through its normal guarded pipeline.
"""

import asyncio
import math
import re
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import quote, unquote, urlsplit

import httpx

from invio.sources.errors import BlockedError, FetchError
from invio.sources.netguard import DEFAULT_PORTS, Origin

__all__ = [
    "ROBOTS_AGENT_TOKEN",
    "ROBOTS_MAX_BYTES",
    "ROBOTS_RETRY_AFTER",
    "RobotsCache",
    "RobotsPolicy",
    "parse_robots",
]

ROBOTS_AGENT_TOKEN: Final = "invio"
ROBOTS_MAX_BYTES: Final = 512_000
ROBOTS_RETRY_AFTER: Final = 300.0
# Failures that may be gone on the next attempt; anything else is kept for the run.
_TRANSIENT_REASONS: Final = frozenset({"timeout", "connection_failed", "dns_failed"})
# Characters left as they are when paths and patterns are normalised (``*`` and ``$`` are the
# pattern operators); everything else is compared in its percent-encoded form.
_PATH_SAFE: Final = "/?&=:;@!,+*$'()~"

RobotsFetch = Callable[[httpx.URL], Awaitable[tuple[int, bytes]]]


@dataclass(frozen=True, slots=True)
class _Rule:
    allow: bool
    length: int  # length of the pattern as written; the longest match wins
    pattern: re.Pattern[str]


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """What robots.txt allows on one origin.

    ``expires_at`` (a monotonic time) is only set for a policy that stands in for a transient
    fetch failure; the cache fetches robots.txt again once it has passed.
    """

    mode: Literal["parsed", "allow_all", "disallow_all"]
    rules: tuple[_Rule, ...] = ()
    crawl_delay: float | None = None
    expires_at: float | None = None

    def allows(self, url: str) -> bool:
        """True if the ``invio`` agent may fetch ``url``."""
        if self.mode == "disallow_all":
            return False
        parts = urlsplit(url)
        if parts.path == "/robots.txt":
            return True  # RFC 9309 2.2.2: robots.txt itself is always allowed
        target = _normalise((parts.path or "/") + (f"?{parts.query}" if parts.query else ""))
        best: _Rule | None = None
        for rule in self.rules:
            if rule.pattern.match(target) and (
                best is None
                or rule.length > best.length
                or (rule.length == best.length and rule.allow)
            ):
                best = rule
        return best is None or best.allow


class RobotsCache:
    """Per-origin robots.txt policies, fetched lazily and at most once (per retry window)."""

    def __init__(self, fetch: RobotsFetch, clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._clock = clock
        self._policies: dict[Origin, RobotsPolicy] = {}
        self._locks: dict[Origin, asyncio.Lock] = {}

    def known(self, origin: Origin) -> RobotsPolicy | None:
        """The policy if it has already been fetched; never triggers a fetch."""
        return self._policies.get(origin)

    async def policy(self, origin: Origin) -> RobotsPolicy:
        """Return the policy of ``origin``; concurrent first callers share one fetch."""
        cached = self._current(origin)
        if cached is not None:
            return cached
        # setdefault runs without an await in between, so every caller gets the same lock.
        async with self._locks.setdefault(origin, asyncio.Lock()):
            cached = self._current(origin)
            if cached is None:
                cached = await self._load(origin)
                self._policies[origin] = cached
            return cached

    def _current(self, origin: Origin) -> RobotsPolicy | None:
        cached = self._policies.get(origin)
        if cached is not None and cached.expires_at is not None:
            return cached if self._clock() < cached.expires_at else None
        return cached

    async def _load(self, origin: Origin) -> RobotsPolicy:
        try:
            status, body = await self._fetch(self._robots_url(origin))
        except BlockedError:
            return RobotsPolicy("disallow_all")
        except FetchError as error:
            if error.reason in _TRANSIENT_REASONS:
                return RobotsPolicy("disallow_all", expires_at=self._clock() + ROBOTS_RETRY_AFTER)
            return RobotsPolicy("disallow_all")
        if 200 <= status < 300:
            return parse_robots(body[:ROBOTS_MAX_BYTES].decode("utf-8", errors="replace"))
        if 400 <= status < 500:
            return RobotsPolicy("allow_all")
        return RobotsPolicy("disallow_all")

    @staticmethod
    def _robots_url(origin: Origin) -> httpx.URL:
        port = None if origin.port == DEFAULT_PORTS[origin.scheme] else origin.port
        return httpx.URL(scheme=origin.scheme, host=origin.host, port=port, path="/robots.txt")


@dataclass
class _Group:
    agents: list[str]
    lines: list[tuple[str, str]]  # (lower-cased key, value) of the rules and Crawl-delay


def parse_robots(text: str) -> RobotsPolicy:
    """Parse robots.txt ``text`` into the policy of the ``invio`` agent (RFC 9309)."""
    groups = _groups(text.removeprefix("﻿").splitlines())
    own = [group for group in groups if ROBOTS_AGENT_TOKEN in group.agents]
    selected = own or [group for group in groups if "*" in group.agents]
    lines = [line for group in selected for line in group.lines]
    rules = tuple(
        _rule(key == "allow", value)
        for key, value in lines
        if key in ("allow", "disallow") and value  # an empty Disallow allows everything
    )
    delays = (_crawl_delay(value) for key, value in lines if key == "crawl-delay")
    return RobotsPolicy("parsed", rules, next((d for d in delays if d is not None), None))


def _groups(lines: Iterable[str]) -> list[_Group]:
    """Split robots.txt into groups: user-agent lines followed by their rules."""
    groups: list[_Group] = []
    current: _Group | None = None
    for raw in lines:
        key, sep, value = raw.split("#", 1)[0].partition(":")
        if not sep:
            continue
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            if current is None or current.lines:
                current = _Group([], [])
                groups.append(current)
            # Only the product token counts ("invio/1.0" is "invio"), case-insensitively.
            current.agents.append(value.split("/", 1)[0].strip().lower())
        elif key in ("allow", "disallow", "crawl-delay") and current is not None:
            current.lines.append((key, value))
    return groups


def _rule(allow: bool, value: str) -> _Rule:
    anchored = value.endswith("$")
    path = _normalise(value[:-1] if anchored else value)
    regex = ".*".join(re.escape(part) for part in path.split("*"))
    return _Rule(allow, len(value), re.compile(regex + (r"\Z" if anchored else "")))


def _normalise(path: str) -> str:
    """Compare paths in one percent-encoded form, so ``/a%7Eb`` and ``/a~b`` are the same."""
    return quote(unquote(path), safe=_PATH_SAFE)


def _crawl_delay(raw: str) -> float | None:
    try:
        delay = float(raw)
    except ValueError:
        return None
    if not math.isfinite(delay) or delay < 0:
        return None
    return delay
