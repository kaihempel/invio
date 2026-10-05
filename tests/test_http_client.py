"""Foundation tests for ``SafeHttpClient``: result mapping, errors, redirects and lifecycle."""

from collections.abc import Callable

import httpx2
import pytest
from pydantic import ValidationError

import invio
from invio.config.settings import Settings
from invio.sources import http
from invio.sources.http import (
    BlockedError,
    BlockReason,
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


def make_client(**overrides: object) -> SafeHttpClient:
    """A client for loopback tests: robots off and a short per-host interval."""
    fields: dict[str, object] = {"respect_robots": False, "host_interval": 0.01} | overrides
    return SafeHttpClient(HttpClientConfig.model_validate(fields), allow_networks=LOOPBACK)


# --- successful and failing responses --------------------------------------------------------


async def test_200_returns_fetch_result(server: LoopbackServer) -> None:
    server.routes["/feed"] = Route(
        headers={"Content-Type": "text/plain; charset=utf-8", "ETag": '"abc"'}, body=b"hello"
    )
    url = f"{server.base_url}/feed"

    async with make_client() as client:
        result = await client.get(url)

    assert isinstance(result, FetchResult)
    assert (result.status, result.content) == (200, b"hello")
    assert (result.url, result.requested_url) == (url, url)
    assert result.headers["Content-Type"] == "text/plain; charset=utf-8"  # case-insensitive
    assert result.etag == '"abc"'
    assert result.last_modified is None
    assert result.text() == "hello"


async def test_user_agent_and_host_are_set_by_the_client(server: LoopbackServer) -> None:
    server.routes["/"] = Route(body=b"x")
    extra = {"User-Agent": "evil", "Host": "evil.example", "X-A": "1"}

    async with make_client() as client:
        await client.get(f"{server.base_url}/", headers=extra)

    sent = server.requests[0].headers
    assert sent["user-agent"] == HttpClientConfig().user_agent
    assert sent["host"] == f"127.0.0.1:{server.origin_port}"
    assert sent["accept-encoding"] == "gzip, deflate"
    assert sent["x-a"] == "1"


@pytest.mark.parametrize("status", [404, 503])
async def test_error_status_raises_fetch_error(server: LoopbackServer, status: int) -> None:
    server.routes["/x"] = Route(status=status)

    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/x")

    assert (info.value.reason, info.value.status) == ("http_status", status)


async def test_connection_refused_is_connection_failed(server: LoopbackServer) -> None:
    url = f"{server.base_url}/"
    server.stop()

    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(url)

    assert info.value.reason == "connection_failed"


@pytest.mark.parametrize("url", ["", "http://", "http:///path", "http://[::1/", "not a url"])
async def test_malformed_url_is_invalid_url(url: str) -> None:
    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(url)

    assert info.value.reason == "invalid_url"


# --- redirects -------------------------------------------------------------------------------


async def test_relative_redirect_is_followed(server: LoopbackServer) -> None:
    server.routes["/old"] = Route(status=302, headers={"Location": "/new"})
    server.routes["/new"] = Route(body=b"moved")
    requested = f"{server.base_url}/old"

    async with make_client() as client:
        result = await client.get(requested)

    assert isinstance(result, FetchResult)
    assert result.content == b"moved"
    assert result.url == f"{server.base_url}/new"
    assert result.requested_url == requested


async def test_redirect_without_location_fails(server: LoopbackServer) -> None:
    server.routes["/r"] = Route(status=302)

    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/r")

    assert info.value.reason == "missing_location"


async def test_too_many_redirects_fails(server: LoopbackServer) -> None:
    server.routes["/a"] = Route(status=301, headers={"Location": "/a"})

    async with make_client(max_redirects=2) as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/a")

    assert info.value.reason == "too_many_redirects"
    assert server.paths() == ["/a", "/a", "/a"]  # the initial request plus two hops


async def test_zero_redirects_allowed_rejects_first_redirect(server: LoopbackServer) -> None:
    server.routes["/a"] = Route(status=302, headers={"Location": "/b"})

    async with make_client(max_redirects=0) as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/a")

    assert info.value.reason == "too_many_redirects"


# --- types -----------------------------------------------------------------------------------


def test_error_subclasses() -> None:
    blocked = BlockedError(BlockReason.NON_PUBLIC_ADDRESS, url="http://h/")
    too_large = TooLargeError(url="http://h/", limit=5)

    assert isinstance(blocked, FetchError)
    assert blocked.reason is BlockReason.NON_PUBLIC_ADDRESS
    assert isinstance(too_large, FetchError)
    assert (too_large.reason, too_large.limit) == ("too_large", 5)


def test_error_message_and_url_are_redacted() -> None:
    error = FetchError("x", url="http://u:p@h/")

    assert "u:p" not in str(error)
    assert "u:p" not in error.url
    assert str(error) == "x: http://h/"
    assert str(FetchError("x", url="http://h/", status=500)) == "x: http://h/ (HTTP 500)"


def test_fetch_result_text_uses_header_charset_and_replaces_errors() -> None:
    latin = FetchResult(
        url="u",
        requested_url="u",
        status=200,
        headers={"content-type": "text/html; charset=latin-1"},
        content="café".encode("latin-1"),
        etag=None,
        last_modified=None,
    )
    broken = FetchResult("u", "u", 200, {}, b"a\xffb", None, None)

    assert latin.text() == "café"
    assert latin.text("ascii") == "caf�"
    assert broken.text() == "a�b"


def test_fetch_result_text_ignores_unknown_charset() -> None:
    result = FetchResult("u", "u", 200, {"content-type": "text/x; charset=nope"}, b"ok", None, None)

    assert result.text() == "ok"


def test_config_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        HttpClientConfig.model_validate({"foo": 1})


@pytest.mark.parametrize(
    "overrides",
    [
        {"contact": ""},
        {"contact": "a\nb"},
        {"max_response_bytes": 0},
        {"max_redirects": -1},
        {"read_timeout": float("nan")},
        {"host_interval": 0},
        {"read_timeout": 30.0, "total_timeout": 10.0},
        {"total_timeout": 5.0},  # below the default read timeout
    ],
)
def test_config_validation(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        HttpClientConfig.model_validate(overrides)


def test_config_is_frozen() -> None:
    config = HttpClientConfig()

    with pytest.raises(ValidationError):
        config.contact = "x"  # type: ignore[misc]


def test_config_defaults_and_user_agent() -> None:
    config = HttpClientConfig()

    assert config.user_agent == (
        f"invio/{invio.__version__} (+https://github.com/kaihempel/invio; "
        "contact: admin@example.invalid)"
    )
    assert (config.max_response_bytes, config.max_redirects) == (10_485_760, 5)
    assert (config.connect_timeout, config.read_timeout, config.total_timeout) == (10.0, 30.0, 60.0)
    assert (config.host_interval, config.respect_robots) == (1.0, True)


def test_config_from_settings_maps_all_fields() -> None:
    settings = Settings(
        http_contact="ops@example.org",
        http_max_response_bytes=123,
        http_max_redirects=2,
        http_connect_timeout_seconds=1.5,
        http_read_timeout_seconds=2.5,
        http_total_timeout_seconds=3.5,
        http_host_interval_seconds=0.25,
        http_respect_robots=False,
    )

    config = HttpClientConfig.from_settings(settings)

    assert config == HttpClientConfig(
        contact="ops@example.org",
        max_response_bytes=123,
        max_redirects=2,
        connect_timeout=1.5,
        read_timeout=2.5,
        total_timeout=3.5,
        host_interval=0.25,
        respect_robots=False,
    )


def test_config_from_settings_defaults_to_global_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INVIO_HTTP_MAX_REDIRECTS", "7")

    assert HttpClientConfig.from_settings().max_redirects == 7


def test_public_names() -> None:
    assert set(http.__all__) == {
        "BlockReason",
        "BlockedError",
        "FetchError",
        "FetchResult",
        "HttpClientConfig",
        "NotModified",
        "RenderUnavailableError",
        "SafeHttpClient",
        "TooLargeError",
        "charset_label",
    }


@pytest.mark.parametrize(
    ("content_type", "label"),
    [
        ("text/html; charset=UTF-8", "UTF-8"),
        ('text/html; charset="gb2312"', "gb2312"),
        ("text/html;charset = 'koi8-r' ; x=y", "koi8-r"),
        ("text/html", None),
        ("", None),
    ],
)
def test_charset_label_is_returned_as_written(content_type: str, label: str | None) -> None:
    assert http.charset_label(content_type) == label


# --- lifecycle -------------------------------------------------------------------------------


async def test_context_manager_closes_and_aclose_is_idempotent() -> None:
    client = SafeHttpClient(HttpClientConfig())

    async with client as entered:
        assert entered is client
    await client.aclose()
    await client.aclose()


async def test_default_config_comes_from_settings(
    server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INVIO_HTTP_CONTACT", "me@example.org")
    monkeypatch.setenv("INVIO_HTTP_RESPECT_ROBOTS", "false")
    server.routes["/"] = Route(body=b"x")

    async with SafeHttpClient(allow_networks=LOOPBACK) as client:
        await client.get(f"{server.base_url}/")

    assert "me@example.org" in server.requests[0].headers["user-agent"]


def test_config_rejects_non_ascii_contact() -> None:
    with pytest.raises(ValidationError, match="contact"):
        HttpClientConfig(contact="café@example.org")


async def test_error_status_after_redirect_carries_the_final_url(server: LoopbackServer) -> None:
    server.routes["/old"] = Route(status=301, headers={"Location": "/gone"})
    server.routes["/gone"] = Route(status=410)

    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/old")

    assert (info.value.reason, info.value.status) == ("http_status", 410)
    assert info.value.url == f"{server.base_url}/gone"


@pytest.mark.parametrize(
    "location", ["http://[::1/broken", "https:?x"], ids=["bad-port", "no-host"]
)
async def test_malformed_redirect_target_is_invalid_response(
    server: LoopbackServer, location: str
) -> None:
    # contracts/python-api.md: "malformed redirect target" -> invalid_response (the caller's
    # URL was fine; the server's answer was not).
    server.routes["/r"] = Route(status=302, headers={"Location": location})

    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(f"{server.base_url}/r")

    assert type(info.value) is FetchError
    assert info.value.reason == "invalid_response"
    assert server.paths() == ["/r"]


async def test_other_protocol_errors_are_connection_failed() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.RemoteProtocolError("Server disconnected without sending a response.")

    client = SafeHttpClient(
        HttpClientConfig(respect_robots=False, host_interval=0.01),
        resolver=FakeResolver({"example.org": ["93.184.216.34"]}),
        transport=RecordingTransport(handler),
    )
    async with client:
        with pytest.raises(FetchError) as info:
            await client.get("http://example.org/")

    assert info.value.reason == "connection_failed"


def test_non_public_allowance_is_not_configurable() -> None:
    # FR-010: only the ``allow_networks`` constructor argument can open loopback/private
    # targets; neither the client config nor the application settings (env, job files) can.
    with pytest.raises(ValidationError):
        HttpClientConfig.model_validate({"allow_networks": ["127.0.0.0/8"]})
    assert not [name for name in Settings.model_fields if "allow" in name or "network" in name]


# --- several resolved addresses --------------------------------------------------------------


def dual_stack_client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> tuple[SafeHttpClient, RecordingTransport]:
    transport = RecordingTransport(handler)
    client = SafeHttpClient(
        HttpClientConfig(respect_robots=False, host_interval=0.01),
        resolver=FakeResolver({"example.org": ["2606:4700::1111", "93.184.216.34"]}),
        transport=transport,
    )
    return client, transport


@pytest.mark.parametrize("error", [httpx2.ConnectError, httpx2.ConnectTimeout])
async def test_unreachable_first_address_falls_back_to_the_next(
    error: type[httpx2.TransportError],
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.host == "2606:4700::1111":
            raise error("no route to host")
        return httpx2.Response(200, content=b"ok")

    client, transport = dual_stack_client(handler)
    async with client:
        result = await client.get("https://example.org/feed")

    assert isinstance(result, FetchResult)
    assert [r.url.host for r in transport.requests] == ["2606:4700::1111", "93.184.216.34"]
    assert all(r.headers["host"] == "example.org" for r in transport.requests)
    assert all(r.extensions["sni_hostname"] == "example.org" for r in transport.requests)


async def test_every_address_unreachable_is_connection_failed() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("no route to host")

    client, transport = dual_stack_client(handler)
    async with client:
        with pytest.raises(FetchError) as info:
            await client.get("https://example.org/feed")

    assert info.value.reason == "connection_failed"
    assert len(transport.requests) == 2


async def test_failure_after_connecting_is_not_retried_on_another_address() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadError("connection reset")

    client, transport = dual_stack_client(handler)
    async with client:
        with pytest.raises(FetchError) as info:
            await client.get("https://example.org/feed")

    assert info.value.reason == "connection_failed"
    assert len(transport.requests) == 1


# --- cookies and query strings ---------------------------------------------------------------


async def test_cookies_are_never_stored_or_sent(server: LoopbackServer) -> None:
    server.routes["/login"] = Route(headers={"Set-Cookie": "session=abc; Path=/"}, body=b"x")
    server.routes["/next"] = Route(body=b"y")

    async with make_client() as client:
        await client.get(f"{server.base_url}/login")
        await client.get(f"{server.base_url}/next")

    assert "cookie" not in server.requests[1].headers


async def test_cookies_do_not_cross_between_hosts_on_one_address() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"Set-Cookie": "session=abc"}, content=b"x")

    transport = RecordingTransport(handler)
    shared = ["93.184.216.34"]
    client = SafeHttpClient(
        HttpClientConfig(respect_robots=False, host_interval=0.01),
        resolver=FakeResolver({"a.example": shared, "b.example": shared}),
        transport=transport,
    )
    async with client:
        await client.get("http://a.example/")
        await client.get("http://b.example/")

    assert "cookie" not in transport.requests[1].headers


async def test_query_and_fragment_stay_out_of_messages_and_logs(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level("DEBUG", logger="invio.sources.http")
    url = f"{server.base_url}/feed?token=s3cret#frag"

    async with make_client() as client:
        with pytest.raises(FetchError) as info:
            await client.get(url)

    assert info.value.url == url  # kept for the caller, only redacted of userinfo
    assert str(info.value) == f"http_status: {server.base_url}/feed (HTTP 404)"  # no route: 404
    assert caplog.records[0].__dict__["url"] == f"{server.base_url}/feed"
    assert "s3cret" not in caplog.text
