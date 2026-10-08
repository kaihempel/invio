"""Sitemap source adapter: one sitemap in, one :class:`Candidate` per usable ``<url>`` out.

Handles a ``urlset`` and a ``sitemapindex`` (children are followed up to :data:`MAX_DEPTH`
levels, so an index may list ``urlset`` files but an index inside an index is ignored). Bodies
may be gzip-compressed (``sitemap.xml.gz``). Sitemaps are untrusted input: XML is parsed with
``defusedxml``, which refuses DTDs and entity declarations (entity-expansion attacks), and a
gzip body is inflated only up to the response size limit of the HTTP client.
"""

import logging
import re
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring

from invio.config.job import SitemapSource
from invio.domain import Candidate, url_hash
from invio.sources.errors import FetchError
from invio.sources.freshness import clamp_or_expire
from invio.sources.http import NotModified, SafeHttpClient
from invio.sources.urls import http_url_or_none

__all__ = ["MAX_CHILD_SITEMAPS", "MAX_DEPTH", "MAX_ENTRIES_PER_SITEMAP", "SitemapUrlSource"]

MAX_DEPTH: Final = 2
MAX_ENTRIES_PER_SITEMAP: Final = 500
MAX_CHILD_SITEMAPS: Final = 50

_log = logging.getLogger("invio.sources.sitemap")

_GZIP_MAGIC: Final = b"\x1f\x8b"
# W3C datetime: YYYY, YYYY-MM, YYYY-MM-DD or a full timestamp; the short forms are not accepted
# by ``datetime.fromisoformat``.
_PARTIAL_DATE: Final = re.compile(r"^(\d{4})(?:-(\d{2}))?$")


@dataclass(frozen=True, slots=True)
class _Entry:
    loc: str
    lastmod: datetime | None


class SitemapUrlSource:
    """Source adapter for :class:`~invio.config.job.SitemapSource` configs.

    ``now`` returns the current time as an aware datetime and exists for tests.
    """

    def __init__(
        self, client: SafeHttpClient, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self._client = client
        self._now = now if now is not None else lambda: datetime.now(UTC)

    async def fetch(self, config: SitemapSource) -> list[Candidate]:
        """Fetch the sitemap and return the URLs that pass the filters, in sitemap order.

        Raises :class:`FetchError` (``"malformed_sitemap"``) when the top-level document is no
        sitemap. A failing child of an index is logged and skipped. A URL that occurs more than
        once (after canonicalization) is decided by its first occurrence; ``lastmod`` becomes
        ``published_at`` (a date in the future is clamped to now). Per sitemap at most
        :data:`MAX_ENTRIES_PER_SITEMAP` entries are kept.

        ``[]`` for HTTP 304 of the top-level document.
        """
        pattern = re.compile(config.url_pattern) if config.url_pattern is not None else None
        now = self._now()
        cutoff = now - timedelta(days=config.max_age_days) if config.max_age_days else None
        candidates: dict[str, Candidate] = {}
        visited: set[str] = set()

        async def walk(url: str, depth: int) -> None:
            visited.add(url)
            result = await self._client.get(url)
            if isinstance(result, NotModified):
                return
            root = _parse(result.content, self._client.max_response_bytes, url=url)
            kind = _local_name(root.tag)
            if kind == "urlset":
                kept = 0
                for entry in _urlset_entries(root, result.url):
                    if pattern is not None and pattern.search(entry.loc) is None:
                        continue
                    keep, published = clamp_or_expire(entry.lastmod, now, cutoff)
                    if not keep or entry.loc in candidates:
                        continue
                    if kept >= MAX_ENTRIES_PER_SITEMAP:
                        break
                    kept += 1
                    candidates[entry.loc] = Candidate(
                        url=entry.loc,
                        url_hash=url_hash(entry.loc),
                        title=entry.loc,
                        published_at=published,
                        type="article",
                        teaser=None,
                        content_hash=None,
                    )
            elif kind == "sitemapindex":
                if depth >= MAX_DEPTH:
                    return
                children = _index_children(root, result.url)[:MAX_CHILD_SITEMAPS]
                for child in children:
                    if child in visited:
                        continue
                    try:
                        await walk(child, depth + 1)
                    except FetchError as exc:
                        _log.warning("skipping child sitemap: %s", exc)
            else:
                raise FetchError("malformed_sitemap", url=url)

        await walk(str(config.url), 1)
        return list(candidates.values())


def _local_name(tag: object) -> str:
    """The tag without its ``{namespace}`` prefix (sitemaps in the wild omit or vary it)."""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _parse(content: bytes, limit: int, *, url: str) -> Element:
    """Parse a sitemap body (gzip allowed) or raise ``FetchError``."""
    if content.startswith(_GZIP_MAGIC):
        content = _gunzip(content, limit, url=url)
    try:
        root: Element = fromstring(content, forbid_dtd=True)
        return root
    except (ParseError, DefusedXmlException, ValueError):
        raise FetchError("malformed_sitemap", url=url) from None


def _gunzip(content: bytes, limit: int, *, url: str) -> bytes:
    """Inflate ``content`` to at most ``limit`` bytes (a larger result is a ``too_large``)."""
    inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        data = inflater.decompress(content, limit + 1)
    except zlib.error:
        raise FetchError("malformed_sitemap", url=url) from None
    if len(data) > limit:
        raise FetchError("too_large", url=url)
    return data


def _children_text(root: Element, container: str) -> list[tuple[str, Element]]:
    """``(loc text, container element)`` for each ``container`` child that has a ``loc``."""
    found: list[tuple[str, Element]] = []
    for node in root:
        if _local_name(node.tag) != container:
            continue
        loc = next((c.text for c in node if _local_name(c.tag) == "loc" and c.text), None)
        if loc is not None:
            found.append((loc, node))
    return found


def _index_children(root: Element, base_url: str) -> list[str]:
    urls = (http_url_or_none(loc, base_url) for loc, _ in _children_text(root, "sitemap"))
    return [url for url in urls if url is not None]


def _urlset_entries(root: Element, base_url: str) -> list[_Entry]:
    entries: list[_Entry] = []
    for loc, node in _children_text(root, "url"):
        url = http_url_or_none(loc, base_url)
        if url is None:
            continue
        lastmod = next((c.text for c in node if _local_name(c.tag) == "lastmod"), None)
        entries.append(_Entry(url, _parse_lastmod(lastmod)))
    return entries


def _parse_lastmod(value: str | None) -> datetime | None:
    """A W3C datetime as aware UTC (naive means UTC); ``None`` if absent or unparsable."""
    if value is None:
        return None
    text = value.strip()
    try:
        match = _PARTIAL_DATE.match(text)
        if match is not None:
            return datetime(int(match.group(1)), int(match.group(2) or 1), 1, tzinfo=UTC)
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
