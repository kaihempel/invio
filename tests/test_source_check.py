"""Tests for source checks: feed detection and ``HttpSourceChecker`` on a loopback server."""

import http.client
import http.server
import socket
import socketserver
import ssl
import threading
import urllib.error
from collections.abc import Iterator
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

import invio
from invio.cli import source_check
from invio.cli.source_check import HttpSourceChecker, is_feed_document

RSS = b'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title></channel></rss>'
RDF = b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"></rdf:RDF>'
ATOM = b'<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title></feed>'


# --- is_feed_document ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "head",
    [
        RSS,
        RDF,
        ATOM,
        b"\xef\xbb\xbf" + RSS,
        b"<!-- hello -->\n  " + ATOM,
        b"\n\n  " + RSS,
        RSS[:40],  # truncated after the root start tag
    ],
)
def test_feed_documents(head: bytes) -> None:
    assert is_feed_document(head) is True


@pytest.mark.parametrize(
    "head",
    [
        b"<html><body>hi</body></html>",
        b"<feed><title>no namespace</title></feed>",
        b"just text",
        b"",
        b"<?xml version",
        b"\x00\xff\xfe\x01garbage",
        b"<rss",
    ],
)
def test_non_feed_documents(head: bytes) -> None:
    assert is_feed_document(head) is False


# --- HttpSourceChecker -----------------------------------------------------------------------


@dataclass
class Server:
    base: str
    requests: list[tuple[str, str, str]]  # (method, path, user-agent)


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> Iterator[Server]:
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")
    release = threading.Event()
    seen: list[tuple[str, str, str]] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _respond(self, send_body: bool) -> None:
            seen.append((self.command, self.path, self.headers.get("User-Agent", "")))
            path = self.path
            if path == "/slow":
                release.wait()
                return
            if path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "/feed")
                self.end_headers()
                return
            if path == "/missing":
                self.send_error(404)
                return
            if path == "/nohead" and self.command == "HEAD":
                self.send_error(405)
                return
            if path == "/headforbidden" and self.command == "HEAD":
                self.send_error(403)
                return
            body, ctype = {
                "/feed": (RSS, "application/rss+xml"),
                "/nohead": (RSS, "application/rss+xml"),
                "/page": (b"<html><body>hello</body></html>", "text/html"),
                "/huge": (b"<html>" + b"x" * 200_000, "text/html"),
            }.get(path, (b"ok", "text/plain"))
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if send_body:
                self.wfile.write(body)

        def do_GET(self) -> None:
            self._respond(True)

        def do_HEAD(self) -> None:
            self._respond(False)

    class LoopbackServer(http.server.ThreadingHTTPServer):
        def server_bind(self) -> None:
            # Skip HTTPServer's reverse DNS lookup (socket.getfqdn), which can take seconds.
            socketserver.TCPServer.server_bind(self)
            self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    httpd = LoopbackServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    try:
        yield Server(f"http://127.0.0.1:{httpd.server_address[1]}", seen)
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_defaults() -> None:
    checker = HttpSourceChecker()

    assert checker.timeout == 10.0
    assert checker.max_bytes == 65_536


def test_feed_detected(server: Server) -> None:
    result = HttpSourceChecker().check(f"{server.base}/feed", expect_feed=True)

    assert (result.reachable, result.status, result.is_feed) == (True, 200, True)
    assert result.reason is None


def test_html_page_is_not_a_feed(server: Server) -> None:
    result = HttpSourceChecker().check(f"{server.base}/page", expect_feed=True)

    assert (result.reachable, result.is_feed) == (True, False)


def test_feed_flag_not_checked_when_not_expected(server: Server) -> None:
    result = HttpSourceChecker().check(f"{server.base}/page", expect_feed=False)

    assert (result.reachable, result.is_feed) == (True, None)
    assert [m for m, _, _ in server.requests] == ["HEAD"]


def test_not_found(server: Server) -> None:
    result = HttpSourceChecker().check(f"{server.base}/missing", expect_feed=False)

    assert (result.reachable, result.status, result.reason) == (False, 404, "HTTP 404")


def test_head_not_allowed_falls_back_to_get(server: Server) -> None:
    result = HttpSourceChecker().check(f"{server.base}/nohead", expect_feed=False)

    assert result.reachable is True
    assert [m for m, _, _ in server.requests] == ["HEAD", "GET"]


def test_head_rejected_falls_back_to_get(server: Server) -> None:
    result = HttpSourceChecker().check(f"{server.base}/headforbidden", expect_feed=False)

    assert (result.reachable, result.status) == (True, 200)
    assert [m for m, _, _ in server.requests] == ["HEAD", "GET"]


def test_get_error_is_reported_after_head_error(server: Server) -> None:
    HttpSourceChecker().check(f"{server.base}/missing", expect_feed=False)

    assert [m for m, _, _ in server.requests] == ["HEAD", "GET"]


def test_redirect_is_followed(server: Server) -> None:
    result = HttpSourceChecker().check(f"{server.base}/redirect", expect_feed=True)

    assert (result.reachable, result.is_feed) == (True, True)


def test_slow_server_times_out(server: Server) -> None:
    result = HttpSourceChecker(timeout=0.2).check(f"{server.base}/slow", expect_feed=False)

    assert result.reachable is False
    assert result.reason is not None and result.reason.startswith("timeout")


def test_body_read_is_capped(server: Server) -> None:
    result = HttpSourceChecker(max_bytes=1024).check(f"{server.base}/huge", expect_feed=True)

    assert (result.reachable, result.is_feed) == (True, False)


def test_user_agent_identifies_invio(server: Server) -> None:
    HttpSourceChecker().check(f"{server.base}/feed", expect_feed=False)

    assert server.requests[0][2].startswith("invio/")
    assert invio.__version__ in server.requests[0][2]


def _no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")


def _refused_port() -> int:
    """Bind an ephemeral loopback port and release it, so connecting to it is refused."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
    return port


def test_connection_refused_does_not_raise(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_proxy(monkeypatch)

    url = f"http://127.0.0.1:{_refused_port()}/"
    result = HttpSourceChecker(timeout=2).check(url, expect_feed=False)

    assert result.reachable is False
    assert result.reason is not None and result.status is None


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://example.com/x", "not a url", "http://", ""]
)
def test_unsupported_urls_do_not_raise(url: str) -> None:
    result = HttpSourceChecker(timeout=1).check(url, expect_feed=False)

    assert result.reachable is False
    assert result.reason


def test_userinfo_is_redacted_from_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_proxy(monkeypatch)

    url = f"http://bob:s3cret@127.0.0.1:{_refused_port()}/"
    result = HttpSourceChecker(timeout=2).check(url, expect_feed=False)

    assert result.reason is not None
    assert "s3cret" not in result.reason and "bob" not in result.reason


def test_dns_failure_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError(socket.gaierror(-2, "Name or service not known"))

    monkeypatch.setattr(source_check.urllib.request.OpenerDirector, "open", fail)

    result = HttpSourceChecker().check("http://example.invalid/", expect_feed=False)

    assert result.reason == "DNS lookup failed"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (TimeoutError("t"), "timeout after 10s"),
        (ssl.SSLError("bad cert"), "TLS error: "),
        (http.client.BadStatusLine("x"), "invalid HTTP response"),
        (OSError("boom"), "connection failed: boom"),
        (ValueError("weird"), "invalid URL: weird"),
    ],
)
def test_exception_mapping(monkeypatch: pytest.MonkeyPatch, error: Exception, reason: str) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise error

    monkeypatch.setattr(source_check.urllib.request.OpenerDirector, "open", fail)

    result = HttpSourceChecker().check("http://example.com/", expect_feed=False)

    assert result.reachable is False
    assert result.reason is not None and result.reason.startswith(reason)


def test_url_error_with_ssl_and_other_reasons(monkeypatch: pytest.MonkeyPatch) -> None:
    reasons = iter([ssl.SSLError("x"), "plain text", TimeoutError()])

    def fail(*args: object, **kwargs: object) -> None:
        raise urllib.error.URLError(next(reasons))

    monkeypatch.setattr(source_check.urllib.request.OpenerDirector, "open", fail)
    checker = HttpSourceChecker()

    got = [checker.check("http://example.com/", expect_feed=False).reason for _ in range(3)]

    assert got[0] is not None and got[0].startswith("TLS error: ")
    assert got[1:] == ["connection failed: plain text", "timeout after 10s"]


def test_total_deadline_stops_slow_drip(monkeypatch: pytest.MonkeyPatch) -> None:
    class Drip:
        status = 200

        def read(self, n: int = -1) -> bytes:
            return b"<"

        def close(self) -> None:
            self.closed = True

    ticks = iter([0.0, 0.0, 5.0, 50.0, 500.0])
    monkeypatch.setattr(source_check, "time", SimpleNamespace(monotonic=lambda: next(ticks)))
    monkeypatch.setattr(source_check.urllib.request.OpenerDirector, "open", lambda *a, **k: Drip())

    result = HttpSourceChecker(timeout=10).check("http://example.com/", expect_feed=True)

    assert result.reachable is False
    assert result.reason == "timeout after 10s"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("see http://bob:s3cret@h/x", "see http://h/x"),
        ("https://bob:pa@ss@h/x", "https://h/x"),
        ("https://h/x?a=b@c", "https://h/x?a=b@c"),
        ("http://host?x=a@b", "http://host?x=a@b"),
        ("http://host#a@b", "http://host#a@b"),
        ("http://u:p@host?x=a@b", "http://host?x=a@b"),
        ("no url here", "no url here"),
    ],
)
def test_redact(text: str, expected: str) -> None:
    assert source_check.redact(text) == expected
