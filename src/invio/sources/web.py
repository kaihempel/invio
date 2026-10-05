"""Web page source adapter: one page in, one page candidate (or its article links) out.

Flow of one fetch: fetch the page with :class:`SafeHttpClient` (or render it in a headless
browser, ``render: js``) -> decode it -> check that it is HTML -> parse it and drop the
non-text elements -> pick the region (``<body>`` or the ``selector`` matches) -> readable text,
fingerprint and teaser (``mode: page``) or the links of the region (``mode: links``).

Pages are untrusted input: the document is decoded without ever failing, a response that is no
HTML is refused, and a ``selector`` that matches nothing is an error rather than an empty
fingerprint. The text rules (which elements hold no text, which separate words) are the ones
of the RSS source (:mod:`invio.sources.text`).
"""

import asyncio
import codecs
import hashlib
import importlib
import re
import unicodedata
from collections.abc import Iterator, Mapping, Sequence
from typing import Final, NamedTuple, Protocol, Self
from urllib.parse import urljoin, urlsplit

from selectolax.lexbor import LexborHTMLParser, LexborNode

from invio.config.job import WebSource
from invio.domain import Candidate, url_hash
from invio.sources.errors import FetchError, RenderUnavailableError, TooLargeError
from invio.sources.http import FetchResult, NotModified, SafeHttpClient, charset_label
from invio.sources.text import BLOCK_TAGS, NON_TEXT_TAGS, collapse, teaser
from invio.sources.urls import canonical_url, http_url_or_none

__all__ = ["PageRenderer", "RenderedPage", "WebPageSource"]

_HTML_TYPES: Final = frozenset({"text/html", "application/xhtml+xml"})
_BOMS: Final = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),  # the codec reads the byte order from the BOM
    (codecs.BOM_UTF16_BE, "utf-16"),
)
# ``<meta charset=x>`` and ``<meta http-equiv="Content-Type" content="text/html; charset=x">``.
_META_CHARSET: Final = re.compile(
    r"<meta[^>]*?charset\s*=\s*[\"']?\s*([^\s\"'/>;]+)", re.IGNORECASE
)
# Python's canonical codec names of the encodings of the WHATWG Encoding Standard that matter
# for pages, after ``_SUPERSETS`` has been applied.
_WEB_ENCODINGS: Final = re.compile(
    r"utf-8|utf-16(-le|-be)?|cp125[0-8]|cp866|cp874|iso8859-([2-9]|1[0-6])|koi8-[ru]"
    r"|mac-roman|mac-cyrillic|cp932|euc_jp|iso2022_jp|cp949|gbk|gb18030|big5hkscs|cp950"
)
# WHATWG labels that Python does not know, mapped to a Python codec name.
_EXTRA_LABELS: Final = {
    **dict.fromkeys(["csgb2312", "gb_2312", "gb_2312-80", "x-gbk"], "gbk"),
    **dict.fromkeys(["csksc56011987", "iso-ir-149", "ks_c_5601-1989", "ksc_5601"], "cp949"),
    **dict.fromkeys(["windows-949"], "cp949"),
    **dict.fromkeys(["logical", "csiso88598i"], "iso8859-8"),
    **dict.fromkeys(["x-mac-cyrillic", "x-mac-ukrainian"], "mac-cyrillic"),
    **dict.fromkeys(["x-mac-roman", "mac", "csmacintosh"], "mac-roman"),
    **dict.fromkeys(["x-euc-jp", "cseucpkdfmtjapanese"], "euc_jp"),
    **dict.fromkeys(["x-x-big5", "cn-big5"], "big5hkscs"),
    **dict.fromkeys(["koi8", "koi"], "koi8-r"),
    **dict.fromkeys(["unicode-1-1-utf-8", "unicode11utf8", "unicode20utf8"], "utf-8"),
    "x-sjis": "cp932",
    "dos-874": "cp874",
    "koi8-ru": "koi8-u",
}
# Encodings that browsers read as a superset (WHATWG): Latin-1 and ASCII as windows-1252,
# GB2312 as GBK, EUC-KR as windows-949, Shift_JIS as windows-31j, Big5 with the HKSCS
# extensions and TIS-620 as windows-874.
_SUPERSETS: Final = {
    "iso8859-1": "cp1252",
    "ascii": "cp1252",
    "gb2312": "gbk",
    "euc_kr": "cp949",
    "shift_jis": "cp932",
    "big5": "big5hkscs",
    "tis-620": "cp874",
    "iso8859-11": "cp874",
}
_META_PRESCAN_BYTES: Final = 1024  # the HTML standard's prescan window
_SNIFF_BYTES: Final = 512  # the MIME Sniffing standard's resource header
# Starts of an HTML resource without a Content-Type (MIME Sniffing standard, "text/html"):
# each tag name is followed by a space or ">"; a comment needs no such terminator.
_HTML_START: Final = re.compile(
    rb"<(?:!doctype html|html|head|script|iframe|h1|div|font|table|a|style|title|b|body|br|p)"
    rb"[\t\n\x0c\r >]|<!--",
    re.IGNORECASE,
)
_WHITESPACE: Final = b"\t\n\x0c\r "
# Fragments that are a route of a single-page application ("#/post/1", "#!/post/1"): unlike an
# anchor in the page, each one is a different article.
_ROUTE_FRAGMENT: Final = ("/", "!")


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
        # Playwright or Chromium is missing: later renders fail at once instead of retrying.
        self._unavailable = False

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
            if self._unavailable:
                raise RenderUnavailableError(url=url)
            if self._renderer is None:
                try:
                    module = importlib.import_module("invio.sources.browser")
                except ImportError as error:
                    # Only a missing Playwright package means "not installed"; a broken
                    # installation or any other import error must not be mistaken for it.
                    if error.name != "playwright":
                        raise
                    self._unavailable = True
                    raise RenderUnavailableError(url=url) from error
                try:
                    self._renderer = await module.PlaywrightRenderer.start(self._client)
                except RenderUnavailableError as error:  # Chromium is missing: name the page
                    self._unavailable = True
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
    label = charset_label(result.headers.get("content-type", ""))
    encoding = _web_encoding(label) if label is not None else None
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
    ``idna``, ...), which a page must not be able to select, and misses some WHATWG labels
    (``x-gbk``, ``windows-949``, ...). A label is read as browsers read it: legacy encodings as
    their WHATWG superset (``gb2312`` as GBK, Latin-1 as windows-1252, ...). ``None`` for an
    unknown or disallowed label.
    """
    label = label.strip().lower()
    try:
        name = codecs.lookup(_EXTRA_LABELS.get(label, label)).name
    except LookupError:
        return None
    name = _SUPERSETS.get(name, name)
    return name if _WEB_ENCODINGS.fullmatch(name) else None


def _is_html(headers: Mapping[str, str], content: bytes) -> bool:
    """HTML by ``Content-Type``; without one, by the start of the body (MIME sniffing).

    The start may follow a BOM (a UTF-16 body is read as such) and whitespace, and is a
    doctype, a comment or one of the tags the MIME Sniffing standard lists (``<html``,
    ``<head``, ``<body``, ``<p``, ...).
    """
    content_type = headers.get("content-type", "")
    if content_type.strip():
        return content_type.partition(";")[0].strip().lower() in _HTML_TYPES
    head = content[:_SNIFF_BYTES]
    for bom, bom_encoding in _BOMS:
        if head.startswith(bom):
            # The tag names are ASCII: re-encode the decoded start so one pattern fits all.
            text = head.decode(bom_encoding, errors="replace")
            head = text.encode("ascii", errors="replace")
            break
    return _HTML_START.match(head.lstrip(_WHITESPACE)) is not None


def _parse(html: str) -> LexborHTMLParser:
    """Parse ``html`` (HTML5 tree building) and remove the elements that hold no text."""
    tree = LexborHTMLParser(html)
    tree.strip_tags(sorted(NON_TEXT_TAGS))
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

    The nodes are joined by a space.
    """
    return _readable(" ".join(_node_text(node) for node in nodes))


def _readable(text: str) -> str:
    """``text`` in NFC with its whitespace collapsed: the form that is hashed and shown."""
    return collapse(unicodedata.normalize("NFC", text))


def _node_text(root: LexborNode) -> str:
    """The text nodes under ``root`` in document order; block elements leave a space.

    Read from the parsed tree itself (comments carry no text, and :func:`_parse` has removed
    the non-text elements), with the block elements of the RSS source
    (:data:`~invio.sources.text.BLOCK_TAGS`). selectolax's own ``text()`` either merges
    paragraphs or splits inline words. Iterative, so a deeply nested page cannot exhaust the
    stack.
    """
    parts: list[str] = []
    pending: list[tuple[LexborNode, bool]] = [(root, False)]  # (node, its end was reached)
    while pending:
        node, closing = pending.pop()
        if node.is_text_node:
            parts.append(node.text_content or "")
            continue
        if node.tag in BLOCK_TAGS:
            parts.append(" ")
        if closing or not (node.is_element_node or node is root):
            continue
        pending.append((node, True))
        pending.extend((child, False) for child in reversed(list(node.iter(include_text=True))))
    return "".join(parts)


def _content_hash(text: str) -> str:
    """SHA-256 (hex) of the normalized ``text``; stable across runs and machines.

    The text comes from selectolax's tree alone (no second parser), so only a selectolax
    upgrade that parses a page differently can change the hash of an unchanged page; the golden
    hashes in the tests catch that before a release does.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _title(tree: LexborHTMLParser, fallback: str) -> str:
    """The collapsed ``<title>`` of the document head, else ``fallback``."""
    node = tree.css_first("head > title")
    title = _readable(node.text()) if node is not None else ""
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
        url = _link_url(anchor.attributes.get("href") or "", base)
        if url is None or url in seen:
            continue
        seen.add(url)  # a repeated URL gets the same verdict, so the first one decides
        if not (pattern.search(url) if pattern is not None else urlsplit(url).hostname == host):
            continue
        title = _readable(_node_text(anchor))
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


def _link_url(href: str, base: str) -> str | None:
    """The canonical URL of a link, keeping a single-page-app route fragment (``#/post/1``).

    :func:`~invio.sources.urls.canonical_url` drops every fragment, which would turn all links
    of a fragment-routed index page into the page itself.
    """
    url = http_url_or_none(href, base)
    if url is None:
        return None
    fragment = urlsplit(urljoin(base, href.strip())).fragment
    return f"{url}#{fragment}" if fragment.startswith(_ROUTE_FRAGMENT) else url


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
