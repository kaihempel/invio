"""Conditional GET: validators are remembered per URL for the lifetime of one client."""

from invio.sources.http import FetchResult, HttpClientConfig, NotModified, SafeHttpClient
from tests.http_helpers import LOOPBACK, LoopbackServer, Route, server  # noqa: F401

ETAG = '"v1"'
MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"


def make_client() -> SafeHttpClient:
    config = HttpClientConfig(respect_robots=False, host_interval=0.01)
    return SafeHttpClient(config, allow_networks=LOOPBACK)


def validated(server: LoopbackServer, path: str = "/feed") -> str:
    server.routes[path] = Route(
        headers={"ETag": ETAG, "Last-Modified": MODIFIED}, body=b"feed", conditional=True
    )
    return f"{server.base_url}{path}"


async def test_second_fetch_is_conditional_and_returns_not_modified(
    server: LoopbackServer,
) -> None:
    url = validated(server)

    async with make_client() as client:
        first = await client.get(url)
        second = await client.get(url)

    assert isinstance(first, FetchResult)
    assert (first.etag, first.last_modified) == (ETAG, MODIFIED)
    assert "if-none-match" not in server.requests[0].headers
    assert "if-modified-since" not in server.requests[0].headers
    assert server.requests[1].headers["if-none-match"] == ETAG
    assert server.requests[1].headers["if-modified-since"] == MODIFIED
    assert second == NotModified(url, ETAG, MODIFIED)


async def test_route_without_validators_is_fetched_unconditionally(
    server: LoopbackServer,
) -> None:
    server.routes["/plain"] = Route(body=b"x")

    async with make_client() as client:
        await client.get(f"{server.base_url}/plain")
        await client.get(f"{server.base_url}/plain")

    assert all("if-none-match" not in r.headers for r in server.requests)


async def test_a_new_client_has_no_cached_validators(server: LoopbackServer) -> None:
    url = validated(server)

    async with make_client() as client:
        await client.get(url)
    async with make_client() as client:
        result = await client.get(url)

    assert isinstance(result, FetchResult)
    assert "if-none-match" not in server.requests[1].headers


async def test_caller_validators_override_the_cached_ones(
    server: LoopbackServer,
) -> None:
    url = validated(server)

    async with make_client() as client:
        await client.get(url)
        result = await client.get(url, headers={"If-None-Match": '"other"'})

    assert server.requests[1].headers["if-none-match"] == '"other"'
    assert server.requests[1].headers["if-modified-since"] == MODIFIED  # still from the cache
    assert result == NotModified(url, '"other"', MODIFIED)


async def test_validators_are_stored_under_the_requested_url_after_a_redirect(
    server: LoopbackServer,
) -> None:
    final = validated(server, "/final")
    server.routes["/old"] = Route(status=301, headers={"Location": "/final"})
    requested = f"{server.base_url}/old"

    async with make_client() as client:
        await client.get(requested)
        second = await client.get(requested)

    assert second == NotModified(requested, ETAG, MODIFIED)
    assert final != requested


async def test_304_without_a_cached_entry_is_not_modified_with_empty_validators(
    server: LoopbackServer,
) -> None:
    server.routes["/n"] = Route(status=304)

    async with make_client() as client:
        result = await client.get(f"{server.base_url}/n")

    assert result == NotModified(f"{server.base_url}/n", None, None)


async def test_304_reports_the_validators_the_caller_sent(
    server: LoopbackServer,
) -> None:
    url = validated(server)

    async with make_client() as client:
        result = await client.get(url, headers={"If-None-Match": ETAG})

    assert result == NotModified(url, ETAG, None)
