"""Source discovery: from a website address to the feeds, sitemaps and channels it offers.

Flow of one :func:`discover` call: read the start page (a feed or sitemap entered directly is
offered as it is), collect *findings* (feeds the page announces, the common locations, the
``Sitemap:`` entries of robots.txt), validate every finding by fetching it and classifying the
body, then drop duplicates and order the result. Everything a server sends is untrusted: a
finding is only offered once its content has been parsed as a feed or a sitemap.

Layering: this module imports only ``invio.sources.*`` and ``invio.config.job``, never the CLI,
the services, the pipeline or the database. All requests go through
:class:`~invio.sources.http.SafeHttpClient` (address guard, robots.txt, rate limit, size limit);
:func:`discover` never raises :class:`~invio.sources.errors.FetchError` but reports each
failure as a :class:`Rejection`.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Final, Literal
from urllib.parse import SplitResult, urlsplit

from pydantic import ValidationError
from selectolax.lexbor import LexborHTMLParser

from invio.config.job import (
    RssSource,
    SitemapSource,
    SourceConfig,
    YoutubeChannelSource,
    YoutubePlaylistSource,
    parse_youtube_channel,
    parse_youtube_playlist,
)
from invio.sources.errors import FetchError
from invio.sources.http import FetchResult, SafeHttpClient
from invio.sources.rss import describe_feed
from invio.sources.sitemap import describe_sitemap
from invio.sources.text import collapse, parse_html
from invio.sources.urls import (
    WEB_SCHEMES,
    canonical_url,
    http_url_or_none,
    redact_url,
    without_query,
)
from invio.sources.web import decode_html, html_base_url, is_html
from invio.sources.youtube import YoutubeSource

__all__ = [
    "MAX_ANNOUNCED_FEEDS",
    "MAX_ROBOTS_SITEMAPS",
    "MAX_YOUTUBE_LINKS",
    "PROBE_PATHS",
    "DiscoveredSource",
    "DiscoveryReport",
    "DiscoveryTarget",
    "FindingOrigin",
    "Rejection",
    "discover",
    "is_comment_feed",
    "parse_target",
]

MAX_ANNOUNCED_FEEDS: Final = 20
MAX_YOUTUBE_LINKS: Final = 20
MAX_ROBOTS_SITEMAPS: Final = 5
PROBE_PATHS: Final = ("feed", "rss", "atom.xml", "sitemap.xml")

_FEED_LINK_TYPES: Final = frozenset({"application/rss+xml", "application/atom+xml"})
_COMMENT_PATH_ENDINGS: Final = ("/comments/feed", "/comments/feed/", "/comments/")
_NOT_A_SOURCE: Final = "not_a_feed_or_sitemap"
_UNREADABLE: Final = "unreadable"
_SCHEME: Final = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://")


@dataclass(frozen=True, slots=True)
class DiscoveryTarget:
    """The normalised address the operator entered.

    ``root`` is ``scheme://host[:port]/``; ``folder`` is the page's directory when that is not
    the root (``https://example.com/blog/`` for ``/blog/post-1``), else ``None``.
    """

    url: str
    root: str
    folder: str | None

    def rebased(self, final_url: str) -> "DiscoveryTarget":
        """The same structure for the start page's ``final_url`` (after redirects).

        An unusable ``final_url`` leaves the target as it is.
        """
        try:
            return parse_target(final_url)
        except ValueError:
            return self


class FindingOrigin(IntEnum):
    """Where a finding came from; the value is the sort rank (direct first)."""

    DIRECT = 0
    ANNOUNCED = 1
    PROBED = 2
    ROBOTS = 3
    LINKED = 4


@dataclass(frozen=True, slots=True)
class _Finding:
    """A possible source before validation."""

    kind: Literal["feed_or_sitemap", "youtube_channel", "youtube_playlist"]
    locator: str
    origin: FindingOrigin
    position: int
    hint_title: str | None = None


@dataclass(frozen=True, slots=True)
class DiscoveredSource:
    """A validated finding: what the operator sees and may add to a job."""

    source: SourceConfig
    title: str | None
    entry_count: int
    newest: datetime | None
    sitemap_index: bool
    is_comment_feed: bool
    origin: FindingOrigin
    position: int
    final_url: str


@dataclass(frozen=True, slots=True)
class Rejection:
    """Why a finding (or the start page) was not offered; ``locator`` is redacted."""

    locator: str
    reason: str
    status: int | None = None


@dataclass(frozen=True, slots=True)
class DiscoveryReport:
    """The outcome of :func:`discover`."""

    target: DiscoveryTarget
    sources: tuple[DiscoveredSource, ...]
    page_problem: Rejection | None
    rejected: tuple[Rejection, ...]
    skipped: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _StartPage:
    """What is known about the start page after reading it."""

    final_url: str | None  # ``None`` when the fetch failed
    tree: LexborHTMLParser | None  # only for an HTML page
    direct: DiscoveredSource | None  # the page is itself a feed or sitemap
    problem: Rejection | None


def parse_target(raw: str) -> DiscoveryTarget:
    """Normalise the operator's input; raises ``ValueError`` (with a message) if unusable.

    A scheme-less input such as ``example.com`` or ``localhost:8080`` gets ``https://``; a
    scheme-less single word such as ``example`` is refused as a typo rather than guessed. Only
    http(s) URLs with a host and without credentials pass. No network access: the address
    guard of the HTTP client refuses private addresses when the URL is fetched.
    """
    text = raw.strip()
    if not text:
        raise ValueError("the address is empty")
    if any(char.isspace() or not char.isprintable() for char in text):
        raise ValueError("the address must not contain whitespace or control characters")
    scheme_less = not _SCHEME.match(text)
    if scheme_less:
        text = f"https://{text}"
    try:
        parts = urlsplit(text)
        port = parts.port
    except ValueError:
        raise ValueError("the address is not a valid URL") from None
    if parts.scheme.lower() not in WEB_SCHEMES:
        raise ValueError("only http and https addresses are supported")
    if not parts.hostname:
        raise ValueError("the address has no host")
    if scheme_less and "." not in parts.hostname and parts.hostname != "localhost":
        raise ValueError("the address needs a scheme or a domain name such as example.com")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise ValueError("the address must not contain credentials")
    if port == 0:
        raise ValueError("the address has an invalid port")
    return _target(parts)


def _target(parts: SplitResult) -> DiscoveryTarget:
    url = canonical_url(parts._replace(fragment="").geturl())
    canonical = urlsplit(url)
    root = f"{canonical.scheme}://{canonical.netloc}/"
    directory = canonical.path.rpartition("/")[0] + "/"
    folder = f"{root.rstrip('/')}{directory}" if directory != "/" else None
    return DiscoveryTarget(url=url, root=root, folder=folder)


def is_comment_feed(final_url: str, *titles: str | None) -> bool:
    """True for a comments feed: by its path or because a title says "comments".

    The path ends with ``/comments/feed``, ``/comments/feed/`` or ``/comments/``; the titles are
    the ``title`` attribute of the announcing link and the title of the feed itself.
    """
    if urlsplit(final_url).path.endswith(_COMMENT_PATH_ENDINGS):
        return True
    return any(title is not None and "comments" in title.lower() for title in titles)


async def discover(
    target: DiscoveryTarget, *, client: SafeHttpClient, youtube: YoutubeSource
) -> DiscoveryReport:
    """Find the sources ``target`` offers; every failure becomes a :class:`Rejection`.

    Every HTTP request goes through ``client.get(..., conditional=False)`` and robots.txt
    sitemaps through ``client.robots_sitemaps``. The result is ordered direct, announced,
    probed, robots (comment feeds last) and free of duplicates.
    """
    return await _Discovery(target, client, youtube).run()


class _Discovery:
    """One discovery run: the state that the steps of :func:`discover` share."""

    def __init__(
        self, target: DiscoveryTarget, client: SafeHttpClient, youtube: YoutubeSource
    ) -> None:
        self._target = target
        self._client = client
        self._youtube = youtube  # validates YouTube findings
        self._skipped: list[str] = []

    async def run(self) -> DiscoveryReport:
        direct = _youtube_finding(self._target.url, FindingOrigin.DIRECT, 0)
        if direct is not None:  # a channel or playlist address: nothing to fetch but the listing
            return self._report(await self._validate_all([direct]), None)
        page = await self._read_start_page()
        base = self._target.rebased(page.final_url) if page.final_url else self._target
        findings = self._announced(page)
        findings += self._probes(base)
        findings += await self._robots(base)
        findings += self._linked(page)
        fetched = {("feed_or_sitemap", canonical_url(self._target.url))}
        if page.final_url is not None:
            fetched.add(("feed_or_sitemap", canonical_url(page.final_url)))
        outcomes = await self._validate_all(_unique(findings, fetched))
        return self._report(outcomes, page, direct=page.direct)

    async def _validate_all(self, findings: list[_Finding]) -> list[DiscoveredSource | Rejection]:
        return list(await asyncio.gather(*(self._validate(f) for f in findings)))

    def _report(
        self,
        outcomes: list[DiscoveredSource | Rejection],
        page: _StartPage | None,
        *,
        direct: DiscoveredSource | None = None,
    ) -> DiscoveryReport:
        found = [direct] if direct is not None else []
        found += [o for o in outcomes if isinstance(o, DiscoveredSource)]
        return DiscoveryReport(
            target=self._target,
            sources=tuple(_merge(found)),
            page_problem=page.problem if page is not None else None,
            rejected=tuple(o for o in outcomes if isinstance(o, Rejection)),
            skipped=tuple(self._skipped),
        )

    # --- the start page and the findings -------------------------------------------------------

    async def _read_start_page(self) -> _StartPage:
        url = self._target.url
        try:
            result = await self._get(url)
        except FetchError as error:
            return _StartPage(None, None, None, _rejection(url, error))
        try:
            direct = await self._classify(result, FindingOrigin.DIRECT, 0, None)
            if direct is not None:
                return _StartPage(result.url, None, direct, None)
            if is_html(result.headers, result.content):
                tree = parse_html(decode_html(result))
                return _StartPage(result.url, tree, None, None)
        except Exception:  # a hostile body must not end the run (e.g. RecursionError)
            return _StartPage(result.url, None, None, Rejection(_shown(url), _UNREADABLE))
        return _StartPage(result.url, None, None, Rejection(_shown(url), _NOT_A_SOURCE))

    def _announced(self, page: _StartPage) -> list[_Finding]:
        """Feeds the page announces with ``<link rel="alternate" type="application/…+xml">``."""
        if page.tree is None or page.final_url is None:
            return []
        base = html_base_url(page.tree, page.final_url)
        found: dict[str, _Finding] = {}
        for node in page.tree.css("link[href]"):
            attributes = node.attributes
            if "alternate" not in (attributes.get("rel") or "").lower().split():
                continue
            kind = (attributes.get("type") or "").partition(";")[0].strip().lower()
            url = http_url_or_none(attributes.get("href") or "", base)
            if kind not in _FEED_LINK_TYPES or url is None or url in found:
                continue
            title = collapse(attributes.get("title") or "") or None
            found[url] = _Finding(
                "feed_or_sitemap", url, FindingOrigin.ANNOUNCED, len(found), title
            )
        return self._capped(list(found.values()), MAX_ANNOUNCED_FEEDS, "announced feeds")

    @staticmethod
    def _probes(base: DiscoveryTarget) -> list[_Finding]:
        """The common locations under the root, then under the page's folder."""
        bases = [base.root] + ([base.folder] if base.folder is not None else [])
        locations = [f"{prefix}{path}" for prefix in bases for path in PROBE_PATHS]
        return [
            _Finding("feed_or_sitemap", url, FindingOrigin.PROBED, position)
            for position, url in enumerate(locations)
        ]

    async def _robots(self, base: DiscoveryTarget) -> list[_Finding]:
        """The sitemaps robots.txt announces, in file order."""
        announced = await self._client.robots_sitemaps(base.root)
        urls = dict.fromkeys(u for raw in announced if (u := http_url_or_none(raw, base.root)))
        findings = [
            _Finding("feed_or_sitemap", url, FindingOrigin.ROBOTS, position)
            for position, url in enumerate(urls)
        ]
        return self._capped(findings, MAX_ROBOTS_SITEMAPS, "robots.txt sitemaps")

    def _linked(self, page: _StartPage) -> list[_Finding]:
        """YouTube channels and playlists the page links to (``a`` and ``link`` elements)."""
        if page.tree is None or page.final_url is None:
            return []
        base = html_base_url(page.tree, page.final_url)
        found: dict[tuple[str, str], _Finding] = {}
        for node in page.tree.css("a[href], link[href]"):
            url = http_url_or_none(node.attributes.get("href") or "", base)
            finding = _youtube_finding(url, FindingOrigin.LINKED, len(found)) if url else None
            if finding is not None:
                found.setdefault(_key(finding), finding)
        return self._capped(list(found.values()), MAX_YOUTUBE_LINKS, "YouTube links")

    def _capped(self, findings: list[_Finding], limit: int, what: str) -> list[_Finding]:
        if len(findings) > limit:
            self._skipped.append(f"{len(findings) - limit} more {what} (limit {limit})")
        return findings[:limit]

    # --- validation ----------------------------------------------------------------------------

    async def _validate(self, finding: _Finding) -> DiscoveredSource | Rejection:
        if finding.kind != "feed_or_sitemap":
            return await self._validate_youtube(finding)
        try:
            result = await self._get(finding.locator)
        except FetchError as error:
            return _rejection(finding.locator, error)
        try:
            found = await self._classify(
                result, finding.origin, finding.position, finding.hint_title
            )
        except Exception:  # a hostile body must not end the run (e.g. RecursionError)
            return Rejection(_shown(finding.locator), _UNREADABLE)
        return found if found is not None else Rejection(_shown(finding.locator), _NOT_A_SOURCE)

    async def _validate_youtube(self, finding: _Finding) -> DiscoveredSource | Rejection:
        """Ask the YouTube adapter for the listing; an unreadable or empty one is not offered."""
        source: YoutubeChannelSource | YoutubePlaylistSource
        if finding.kind == "youtube_channel":
            source = YoutubeChannelSource(type="youtube_channel", channel_id=finding.locator)
        else:
            source = YoutubePlaylistSource(type="youtube_playlist", playlist_id=finding.locator)
        try:
            summary = await self._youtube.describe(source)
        except FetchError as error:
            return Rejection(finding.locator, str(error.reason), error.status)
        if summary.entry_count == 0:
            return Rejection(finding.locator, "empty_listing")
        return DiscoveredSource(
            source=source,
            title=summary.title,
            entry_count=summary.entry_count,
            newest=summary.newest,
            sitemap_index=False,
            is_comment_feed=False,
            origin=finding.origin,
            position=finding.position,
            final_url=_youtube_url(finding),
        )

    async def _get(self, url: str) -> FetchResult:
        result = await self._client.get(url, conditional=False)
        assert isinstance(result, FetchResult)  # an unconditional fetch is never "not modified"
        return result

    async def _classify(
        self,
        result: FetchResult,
        origin: FindingOrigin,
        position: int,
        hint_title: str | None,
    ) -> DiscoveredSource | None:
        """:func:`_classify` off the event loop: parsing a large body is CPU work."""
        limit = self._client.max_response_bytes
        return await asyncio.to_thread(_classify, result, origin, position, hint_title, limit=limit)


def _classify(
    result: FetchResult,
    origin: FindingOrigin,
    position: int,
    hint_title: str | None,
    *,
    limit: int,
) -> DiscoveredSource | None:
    """The feed or sitemap ``result`` contains, or ``None``; feeds are tried first."""
    final_url = http_url_or_none(result.url, result.url)
    if final_url is None:
        return None
    try:
        feed = describe_feed(result.content, base_url=final_url)
        if feed is not None:
            return DiscoveredSource(
                source=RssSource(type="rss", url=final_url),
                title=feed.title,
                entry_count=feed.entry_count,
                newest=feed.newest,
                sitemap_index=False,
                is_comment_feed=is_comment_feed(final_url, hint_title, feed.title),
                origin=origin,
                position=position,
                final_url=final_url,
            )
        sitemap = describe_sitemap(result.content, url=final_url, limit=limit)
        if sitemap is not None:
            return DiscoveredSource(
                source=SitemapSource(type="sitemap", url=final_url),
                title=None,
                entry_count=sitemap.entry_count,
                newest=sitemap.newest,
                sitemap_index=sitemap.is_index,
                is_comment_feed=False,
                origin=origin,
                position=position,
                final_url=final_url,
            )
    except ValidationError:  # a URL the job model refuses (e.g. over-long)
        return None
    return None


def _youtube_finding(url: str, origin: FindingOrigin, position: int) -> _Finding | None:
    """The channel or playlist ``url`` points to, or ``None`` for any other address.

    ``http`` links are upgraded to ``https`` (the job model only accepts https). The parsers of
    the job model decide what a YouTube address is, including the allowed hosts.
    """
    secure = f"https://{url[7:]}" if url.startswith("http://") else url
    try:
        kind, token = parse_youtube_channel(secure)
    except ValueError:
        pass
    else:
        locator = f"@{token}" if kind == "handle" else token
        return _Finding("youtube_channel", locator, origin, position)
    try:
        return _Finding("youtube_playlist", parse_youtube_playlist(secure), origin, position)
    except ValueError:
        return None


def _youtube_url(finding: _Finding) -> str:
    """A canonical address for a YouTube finding (identity for merging, not fetched)."""
    if finding.kind == "youtube_playlist":
        return f"https://www.youtube.com/playlist?list={finding.locator}"
    if finding.locator.startswith("@"):
        return f"https://www.youtube.com/{finding.locator.lower()}"
    return f"https://www.youtube.com/channel/{finding.locator}"


def _key(finding: _Finding) -> tuple[str, str]:
    """What makes two findings the same: canonical URL, or the type and id of a YouTube source."""
    if finding.kind == "feed_or_sitemap":
        return finding.kind, canonical_url(finding.locator)
    return finding.kind, finding.locator.lower() if finding.locator.startswith(
        "@"
    ) else finding.locator


def _unique(findings: list[_Finding], fetched: set[tuple[str, str]]) -> list[_Finding]:
    """Drop findings that are already known; the first (lowest rank) one stays."""
    seen = set(fetched)
    kept: list[_Finding] = []
    for finding in sorted(findings, key=lambda f: (f.origin, f.position)):
        key = _key(finding)
        if key not in seen:
            seen.add(key)
            kept.append(finding)
    return kept


def _merge(found: list[DiscoveredSource]) -> list[DiscoveredSource]:
    """Keep the best-ranked of the sources with the same type and final URL; comments last."""
    seen: set[tuple[str, str]] = set()
    kept: list[DiscoveredSource] = []
    for item in sorted(found, key=lambda f: (f.origin, f.position)):
        key = (item.source.type, canonical_url(item.final_url))
        if key not in seen:
            seen.add(key)
            kept.append(item)
    return sorted(kept, key=lambda f: f.is_comment_feed)  # stable: keeps the rank order


def _shown(url: str) -> str:
    """``url`` as it may be shown: no credentials, no query string."""
    return without_query(redact_url(url))


def _rejection(url: str, error: FetchError) -> Rejection:
    return Rejection(_shown(url), str(error.reason), error.status)
