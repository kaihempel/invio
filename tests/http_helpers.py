"""Helpers for the safe HTTP client tests: loopback origins, fake DNS and a mock transport.

No helper touches the internet. Loopback servers bind ``127.0.0.1`` only; tests that talk to
them create the client with ``allow_networks=LOOPBACK``.
"""

import http.server
import socket
import socketserver
import sys
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from ipaddress import IPv4Network, IPv6Network, ip_network

import httpx
import pytest

__all__ = [
    "LOOPBACK",
    "FakeResolver",
    "LoopbackServer",
    "RecordedRequest",
    "RecordingTransport",
    "Route",
    "loopback_server",
    "second_server",
    "server",
]

LOOPBACK: tuple[IPv4Network | IPv6Network, ...] = (ip_network("127.0.0.0/8"),)


@dataclass
class Route:
    """How the loopback server answers one path."""

    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes = b""
    chunked: bool = False  # send the body with chunked transfer encoding (no Content-Length)
    delay: float = 0.0  # seconds to wait before answering
    stall: bool = False  # send the headers, then never finish the body
    drip: float = 0.0  # send the body one byte per this many seconds
    # Answer 304 when If-None-Match equals the route's ETag or If-Modified-Since its Last-Modified.
    conditional: bool = False


@dataclass(frozen=True)
class RecordedRequest:
    """One request as seen by the loopback server."""

    path: str
    headers: dict[str, str]  # lower-cased names
    started: float  # time.monotonic() when the request line was read


class _Server(http.server.ThreadingHTTPServer):
    def server_bind(self) -> None:
        # Skip HTTPServer's reverse DNS lookup (socket.getfqdn), which can take seconds.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = "127.0.0.1", self.server_address[1]

    def handle_error(self, request: object, client_address: object) -> None:
        # Clients that hang up on purpose raise ConnectionError in the handler thread; stay quiet.
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)  # type: ignore[arg-type]


class LoopbackServer:
    """A local HTTP origin with mutable ``routes`` and a thread-safe request log.

    Unknown paths answer 404, so ``/robots.txt`` is "no robots.txt" until a route is set.
    """

    def __init__(self) -> None:
        self.routes: dict[str, Route] = {}
        self._requests: list[RecordedRequest] = []
        self._lock = threading.Lock()
        self._release = threading.Event()
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"  # chunked framing needs HTTP/1.1

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_GET(self) -> None:
                owner._serve(self)

        self._httpd = _Server(("127.0.0.1", 0), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=lambda: self._httpd.serve_forever(poll_interval=0.01), daemon=True
        )

    @property
    def origin_port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.origin_port}"

    @property
    def requests(self) -> list[RecordedRequest]:
        with self._lock:
            return list(self._requests)

    def paths(self) -> list[str]:
        return [request.path for request in self.requests]

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and release stalled handlers; safe to call more than once."""
        self._release.set()
        if self._thread.is_alive():
            self._httpd.shutdown()
            self._thread.join(timeout=5)
        self._httpd.server_close()

    def _serve(self, handler: http.server.BaseHTTPRequestHandler) -> None:
        started = time.monotonic()
        headers = {key.lower(): value for key, value in handler.headers.items()}
        with self._lock:
            self._requests.append(RecordedRequest(handler.path, headers, started))
        route = self._conditional(self.routes.get(handler.path, Route(status=404)), headers)
        if route.delay:
            self._release.wait(route.delay)
        handler.send_response(route.status)
        handler.send_header("Connection", "close")  # one request per connection
        handler.close_connection = True
        for name, value in route.headers.items():
            handler.send_header(name, value)
        has_length = any(name.lower() == "content-length" for name in route.headers)
        if route.chunked:
            handler.send_header("Transfer-Encoding", "chunked")
        elif not has_length and not route.stall:
            handler.send_header("Content-Length", str(len(route.body)))
        try:
            handler.end_headers()
            self._send_body(handler, route)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the client gave up (limit hit, timeout): expected in the limit tests

    @staticmethod
    def _conditional(route: Route, request_headers: dict[str, str]) -> Route:
        if not route.conditional:
            return route
        lowered = {name.lower(): value for name, value in route.headers.items()}
        etag, modified = lowered.get("etag"), lowered.get("last-modified")
        if (etag and request_headers.get("if-none-match") == etag) or (
            modified and request_headers.get("if-modified-since") == modified
        ):
            return Route(status=304, headers=route.headers)
        return route

    def _send_body(self, handler: http.server.BaseHTTPRequestHandler, route: Route) -> None:
        if route.stall:
            handler.wfile.flush()
            self._release.wait(30)  # never block a handler thread forever
        elif route.drip:
            for start in range(len(route.body)):
                handler.wfile.write(route.body[start : start + 1])
                handler.wfile.flush()
                if self._release.wait(route.drip):
                    break
        elif route.chunked:
            for start in range(0, len(route.body), 1024):
                chunk = route.body[start : start + 1024]
                handler.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
            handler.wfile.write(b"0\r\n\r\n")
        else:
            handler.wfile.write(route.body)


def loopback_server() -> Iterator[LoopbackServer]:
    """Start a :class:`LoopbackServer` and stop it afterwards (use inside a fixture)."""
    instance = LoopbackServer()
    instance.start()
    try:
        yield instance
    finally:
        instance.stop()


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    yield from loopback_server()


@pytest.fixture
def second_server() -> Iterator[LoopbackServer]:
    """An independent second origin (different port)."""
    yield from loopback_server()


class FakeResolver:
    """Maps host -> IP strings, recording every call.

    ``FakeResolver({"h": ["1.2.3.4"]})`` always answers the same. A list of lists is consumed
    one answer per call, e.g. ``{"h": [["1.2.3.4"], ["127.0.0.1"]]}`` answers a public address
    first and loopback second (DNS rebinding); the last answer repeats once the sequence is
    used up. Unknown hosts raise ``socket.gaierror`` like a failed lookup.
    """

    def __init__(self, mapping: dict[str, list[str] | list[list[str]]]) -> None:
        self.calls: list[tuple[str, int]] = []
        self._answers: dict[str, deque[list[str]]] = {}
        for host, value in mapping.items():
            if value and isinstance(value[0], list):
                answers = [list(answer) for answer in value if isinstance(answer, list)]
            else:
                answers = [[str(address) for address in value]]
            self._answers[host] = deque(answers)

    async def resolve(self, host: str, port: int) -> list[str]:
        self.calls.append((host, port))
        queue = self._answers.get(host)
        if not queue:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return list(queue.popleft() if len(queue) > 1 else queue[0])


class _RawStream(httpx.AsyncByteStream):
    """An unread body, delivered in network-sized chunks like a real connection."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._body), 16_384):
            yield self._body[start : start + 16_384]


class RecordingTransport(httpx.AsyncBaseTransport):
    """``httpx.MockTransport`` that also logs every request it is asked to send.

    A response built with ``content=`` is read (and decoded) by httpx on creation; it is handed
    on as an unread raw stream instead, so the client decodes it as it would a network body.
    """

    def __init__(self, handler: Callable[[httpx.Request], httpx.Response]) -> None:
        self.requests: list[httpx.Request] = []
        self._inner = httpx.MockTransport(handler)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        response = await self._inner.handle_async_request(request)
        if not isinstance(response.stream, httpx.ByteStream):
            return response
        raw = b"".join([chunk async for chunk in response.stream])
        return httpx.Response(
            response.status_code,
            headers=response.headers,
            stream=_RawStream(raw),
            extensions=response.extensions,
            request=request,
        )

    async def aclose(self) -> None:
        await self._inner.aclose()
