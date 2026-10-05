"""URL helpers shared by the source checks, the safe HTTP client and the source adapters."""

import re
from typing import Final
from urllib.parse import unquote, urlsplit, urlunsplit

__all__ = ["canonical_url", "redact", "redact_url", "without_query"]

# Greedy up to the last "@" before the path, so a password containing "@" is removed too. The
# userinfo ends at "/", "?" or "#", so an "@" in a query string or fragment is left alone.
# A leading "user:password@" in a string without "//", e.g. "user:secret@host/feed".
_LEADING_USERINFO: Final = re.compile(r"^[^/?#\s]*@")
_USERINFO: Final = re.compile(r"(?<=//)[^/?#\s]*@")
_QUERY: Final = re.compile(r"[?#].*", re.DOTALL)
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}
# Click and campaign identifiers that do not change the page; ``utm_*`` is matched by prefix.
_TRACKING_PARAMS: Final = frozenset(
    {"fbclid", "gclid", "dclid", "gbraid", "wbraid", "msclkid", "yclid", "igshid"}
    | {"mc_cid", "mc_eid"}  # Mailchimp
)


def redact(text: str) -> str:
    """Remove ``user:password@`` from any URL in ``text``."""
    return _USERINFO.sub("", text)


def redact_url(url: str) -> str:
    """Remove userinfo from a string that is meant to be a URL, even if it lacks ``//``."""
    return _LEADING_USERINFO.sub("", redact(url))


def without_query(url: str) -> str:
    """``url`` up to its query or fragment, which often carry tokens or API keys."""
    return _QUERY.sub("", url)


def canonical_url(url: str) -> str:
    """Canonical form of ``url`` for identity: same page, same string, same ``url_hash``.

    Lower-cases the scheme and host, drops userinfo (credentials must never end up in a stored
    URL), the default port, the fragment and tracking parameters (``utm_*``, ``fbclid``,
    ``gclid``, ...). The remaining query parameters keep their order and their exact encoding;
    an empty path of an http(s) URL becomes ``/``. Percent-encoding case (``%7e``/``%7E``) and
    trailing slashes are left as they are, so such variants stay distinct.
    Raises ``ValueError`` for a URL that cannot be split (e.g. an unclosed IPv6 bracket).
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = parts.netloc.rpartition("@")[2].lower().removesuffix(":")
    try:
        port = parts.port
    except ValueError:
        port = None  # not a number: leave the host part as it is
    if port is not None and port == _DEFAULT_PORTS.get(scheme):
        host = host[: host.rfind(":")]
    path = parts.path or ("/" if scheme in _DEFAULT_PORTS and host else "")
    query = "&".join(param for param in parts.query.split("&") if param and not _is_tracking(param))
    return urlunsplit((scheme, host, path, query, ""))


def _is_tracking(param: str) -> bool:
    name = unquote(param.partition("=")[0]).lower()
    return name.startswith("utm_") or name in _TRACKING_PARAMS
