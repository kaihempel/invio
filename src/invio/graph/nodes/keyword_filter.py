"""Keyword prefilter: drop obviously irrelevant items before any LLM token is spent.

The rules come from the job config ``search.keywords``. Text and terms are compared in their
canonical caseless form (NFD, ``casefold()``, NFC), so case and composed/decomposed umlauts do
not matter. A term only matches as a whole word: it must not be preceded or followed by a word
character (Unicode ``\\w``) or a combining mark, which also works for terms such as ``C++``. A
multi-word term matches as a phrase with any run of whitespace (line breaks included) between
its words.

Rules, in order: any ``exclude`` hit rejects; every ``all`` term must occur; ``any`` needs at
least one hit when it is non-empty; with both ``any`` and ``all`` empty everything passes
(unless excluded). Terms are stripped and blank terms are ignored.

Known limitations:

- Folding is Unicode default case folding without language tailoring, so the Turkish dotted
  ``İ`` does not match a plain ``i`` (``İstanbul`` matches ``İSTANBUL`` but not ``istanbul``).
- Umlauts are not stripped to their base letters: ``Äpfel`` does not match ``Apfel``.
- A term that starts or ends with a non-word character needs a non-word neighbour on that
  side: ``.NET`` matches ``use .NET`` but not ``x.NET``, and ``-foo`` matches ``a -foo`` but not
  ``a-foo``.
"""

import functools
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from invio.config.job import KeywordsConfig
from invio.db.models import Item
from invio.db.repositories import ItemRepository
from invio.domain import ItemStatus

__all__ = ["MatchResult", "item_text", "keyword_filter", "matches"]


@dataclass(frozen=True, kw_only=True, slots=True)
class MatchResult:
    """Outcome of the keyword rules for one text.

    ``hits`` are the configured terms (spelled as in the config, stripped) that occur in the
    text, in the config order ``any``, ``all``, ``exclude``. A term listed more than once, also
    in another spelling of the same case-folded form, is reported once (first spelling wins).
    """

    matched: bool
    hits: list[str]


def _fold(text: str) -> str:
    """Return the canonical caseless form of ``text`` (Unicode ``NFC(casefold(NFD(text)))``)."""
    return unicodedata.normalize("NFC", unicodedata.normalize("NFD", text).casefold())


def _is_mark(char: str) -> bool:
    """Return whether ``char`` is a combining mark (Unicode category ``M*``)."""
    return unicodedata.category(char).startswith("M")


@functools.lru_cache(maxsize=1024)
def _pattern(folded_term: str) -> re.Pattern[str]:
    """Compile the whole-word, whitespace-tolerant pattern of a folded, non-blank term."""
    phrase = r"\s+".join(re.escape(word) for word in folded_term.split())
    return re.compile(rf"(?<!\w){phrase}(?!\w)")


def _occurs(term: str, folded: str) -> bool:
    """Return whether ``term`` occurs as a whole word in the folded text ``folded``.

    ``\\w`` does not cover combining marks, so a match next to one (e.g. ``x`` in ``x\\u0301``,
    a letter without a precomposed form) is inside a word and does not count.
    """
    pattern = _pattern(_fold(term))
    pos = 0
    while match := pattern.search(folded, pos):
        start, end = match.span()
        neighbours = folded[max(start - 1, 0) : start] + folded[end : end + 1]
        if not any(_is_mark(char) for char in neighbours):
            return True
        pos = start + 1
    return False


def _terms(terms: Iterable[str]) -> list[str]:
    """Return the stripped, non-blank ``terms``."""
    return [stripped for term in terms if (stripped := term.strip())]


def matches(text: str, keywords: KeywordsConfig) -> MatchResult:
    """Apply the keyword rules of ``keywords`` to ``text`` (pure, no side effects)."""
    folded = _fold(text)

    def hits_of(terms: list[str]) -> list[str]:
        return [term for term in terms if _occurs(term, folded)]

    any_terms = _terms(keywords.any)
    all_terms = _terms(keywords.all)
    any_hits = hits_of(any_terms)
    all_hits = hits_of(all_terms)
    exclude_hits = hits_of(_terms(keywords.exclude))

    matched = (
        not exclude_hits and len(all_hits) == len(all_terms) and (not any_terms or bool(any_hits))
    )
    hits: dict[str, str] = {}
    for term in (*any_hits, *all_hits, *exclude_hits):
        hits.setdefault(_fold(term), term)
    return MatchResult(matched=matched, hits=list(hits.values()))


def item_text(title: str | None, teaser: str | None, text: str | None) -> str:
    """Join title, teaser and extracted text with newlines; ``None`` and blank parts are skipped."""
    return "\n".join(part for part in (title, teaser, text) if part and part.strip())


def keyword_filter(item: Item, keywords: KeywordsConfig, items: ItemRepository) -> MatchResult:
    """Apply the keyword rules to a stored item; a rejected item gets ``skipped_keyword``.

    The text is the item's title, teaser and extracted text (``raw_content``). A passing item
    keeps its status. The status change is flushed, not committed (see ``ItemRepository``).
    """
    result = matches(item_text(item.title, item.teaser, item.raw_content), keywords)
    if not result.matched:
        items.set_status(item, ItemStatus.SKIPPED_KEYWORD)
    return result
