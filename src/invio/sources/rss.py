"""RSS 2.0 / Atom source adapter: one feed in, one :class:`Candidate` per usable entry out.

The feed is fetched with :class:`SafeHttpClient` and parsed by ``feedparser`` from the raw
bytes, so the encoding in the XML declaration (or BOM) wins over a wrong HTTP charset. Feeds are
untrusted input: every entry field is checked before use, and an entry that cannot be mapped is
skipped rather than failing the whole feed.

``feedparser`` ships no type information; its untyped results are confined to :func:`_parse`
and the ``_entry_*`` accessors, which hand typed values to the rest of the module.
"""

import io
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from typing import Any, Final
from urllib.parse import urljoin, urlsplit

import feedparser
from feedparser.exceptions import CharacterEncodingOverride, NonXMLContentType

from invio.config.job import RssSource
from invio.domain import Candidate, url_hash
from invio.sources.errors import FetchError
from invio.sources.http import NotModified, SafeHttpClient
from invio.sources.urls import normalize_url

__all__ = ["TEASER_MAX_CHARS", "RssFeedSource"]

TEASER_MAX_CHARS: Final = 500
"""Longest teaser kept from an entry summary, ellipsis included."""

_ELLIPSIS: Final = "…"
_SCHEMES: Final = frozenset({"http", "https"})
# Notes feedparser raises for a feed it parsed fine: no or a non-XML Content-Type (the body is
# passed without headers on purpose) and an encoding that differs from the declared one.
_HARMLESS_BOZO: Final = (NonXMLContentType, CharacterEncodingOverride)


@dataclass(frozen=True, slots=True)
class _ParsedFeed:
    """The parts of a ``feedparser`` result the adapter uses."""

    version: str  # e.g. "rss20" or "atom10"; empty when the body is no known feed format
    broken: bool  # the parser hit a real error (not just a harmless bozo note)
    entries: list[Mapping[str, Any]]


class RssFeedSource:
    """Source adapter for :class:`~invio.config.job.RssSource` configs (RSS 0.9x-2.0, Atom).

    ``now`` returns the current time as an aware datetime and exists for tests; it defaults to
    the system clock in UTC.
    """

    def __init__(
        self, client: SafeHttpClient, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self._client = client
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def fetch(self, config: RssSource) -> list[Candidate]:
        """Fetch and parse the feed; raises :class:`FetchError` (``"malformed_feed"``).

        Entries without a usable http(s) link are skipped; entries older than
        ``config.max_age_days`` are dropped (undated ones are kept); a URL that occurs more
        than once (after normalization) is kept at its first position.
        """
        url = str(config.url)
        result = await self._client.get(url)
        if isinstance(result, NotModified):
            return []
        feed = _parse(result.content)
        mapped = [_candidate(entry, base_url=result.url) for entry in feed.entries]
        candidates = [candidate for candidate in mapped if candidate is not None]
        # Some entries survive a parse error (truncated feed); none means it is unusable.
        if not feed.version or (feed.broken and not candidates):
            raise FetchError("malformed_feed", url=url)
        cutoff = (
            self._now() - timedelta(days=config.max_age_days)
            if config.max_age_days is not None
            else None
        )
        seen: set[str] = set()
        kept: list[Candidate] = []
        for candidate in candidates:
            if candidate.url in seen:
                continue
            published = candidate.published_at
            if cutoff is not None and published is not None and published < cutoff:
                continue
            seen.add(candidate.url)
            kept.append(candidate)
        return kept


def _parse(content: bytes) -> _ParsedFeed:
    """Parse ``content`` with feedparser.

    The bytes are wrapped in a stream: given a ``bytes`` or ``str`` argument, feedparser first
    tries to open it as a file name (or fetch it as a URL), which must never happen for a
    response body.
    """
    parsed: Any = feedparser.parse(io.BytesIO(content))
    error = parsed.get("bozo_exception")
    entries = [entry for entry in parsed.get("entries", []) if isinstance(entry, Mapping)]
    return _ParsedFeed(
        version=str(parsed.get("version") or ""),
        broken=error is not None and not isinstance(error, _HARMLESS_BOZO),
        entries=entries,
    )


def _candidate(entry: Mapping[str, Any], *, base_url: str) -> Candidate | None:
    """Map one entry, or ``None`` if it has no usable link."""
    url = _entry_url(entry, base_url=base_url)
    if url is None:
        return None
    title = _collapse(_entry_text(entry, "title"))
    return Candidate(
        url=url,
        url_hash=url_hash(url),
        title=title or url,
        published_at=_entry_date(entry),
        type="article",
        teaser=_teaser(_entry_text(entry, "summary")),
        content_hash=None,
    )


def _entry_url(entry: Mapping[str, Any], *, base_url: str) -> str | None:
    """The entry link resolved against the feed URL and normalized; http(s) only."""
    link = entry.get("link")
    if not isinstance(link, str) or not link.strip():
        return None
    try:
        url = normalize_url(urljoin(base_url, link.strip()))
        parts = urlsplit(url)
        # ``hostname`` (unlike ``netloc``) is empty for "http://user@" and "http://:80"; an
        # invalid port raises ``ValueError``.
        if parts.scheme not in _SCHEMES or not parts.hostname or parts.port == 0:
            return None
    except ValueError:  # e.g. an unclosed IPv6 bracket or an invalid port
        return None
    return url


def _entry_text(entry: Mapping[str, Any], key: str) -> str:
    """Plain text of ``title`` or ``summary``: HTML (as declared by feedparser) is stripped."""
    value = entry.get(key)
    if not isinstance(value, str):
        return ""
    detail = entry.get(f"{key}_detail")
    content_type = detail.get("type") if isinstance(detail, Mapping) else None
    return value if content_type == "text/plain" else _html_to_text(value)


def _entry_date(entry: Mapping[str, Any]) -> datetime | None:
    """``published``, else ``updated``, as an aware UTC datetime; ``None`` if absent or bad."""
    for key in ("published_parsed", "updated_parsed"):
        # ``in`` first: a missing ``updated_parsed`` is otherwise mapped (with a deprecation
        # warning) to ``published_parsed``.
        if key not in entry:
            continue
        value = entry[key]
        if value is None:
            continue
        try:
            # feedparser normalizes dates to UTC ``struct_time``s.
            return datetime(*time.struct_time(value)[:6], tzinfo=UTC)
        except (TypeError, ValueError, OverflowError):
            continue
    return None


def _teaser(text: str) -> str | None:
    """Whitespace-collapsed ``text`` cut at a word boundary to :data:`TEASER_MAX_CHARS`."""
    text = _collapse(text)
    if not text:
        return None
    if len(text) <= TEASER_MAX_CHARS:
        return text
    cut = text[: TEASER_MAX_CHARS - len(_ELLIPSIS)]
    head, space, _ = cut.rpartition(" ")
    return (head if space and head else cut).rstrip() + _ELLIPSIS


def _collapse(text: str) -> str:
    return " ".join(text.split())


class _TextExtractor(HTMLParser):
    """Collects the text nodes of an HTML fragment; character references are decoded."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.parts.append(" ")  # "<p>a</p><p>b</p>" reads "a b", not "ab"


def _html_to_text(html: str) -> str:
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return "".join(extractor.parts)
