"""Foundation tests for ``SafeHttpClient``: result mapping, errors, redirects and lifecycle."""

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
from tests.http_helpers import LOOPBACK, LoopbackServer, Route, server  # noqa: F401


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
        "SafeHttpClient",
        "TooLargeError",
        "redact",
    }


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
