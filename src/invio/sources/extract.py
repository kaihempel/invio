"""Article text extraction: the HTML of an article page in, its main text and metadata out.

Flow of one extraction: trafilatura (precision mode, metadata on, comments off) -> if it finds
no text or fails on the input, the plain text of ``<body>`` with the rules of the web page
source (:func:`~invio.sources.text.node_text`) -> normalize (NFC, whitespace collapsed per
line, blank lines dropped) -> too short is an :class:`ExtractionError` -> cut to ``max_chars``.

Title, publish date and language come from trafilatura where it found them; otherwise the
title is ``og:title`` or ``<title>`` and the language the primary subtag of ``<html lang>``.
The input is untrusted HTML that was already fetched: extraction never touches the network,
and malformed input degrades to the fallback text instead of raising. Input longer than
``max_input_chars`` is rejected before parsing, since extraction time grows with the page size.
Extraction is synchronous and CPU-bound: async callers run it off the event loop
(``asyncio.to_thread``).
"""

import functools
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Final

import trafilatura
from selectolax.lexbor import LexborHTMLParser
from trafilatura.settings import Document

from invio.sources.text import node_text, parse_html, readable
from invio.sources.urls import redact_url, without_query

__all__ = [
    "MAX_CHARS",
    "MAX_INPUT_CHARS",
    "MIN_CHARS",
    "ExtractedText",
    "ExtractionError",
    "extract_text",
]

MAX_CHARS: Final = 200_000
"""Longest text kept from one article; longer texts are cut and marked ``truncated``."""
MIN_CHARS: Final = 200
"""Shortest text that counts as an article; shorter pages raise :class:`ExtractionError`."""
MAX_INPUT_CHARS: Final = 4_000_000
"""Longest HTML accepted; longer input raises :class:`ExtractionError` before parsing."""

# An ISO 639 primary language subtag ("de" of "de-DE"); the 4-8 letter BCP 47 subtags are
# reserved or registered names, not languages, and count as no language.
_LANGUAGE: Final = re.compile(r"[a-z]{2,3}")

_log = logging.getLogger("invio.sources.extract")


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
    """No usable text; ``reason`` is a short machine-readable code.

    ``"too_short"``: the text is shorter than ``min_chars``. ``"too_large"``: the HTML is longer
    than ``max_input_chars``. ``url`` is stored redacted, and the message leaves out query and
    fragment, like :class:`~invio.sources.errors.FetchError`.
    """

    def __init__(self, reason: str, *, url: str) -> None:
        self.url = redact_url(url)
        self.reason = reason
        super().__init__(f"{reason}: {without_query(self.url)}")


def extract_text(
    html: str,
    url: str,
    *,
    max_chars: int = MAX_CHARS,
    min_chars: int = MIN_CHARS,
    max_input_chars: int = MAX_INPUT_CHARS,
) -> ExtractedText:
    """Extract the main text, title, publish date and language of the article page ``html``.

    ``url`` is the page's URL (trafilatura uses it for metadata; errors name it). Raises
    :class:`ExtractionError` (``"too_large"``) when ``html`` is longer than ``max_input_chars``
    characters, (``"too_short"``) when the text has fewer than ``min_chars`` characters, and
    ``ValueError`` when a limit is not positive or ``min_chars`` exceeds ``max_chars``.
    """
    if max_chars < 1 or min_chars < 1 or max_input_chars < 1:
        raise ValueError("max_chars, min_chars and max_input_chars must be positive")
    if min_chars > max_chars:
        raise ValueError("min_chars must not exceed max_chars")
    if len(html) > max_input_chars:
        raise ExtractionError("too_large", url=url)
    document = _bare_extraction(html, url)
    # The tree serves only the fallbacks; when trafilatura found everything it is never built.
    tree = functools.cache(lambda: parse_html(html))
    text = _normalize(document.text or "") if document is not None else ""
    if not text:
        root = tree().body or tree().root
        text = _normalize(node_text(root, separator="\n")) if root is not None else ""
    if len(text) < min_chars:
        raise ExtractionError("too_short", url=url)
    text, truncated = _truncate(text, max_chars)
    return ExtractedText(
        title=_title(document, tree),
        text=text,
        published_at=_published_at(document.date if document is not None else None),
        language=_language(document.language if document is not None else None)
        or _language(_html_lang(tree())),
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
    except Exception as exc:
        # The type only: the message may quote the page. Visible when an upgrade breaks it.
        _log.debug("trafilatura failed, using the body text: %s", type(exc).__name__)
        return None
    # Without ``as_dict=True`` the result is a Document; the dict branch is never taken.
    return result if isinstance(result, Document) else None


def _normalize(text: str) -> str:
    """``text`` in NFC, whitespace collapsed within each line, blank lines dropped."""
    lines = (readable(line) for line in text.splitlines())
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


def _title(document: Document | None, tree: Callable[[], LexborHTMLParser]) -> str | None:
    """trafilatura's title, else ``og:title``, else ``<title>``; ``None`` when all are blank.

    ``tree`` is called only when trafilatura's title is blank.
    """
    title = readable(document.title or "") if document is not None else ""
    if title:
        return title
    og_title = tree().css_first('meta[property="og:title"]')
    head_title = tree().css_first("head > title")
    candidates = (
        og_title.attributes.get("content") if og_title is not None else None,
        head_title.text() if head_title is not None else None,
    )
    return next((title for c in candidates if (title := readable(c or ""))), None)


def _published_at(value: str | None) -> datetime | None:
    """Midnight UTC of the ISO date that ``value`` starts with; ``None`` if it is not one.

    trafilatura gives ``YYYY-MM-DD``; a time after the date (``YYYY-MM-DDTHH:MM``) is ignored.
    """
    if not value:
        return None
    try:
        day = date.fromisoformat(value[:10])
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
