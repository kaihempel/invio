"""Article text extraction: the HTML of an article page in, its main text and metadata out.

Flow of one extraction: trafilatura (precision mode, metadata on, comments off) -> if it finds
no text or fails on the input, the plain text of ``<body>`` with the rules of the web page
source (:func:`~invio.sources.text.node_text`) -> normalize (NFC, whitespace collapsed per
line, blank lines dropped) -> too short is an :class:`ExtractionError` -> cut to ``max_chars``.

Title, publish date and language come from trafilatura where it found them; otherwise the
title is ``og:title`` or ``<title>`` and the language the primary subtag of ``<html lang>``.
The input is untrusted HTML that was already fetched: extraction never touches the network,
and malformed input degrades to the fallback text instead of raising.
"""

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

import trafilatura
from selectolax.lexbor import LexborHTMLParser
from trafilatura.settings import Document

from invio.sources.text import collapse, node_text, parse_html
from invio.sources.urls import redact_url, without_query

__all__ = ["MAX_CHARS", "MIN_CHARS", "ExtractedText", "ExtractionError", "extract_text"]

MAX_CHARS: Final = 200_000
"""Longest text kept from one article; longer texts are cut and marked ``truncated``."""
MIN_CHARS: Final = 200
"""Shortest text that counts as an article; shorter pages raise :class:`ExtractionError`."""

# A BCP 47 primary language subtag ("de" of "de-DE"); anything else is no language.
_LANGUAGE: Final = re.compile(r"[a-z]{2,8}")


@dataclass(frozen=True, kw_only=True, slots=True)
class ExtractedText:
    """The main text of an article page and what is known about it.

    ``text`` is NFC with one paragraph per line. ``published_at`` is midnight UTC of the publish
    day (trafilatura dates have no time). ``language`` is a lower-case primary subtag (``"de"``).
    """

    title: str | None
    text: str
    published_at: datetime | None
    language: str | None
    truncated: bool


class ExtractionError(Exception):
    """No usable text; ``reason`` is a short machine-readable code (``"too_short"``).

    ``url`` is stored redacted, and the message leaves out query and fragment, like
    :class:`~invio.sources.errors.FetchError`.
    """

    def __init__(self, reason: str, *, url: str) -> None:
        self.url = redact_url(url)
        self.reason = reason
        super().__init__(f"{reason}: {without_query(self.url)}")


def extract_text(
    html: str, url: str, *, max_chars: int = MAX_CHARS, min_chars: int = MIN_CHARS
) -> ExtractedText:
    """Extract the main text, title, publish date and language of the article page ``html``.

    ``url`` is the page's URL (trafilatura uses it for metadata; errors name it). Raises
    :class:`ExtractionError` (``"too_short"``) when the text has fewer than ``min_chars``
    characters, and ``ValueError`` when ``max_chars`` or ``min_chars`` is not positive or
    ``min_chars`` exceeds ``max_chars``.
    """
    if max_chars < 1 or min_chars < 1:
        raise ValueError("max_chars and min_chars must be positive")
    if min_chars > max_chars:
        raise ValueError("min_chars must not exceed max_chars")
    document = _bare_extraction(html, url)
    tree = parse_html(html)
    text = _normalize(document.text or "") if document is not None else ""
    if not text:
        root = tree.body or tree.root
        text = _normalize(node_text(root, separator="\n")) if root is not None else ""
    if len(text) < min_chars:
        raise ExtractionError("too_short", url=url)
    text, truncated = _truncate(text, max_chars)
    return ExtractedText(
        title=_title(document, tree),
        text=text,
        published_at=_published_at(document.date if document is not None else None),
        language=_language(document.language if document is not None else None)
        or _language(_html_lang(tree)),
        truncated=truncated,
    )


def _bare_extraction(html: str, url: str) -> Document | None:
    """trafilatura's result for ``html``; ``None`` when it finds nothing or fails.

    Any exception counts as "nothing found": the input is untrusted, and the fallback text is
    better than no text.
    """
    try:
        result = trafilatura.bare_extraction(
            html, url=url, favor_precision=True, with_metadata=True, include_comments=False
        )
    except Exception:
        return None
    # Without ``as_dict=True`` the result is a Document; the dict branch is never taken.
    return result if isinstance(result, Document) else None


def _normalize(text: str) -> str:
    """``text`` in NFC, whitespace collapsed within each line, blank lines dropped."""
    lines = (collapse(line) for line in unicodedata.normalize("NFC", text).splitlines())
    return "\n".join(line for line in lines if line)


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """``text`` cut to ``max_chars`` and whether it was cut.

    The cut falls on a word or line boundary where possible; a single word longer than the
    limit is cut inside the word.
    """
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    if not text[max_chars].isspace():  # the cut splits a word: drop its head
        boundary = max(cut.rfind(" "), cut.rfind("\n"))
        if boundary > 0:
            cut = cut[:boundary]
    return cut.rstrip(), True


def _title(document: Document | None, tree: LexborHTMLParser) -> str | None:
    """trafilatura's title, else ``og:title``, else ``<title>``; ``None`` when all are blank."""
    candidates: list[str | None] = [document.title if document is not None else None]
    og_title = tree.css_first('meta[property="og:title"]')
    if og_title is not None:
        candidates.append(og_title.attributes.get("content"))
    head_title = tree.css_first("head > title")
    if head_title is not None:
        candidates.append(head_title.text())
    for candidate in candidates:
        title = collapse(unicodedata.normalize("NFC", candidate or ""))
        if title:
            return title
    return None


def _published_at(value: str | None) -> datetime | None:
    """Midnight UTC of the ISO date ``value`` (``YYYY-MM-DD``); ``None`` if it is not one."""
    if not value:
        return None
    try:
        day = date.fromisoformat(value)
    except ValueError:
        return None
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def _html_lang(tree: LexborHTMLParser) -> str | None:
    """The ``lang`` attribute of the root ``<html>`` element, if any."""
    root = tree.css_first("html")
    return root.attributes.get("lang") if root is not None else None


def _language(value: str | None) -> str | None:
    """The lower-case primary subtag of the language tag ``value`` (``"de-DE"`` -> ``"de"``)."""
    primary = (value or "").strip().replace("_", "-").partition("-")[0].lower()
    return primary if _LANGUAGE.fullmatch(primary) else None
