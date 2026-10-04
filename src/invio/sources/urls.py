"""URL helpers shared by the source checks and the safe HTTP client."""

import re
from typing import Final

__all__ = ["redact"]

# Greedy up to the last "@" before the path, so a password containing "@" is removed too. The
# userinfo ends at "/", "?" or "#", so an "@" in a query string or fragment is left alone.
_USERINFO: Final = re.compile(r"(?<=//)[^/?#\s]*@")


def redact(text: str) -> str:
    """Remove ``user:password@`` from any URL in ``text``."""
    return _USERINFO.sub("", text)
