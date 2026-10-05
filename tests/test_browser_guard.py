"""Unit tests for the request guard of the browser renderer, with fake Playwright routes.

They need the Playwright package (a dev dependency) but no browser: the guard only talks to
the objects it is handed. Redirect handling lives here because Chromium does not route the hops
of a redirect, so the guard has to follow and check them itself.
"""

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from invio.sources.browser import RENDER_TIMEOUT_SECONDS, _RequestGuard
from invio.sources.errors import TooLargeError
from invio.sources.http import HttpClientConfig, SafeHttpClient
from tests.http_helpers import FakeResolver

PUBLIC = "93.184.216.34"


class FakeResponse:
    def __init__(
        self,
        status: int = 200,
        location: str | None = None,
        *,
        body: bytes = b"",
        content_length: int | None = None,
    ) -> None:
        self.status = status
        self.headers = {} if location is None else {"location": location}
        if content_length is not None:
            self.headers["content-length"] = str(content_length)
        self._body = body
        self.body_reads = 0
        self.disposed = 0

    async def body(self) -> bytes:
        self.body_reads += 1
        return self._body

    async def dispose(self) -> None:
        self.disposed += 1


class FakeRoute:
    """Plays the part of a Playwright ``Route``; ``responses`` answer ``fetch`` in turn."""

    def __init__(
        self,
        url: str,
        responses: list[FakeResponse | Exception] | None = None,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        navigation: bool = False,
        main_frame: bool = True,
    ) -> None:
        self.request = SimpleNamespace(
            url=url,
            method=method,
            headers=headers or {},
            is_navigation_request=lambda: navigation,
            frame=SimpleNamespace(parent_frame=None if main_frame else object()),
        )
        self._responses = list(responses or [FakeResponse()])
        self.fetches: list[dict[str, Any]] = []
        self.fulfilled: FakeResponse | None = None
        self.aborted: str | None = None
        self.continued = False
        self.abort_error: Exception | None = None

    async def fetch(self, **kwargs: Any) -> FakeResponse:
        self.fetches.append(kwargs)
        item = self._responses.pop(0) if len(self._responses) > 1 else self._responses[0]
        if isinstance(item, Exception):
            raise item
        return item

    async def fulfill(self, *, response: FakeResponse) -> None:
        self.fulfilled = response

    async def abort(self, code: str) -> None:
        if self.abort_error is not None:
            raise self.abort_error
        self.aborted = code

    async def continue_(self) -> None:
        self.continued = True


class FakeSocket:
    def __init__(self, url: str, *, close_error: Exception | None = None) -> None:
        self.url = url
        self.connected = False
        self.closed = False
        self._close_error = close_error

    def connect_to_server(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True
        if self._close_error is not None:
            raise self._close_error


@pytest.fixture
def resolver() -> FakeResolver:
    return FakeResolver(
        {
            "a.example": [PUBLIC],
            "b.example": [PUBLIC],
            "internal.example": ["10.0.0.1"],
            "broken.example": [],
        }
    )


@pytest.fixture
async def small_guard(resolver: FakeResolver) -> Any:
    config = HttpClientConfig(max_response_bytes=100)
    async with SafeHttpClient(config, resolver=resolver) as client:
        yield _RequestGuard(client)


@pytest.fixture
async def guard(resolver: FakeResolver) -> Any:
    async with SafeHttpClient(HttpClientConfig(), resolver=resolver) as client:
        yield _RequestGuard(client)


async def test_an_allowed_request_is_fetched_without_redirects_and_fulfilled(
    guard: _RequestGuard,
) -> None:
    route = FakeRoute("https://a.example/x")

    await guard.route(route)  # type: ignore[arg-type]

    assert route.fetches == [{"max_redirects": 0}]
    assert route.fulfilled is not None
    assert route.aborted is None


async def test_the_verdict_is_cached_per_host(guard: _RequestGuard, resolver: FakeResolver) -> None:
    for path in ("/1", "/2", "/3"):
        await guard.route(FakeRoute(f"https://a.example{path}"))  # type: ignore[arg-type]
    await guard.route(FakeRoute("https://b.example/"))  # type: ignore[arg-type]

    assert [host for host, _ in resolver.calls] == ["a.example", "b.example"]


async def test_a_blocked_request_is_aborted_and_never_fetched(
    guard: _RequestGuard, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.browser")
    route = FakeRoute("https://internal.example/secret?token=abc")

    await guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"
    assert route.fetches == []
    assert [r.getMessage() for r in caplog.records] == ["browser_request_blocked"]
    assert caplog.records[0].__dict__["host"] == "internal.example"
    assert "token" not in caplog.text
    assert "secret" not in caplog.text


@pytest.mark.parametrize("url", ["ftp://a.example/x", "file:///etc/passwd", "chrome://version"])
async def test_non_web_schemes_are_aborted(guard: _RequestGuard, url: str) -> None:
    route = FakeRoute(url)

    await guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"
    assert route.fetches == []


@pytest.mark.parametrize(
    "url", ["data:text/plain,hi", "blob:https://a.example/uuid", "about:blank"]
)
async def test_local_schemes_are_passed_through(guard: _RequestGuard, url: str) -> None:
    route = FakeRoute(url)

    await guard.route(route)  # type: ignore[arg-type]

    assert route.continued
    assert route.fetches == []


async def test_a_failing_fetch_aborts_the_request(
    guard: _RequestGuard, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.browser")
    route = FakeRoute("https://a.example/x", [RuntimeError("connection reset")])

    await guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"
    assert [r.getMessage() for r in caplog.records] == ["browser_request_failed"]


async def test_an_abort_that_fails_is_ignored(guard: _RequestGuard) -> None:
    route = FakeRoute("https://internal.example/x")
    route.abort_error = RuntimeError("route already handled")

    await guard.route(route)  # type: ignore[arg-type]  # must not raise


async def test_an_unexpected_error_in_the_guard_aborts(
    guard: _RequestGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(self: SafeHttpClient, url: str) -> None:
        raise RuntimeError("resolver crashed")

    monkeypatch.setattr(SafeHttpClient, "check_target", explode)
    route = FakeRoute("https://a.example/x")

    await guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"


# --- redirects: every hop is checked before it is requested ----------------------------------


async def test_a_redirect_to_an_allowed_host_is_followed(guard: _RequestGuard) -> None:
    hop = FakeResponse(302, "https://b.example/next")
    final = FakeResponse(200)
    route = FakeRoute("https://a.example/x", [hop, final], headers={"x-test": "1"})

    await guard.route(route)  # type: ignore[arg-type]

    assert route.fulfilled is final
    assert hop.disposed
    assert final.disposed  # after it was handed to the browser
    assert route.fetches[1]["url"] == "https://b.example/next"
    assert route.fetches[1]["max_redirects"] == 0


async def test_relative_locations_resolve_against_the_current_url(guard: _RequestGuard) -> None:
    route = FakeRoute("https://a.example/dir/x", [FakeResponse(301, "../y"), FakeResponse()])

    await guard.route(route)  # type: ignore[arg-type]

    assert route.fetches[1]["url"] == "https://a.example/y"
    assert route.fetches[1]["headers"] is None  # same origin: the request headers stay


async def test_a_redirect_to_a_blocked_host_is_aborted_without_a_second_fetch(
    guard: _RequestGuard,
) -> None:
    route = FakeRoute("https://a.example/x", [FakeResponse(302, "http://internal.example/in")])

    await guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"
    assert len(route.fetches) == 1


async def test_the_second_hop_is_checked_too(guard: _RequestGuard) -> None:
    route = FakeRoute(
        "https://a.example/x",
        [FakeResponse(302, "https://b.example/"), FakeResponse(301, "http://10.0.0.1/")],
    )

    await guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"
    assert len(route.fetches) == 2


@pytest.mark.parametrize("location", ["ftp://a.example/f", "file:///etc/passwd", "data:,x"])
async def test_a_redirect_to_a_non_web_scheme_is_aborted(
    guard: _RequestGuard, location: str
) -> None:
    route = FakeRoute("https://a.example/x", [FakeResponse(302, location)])

    await guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"


async def test_credentials_do_not_follow_a_redirect_to_another_origin(guard: _RequestGuard) -> None:
    headers = {"Authorization": "Bearer t", "proxy-authorization": "x", "Accept": "*/*"}
    route = FakeRoute(
        "https://a.example/x",
        [FakeResponse(302, "https://b.example/y"), FakeResponse()],
        headers=headers,
    )

    await guard.route(route)  # type: ignore[arg-type]

    assert route.fetches[1]["headers"] == {"Accept": "*/*"}


async def test_a_redirected_post_is_aborted(guard: _RequestGuard) -> None:
    route = FakeRoute("https://a.example/x", [FakeResponse(307, "/y")], method="POST")

    await guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"
    assert len(route.fetches) == 1


async def test_a_redirect_without_location_is_passed_on_as_it_is(guard: _RequestGuard) -> None:
    response = FakeResponse(302)
    route = FakeRoute("https://a.example/x", [response])

    await guard.route(route)  # type: ignore[arg-type]

    assert route.fulfilled is response


async def test_too_many_redirects_are_aborted(guard: _RequestGuard) -> None:
    route = FakeRoute("https://a.example/x", [FakeResponse(302, "/x")])

    await guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"
    assert len(route.fetches) == 11  # the request plus ten hops


# --- the main document's final URL -----------------------------------------------------------


async def test_the_final_url_of_a_main_navigation_is_recorded(guard: _RequestGuard) -> None:
    route = FakeRoute(
        "https://a.example/old",
        [FakeResponse(301, "/new"), FakeResponse()],
        navigation=True,
    )

    await guard.route(route)  # type: ignore[arg-type]

    assert guard.document_url == "https://a.example/new"


@pytest.mark.parametrize(("navigation", "main_frame"), [(False, True), (True, False)])
async def test_other_requests_do_not_set_the_document_url(
    guard: _RequestGuard, navigation: bool, main_frame: bool
) -> None:
    route = FakeRoute(
        "https://a.example/x",
        [FakeResponse(301, "/y"), FakeResponse()],
        navigation=navigation,
        main_frame=main_frame,
    )

    await guard.route(route)  # type: ignore[arg-type]

    assert guard.document_url is None


# --- WebSockets ------------------------------------------------------------------------------


async def test_an_allowed_websocket_is_connected(guard: _RequestGuard) -> None:
    socket = FakeSocket("wss://a.example/feed")

    await guard.web_socket(socket)  # type: ignore[arg-type]

    assert socket.connected
    assert not socket.closed


async def test_a_blocked_websocket_is_closed(guard: _RequestGuard) -> None:
    socket = FakeSocket("ws://internal.example/feed")

    await guard.web_socket(socket)  # type: ignore[arg-type]

    assert socket.closed
    assert not socket.connected


async def test_a_websocket_that_cannot_be_checked_is_closed(
    guard: _RequestGuard, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(self: SafeHttpClient, url: str) -> None:
        raise RuntimeError("resolver crashed")

    monkeypatch.setattr(SafeHttpClient, "check_target", explode)
    socket = FakeSocket("ws://a.example/feed", close_error=RuntimeError("already closed"))

    await guard.web_socket(socket)  # type: ignore[arg-type]  # a failing close does not raise

    assert socket.closed
    assert not socket.connected


async def test_websocket_and_http_share_one_verdict(
    guard: _RequestGuard, resolver: FakeResolver
) -> None:
    await guard.route(FakeRoute("https://a.example/"))  # type: ignore[arg-type]
    await guard.web_socket(FakeSocket("wss://a.example/"))  # type: ignore[arg-type]

    assert len(resolver.calls) == 1


# --- credentials follow only the same origin -------------------------------------------------

SENSITIVE = {
    "Authorization": "Bearer t",
    "proxy-authorization": "x",
    "Cookie": "a=1",
    "Accept": "*/*",
}


@pytest.mark.parametrize(
    "target",
    [
        "http://a.example/y",  # https -> http on the same host
        "https://a.example:8443/y",  # another port
        "https://b.example/y",
    ],
)
async def test_credentials_are_dropped_unless_the_origin_is_the_same(
    guard: _RequestGuard, target: str
) -> None:
    route = FakeRoute(
        "https://a.example/x", [FakeResponse(302, target), FakeResponse()], headers=SENSITIVE
    )

    await guard.route(route)  # type: ignore[arg-type]

    assert route.fetches[1]["headers"] == {"Accept": "*/*"}


@pytest.mark.parametrize("target", ["/y", "https://a.example:443/y", "https://A.EXAMPLE/y"])
async def test_the_same_origin_keeps_the_headers(guard: _RequestGuard, target: str) -> None:
    route = FakeRoute(
        "https://a.example/x", [FakeResponse(302, target), FakeResponse()], headers=SENSITIVE
    )

    await guard.route(route)  # type: ignore[arg-type]

    assert route.fetches[1]["headers"] is None


# --- size cap --------------------------------------------------------------------------------


async def test_a_sub_resource_over_the_cap_by_content_length_is_aborted_unread(
    small_guard: _RequestGuard,
) -> None:
    response = FakeResponse(content_length=101, body=b"x" * 101)
    route = FakeRoute("https://a.example/big.js", [response])

    await small_guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"
    assert response.body_reads == 0
    assert response.disposed
    assert small_guard.failure is None  # a sub-resource does not fail the render


async def test_a_sub_resource_over_the_cap_by_actual_size_is_aborted(
    small_guard: _RequestGuard,
) -> None:
    response = FakeResponse(body=b"x" * 101)  # no or a lying Content-Length
    route = FakeRoute("https://a.example/big.js", [response])

    await small_guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"
    assert response.disposed


async def test_a_response_at_the_cap_is_fulfilled(small_guard: _RequestGuard) -> None:
    route = FakeRoute(
        "https://a.example/ok.js", [FakeResponse(body=b"x" * 100, content_length=100)]
    )

    await small_guard.route(route)  # type: ignore[arg-type]

    assert route.fulfilled is not None


@pytest.mark.parametrize("by_header", [True, False])
async def test_an_oversized_main_document_fails_the_render(
    small_guard: _RequestGuard, by_header: bool
) -> None:
    response = FakeResponse(content_length=101 if by_header else None, body=b"x" * 101)
    route = FakeRoute("https://a.example/", [response], navigation=True)

    await small_guard.route(route)  # type: ignore[arg-type]

    assert route.aborted == "blockedbyclient"
    assert isinstance(small_guard.failure, TooLargeError)
    assert small_guard.failure.reason == "too_large"
    assert small_guard.failure.limit == 100


async def test_a_redirected_oversized_document_is_judged_by_its_final_response(
    small_guard: _RequestGuard,
) -> None:
    route = FakeRoute(
        "https://a.example/",
        [FakeResponse(302, "/big"), FakeResponse(content_length=500)],
        navigation=True,
    )

    await small_guard.route(route)  # type: ignore[arg-type]

    assert isinstance(small_guard.failure, TooLargeError)
    assert small_guard.failure.url == "https://a.example/big"


# --- responses are always released -----------------------------------------------------------


async def test_the_response_is_disposed_when_redirects_are_exhausted(guard: _RequestGuard) -> None:
    responses = [FakeResponse(302, "/x") for _ in range(11)]
    route = FakeRoute("https://a.example/x", responses)  # type: ignore[arg-type]

    await guard.route(route)  # type: ignore[arg-type]

    assert len(route.fetches) == 11
    assert all(r.disposed for r in responses)


async def test_the_response_is_disposed_when_a_hop_is_blocked(guard: _RequestGuard) -> None:
    last = FakeResponse(302, "http://internal.example/")
    route = FakeRoute("https://a.example/x", [last])

    await guard.route(route)  # type: ignore[arg-type]

    assert last.disposed


async def test_the_response_is_disposed_when_fulfilling_fails(guard: _RequestGuard) -> None:
    response = FakeResponse()
    route = FakeRoute("https://a.example/x", [response])

    async def boom(*, response: FakeResponse) -> None:
        raise RuntimeError("target closed")

    route.fulfill = boom  # type: ignore[method-assign]

    await guard.route(route)  # type: ignore[arg-type]

    assert response.disposed
    assert route.aborted == "blockedbyclient"


# --- verdict cache ---------------------------------------------------------------------------


async def test_a_transient_lookup_failure_is_not_cached(
    guard: _RequestGuard, resolver: FakeResolver
) -> None:
    await guard.route(FakeRoute("https://broken.example/1"))  # type: ignore[arg-type]
    await guard.route(FakeRoute("https://broken.example/2"))  # type: ignore[arg-type]

    assert [h for h, _ in resolver.calls] == ["broken.example", "broken.example"]


async def test_a_blocked_verdict_is_cached(guard: _RequestGuard, resolver: FakeResolver) -> None:
    await guard.route(FakeRoute("https://internal.example/1"))  # type: ignore[arg-type]
    await guard.route(FakeRoute("https://internal.example/2"))  # type: ignore[arg-type]

    assert [h for h, _ in resolver.calls] == ["internal.example"]


async def test_the_first_response_is_disposed_when_the_next_hop_fails(guard: _RequestGuard) -> None:
    first = FakeResponse(302, "/y")
    route = FakeRoute("https://a.example/x", [first, RuntimeError("connection reset")])

    await guard.route(route)  # type: ignore[arg-type]

    assert first.disposed
    assert route.aborted == "blockedbyclient"


def test_the_render_cap_is_30_seconds() -> None:
    assert RENDER_TIMEOUT_SECONDS == 30.0  # FR-018
