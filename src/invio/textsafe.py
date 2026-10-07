"""Make untrusted text (item titles, messages) safe to print to a terminal.

A leaf module: it imports nothing from ``invio``.
"""

import re
from typing import Final

__all__ = ["strip_control"]

# An ANSI escape sequence (CSI and OSC), removed whole so no stray "[31m" is left behind.
_ANSI: Final = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?)")
# C0 and C1 control characters, DEL, and the Unicode line/paragraph separators.
_CONTROL: Final = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")

_CONTROL_KEEPING_LAYOUT: Final = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")


def strip_control(text: str, *, limit: int | None = None, multiline: bool = False) -> str:
    """``text`` without ANSI escapes and control characters, whitespace-collapsed, cut at ``limit``.

    Newlines and tabs become single spaces, so a title can never start a new output line.
    With ``multiline`` (a document body) the text keeps its newlines and tabs and is neither
    collapsed nor cut: ``\r\n`` and a lone ``\r`` become ``\n``; ANSI escapes and every other
    C0, C1 and DEL character are removed.
    """
    if multiline:
        text = _ANSI.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
        return _CONTROL_KEEPING_LAYOUT.sub("", text)
    cleaned = _CONTROL.sub(" ", _ANSI.sub("", text))
    cleaned = " ".join(cleaned.split())
    return cleaned if limit is None else cleaned[:limit]
