"""Resource limits of ``SafeHttpClient``: body size, redirects, timeouts and the User-Agent."""

import gzip
import random
import re
import time
import zlib

import httpx
import pytest

from invio.sources.http import (
    FetchError,
    FetchResult,
    HttpClientConfig,
    SafeHttpClient,
    TooLargeError,
)
from tests.http_helpers import (  # noqa: F401
    LOOPBACK,
    FakeResolver,
    LoopbackServer,
    RecordingTransport,
    Route,
    server,
)

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
    # One byte every 20 ms against a 300 ms read timeout: only the total deadline can fire.
    server.routes["/drip"] = Route(body=b"x" * 200, drip=0.02)

    started = time.monotonic()
    async with make_client(read_timeout=0.3, total_timeout=0.5) as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/drip")

    elapsed = time.monotonic() - started
    assert info.value.reason == "timeout"
    assert 0.5 - 0.05 <= elapsed < 2.0


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


# --- decompression bombs ---------------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["gzip, gzip", "gzip,deflate", "br", "compress", "zstd"])
async def test_stacked_or_unknown_content_encoding_is_rejected(
    server: LoopbackServer,
    encoding: str,
) -> None:
    body = gzip.compress(gzip.compress(b"a" * 100_000))
    server.routes["/s"] = Route(headers={"Content-Encoding": encoding}, body=body)

    async with make_client(max_response_bytes=10 * LIMIT) as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/s")

    assert info.value.reason == "invalid_response"


async def test_identity_and_deflate_encodings_are_accepted(
    server: LoopbackServer,
) -> None:
    import zlib

    server.routes["/id"] = Route(headers={"Content-Encoding": "identity"}, body=b"plain")
    server.routes["/zlib"] = Route(
        headers={"Content-Encoding": "deflate"}, body=zlib.compress(b"zz")
    )
    raw = zlib.compressobj(wbits=-15)
    server.routes["/raw"] = Route(
        headers={"Content-Encoding": "deflate"}, body=raw.compress(b"rr") + raw.flush()
    )

    async with make_client() as client:
        results = [await client.get(f"{server.base_url}{p}") for p in ("/id", "/zlib", "/raw")]

    assert [r.content for r in results if isinstance(r, FetchResult)] == [b"plain", b"zz", b"rr"]


async def test_high_ratio_payload_is_stopped_without_decoding_it_all(
    server: LoopbackServer,
) -> None:
    import tracemalloc

    bomb = gzip.compress(b"\0" * 50_000_000, compresslevel=9)
    server.routes["/bomb"] = Route(headers={"Content-Encoding": "gzip"}, body=bomb)

    tracemalloc.start()
    try:
        async with make_client(max_response_bytes=1_048_576) as client:
            with pytest.raises(TooLargeError):
                await client.get(f"{server.base_url}/bomb")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 20_000_000  # nowhere near the 50 MB the payload expands to


async def test_truncated_gzip_stream_is_invalid_response(
    server: LoopbackServer,
) -> None:
    server.routes["/t"] = Route(
        headers={"Content-Encoding": "gzip"}, body=gzip.compress(b"hello" * 100)[:-10]
    )

    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/t")

    assert info.value.reason == "invalid_response"


async def test_corrupt_gzip_body_is_invalid_response(server: LoopbackServer) -> None:
    server.routes["/c"] = Route(headers={"Content-Encoding": "gzip"}, body=b"not gzip at all")

    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/c")

    assert info.value.reason == "invalid_response"


async def test_gzip_body_split_over_many_chunks_is_decoded(server: LoopbackServer) -> None:
    payload = random.Random(10).randbytes(50_000)  # incompressible: ~50 chunks on the wire
    server.routes["/g"] = Route(
        headers={"Content-Encoding": "gzip"}, body=gzip.compress(payload), chunked=True
    )

    async with make_client(max_response_bytes=100_000) as client:
        result = await client.get(f"{server.base_url}/g")

    assert isinstance(result, FetchResult)
    assert result.content == payload


async def test_compressed_content_length_above_the_limit_is_judged_by_decoded_size(
    server: LoopbackServer,
) -> None:
    payload = random.Random(1).randbytes(LIMIT)  # incompressible: gzip adds a few bytes
    compressed = gzip.compress(payload)
    assert len(compressed) > LIMIT
    server.routes["/g"] = Route(headers={"Content-Encoding": "gzip"}, body=compressed)

    async with make_client() as client:
        result = await client.get(f"{server.base_url}/g")

    assert isinstance(result, FetchResult)
    assert result.content == payload


@pytest.mark.parametrize("raw", [b"\xb2", b"1e9", b"-5", b" "])
async def test_unusable_content_length_does_not_escape_as_a_bare_exception(raw: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=[(b"Content-Length", raw)], content=b"ok")

    client = SafeHttpClient(
        HttpClientConfig(respect_robots=False, host_interval=0.01),
        resolver=FakeResolver({"example.org": ["93.184.216.34"]}),
        transport=RecordingTransport(handler),
    )
    async with client:
        result = await client.get("http://example.org/")

    assert isinstance(result, FetchResult)
    assert result.content == b"ok"


async def test_every_gzip_member_is_decoded(server: LoopbackServer) -> None:
    server.routes["/m"] = Route(
        headers={"Content-Encoding": "gzip"},
        body=gzip.compress(b"first,") + gzip.compress(b"second") + b"\0\0\0",
    )

    async with make_client() as client:
        result = await client.get(f"{server.base_url}/m")

    assert isinstance(result, FetchResult)
    assert result.content == b"first,second"


async def test_gzip_members_together_are_held_to_the_limit(server: LoopbackServer) -> None:
    member = gzip.compress(b"a" * (LIMIT // 2 + 1))
    server.routes["/m"] = Route(headers={"Content-Encoding": "gzip"}, body=member + member)

    async with make_client() as client:
        with pytest.raises(TooLargeError):
            await client.get(f"{server.base_url}/m")


@pytest.mark.parametrize(
    ("encoding", "body"),
    [
        ("deflate", zlib.compress(b"hello") + b"trailing"),
        ("gzip", gzip.compress(b"hello") + b"garbage!"),
        ("gzip", gzip.compress(b"hello") + gzip.compress(b"cut off")[:-4]),
    ],
)
async def test_data_after_the_compressed_stream_is_invalid_response(
    server: LoopbackServer, encoding: str, body: bytes
) -> None:
    server.routes["/t"] = Route(headers={"Content-Encoding": encoding}, body=body)

    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/t")

    assert info.value.reason == "invalid_response"


async def test_empty_body_with_a_content_encoding_is_empty(server: LoopbackServer) -> None:
    server.routes["/e"] = Route(headers={"Content-Encoding": "gzip"}, body=b"")

    async with make_client() as client:
        result = await client.get(f"{server.base_url}/e")

    assert isinstance(result, FetchResult)
    assert result.content == b""
