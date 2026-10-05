"""Web page source adapter: one page in, one page candidate (or its article links) out.

Flow of one fetch: fetch the page with :class:`SafeHttpClient` (or render it in a headless
browser, ``render: js``) -> decode it -> check that it is HTML -> parse it and drop the
non-text elements -> pick the region (``<body>`` or the ``selector`` matches) -> readable text,
fingerprint and teaser (``mode: page``) or the links of the region (``mode: links``).

Pages are untrusted input: the document is decoded without ever failing, a response that is no
HTML is refused, and a ``selector`` that matches nothing is an error rather than an empty
fingerprint. The text rules are the ones of the RSS source (:mod:`invio.sources.text`).
"""

import asyncio
import codecs
import hashlib
import importlib
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from typing import Final, NamedTuple, Protocol, Self
from urllib.parse import urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from invio.config.job import WebSource
from invio.domain import Candidate, url_hash
from invio.sources.errors import FetchError, RenderUnavailableError, TooLargeError
from invio.sources.http import FetchResult, NotModified, SafeHttpClient
from invio.sources.text import collapse, html_to_text, teaser
from invio.sources.urls import canonical_url, http_url_or_none

__all__ = ["PageRenderer", "RenderedPage", "WebPageSource"]

# Elements whose content is never part of the readable text.
_NON_TEXT_TAGS: Final = ["script", "style", "noscript", "template"]
_HTML_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
_BOMS: Final = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),  # the codec reads the byte order from the BOM
    (codecs.BOM_UTF16_BE, "utf-16"),
)
_HEADER_CHARSET: Final = re.compile(r"charset\s*=\s*[\"']?([^\s;\"']+)", re.IGNORECASE)
# ``<meta charset=x>`` and ``<meta http-equiv="Content-Type" content="text/html; charset=x">``.
_META_CHARSET: Final = re.compile(
    r"<meta[^>]*?charset\s*=\s*[\"']?\s*([^\s\"'/>;]+)", re.IGNORECASE
)
# Python's canonical codec names of the encodings of the WHATWG Encoding Standard that matter
# for pages; ``_LATIN1_NAMES`` are read as windows-1252, which is a superset.
_WEB_ENCODINGS: Final = re.compile(
    r"utf-8|utf-16(-le|-be)?|cp125[0-8]|iso8859-([2-9]|1[0-6])|koi8-[ru]|shift_jis|cp932"
    r"|euc_jp|iso2022_jp|euc_kr|gbk|gb18030|big5|big5hkscs|cp950"
)
_LATIN1_NAMES: Final = frozenset({"iso8859-1", "ascii"})
_META_PRESCAN_BYTES: Final = 1024  # the HTML standard's prescan window
_SNIFF_BYTES: Final = 64
_HTML_START: Final = (b"<!doctype html", b"<html")


class RenderedPage(NamedTuple):
    """A page after a headless browser has run its scripts."""

    url: str  # final URL after redirects and navigation
    html: str


class PageRenderer(Protocol):
    """Loads a page in a browser and returns the resulting document.

    The one implementation, :class:`invio.sources.browser.PlaywrightRenderer`, needs the
    optional ``render`` extra; this protocol keeps :mod:`invio.sources.web` free of it.
    """

    async def render(self, url: str, *, wait_for: str | None) -> RenderedPage:
        """Render ``url``; raises :class:`FetchError` (``"timeout"``, ``"http_status"``, ...)."""
        ...

    async def aclose(self) -> None:
        """Release the browser."""
        ...


class WebPageSource:
    """Source adapter for :class:`~invio.config.job.WebSource` configs.

    Use it as ``async with`` (or call :meth:`aclose`).

    Page mode reports a page with no readable text (an empty body, or a ``selector`` that
    matches only empty elements) as a candidate with the hash of the empty text and no teaser:
    an empty page is a stable state, and a change from or to it is a change. Only a
    ``selector`` that matches no element at all is an error.

    ``render: js`` sources are rendered by a :class:`PageRenderer`. By default the first such
    fetch loads :mod:`invio.sources.browser` (and with it Playwright, the optional ``render``
    extra) and starts one Chromium that all later fetches share, so nothing is imported or
    launched for static sources. ``renderer`` replaces that for tests; either way the source
    owns the renderer and closes it in :meth:`aclose`.
    """

    def __init__(self, client: SafeHttpClient, *, renderer: PageRenderer | None = None) -> None:
        self._client = client
        self._renderer = renderer
        self._start_lock = asyncio.Lock()  # concurrent fetches must start one browser, not many
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the renderer if there is one; calling it again is harmless.

        Waits for a renderer that is still starting and closes that one too. Afterwards a
        ``render: js`` fetch raises ``RuntimeError``; static fetches keep working.
        """
        async with self._start_lock:
            self._closed = True
            renderer, self._renderer = self._renderer, None
        if renderer is not None:
            await renderer.aclose()

    async def fetch(self, config: WebSource, /) -> list[Candidate]:
        """Fetch the page; raises :class:`FetchError` (``"not_html"``, ``"selector_not_found"``).

        ``[]`` for HTTP 304: the client only sends validators it remembers from an earlier
        fetch by the same client instance; they are not persisted across runs yet. Rendered
        pages (``render: js``) are always fetched in full. Safe to call concurrently.
        """
        if config.render == "js":
            return await self._fetch_rendered(config)
        url = str(config.url)
        result = await self._client.get(url)
        if isinstance(result, NotModified):
            return []
        if not _is_html(result.headers, result.content):
            raise FetchError("not_html", url=url)
        return _candidates(config, _decode(result), final_url=result.url)

    async def _fetch_rendered(self, config: WebSource) -> list[Candidate]:
        url = str(config.url)
        renderer = await self._renderer_for(url)
        rendered = await renderer.render(url, wait_for=config.wait_for)
        # The size is judged in UTF-8 like a response body; lone surrogates, which a page can
        # produce, are replaced here so that nothing downstream fails to encode the text.
        document = rendered.html.encode("utf-8", errors="replace")
        limit = self._client.max_response_bytes
        if len(document) > limit:
            raise TooLargeError(url=url, limit=limit)
        return _candidates(config, document.decode("utf-8"), final_url=rendered.url)

    async def _renderer_for(self, url: str) -> PageRenderer:
        async with self._start_lock:
            if self._closed:
                raise RuntimeError("WebPageSource is closed")
            if self._renderer is None:
                try:
                    module = importlib.import_module("invio.sources.browser")
                except ImportError as error:
                    # Only a missing Playwright means "not installed"; any other import error
                    # is a bug that must not be mistaken for it.
                    if (error.name or "").partition(".")[0] != "playwright":
                        raise
                    raise RenderUnavailableError(url=url) from error
                try:
                    self._renderer = await module.PlaywrightRenderer.start(self._client)
                except RenderUnavailableError as error:  # Chromium is missing: name the page
                    raise RenderUnavailableError(url=url) from error
                except FetchError as error:
                    if error.url:
                        raise
                    raise FetchError(error.reason, url=url) from error
            return self._renderer


def _candidates(config: WebSource, html: str, *, final_url: str) -> list[Candidate]:
    """Map a decoded document to the source's candidates (the same for static and rendered)."""
    url = str(config.url)
    tree = _parse(html)
    regions = _regions(tree, config.selector, url=url)
    page_url = canonical_url(url)
    if config.mode == "links":
        return _links(
            tree,
            regions,
            final_url=final_url,
            page_urls=frozenset({page_url, canonical_url(final_url)}),
            url_pattern=config.url_pattern,
        )
    text = _region_text(regions)
    return [
        Candidate(
            url=page_url,
            url_hash=url_hash(page_url),
            title=_title(tree, page_url),
            published_at=None,
            type="article",
            teaser=teaser(text),
            content_hash=_content_hash(text),
        )
    ]


def _decode(result: FetchResult) -> str:
    """Decode the body: BOM, then the header charset, then ``<meta>``, then UTF-8.

    The HTML standard's precedence without heuristics. Invalid bytes are replaced, so the same
    bytes always give the same text.
    """
    content = result.content
    for bom, bom_encoding in _BOMS:
        if content.startswith(bom):
            return result.text(bom_encoding)
    header = _HEADER_CHARSET.search(result.headers.get("content-type", ""))
    encoding = _web_encoding(header.group(1)) if header is not None else None
    if encoding == "utf-16":  # a header charset without a BOM: the standard says little-endian
        encoding = "utf-16-le"
    if encoding is None:
        meta = _META_CHARSET.search(content[:_META_PRESCAN_BYTES].decode("latin-1"))
        encoding = _web_encoding(meta.group(1)) if meta is not None else None
        # A declared UTF-16 cannot be right: the declaration itself was read as ASCII.
        if encoding is not None and encoding.startswith("utf-16"):
            encoding = None
    return result.text(encoding or "utf-8")


def _web_encoding(label: str) -> str | None:
    """The Python codec for a charset ``label``, if it is one of the web's encodings.

    Python knows codecs that are no text encodings of the web (``utf-7``, ``unicode_escape``,
    ``idna``, ...), which a page must not be able to select. Latin-1 and ASCII labels mean
    windows-1252, as in browsers. ``None`` for an unknown or disallowed label.
    """
    try:
        name = codecs.lookup(label.strip()).name
    except LookupError:
        return None
    if name in _LATIN1_NAMES:
        return "cp1252"
    return name if _WEB_ENCODINGS.fullmatch(name) else None


def _is_html(headers: Mapping[str, str], content: bytes) -> bool:
    """HTML by ``Content-Type``; without one, by a leading doctype or ``<html``."""
    content_type = headers.get("content-type", "")
    if content_type.strip():
        return content_type.partition(";")[0].strip().lower() in _HTML_TYPES
    head = content[:_SNIFF_BYTES].removeprefix(codecs.BOM_UTF8).lstrip().lower()
    return head.startswith(_HTML_START)


def _parse(html: str) -> LexborHTMLParser:
    """Parse ``html`` (HTML5 tree building) and remove the elements that hold no text."""
    tree = LexborHTMLParser(html)
    tree.strip_tags(_NON_TEXT_TAGS)
    return tree


def _regions(tree: LexborHTMLParser, selector: str | None, *, url: str) -> Sequence[LexborNode]:
    """The nodes whose text counts, in document order.

    ``<body>`` (else the whole document) without a ``selector``, else every match; a
    ``selector`` that matches nothing raises ``selector_not_found``. Matches nested in each
    other are all returned, so their text counts once per match: the result only has to be
    stable, and it is.
    """
    if selector is None:
        root = tree.body or tree.root
        return [] if root is None else [root]
    matches: Sequence[LexborNode] = tree.css(selector)
    if not matches:
        raise FetchError("selector_not_found", url=url)
    return matches


def _region_text(nodes: Sequence[LexborNode]) -> str:
    """Readable text of ``nodes``: NFC, whitespace collapsed, block elements separate words.

    Each node goes through the shared extractor as HTML (selectolax's own ``text()`` either
    merges paragraphs or splits inline words), and the nodes are joined by a space.
    """
    text = " ".join(html_to_text(node.html or "") for node in nodes)
    return collapse(unicodedata.normalize("NFC", text))


def _content_hash(text: str) -> str:
    """SHA-256 (hex) of the normalized ``text``; stable across runs and machines."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _title(tree: LexborHTMLParser, fallback: str) -> str:
    """The collapsed ``<title>`` of the document head, else ``fallback``."""
    node = tree.css_first("head > title")
    title = collapse(node.text()) if node is not None else ""
    return title or fallback


def _links(
    tree: LexborHTMLParser,
    regions: Sequence[LexborNode],
    *,
    final_url: str,
    page_urls: frozenset[str],
    url_pattern: str | None,
) -> list[Candidate]:
    """One candidate per qualifying link of the regions, in page order.

    A link qualifies when it resolves to a canonical http(s) URL that is not the page itself
    and either matches ``url_pattern`` (searched in the absolute URL, any host) or, without a
    pattern, is on the host of ``final_url``. The first occurrence of a URL wins.
    """
    base = _base_url(tree, final_url)
    pattern = re.compile(url_pattern) if url_pattern is not None else None
    host = urlsplit(final_url).hostname  # lower-cased
    seen = set(page_urls)
    candidates: list[Candidate] = []
    for anchor in _anchors(regions):
        url = http_url_or_none(anchor.attributes.get("href") or "", base)
        if url is None or url in seen:
            continue
        seen.add(url)  # a repeated URL gets the same verdict, so the first one decides
        if not (pattern.search(url) if pattern is not None else urlsplit(url).hostname == host):
            continue
        title = collapse(html_to_text(anchor.html or ""))
        candidates.append(
            Candidate(
                url=url,
                url_hash=url_hash(url),
                title=title or url,
                published_at=None,
                type="article",
                teaser=None,
                content_hash=None,
            )
        )
    return candidates


def _anchors(regions: Sequence[LexborNode]) -> Iterator[LexborNode]:
    """The ``a[href]`` elements in ``regions``, a region that is an ``<a>`` itself included."""
    for region in regions:
        if region.tag == "a" and "href" in region.attributes:
            yield region
        yield from region.css("a[href]")


def _base_url(tree: LexborHTMLParser, final_url: str) -> str:
    """The URL relative links resolve against: the first valid ``<base href>``, else the page."""
    node = tree.css_first("base[href]")
    if node is None:
        return final_url
    return http_url_or_none(node.attributes.get("href") or "", final_url) or final_url
