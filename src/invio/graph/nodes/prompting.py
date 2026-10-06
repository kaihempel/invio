"""Shared prompt helpers: untrusted document text, the research interest and the language."""

import re
from typing import Final

from invio.config.languages import language_name

__all__ = [
    "MAX_TITLE_CHARS",
    "document_message",
    "interest_section",
    "language_instruction",
    "neutralise",
]

MAX_TITLE_CHARS: Final = 500  # title characters sent; a feed title can be arbitrarily long

_OPEN: Final = chr(0x2039)  # single left angle quote, replaces "<" in neutralised tags
_CLOSE: Final = chr(0x203A)  # single right angle quote, replaces ">" in neutralised tags
# Any opening or closing delimiter tag, also with attributes, spaces around the slash or
# trailing text, and also unterminated (no ">"), which the template's own tag would complete.
# Each whitespace run has its own anchor ("<" or "/"): two adjacent runs would backtrack
# quadratically on "<" followed by a long run of spaces.
_DELIMITER = re.compile(r"<\s*(?:/\s*)?(?:document|title|content)\b[^<>]*>?", re.IGNORECASE)


def neutralise(text: str) -> str:
    """Swap the angle brackets of delimiter tags for single angle quotes (U+2039, U+203A).

    The text stays readable, but it can no longer open or close a delimiter. Length-preserving
    and idempotent.
    """
    return _DELIMITER.sub(lambda m: m[0].replace("<", _OPEN).replace(">", _CLOSE), text)


def document_message(title: str, content: str) -> str:
    """Return the user message: one ``<document>`` block with neutralised title and content.

    The title is cut to ``MAX_TITLE_CHARS`` after neutralising, so the cut never lands inside a
    real tag. ``content`` is sent as given apart from neutralising; callers bound its size.
    """
    return (
        f"<document>\n<title>{neutralise(title)[:MAX_TITLE_CHARS]}</title>\n"
        f"<content>{neutralise(content)}</content>\n</document>"
    )


def interest_section(interest: str) -> str:
    """Return the job's research interest as the ``<interest>`` system-message section."""
    return f"<interest>{interest}</interest>"


def language_instruction(language: str) -> str:
    """Return the instruction to write all text in ``language`` (an ISO 639-1 code).

    Raises:
        KeyError: ``language`` is not an ISO 639-1 code (the name comes from the closed table).
    """
    return f'Write all text in {language_name(language)} (ISO 639-1 code "{language}").'
