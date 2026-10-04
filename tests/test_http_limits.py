"""Resource limits of ``SafeHttpClient``: body size, redirects, timeouts and the User-Agent."""

import gzip
import re
import time

import pytest

from invio.sources.http import (
    FetchError,
    FetchResult,
    HttpClientConfig,
    SafeHttpClient,
    TooLargeError,
)
from tests.http_helpers import LOOPBACK, LoopbackServer, Route, server  # noqa: F401

LIMIT = 1024


def make_client(**overrides: object) -> SafeHttpClient:
    fields: dict[str, object] = {
        "respect_robots": False,
        "host_interval": 0.01,
        "max_response_bytes": LIMIT,
    } | overrides
    return SafeHttpClient(HttpClientConfig.model_validate(fields), allow_networks=LOOPBACK)


# --- body size -------------------------------------------------------------------------------


async def test_announced_oversized_body_is_rejected_quickly(server: LoopbackServer) -> None:
    server.routes["/big"] = Route(body=b"x" * 10_485_760)

    started = time.monotonic()
    async with make_client() as client:
        with pytest.raises(TooLargeError) as info:
            await client.get(f"{server.base_url}/big")

    assert info.value.limit == LIMIT
    assert time.monotonic() - started < 2


async def test_chunked_oversized_body_is_rejected(server: LoopbackServer) -> None:
    server.routes["/chunked"] = Route(body=b"x" * 4096, chunked=True)

    async with make_client() as client:
        with pytest.raises(TooLargeError):
            await client.get(f"{server.base_url}/chunked")


async def test_body_shorter_than_actual_stream_is_cut_at_content_length(
    server: LoopbackServer,
) -> None:
    server.routes["/short"] = Route(headers={"Content-Length": "10"}, body=b"y" * 4096)

    async with make_client() as client:
        result = await client.get(f"{server.base_url}/short")

    assert isinstance(result, FetchResult)
    assert result.content == b"y" * 10


async def test_gzip_bomb_is_rejected_on_decoded_size(server: LoopbackServer) -> None:
    compressed = gzip.compress(b"a" * 100_000)
    assert len(compressed) < LIMIT
    server.routes["/bomb"] = Route(headers={"Content-Encoding": "gzip"}, body=compressed)

    async with make_client() as client:
        with pytest.raises(TooLargeError):
            await client.get(f"{server.base_url}/bomb")


async def test_body_exactly_at_the_limit_succeeds(server: LoopbackServer) -> None:
    server.routes["/exact"] = Route(body=b"z" * LIMIT)

    async with make_client() as client:
        result = await client.get(f"{server.base_url}/exact")

    assert isinstance(result, FetchResult)
    assert len(result.content) == LIMIT


async def test_body_one_byte_over_the_limit_fails(server: LoopbackServer) -> None:
    server.routes["/over"] = Route(body=b"z" * (LIMIT + 1), chunked=True)

    async with make_client() as client:
        with pytest.raises(TooLargeError):
            await client.get(f"{server.base_url}/over")


# --- redirects -------------------------------------------------------------------------------


def chain(server: LoopbackServer, hops: int) -> None:
    for number in range(hops):
        server.routes[f"/r{number}"] = Route(status=302, headers={"Location": f"/r{number + 1}"})
    server.routes[f"/r{hops}"] = Route(body=b"end")


async def test_five_redirect_hops_succeed(server: LoopbackServer) -> None:
    chain(server, 5)

    async with make_client(max_redirects=5) as client:
        result = await client.get(f"{server.base_url}/r0")

    assert isinstance(result, FetchResult)
    assert result.content == b"end"


async def test_six_redirect_hops_fail(server: LoopbackServer) -> None:
    chain(server, 6)

    async with make_client(max_redirects=5) as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/r0")

    assert info.value.reason == "too_many_redirects"


# --- timeouts --------------------------------------------------------------------------------


async def test_stalled_response_times_out_within_the_total_deadline(
    server: LoopbackServer,
) -> None:
    server.routes["/stall"] = Route(headers={"Content-Length": "100"}, body=b"", stall=True)

    started = time.monotonic()
    async with make_client(read_timeout=0.2, total_timeout=0.5) as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/stall")

    assert info.value.reason == "timeout"
    assert time.monotonic() - started < 1.5


async def test_dripping_response_hits_the_total_deadline(server: LoopbackServer) -> None:
    server.routes["/drip"] = Route(body=b"x" * 100, drip=0.1)  # each read succeeds in time

    started = time.monotonic()
    async with make_client(read_timeout=0.3, total_timeout=0.5) as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/drip")

    assert info.value.reason == "timeout"
    assert time.monotonic() - started < 1.5


# --- identity --------------------------------------------------------------------------------


async def test_user_agent_matches_the_documented_format(server: LoopbackServer) -> None:
    server.routes["/"] = Route(body=b"x")
    config = HttpClientConfig(contact="ops@example.org", respect_robots=False)

    async with SafeHttpClient(config, allow_networks=LOOPBACK) as client:
        await client.get(f"{server.base_url}/", headers={"User-Agent": "someone-else"})

    sent = server.requests[0].headers
    assert sent["user-agent"] == config.user_agent
    assert re.fullmatch(
        r"invio/\S+ \(\+https://github\.com/kaihempel/invio; contact: ops@example\.org\)",
        sent["user-agent"],
    )
    assert sent["accept-encoding"] == "gzip, deflate"
