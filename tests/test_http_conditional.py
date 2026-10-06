"""Conditional GET: validators are remembered per URL for the lifetime of one client."""

import pytest

from invio.sources import http
from invio.sources.http import (
    FetchError,
    FetchResult,
    HttpClientConfig,
    NotModified,
    SafeHttpClient,
)
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


async def test_304_to_an_unconditional_request_is_an_error(server: LoopbackServer) -> None:
    # The caller has no copy that could be "not modified", so it must not skip the source.
    server.routes["/n"] = Route(status=304)

    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/n")

    assert (info.value.reason, info.value.status) == ("http_status", 304)


async def test_304_reports_the_validators_the_caller_sent(
    server: LoopbackServer,
) -> None:
    url = validated(server)

    async with make_client() as client:
        result = await client.get(url, headers={"If-None-Match": ETAG})

    assert result == NotModified(url, ETAG, None)


async def test_last_modified_alone_makes_the_request_conditional(
    server: LoopbackServer,
) -> None:
    server.routes["/lm"] = Route(headers={"Last-Modified": MODIFIED}, body=b"x", conditional=True)
    url = f"{server.base_url}/lm"

    async with make_client() as client:
        await client.get(url)
        second = await client.get(url)

    assert "if-none-match" not in server.requests[1].headers
    assert server.requests[1].headers["if-modified-since"] == MODIFIED
    assert second == NotModified(url, None, MODIFIED)


async def test_caller_if_modified_since_overrides_the_cached_one(
    server: LoopbackServer,
) -> None:
    url = validated(server)
    older = "Tue, 20 Oct 2015 07:28:00 GMT"

    async with make_client() as client:
        await client.get(url)
        await client.get(url, headers={"if-modified-since": older})

    assert server.requests[1].headers["if-modified-since"] == older
    assert server.requests[1].headers["if-none-match"] == ETAG  # still from the cache


async def test_validators_are_shared_by_spellings_of_the_same_url(server: LoopbackServer) -> None:
    validated(server)
    port = server.origin_port

    async with make_client() as client:
        await client.get(f"http://127.0.0.1:{port}/feed#top")
        result = await client.get(f"HTTP://127.0.0.1:{port}/feed")

    assert isinstance(result, NotModified)
    assert server.requests[1].headers["if-none-match"] == ETAG


async def test_least_recently_used_validators_are_dropped_beyond_the_cap(
    server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(http, "_MAX_VALIDATORS", 2)
    a, b, c = (validated(server, path) for path in ("/a", "/b", "/c"))

    async with make_client() as client:
        await client.get(a)
        await client.get(b)
        await client.get(a)  # conditional: "a" is now the most recently used
        await client.get(c)  # evicts "b"
        again_a = await client.get(a)
        again_b = await client.get(b)

    assert isinstance(again_a, NotModified)
    assert isinstance(again_b, FetchResult)


async def test_unconditional_get_sends_no_validators_and_keeps_the_remembered_ones(
    server: LoopbackServer,
) -> None:
    url = validated(server)

    async with make_client() as client:
        await client.get(url)  # remembers the validators
        plain = await client.get(url, conditional=False)
        again = await client.get(url)  # still conditional with the original validators

    assert isinstance(plain, FetchResult)
    assert "if-none-match" not in server.requests[1].headers
    assert "if-modified-since" not in server.requests[1].headers
    assert server.requests[2].headers["if-none-match"] == ETAG
    assert again == NotModified(url, ETAG, MODIFIED)


async def test_unconditional_get_does_not_remember_the_validators_of_its_response(
    server: LoopbackServer,
) -> None:
    url = validated(server)

    async with make_client() as client:
        first = await client.get(url, conditional=False)
        second = await client.get(url)

    assert isinstance(first, FetchResult)
    assert isinstance(second, FetchResult)  # nothing was remembered, so no conditional request
    assert "if-none-match" not in server.requests[1].headers
