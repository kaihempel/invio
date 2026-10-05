"""Shared prompt helpers for untrusted document text."""

import re
from typing import Final

__all__ = ["neutralise"]

_OPEN: Final = chr(0x2039)  # single left angle quote, replaces "<" in neutralised tags
_CLOSE: Final = chr(0x203A)  # single right angle quote, replaces ">" in neutralised tags
# Any opening or closing delimiter tag, also with attributes, spaces around the slash or
# trailing text, and also unterminated (no ">"), which the template's own tag would complete.
# Each whitespace run has its own anchor ("<" or "/"): two adjacent runs would backtrack
# quadratically on "<" followed by a long run of spaces.
_DELIMITER = re.compile(r"<\s*(?:/\s*)?(?:document|title|content)\b[^<>]*>?", re.IGNORECASE)


def neutralise(text: str) -> str:
    """Swap the angle brackets of delimiter tags for single angle quotes (U+2039, U+203A).

    The text stays readable, but it can no longer open or close a delimiter.
    """
    return _DELIMITER.sub(lambda m: m[0].replace("<", _OPEN).replace(">", _CLOSE), text)
