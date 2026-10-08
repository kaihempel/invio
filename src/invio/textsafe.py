"""Make untrusted text (item titles, messages) safe to print to a terminal.

A leaf module: it imports nothing from ``invio``.
"""

import re
from typing import Final

__all__ = ["strip_control"]

# An ANSI escape sequence, removed whole so no stray "[31m" is left behind: CSI, and the string
# sequences OSC, DCS, SOS, PM and APC up to their terminator (BEL or ST). An unterminated string
# sequence is not matched, so it never swallows the rest of the text; its ESC is removed with the
# other control characters.
_ANSI: Final = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[\]PX^_][^\x07\x1b]*(?:\x07|\x1b\\))")
# C0 and C1 control characters, DEL, and the Unicode line/paragraph separators.
_CONTROL: Final = re.compile("[\x00-\x1f\x7f-\x9f\u2028\u2029]")

_CONTROL_KEEPING_LAYOUT: Final = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
# Invisible characters that can reorder or hide text: zero-width spaces and marks, bidi
# embeddings, overrides and isolates, invisible operators, the byte order mark and the Unicode
# tag characters (which can smuggle hidden text). Lone surrogates (from a JSON ``\ud800``) cannot
# be encoded on a UTF-8 stream, so they are removed too.
_INVISIBLE: Final = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff\ud800-\udfff\U000e0000-\U000e007f]"
)


def strip_control(text: str, *, limit: int | None = None, multiline: bool = False) -> str:
    """``text`` without ANSI escapes and control characters, whitespace-collapsed, cut at ``limit``.

    Newlines and tabs become single spaces, so a title can never start a new output line.
    With ``multiline`` (a document body) the text keeps its newlines and tabs and is neither
    collapsed nor cut: ``\\r\\n`` and a lone ``\\r`` become ``\\n``; ANSI escapes and every other
    C0, C1 and DEL character are removed. Bidi, zero-width and tag characters and lone
    surrogates are removed in both modes.
    """
    text = _INVISIBLE.sub("", _ANSI.sub("", text))
    if multiline:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        return _CONTROL_KEEPING_LAYOUT.sub("", text)
    cleaned = _CONTROL.sub(" ", text)
    cleaned = " ".join(cleaned.split())
    return cleaned if limit is None else cleaned[:limit]
