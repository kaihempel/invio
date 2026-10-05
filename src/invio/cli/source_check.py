"""Reachability and feed checks for sources entered in the job wizard."""

import http.client
import socket
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Final, Protocol, cast
from xml.etree.ElementTree import Element, ParseError, XMLPullParser

import invio
from invio.sources.urls import redact

__all__ = ["CheckResult", "HttpSourceChecker", "SourceChecker", "is_feed_document"]

_FEED_ROOTS: Final = frozenset(
    {
        "rss",
        "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF",
        "{http://www.w3.org/2005/Atom}feed",
    }
)
_CHUNK: Final = 8192


@dataclass(frozen=True, slots=True)
class CheckResult:
    """Outcome of checking one source URL."""

    reachable: bool
    status: int | None
    reason: str | None
    is_feed: bool | None


class SourceChecker(Protocol):
    """Checks a source URL; implementations never raise for network problems."""

    def check(self, url: str, *, expect_feed: bool) -> CheckResult: ...


def is_feed_document(head: bytes) -> bool:
    """Return True if the first XML start element is an RSS, RDF or Atom feed root."""
    parser: XMLPullParser[Element] = XMLPullParser(events=("start",))
    try:
        parser.feed(head.removeprefix(b"\xef\xbb\xbf").lstrip())
        for event in parser.read_events():
            # typeshed types read_events() as a union of all event kinds; "start" yields Element.
            _kind, element = cast("tuple[str, Element]", event)
            return element.tag in _FEED_ROOTS
    except ParseError:
        return False
    return False


def _opener() -> urllib.request.OpenerDirector:
    """An opener limited to http(s): no ``file:``, ``ftp:`` or ``data:`` handlers."""
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler(),
        urllib.request.HTTPHandler(),
        urllib.request.HTTPSHandler(),
        urllib.request.HTTPRedirectHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
        urllib.request.UnknownHandler(),  # unknown schemes -> URLError, not None
    ):
        opener.add_handler(handler)
    return opener


class HttpSourceChecker:
    """Check sources over HTTP(S) with the standard library.

    Sends HEAD first, or GET when a feed check is wanted (or HEAD fails with any HTTP error,
    since many servers reject HEAD although GET works), follows
    redirects and reads at most ``max_bytes`` of the body within ``timeout`` seconds in total.
    The socket timeout is per operation, so the body loop also enforces a total deadline.

    Meant for a local operator CLI only: it can reach internal addresses, honours the proxy
    environment variables and must not be used from server-side code paths.
    """

    def __init__(self, *, timeout: float = 10.0, max_bytes: int = 65_536) -> None:
        self.timeout = timeout
        self.max_bytes = max_bytes

    def check(self, url: str, *, expect_feed: bool) -> CheckResult:
        """Check ``url``; every failure becomes ``CheckResult(reachable=False, reason=...)``."""
        try:
            return self._check(url, expect_feed)
        except urllib.error.HTTPError as exc:
            return CheckResult(False, exc.code, f"HTTP {exc.code}", None)
        except urllib.error.URLError as exc:
            return CheckResult(False, None, self._url_error_reason(exc), None)
        except TimeoutError:
            return CheckResult(False, None, self._timeout_reason(), None)
        except ssl.SSLError as exc:
            return CheckResult(False, None, redact(f"TLS error: {exc}"), None)
        except http.client.HTTPException:
            return CheckResult(False, None, "invalid HTTP response", None)
        except OSError as exc:
            return CheckResult(False, None, redact(f"connection failed: {exc}"), None)
        except ValueError as exc:
            return CheckResult(False, None, redact(f"invalid URL: {exc}"), None)

    def _timeout_reason(self) -> str:
        return f"timeout after {self.timeout:g}s"

    def _url_error_reason(self, exc: urllib.error.URLError) -> str:
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return self._timeout_reason()
        if isinstance(reason, socket.gaierror):
            return "DNS lookup failed"
        if isinstance(reason, ssl.SSLError):
            return redact(f"TLS error: {reason}")
        return redact(f"connection failed: {reason}")

    def _check(self, url: str, expect_feed: bool) -> CheckResult:
        if not expect_feed:
            try:
                return self._request(url, "HEAD", expect_feed=False)
            except urllib.error.HTTPError as exc:
                exc.close()  # fall back to GET and report its result
        return self._request(url, "GET", expect_feed=expect_feed)

    def _request(self, url: str, method: str, *, expect_feed: bool) -> CheckResult:
        deadline = time.monotonic() + self.timeout
        request = urllib.request.Request(
            url, method=method, headers={"User-Agent": f"invio/{invio.__version__}"}
        )
        response = _opener().open(request, timeout=self.timeout)
        try:
            status: int = response.status
            is_feed: bool | None = None
            if method == "GET":
                body = self._read(response, deadline)
                is_feed = is_feed_document(body) if expect_feed else None
            return CheckResult(True, status, None, is_feed)
        finally:
            response.close()

    def _read(self, response: http.client.HTTPResponse, deadline: float) -> bytes:
        chunks: list[bytes] = []
        size = 0
        while size < self.max_bytes:
            if time.monotonic() >= deadline:
                raise TimeoutError
            chunk = response.read(min(_CHUNK, self.max_bytes - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        return b"".join(chunks)
