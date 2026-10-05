"""URL helpers shared by the source checks and the safe HTTP client."""

import re
from typing import Final

__all__ = ["redact", "redact_url", "without_query"]

# Greedy up to the last "@" before the path, so a password containing "@" is removed too. The
# userinfo ends at "/", "?" or "#", so an "@" in a query string or fragment is left alone.
# A leading "user:password@" in a string without "//", e.g. "user:secret@host/feed".
_LEADING_USERINFO: Final = re.compile(r"^[^/?#\s]*@")
_USERINFO: Final = re.compile(r"(?<=//)[^/?#\s]*@")
_QUERY: Final = re.compile(r"[?#].*", re.DOTALL)


def redact(text: str) -> str:
    """Remove ``user:password@`` from any URL in ``text``."""
    return _USERINFO.sub("", text)


def redact_url(url: str) -> str:
    """Remove userinfo from a string that is meant to be a URL, even if it lacks ``//``."""
    return _LEADING_USERINFO.sub("", redact(url))


def without_query(url: str) -> str:
    """``url`` up to its query or fragment, which often carry tokens or API keys."""
    return _QUERY.sub("", url)
