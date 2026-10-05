"""Client-level SSRF tests: blocked targets are never contacted and connections are pinned."""

import asyncio
import logging

import httpx2
import pytest

from invio.sources.http import (
    BlockedError,
    BlockReason,
    FetchError,
    FetchResult,
    HttpClientConfig,
    SafeHttpClient,
)
from tests.http_helpers import (
    LOOPBACK,
    FakeResolver,
    LoopbackServer,
    RecordingTransport,
    Route,
    server,  # noqa: F401
)

PUBLIC = "93.184.216.34"


def ok_handler(request: httpx2.Request) -> httpx2.Response:
    return httpx2.Response(200, content=b"ok")


def config(**overrides: object) -> HttpClientConfig:
    fields: dict[str, object] = {"respect_robots": False, "host_interval": 0.01} | overrides
    return HttpClientConfig.model_validate(fields)


@pytest.mark.parametrize("url", ["http://127.0.0.1/", "http://10.0.0.5/", "http://[::1]/"])
async def test_non_public_literal_is_blocked_before_sending(url: str) -> None:
    transport = RecordingTransport(ok_handler)

    async with SafeHttpClient(config(), transport=transport) as client:
        with pytest.raises(BlockedError) as info:
            await client.get(url)

    assert info.value.reason is BlockReason.NON_PUBLIC_ADDRESS
    assert transport.requests == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.org/", "gopher://h/"])
async def test_unsupported_scheme_is_blocked(url: str) -> None:
    transport = RecordingTransport(ok_handler)

    async with SafeHttpClient(config(), transport=transport) as client:
        with pytest.raises(BlockedError) as info:
            await client.get(url)

    assert info.value.reason is BlockReason.UNSUPPORTED_SCHEME
    assert transport.requests == []


async def test_hostname_resolving_to_private_address_is_blocked() -> None:
    transport = RecordingTransport(ok_handler)
    resolver = FakeResolver({"intranet.example": ["10.0.0.5"]})

    async with SafeHttpClient(config(), resolver=resolver, transport=transport) as client:
        with pytest.raises(BlockedError):
            await client.get("http://intranet.example/")

    assert transport.requests == []


async def test_dns_failure_is_dns_failed() -> None:
    async with SafeHttpClient(config(), resolver=FakeResolver({})) as client:
        with pytest.raises(Exception) as info:
            await client.get("http://missing.example/")

    assert getattr(info.value, "reason", None) == "dns_failed"


@pytest.mark.parametrize("target", ["http://10.0.0.5/admin", "http://192.168.1.1/admin"])
async def test_redirect_to_private_address_is_blocked(server: LoopbackServer, target: str) -> None:
    server.routes["/r"] = Route(status=302, headers={"Location": target})

    async with SafeHttpClient(config(), allow_networks=LOOPBACK) as client:
        with pytest.raises(BlockedError) as info:
            await client.get(f"{server.base_url}/r")

    assert info.value.reason is BlockReason.NON_PUBLIC_ADDRESS
    assert server.paths() == ["/r"]


async def test_redirect_to_other_scheme_is_blocked(server: LoopbackServer) -> None:
    server.routes["/r"] = Route(status=301, headers={"Location": "ftp://example.org/"})

    async with SafeHttpClient(config(), allow_networks=LOOPBACK) as client:
        with pytest.raises(BlockedError) as info:
            await client.get(f"{server.base_url}/r")

    assert info.value.reason is BlockReason.UNSUPPORTED_SCHEME
    assert server.paths() == ["/r"]


async def test_request_is_pinned_to_validated_address() -> None:
    transport = RecordingTransport(ok_handler)
    resolver = FakeResolver({"example.org": [PUBLIC]})

    async with SafeHttpClient(config(), resolver=resolver, transport=transport) as client:
        result = await client.get("https://example.org/feed?x=1")

    request = transport.requests[0]
    assert isinstance(result, FetchResult)
    assert result.url == "https://example.org/feed?x=1"  # logical URL, not the pinned one
    assert request.url.host == PUBLIC
    assert request.headers["Host"] == "example.org"
    assert request.extensions["sni_hostname"] == "example.org"
    assert request.url.path == "/feed"


async def test_non_default_port_is_kept_in_host_header() -> None:
    transport = RecordingTransport(ok_handler)
    resolver = FakeResolver({"example.org": [PUBLIC]})

    async with SafeHttpClient(config(), resolver=resolver, transport=transport) as client:
        await client.get("http://example.org:8080/")

    request = transport.requests[0]
    assert request.headers["Host"] == "example.org:8080"
    assert request.url.port == 8080
    assert "sni_hostname" not in request.extensions


async def test_ipv6_address_is_pinned() -> None:
    transport = RecordingTransport(ok_handler)
    resolver = FakeResolver({"v6.example": ["2606:4700::1111"]})

    async with SafeHttpClient(config(), resolver=resolver, transport=transport) as client:
        await client.get("http://v6.example/")

    assert transport.requests[0].url.host == "2606:4700::1111"


async def test_rebinding_after_validation_cannot_redirect_the_connection() -> None:
    transport = RecordingTransport(ok_handler)
    resolver = FakeResolver({"rebind.example": [[PUBLIC], ["127.0.0.1"]]})

    async with SafeHttpClient(config(), resolver=resolver, transport=transport) as client:
        await client.get("http://rebind.example/")

    assert [request.url.host for request in transport.requests] == [PUBLIC]
    assert resolver.calls == [("rebind.example", 80)]


async def test_resolver_is_consulted_once_per_hop() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/a":
            return httpx2.Response(302, headers={"Location": "/b"})
        return httpx2.Response(200, content=b"ok")

    resolver = FakeResolver({"example.org": [PUBLIC]})

    async with SafeHttpClient(
        config(), resolver=resolver, transport=RecordingTransport(handler)
    ) as client:
        await client.get("http://example.org/a")

    assert len(resolver.calls) == 2


async def test_connections_are_not_shared_between_host_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[dict[str, object]] = []
    real = httpx2.AsyncClient

    def spy(*args: object, **kwargs: object) -> httpx2.AsyncClient:
        created.append(kwargs)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(httpx2, "AsyncClient", spy)
    transport = RecordingTransport(ok_handler)
    resolver = FakeResolver({"a.example": [PUBLIC], "b.example": [PUBLIC]})

    async with SafeHttpClient(config(), resolver=resolver, transport=transport) as client:
        await client.get("https://a.example/")
        await client.get("https://b.example/")

    limits = created[0]["limits"]
    assert isinstance(limits, httpx2.Limits)
    assert limits.max_keepalive_connections == 0
    assert [r.extensions["sni_hostname"] for r in transport.requests] == ["a.example", "b.example"]


async def test_caller_host_header_is_ignored() -> None:
    transport = RecordingTransport(ok_handler)
    resolver = FakeResolver({"example.org": [PUBLIC]})

    async with SafeHttpClient(config(), resolver=resolver, transport=transport) as client:
        await client.get("http://example.org/", headers={"Host": "evil.example"})

    assert transport.requests[0].headers["Host"] == "example.org"


async def test_blocked_error_hides_userinfo() -> None:
    async with SafeHttpClient(config()) as client:
        with pytest.raises(BlockedError) as info:
            await client.get("http://user:secret@10.0.0.5/")

    assert "secret" not in str(info.value)
    assert "secret" not in info.value.url


# --- logging ---------------------------------------------------------------------------------


async def test_blocked_request_is_logged_as_http_blocked(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.http")

    async with SafeHttpClient(config()) as client:
        with pytest.raises(BlockedError):
            await client.get("http://user:secret@10.0.0.5/feed")

    (record,) = [r for r in caplog.records if r.name == "invio.sources.http"]
    assert record.getMessage() == "http_blocked"
    assert record.levelno == logging.WARNING
    assert record.__dict__["reason"] == "non_public_address"
    assert record.__dict__["host"] == "10.0.0.5"
    assert record.__dict__["url"] == "http://10.0.0.5/feed"
    assert "secret" not in caplog.text


async def test_failed_request_is_logged_as_http_failed(
    server: LoopbackServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.http")
    server.routes["/x"] = Route(status=503)

    async with SafeHttpClient(config(), allow_networks=LOOPBACK) as client:
        with pytest.raises(Exception, match="http_status"):
            await client.get(f"http://u:secret@127.0.0.1:{server.origin_port}/x")

    (record,) = [r for r in caplog.records if r.name == "invio.sources.http"]
    assert record.getMessage() == "http_failed"
    assert record.levelno == logging.WARNING
    assert record.__dict__["reason"] == "http_status"
    assert record.__dict__["status"] == 503
    assert record.__dict__["host"] == "127.0.0.1"
    assert "secret" not in caplog.text


async def test_success_is_not_logged_as_failure(
    server: LoopbackServer,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.http")
    server.routes["/"] = Route(body=b"x")

    async with SafeHttpClient(config(), allow_networks=LOOPBACK) as client:
        await client.get(f"{server.base_url}/")

    assert [r for r in caplog.records if r.name == "invio.sources.http"] == []


# --- review follow-ups: userinfo, credentials across origins, redirect bodies, DNS timeout ----


async def test_userinfo_is_stripped_from_result_and_never_sent() -> None:
    transport = RecordingTransport(ok_handler)
    resolver = FakeResolver({"example.org": [PUBLIC]})

    async with SafeHttpClient(config(), resolver=resolver, transport=transport) as client:
        result = await client.get("http://user:secret@example.org/feed")

    assert isinstance(result, FetchResult)
    assert "secret" not in result.url
    assert "secret" not in result.requested_url
    assert "authorization" not in transport.requests[0].headers


async def test_credentials_are_not_forwarded_to_another_origin() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.headers["Host"] == "a.example":
            return httpx2.Response(302, headers={"Location": "http://b.example/x"})
        return httpx2.Response(200, content=b"ok")

    transport = RecordingTransport(handler)
    resolver = FakeResolver({"a.example": [PUBLIC], "b.example": [PUBLIC]})
    sent = {"Authorization": "Bearer t", "Cookie": "s=1", "X-Keep": "1", "Accept": "text/xml"}

    async with SafeHttpClient(config(), resolver=resolver, transport=transport) as client:
        await client.get("http://a.example/", headers=sent)

    first, second = transport.requests
    assert first.headers["Authorization"] == "Bearer t"
    assert "authorization" not in second.headers
    assert "cookie" not in second.headers
    assert "x-keep" not in second.headers  # not on the allow-list
    assert second.headers["Accept"] == "text/xml"


async def test_large_redirect_body_is_not_read() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/a":
            return httpx2.Response(302, headers={"Location": "/b"}, content=b"x" * 5000)
        return httpx2.Response(200, content=b"ok")

    resolver = FakeResolver({"example.org": [PUBLIC]})

    async with SafeHttpClient(
        config(max_response_bytes=100), resolver=resolver, transport=RecordingTransport(handler)
    ) as client:
        result = await client.get("http://example.org/a")

    assert isinstance(result, FetchResult)
    assert result.content == b"ok"


async def test_slow_dns_is_a_timeout() -> None:
    class SlowResolver:
        async def resolve(self, host: str, port: int) -> list[str]:
            await asyncio.sleep(5)
            return [PUBLIC]

    async with SafeHttpClient(config(connect_timeout=0.1), resolver=SlowResolver()) as client:
        with pytest.raises(FetchError) as info:
            await client.get("http://slow.example/")

    assert info.value.reason == "timeout"


async def test_corrupt_content_encoding_is_invalid_response() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Content-Encoding": "gzip"}, content=b"not gzip")

    resolver = FakeResolver({"example.org": [PUBLIC]})

    async with SafeHttpClient(
        config(), resolver=resolver, transport=RecordingTransport(handler)
    ) as client:
        with pytest.raises(FetchError) as info:
            await client.get("http://example.org/")

    assert info.value.reason == "invalid_response"


@pytest.mark.parametrize("url", ["data:text/plain,hi", "javascript:alert(1)", "file:///etc/passwd"])
async def test_scheme_is_checked_before_host(url: str) -> None:
    async with SafeHttpClient(config()) as client:
        with pytest.raises(BlockedError) as info:
            await client.get(url)

    assert info.value.reason is BlockReason.UNSUPPORTED_SCHEME


async def test_failure_is_logged_exactly_once(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.http")

    async with SafeHttpClient(config()) as client:
        with pytest.raises(BlockedError):
            await client.get("http://10.0.0.1/")

    assert len([r for r in caplog.records if r.name == "invio.sources.http"]) == 1


@pytest.mark.parametrize("url", ["http://[::7f00:1]/", "http://[::127.0.0.1]/"])
async def test_ipv4_compatible_ipv6_literal_is_blocked_at_client_level(url: str) -> None:
    transport = RecordingTransport(ok_handler)

    async with SafeHttpClient(config(), transport=transport) as client:
        with pytest.raises(BlockedError) as info:
            await client.get(url)

    assert info.value.reason is BlockReason.NON_PUBLIC_ADDRESS
    assert transport.requests == []


@pytest.mark.parametrize(
    "url", ["user:secret@example.com/feed", "foo:bar@host", "http://u:secret@/x", "u:secret@"]
)
async def test_userinfo_without_slashes_never_leaks(
    url: str, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.http")

    async with SafeHttpClient(config()) as client:
        with pytest.raises(FetchError) as info:
            await client.get(url)

    assert "secret" not in str(info.value)
    assert "secret" not in info.value.url
    assert "bar@" not in str(info.value)
    assert "secret" not in caplog.text


async def test_userinfo_in_redirect_location_never_leaks() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(302, headers={"Location": "foo:secret@host"})

    resolver = FakeResolver({"example.org": [PUBLIC]})

    async with SafeHttpClient(
        config(), resolver=resolver, transport=RecordingTransport(handler)
    ) as client:
        with pytest.raises(FetchError) as info:
            await client.get("http://example.org/")

    assert "secret" not in str(info.value)


async def test_real_connection_goes_to_the_pinned_address_with_the_original_host(
    server: LoopbackServer,
) -> None:
    server.routes["/"] = Route(body=b"ok")
    resolver = FakeResolver({"pinned.test": ["127.0.0.1"]})

    async with SafeHttpClient(config(), resolver=resolver, allow_networks=LOOPBACK) as client:
        result = await client.get(f"http://pinned.test:{server.origin_port}/")

    assert isinstance(result, FetchResult)
    assert server.requests[0].headers["host"] == f"pinned.test:{server.origin_port}"
    assert result.url == f"http://pinned.test:{server.origin_port}/"


async def test_total_timeout_covers_the_whole_redirect_chain(
    server: LoopbackServer,
) -> None:
    for number in range(3):
        server.routes[f"/r{number}"] = Route(
            status=302, headers={"Location": f"/r{number + 1}"}, delay=0.3
        )
    server.routes["/r3"] = Route(body=b"end")

    async with SafeHttpClient(
        config(read_timeout=0.4, total_timeout=0.5), allow_networks=LOOPBACK
    ) as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/r0")

    assert info.value.reason == "timeout"
