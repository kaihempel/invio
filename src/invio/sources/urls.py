"""URL helpers shared by the source checks, the safe HTTP client and the source adapters."""

import re
from typing import Final
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

__all__ = [
    "DEFAULT_PORTS",
    "WEB_SCHEMES",
    "canonical_url",
    "http_url_or_none",
    "origin",
    "redact",
    "redact_url",
    "without_query",
]

DEFAULT_PORTS: Final = {"http": 80, "https": 443}
"""The schemes the sources fetch, with their default ports."""
WEB_SCHEMES: Final = frozenset(DEFAULT_PORTS)

# Greedy up to the last "@" before the path, so a password containing "@" is removed too. The
# userinfo ends at "/", "?" or "#", so an "@" in a query string or fragment is left alone.
# A leading "user:password@" in a string without "//", e.g. "user:secret@host/feed".
_LEADING_USERINFO: Final = re.compile(r"^[^/?#\s]*@")
_USERINFO: Final = re.compile(r"(?<=//)[^/?#\s]*@")
_QUERY: Final = re.compile(r"[?#].*", re.DOTALL)
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
    if port is not None and port == DEFAULT_PORTS.get(scheme):
        host = host[: host.rfind(":")]
    path = parts.path or ("/" if scheme in DEFAULT_PORTS and host else "")
    query = "&".join(param for param in parts.query.split("&") if param and not _is_tracking(param))
    return urlunsplit((scheme, host, path, query, ""))


def http_url_or_none(raw: str, base: str) -> str | None:
    """``raw`` resolved against ``base`` and canonicalized, if it is a usable http(s) URL.

    ``None`` for another scheme (``mailto:``, ``javascript:``, ...), a URL without a host and
    one that cannot be parsed (an unclosed IPv6 bracket, an invalid port). Used for links
    found in untrusted feeds and pages.
    """
    try:
        url = canonical_url(urljoin(base, raw.strip()))
        parts = urlsplit(url)
        # ``hostname`` (unlike ``netloc``) is empty for "http://user@" and "http://:80"; an
        # invalid port raises ``ValueError``.
        if parts.scheme not in WEB_SCHEMES or not parts.hostname or parts.port == 0:
            return None
    except ValueError:  # e.g. an unclosed IPv6 bracket or an invalid port
        return None
    return url


def origin(url: str) -> tuple[str, str | None, int | None]:
    """Scheme, lower-cased host and effective port of ``url``: what "same origin" compares."""
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    return scheme, parts.hostname, parts.port or DEFAULT_PORTS.get(scheme)


def _is_tracking(param: str) -> bool:
    name = unquote(param.partition("=")[0]).lower()
    return name.startswith("utm_") or name in _TRACKING_PARAMS
