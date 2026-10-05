"""Readable-text helpers shared by the RSS and web page sources.

Feed summaries and web pages are untrusted HTML. These helpers reduce a fragment to plain
text with one set of rules, so a feed and a page give the same text for the same markup:
comments and the content of ``script``/``style``/``noscript``/``template`` are dropped,
character references are decoded and block elements separate words.
"""

from html.parser import HTMLParser
from typing import Final

__all__ = ["TEASER_MAX_CHARS", "collapse", "html_to_text", "teaser"]

TEASER_MAX_CHARS: Final = 500
"""Longest teaser kept from a summary or a page text, ellipsis included."""

_ELLIPSIS: Final = "…"
# Elements whose content is code or fallback markup, not text.
_NON_TEXT_TAGS: Final = frozenset({"script", "style", "noscript", "template"})
# Elements that separate words: "<p>a</p><p>b</p>" reads "a b", while "wo<b>rd</b>" stays "word".
_BLOCK_TAGS: Final = frozenset(
    {"address", "article", "aside", "blockquote", "br", "dd", "div", "dl", "dt", "figcaption"}
    | {"figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "ol", "p"}
    | {"pre", "section", "table", "td", "th", "tr", "ul"}
)


def collapse(text: str) -> str:
    """``text`` with every run of whitespace (NBSP and other Unicode spaces too) as one space."""
    return " ".join(text.split())


def teaser(text: str) -> str | None:
    """Whitespace-collapsed ``text`` cut at a word boundary to :data:`TEASER_MAX_CHARS`.

    ``None`` when nothing but whitespace is left.
    """
    text = collapse(text)
    if not text:
        return None
    if len(text) <= TEASER_MAX_CHARS:
        return text
    cut = text[: TEASER_MAX_CHARS - len(_ELLIPSIS)]
    head, space, _ = cut.rpartition(" ")
    return (head if space and head else cut).rstrip() + _ELLIPSIS


class _TextExtractor(HTMLParser):
    """Collects the text nodes of an HTML fragment; character references are decoded.

    The content of ``script``/``style``/``noscript``/``template`` elements is dropped, and
    block elements separate words.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._non_text_depth = 0

    def handle_data(self, data: str) -> None:
        if not self._non_text_depth:
            self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _NON_TEXT_TAGS:
            self._non_text_depth += 1
        else:
            self._separate(tag)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._separate(tag)  # "<br/>"; a self-closed "<script/>" opens nothing

    def handle_endtag(self, tag: str) -> None:
        if tag in _NON_TEXT_TAGS:
            self._non_text_depth = max(self._non_text_depth - 1, 0)
        else:
            self._separate(tag)

    def _separate(self, tag: str) -> None:
        if tag in _BLOCK_TAGS and not self._non_text_depth:
            self.parts.append(" ")


def html_to_text(html: str) -> str:
    """The text of an HTML fragment; block elements leave a space, comments leave nothing.

    Whitespace is not collapsed: pass the result through :func:`collapse`.
    """
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    return "".join(extractor.parts)
