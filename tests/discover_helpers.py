"""Shared fakes for the discovery tests: a scripted web site behind the real, guarded client.

``Site`` answers by ``Host`` header and path (the transport sees the pinned IP address in the
URL, so the host name is read from the header). Nothing here touches the network.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import httpx2

from invio.sources.http import HttpClientConfig, SafeHttpClient
from tests.http_helpers import FakeResolver, RecordingTransport

FIXTURES = Path(__file__).parent / "fixtures" / "discovery"
HTML = "text/html; charset=utf-8"
XML = "application/xml"
RSS = "application/rss+xml"
TEXT = "text/plain"
PUBLIC_IP = "93.184.216.34"
HOSTS = ("example.com", "www.example.com", "other.example.org")


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    content_type: str | None = HTML
    headers: dict[str, str] = field(default_factory=dict)
    error: Exception | None = None


class Site:
    """Routes ``(host, path)`` to a :class:`Reply`; anything unknown is a 404."""

    def __init__(self) -> None:
        self.routes: dict[tuple[str, str], Reply] = {}
        self.transport = RecordingTransport(self._handle)

    def add(
        self,
        path: str,
        body: bytes | str = b"",
        content_type: str | None = HTML,
        *,
        host: str = "example.com",
        status: int = 200,
        headers: dict[str, str] | None = None,
        error: Exception | None = None,
    ) -> None:
        data = body.encode() if isinstance(body, str) else body
        self.routes[(host, path)] = Reply(status, data, content_type, headers or {}, error)

    def redirect(self, path: str, location: str, *, host: str = "example.com") -> None:
        self.add(path, status=302, headers={"Location": location}, host=host)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        host = request.headers["host"]
        reply = self.routes.get((host, request.url.path))
        if reply is None:
            return httpx2.Response(404, content=b"not found")
        if reply.error is not None:
            raise reply.error
        headers = dict(reply.headers)
        if reply.content_type is not None:
            headers["Content-Type"] = reply.content_type
        return httpx2.Response(reply.status, headers=headers, content=reply.body)

    @property
    def requested(self) -> list[str]:
        """Every request as ``scheme://host/path`` in the order the transport saw it."""
        return [
            f"{request.url.scheme}://{request.headers['host']}{request.url.path}"
            for request in self.transport.requests
        ]

    def paths(self, host: str = "example.com") -> list[str]:
        prefix = f"https://{host}"
        return [url.removeprefix(prefix) for url in self.requested if url.startswith(prefix)]


def make_client(site: Site, **config: object) -> SafeHttpClient:
    """A real guarded client whose transport is the scripted ``site``."""
    fields: dict[str, object] = {
        "respect_robots": True,
        "host_interval": 0.01,
        "max_response_bytes": 5_000,
    } | config
    return SafeHttpClient(
        HttpClientConfig.model_validate(fields),
        resolver=FakeResolver({host: [PUBLIC_IP] for host in HOSTS}),
        transport=site.transport,
    )


@asynccontextmanager
async def open_client(site: Site, **config: object) -> AsyncIterator[SafeHttpClient]:
    async with make_client(site, **config) as client:
        yield client
