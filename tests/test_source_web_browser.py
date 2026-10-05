"""Tests for ``render: js`` with a real headless Chromium (``-m browser``).

They use loopback origins and ``allow_networks=LOOPBACK``. Anything outside the allow-list
(``10.255.255.1`` here) is a private address that the guard must refuse; it is also unroutable,
so a request that slipped through would hang until the render timeout instead of failing at
once. Timing is driven by the test server (a route that answers only when the test sets an
event, or never), not by sleeps in the tests.
"""

import asyncio
import base64
import hashlib
import logging
import os
import socket
import socketserver
import sys
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest
from playwright.async_api import Error as PlaywrightError

from invio.config.job import WebSource
from invio.sources import browser
from invio.sources.browser import PlaywrightRenderer
from invio.sources.http import (
    BlockedError,
    BlockReason,
    FetchError,
    HttpClientConfig,
    RenderUnavailableError,
    SafeHttpClient,
)
from invio.sources.web import WebPageSource
from tests.http_helpers import (  # noqa: F401
    HTML,
    LOOPBACK,
    LoopbackServer,
    Route,
    second_server,
    server,
    web_source,
)

TEXT = {"Content-Type": "text/plain"}
PRIVATE = "10.255.255.1"
MARK = (
    "function mark(c){const d=document.createElement('div');d.className=c;"
    "d.textContent=c;document.body.appendChild(d)}"
)

pytestmark = pytest.mark.browser

type MakeRenderer = Callable[..., Awaitable[tuple[PlaywrightRenderer, SafeHttpClient]]]


@pytest.fixture
async def make_renderer() -> AsyncIterator[MakeRenderer]:
    """Start a renderer (skipping the test when Chromium cannot be launched)."""
    opened: list[tuple[PlaywrightRenderer, SafeHttpClient]] = []

    async def make(
        *, respect_robots: bool = False, max_response_bytes: int = 10_485_760
    ) -> tuple[PlaywrightRenderer, SafeHttpClient]:
        config = HttpClientConfig(
            respect_robots=respect_robots,
            host_interval=0.01,
            max_response_bytes=max_response_bytes,
        )
        client = SafeHttpClient(config, allow_networks=LOOPBACK)
        try:
            renderer = await PlaywrightRenderer.start(client)
        except RenderUnavailableError:
            await client.aclose()
            if os.environ.get("CI"):  # CI installs Chromium: a missing one is a broken setup
                pytest.fail("Chromium is not available in CI (uv run playwright install chromium)")
            pytest.skip("Chromium is not available (run: uv run playwright install chromium)")
        opened.append((renderer, client))
        return renderer, client

    yield make
    for renderer, client in opened:
        await renderer.aclose()
        await client.aclose()


@pytest.fixture
async def started(make_renderer: MakeRenderer) -> tuple[PlaywrightRenderer, SafeHttpClient]:
    return await make_renderer()


@pytest.fixture
def renderer(started: tuple[PlaywrightRenderer, SafeHttpClient]) -> PlaywrightRenderer:
    return started[0]


@pytest.fixture
def client(started: tuple[PlaywrightRenderer, SafeHttpClient]) -> SafeHttpClient:
    return started[1]


def page(server: LoopbackServer, body: str, path: str = "/page") -> str:
    html = f"<html><head><title>T</title></head><body>{body}</body></html>"
    server.routes[path] = Route(headers=HTML, body=html.encode())
    return f"{server.base_url}{path}"


def config(url: str, **fields: object) -> WebSource:
    return web_source(url, render="js", **fields)


async def request_seen(server: LoopbackServer, path: str) -> None:
    """Return once the server has received a request for ``path``."""
    async with asyncio.timeout(20):
        while path not in server.paths():
            await asyncio.sleep(0.01)


@pytest.fixture
def short_timeout(monkeypatch: pytest.MonkeyPatch) -> float:
    monkeypatch.setattr(browser, "RENDER_TIMEOUT_SECONDS", 2.0)
    return 2.0


@pytest.fixture
def checked_urls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record every URL the browser guard asks the client about (the client still decides)."""
    seen: list[str] = []
    original = SafeHttpClient.check_target

    async def spy(self: SafeHttpClient, url: str) -> None:
        seen.append(url)
        await original(self, url)

    monkeypatch.setattr(SafeHttpClient, "check_target", spy)
    return seen


# --- rendering -------------------------------------------------------------------------------


async def test_text_inserted_by_script_reaches_the_hash(
    server: LoopbackServer, renderer: PlaywrightRenderer, client: SafeHttpClient
) -> None:
    server.routes["/data"] = Route(headers=TEXT, body=b"from-script")
    url = page(
        server,
        '<div id="out"></div><script>fetch("/data").then(r => r.text())'
        '.then(t => { document.getElementById("out").textContent = t })</script>',
    )

    async with WebPageSource(client, renderer=renderer) as source:
        [candidate] = await source.fetch(config(url))

    assert candidate.teaser == "from-script"
    assert candidate.content_hash == hashlib.sha256(b"from-script").hexdigest()
    assert candidate.url == url


async def test_links_inserted_by_script_are_discovered(
    server: LoopbackServer, renderer: PlaywrightRenderer, client: SafeHttpClient
) -> None:
    url = page(
        server,
        '<ul id="list"><li><a href="/news/static">Static</a></li></ul><script>'
        'for (const n of ["one", "two"]) { const a = document.createElement("a");'
        'a.href = "/news/" + n; a.textContent = "News " + n;'
        'document.getElementById("list").appendChild(a) }</script><a href="/about">About</a>',
    )

    async with WebPageSource(client, renderer=renderer) as source:
        found = await source.fetch(
            config(url, mode="links", selector="#list", url_pattern=r"/news/")
        )

    assert [c.url for c in found] == [
        f"{server.base_url}/news/static",
        f"{server.base_url}/news/one",
        f"{server.base_url}/news/two",
    ]
    assert [c.title for c in found] == ["Static", "News one", "News two"]


async def test_selector_and_wait_for_apply_to_the_rendered_region(
    server: LoopbackServer, renderer: PlaywrightRenderer, client: SafeHttpClient
) -> None:
    gate = threading.Event()
    server.routes["/data"] = Route(headers=TEXT, body=b"late news", gate=gate)
    url = page(
        server,
        f"<aside>noise</aside><script>{MARK}"
        'fetch("/data").then(r => r.text()).then(t => {'
        'const d = document.createElement("div"); d.className = "posts"; d.textContent = t;'
        "document.body.appendChild(d) })</script>",
    )

    async with WebPageSource(client, renderer=renderer) as source:
        fetching = asyncio.create_task(
            source.fetch(config(url, selector=".posts", wait_for=".posts"))
        )
        await request_seen(server, "/data")
        gate.set()
        [candidate] = await fetching

    assert candidate.teaser == "late news"
    assert candidate.content_hash == hashlib.sha256(b"late news").hexdigest()


async def test_the_final_url_is_the_page_url_after_redirects(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    target = page(server, "<p>moved</p>", "/new")
    server.routes["/old"] = Route(status=302, headers={"Location": "/new"})

    rendered = await renderer.render(f"{server.base_url}/old", wait_for=None)

    assert rendered.url == target
    assert "moved" in rendered.html


async def test_wait_for_waits_for_an_element_added_after_a_held_request(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    gate = threading.Event()
    server.routes["/step1"] = Route(headers=TEXT, body=b"1")
    server.routes["/step2"] = Route(headers=TEXT, body=b"late-content", gate=gate)
    url = page(
        server,
        "<p>early</p><script>"
        'fetch("/step1").then(() => setTimeout(() => {'
        'fetch("/step2").then(r => r.text()).then(t => {'
        'const d = document.createElement("div"); d.className = "late"; d.textContent = t;'
        "document.body.appendChild(d)})}, 1000))</script>",
    )

    task = asyncio.create_task(renderer.render(url, wait_for=".late"))
    await request_seen(server, "/step2")
    gate.set()
    rendered = await task

    assert "late-content" in rendered.html
    assert "early" in rendered.html


async def test_every_render_gets_a_fresh_context_and_the_clients_user_agent(
    server: LoopbackServer, renderer: PlaywrightRenderer, client: SafeHttpClient
) -> None:
    url = page(
        server,
        "<script>document.body.textContent = 'cookie:[' + document.cookie + ']'"
        "+ ' ls:[' + (localStorage.getItem('k') || '') + ']';"
        "document.cookie = 'seen=1'; localStorage.setItem('k', 'v')</script>",
    )

    first = await renderer.render(url, wait_for=None)
    second = await renderer.render(url, wait_for=None)

    assert "cookie:[] ls:[]" in first.html
    assert "cookie:[] ls:[]" in second.html  # nothing survived the first render
    user_agents = {r.headers["user-agent"] for r in server.requests}
    assert user_agents == {client.user_agent}


async def test_data_urls_are_allowed(server: LoopbackServer, renderer: PlaywrightRenderer) -> None:
    gif = "data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
    url = page(server, f"<script>{MARK}</script><img src='{gif}' onload=\"mark('img-ok')\">")

    rendered = await renderer.render(url, wait_for=".img-ok")

    assert "img-ok" in rendered.html


async def test_concurrent_renders_share_the_browser(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    one = page(server, "<p>one</p>", "/one")
    two = page(server, "<p>two</p>", "/two")

    first, second = await asyncio.gather(
        renderer.render(one, wait_for=None), renderer.render(two, wait_for=None)
    )

    assert "one" in first.html
    assert "two" in second.html


async def test_aclose_is_idempotent(make_renderer: MakeRenderer) -> None:
    renderer, _ = await make_renderer()

    await renderer.aclose()
    await renderer.aclose()


# --- limits ----------------------------------------------------------------------------------


async def test_a_page_that_never_goes_idle_times_out_and_releases_its_context(
    server: LoopbackServer, renderer: PlaywrightRenderer, short_timeout: float
) -> None:
    server.routes["/never"] = Route(headers=TEXT, body=b"x", gate=threading.Event())
    url = page(server, '<p>polling</p><script>fetch("/never")</script>')

    with pytest.raises(FetchError) as info:
        await renderer.render(url, wait_for=None)

    assert info.value.reason == "timeout"
    assert renderer._browser.contexts == []  # the page's context was closed (FR-018)


async def test_a_wait_for_element_that_never_appears_times_out(
    server: LoopbackServer, renderer: PlaywrightRenderer, short_timeout: float
) -> None:
    url = page(server, "<p>static</p>")

    with pytest.raises(FetchError) as info:
        await renderer.render(url, wait_for=".never-there")

    assert info.value.reason == "timeout"
    assert renderer._browser.contexts == []


async def test_a_renderer_stays_usable_after_a_timeout(
    server: LoopbackServer, renderer: PlaywrightRenderer, short_timeout: float
) -> None:
    server.routes["/never"] = Route(headers=TEXT, body=b"x", gate=threading.Event())
    slow = page(server, '<script>fetch("/never")</script>', "/slow")
    fine = page(server, "<p>fine</p>", "/fine")

    with pytest.raises(FetchError):
        await renderer.render(slow, wait_for=None)
    rendered = await renderer.render(fine, wait_for=None)

    assert "fine" in rendered.html


# --- the guard -------------------------------------------------------------------------------


async def test_sub_requests_to_private_addresses_are_aborted(
    server: LoopbackServer,
    renderer: PlaywrightRenderer,
    checked_urls: list[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.browser")
    target = f"http://{PRIVATE}/secret?token=hunter2"
    url = page(
        server,
        f"<p>Still rendered</p><script>{MARK}"
        f'fetch("{target}").catch(() => mark("fetch-blocked"));'
        f'const i = new Image(); i.onerror = () => mark("img-blocked"); i.src = "{target}.png";'
        "</script>",
    )

    rendered = await renderer.render(url, wait_for=".img-blocked")

    assert "Still rendered" in rendered.html
    assert "fetch-blocked" in rendered.html  # refused at once, not hanging until the timeout
    assert any(u.startswith(f"http://{PRIVATE}/secret") for u in checked_urls)
    messages = [r for r in caplog.records if r.name == "invio.sources.browser"]
    assert messages, "aborted sub-requests are logged"
    assert all(r.levelno == logging.DEBUG for r in messages)
    assert "hunter2" not in caplog.text
    assert "secret" not in caplog.text  # host only


async def test_credentials_never_reach_another_origin_on_a_later_same_origin_hop(
    server: LoopbackServer, second_server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    # A -> B/p1 -> B/p2: the second hop is same-origin with the first, but not with A.
    other = second_server.base_url
    server.routes["/start"] = Route(status=302, headers={"Location": f"{other}/p1"})
    second_server.routes["/p1"] = Route(status=302, headers={"Location": "/p2"})
    second_server.routes["/p2"] = Route(headers=TEXT, body=b"done")
    url = page(
        server,
        f"<script>{MARK}"
        'fetch("/start", {headers: {Authorization: "Bearer hunter2"}})'
        '.then(() => mark("fetched"))</script>',
    )

    rendered = await renderer.render(url, wait_for=".fetched")

    assert "fetched" in rendered.html
    start = next(r for r in server.requests if r.path == "/start")
    assert start.headers["authorization"] == "Bearer hunter2"  # its own origin gets it
    assert [r.path for r in second_server.requests] == ["/p1", "/p2"]
    assert all("authorization" not in r.headers for r in second_server.requests)


async def test_a_document_redirected_to_a_private_address_is_blocked(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    server.routes["/hop"] = Route(status=302, headers={"Location": f"http://{PRIVATE}/inside"})

    with pytest.raises(BlockedError) as info:
        await renderer.render(f"{server.base_url}/hop", wait_for=None)

    assert info.value.reason is BlockReason.NON_PUBLIC_ADDRESS
    assert renderer._browser.contexts == []


async def test_a_later_navigation_to_a_refused_url_fails_the_render(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    # Without the check the render would return Chromium's error page as the page's content.
    url = page(server, f'<p>first</p><script>location.href = "http://{PRIVATE}/"</script>')

    with pytest.raises(BlockedError) as info:
        await renderer.render(url, wait_for=None)

    assert info.value.reason is BlockReason.NON_PUBLIC_ADDRESS


async def test_a_navigation_to_a_refused_url_while_waiting_fails_with_the_refusal(
    server: LoopbackServer, renderer: PlaywrightRenderer, short_timeout: float
) -> None:
    # The error page never matches ``wait_for``: the refusal, not a timeout, is the reason.
    url = page(
        server, f'<script>setTimeout(() => location.href = "http://{PRIVATE}/", 600)</script>'
    )

    with pytest.raises(BlockedError) as info:
        await renderer.render(url, wait_for=".never")

    assert info.value.reason is BlockReason.NON_PUBLIC_ADDRESS


async def test_a_redirect_to_a_private_address_is_aborted_at_the_hop(
    server: LoopbackServer, renderer: PlaywrightRenderer, checked_urls: list[str]
) -> None:
    server.routes["/hop"] = Route(status=302, headers={"Location": f"http://{PRIVATE}/inside"})
    url = page(
        server,
        f'<p>Rendered</p><script>{MARK}fetch("/hop").catch(() => mark("hop-blocked"))</script>',
    )

    rendered = await renderer.render(url, wait_for=".hop-blocked")

    assert "hop-blocked" in rendered.html
    assert f"http://{PRIVATE}/inside" in checked_urls
    assert server.paths().count("/hop") == 1


async def test_every_hop_of_a_redirect_chain_is_checked(
    server: LoopbackServer, renderer: PlaywrightRenderer, checked_urls: list[str]
) -> None:
    # Chromium does not route redirect hops: the second hop must not slip through unchecked.
    server.routes["/a"] = Route(status=302, headers={"Location": "/b"})
    server.routes["/b"] = Route(status=301, headers={"Location": f"http://{PRIVATE}/inside"})
    url = page(
        server,
        f'<script>{MARK}fetch("/a").catch(() => mark("chain-blocked"))</script>',
    )

    rendered = await renderer.render(url, wait_for=".chain-blocked")

    assert "chain-blocked" in rendered.html
    assert f"http://{PRIVATE}/inside" in checked_urls
    assert server.paths().count("/b") == 1


async def test_an_allowed_redirect_is_followed_and_the_page_gets_the_final_response(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    server.routes["/a"] = Route(status=302, headers={"Location": "/b"})
    server.routes["/b"] = Route(headers=TEXT, body=b"final-body")
    url = page(
        server,
        '<div id="out"></div><script>fetch("/a").then(r => r.text())'
        '.then(t => { document.getElementById("out").textContent = t })</script>',
    )

    rendered = await renderer.render(url, wait_for=None)

    assert "final-body" in rendered.html


async def test_a_redirect_loop_is_aborted(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    server.routes["/loop"] = Route(status=302, headers={"Location": "/loop"})
    url = page(server, f'<script>{MARK}fetch("/loop").catch(() => mark("loop-blocked"))</script>')

    rendered = await renderer.render(url, wait_for=".loop-blocked")

    assert "loop-blocked" in rendered.html


async def test_the_final_url_follows_the_redirects_of_the_document(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    target = page(server, '<a href="item">Item</a>', "/dir/new")
    server.routes["/one"] = Route(status=301, headers={"Location": "/two"})
    server.routes["/two"] = Route(status=302, headers={"Location": "/dir/new"})

    rendered = await renderer.render(f"{server.base_url}/one", wait_for=None)

    assert rendered.url == target


async def test_non_web_schemes_are_aborted_but_data_and_blob_are_not(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    url = page(
        server,
        f"<script>{MARK}"
        'fetch("ftp://example.org/x").catch(() => mark("ftp-blocked"));'
        'fetch("data:text/plain,hi").then(() => mark("data-ok"));'
        'fetch(URL.createObjectURL(new Blob(["x"]))).then(() => mark("blob-ok"));'
        "</script>",
    )

    rendered = await renderer.render(url, wait_for=".blob-ok")

    assert "ftp-blocked" in rendered.html
    assert "data-ok" in rendered.html


async def test_a_websocket_to_a_private_address_is_closed(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    url = page(
        server,
        f"<script>{MARK}const ws = new WebSocket('ws://{PRIVATE}/socket');"
        "ws.onopen = () => mark('ws-open'); ws.onclose = () => mark('ws-closed');</script>",
    )

    rendered = await renderer.render(url, wait_for=".ws-closed")

    assert 'class="ws-closed"' in rendered.html
    assert 'class="ws-open"' not in rendered.html


_WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class _PageAndSocketServer(socketserver.ThreadingTCPServer):
    """One origin that serves ``page`` over HTTP and answers a WebSocket upgrade with one text
    frame, ``hello-ws``.

    One origin for both keeps the page's URL and the socket's in agreement.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, page_template: str) -> None:
        super().__init__(("127.0.0.1", 0), _PageAndSocketHandler)
        self.page = page_template.replace("__PORT__", str(self.server_address[1]))
        self._thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        self._thread.join(timeout=5)


class _PageAndSocketHandler(socketserver.StreamRequestHandler):
    server: _PageAndSocketServer

    def handle(self) -> None:
        self.rfile.readline()  # the request line
        headers: dict[str, str] = {}
        while (line := self.rfile.readline()) not in (b"\r\n", b"\n", b""):
            name, _, value = line.decode("latin-1").partition(":")
            headers[name.strip().lower()] = value.strip()
        if headers.get("upgrade", "").lower() == "websocket":
            digest = hashlib.sha1(  # the handshake RFC 6455 prescribes, not a security use
                (headers["sec-websocket-key"] + _WEBSOCKET_GUID).encode()
            ).digest()
            self.wfile.write(
                b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                b"Connection: Upgrade\r\nSec-WebSocket-Accept: "
                + base64.b64encode(digest)
                + b"\r\n\r\n\x81\x08hello-ws"
            )
            self.wfile.flush()
            self.rfile.read(1)  # until the browser closes the socket
            return
        body = self.server.page.encode()
        self.wfile.write(
            b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n"
            + f"Content-Length: {len(body)}\r\n\r\n".encode()
            + body
        )


async def test_a_websocket_to_an_allowed_address_is_connected(
    monkeypatch: pytest.MonkeyPatch, make_renderer: MakeRenderer
) -> None:
    # Chromium's own local-network check would refuse the loopback socket before the guard
    # is asked; switch it off to test the guard's allow path.
    args = [a for a in browser._LAUNCH_ARGS if not a.startswith("--disable-features=")]
    monkeypatch.setattr(
        browser, "_LAUNCH_ARGS", [*args, "--disable-features=LocalNetworkAccessChecks"]
    )
    renderer, _ = await make_renderer()
    origin = _PageAndSocketServer(
        f"<script>{MARK}const ws = new WebSocket('ws://127.0.0.1:__PORT__/');"
        "ws.onmessage = e => mark('got-' + e.data)</script>"
    )
    try:
        rendered = await renderer.render(
            f"http://127.0.0.1:{origin.server_address[1]}/", wait_for=".got-hello-ws"
        )
    finally:
        origin.stop()

    assert "got-hello-ws" in rendered.html


async def test_a_page_disallowed_by_robots_is_never_opened(
    server: LoopbackServer, make_renderer: MakeRenderer
) -> None:
    renderer, _ = await make_renderer(respect_robots=True)
    server.routes["/robots.txt"] = Route(headers=TEXT, body=b"User-agent: *\nDisallow: /page\n")
    url = page(server, "<p>secret</p>")

    with pytest.raises(BlockedError) as info:
        await renderer.render(url, wait_for=None)

    assert info.value.reason is BlockReason.BLOCKED_BY_ROBOTS
    assert server.paths() == ["/robots.txt"]
    assert renderer._browser.contexts == []


async def test_a_private_page_url_never_reaches_the_browser(renderer: PlaywrightRenderer) -> None:
    with pytest.raises(BlockedError) as info:
        await renderer.render(f"http://{PRIVATE}/", wait_for=None)

    assert info.value.reason is BlockReason.NON_PUBLIC_ADDRESS
    assert renderer._browser.contexts == []


# --- failures --------------------------------------------------------------------------------


async def test_a_main_response_with_an_error_status_fails(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    with pytest.raises(FetchError) as info:
        await renderer.render(f"{server.base_url}/missing", wait_for=None)

    assert info.value.reason == "http_status"
    assert info.value.status == 404
    assert renderer._browser.contexts == []


async def test_a_page_that_cannot_be_reached_is_render_failed(
    renderer: PlaywrightRenderer,
) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]  # nothing listens here once the socket is closed

    with pytest.raises(FetchError) as info:
        await renderer.render(f"http://127.0.0.1:{port}/", wait_for=None)

    assert info.value.reason == "render_failed"
    assert renderer._browser.contexts == []


async def test_an_oversized_main_document_fails_with_too_large(
    server: LoopbackServer, make_renderer: MakeRenderer
) -> None:
    renderer, _ = await make_renderer(max_response_bytes=2000)
    url = page(server, "<p>" + "x" * 3000 + "</p>")

    with pytest.raises(FetchError) as info:
        await renderer.render(url, wait_for=None)

    assert info.value.reason == "too_large"
    assert renderer._browser.contexts == []


async def test_an_oversized_sub_resource_is_dropped_but_the_page_renders(
    server: LoopbackServer, make_renderer: MakeRenderer
) -> None:
    renderer, _ = await make_renderer(max_response_bytes=2000)
    server.routes["/big"] = Route(headers=TEXT, body=b"y" * 5000)
    url = page(
        server,
        f'<p>Rendered</p><script>{MARK}fetch("/big").catch(() => mark("big-dropped"))</script>',
    )

    rendered = await renderer.render(url, wait_for=".big-dropped")

    assert "Rendered" in rendered.html


async def test_an_invalid_wait_for_selector_has_its_own_reason(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    url = page(server, "<p>x</p>")

    with pytest.raises(FetchError) as info:
        await renderer.render(url, wait_for="div:has-text(")

    assert info.value.reason == "invalid_wait_for"
    assert renderer._browser.contexts == []


async def test_a_page_without_content_is_render_failed(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    server.routes["/empty"] = Route(status=204)  # the browser does not navigate to it

    with pytest.raises(FetchError) as info:
        await renderer.render(f"{server.base_url}/empty", wait_for=None)

    assert info.value.reason == "render_failed"


async def test_a_download_is_render_failed(
    server: LoopbackServer, renderer: PlaywrightRenderer
) -> None:
    server.routes["/file"] = Route(
        headers={"Content-Type": "application/octet-stream", "Content-Disposition": "attachment"},
        body=b"binary",
    )

    with pytest.raises(FetchError) as info:
        await renderer.render(f"{server.base_url}/file", wait_for=None)

    assert info.value.reason == "render_failed"


# --- start -----------------------------------------------------------------------------------


async def test_a_missing_chromium_is_render_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path))  # an empty browser cache
    async with SafeHttpClient(HttpClientConfig()) as client:
        with pytest.raises(RenderUnavailableError) as info:
            await PlaywrightRenderer.start(client)

    assert "playwright install chromium" in str(info.value)


async def test_the_driver_is_stopped_when_the_launch_fails_for_another_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stopped: list[bool] = []

    class FakeChromium:
        executable_path = sys.executable  # any file that exists

        async def launch(self, **kwargs: object) -> None:
            raise RuntimeError("boom")

    class FakeDriver:
        chromium = FakeChromium()

        async def stop(self) -> None:
            stopped.append(True)

    class FakeStarter:
        async def start(self) -> FakeDriver:
            return FakeDriver()

    monkeypatch.setattr(browser, "async_playwright", FakeStarter)
    async with SafeHttpClient(HttpClientConfig()) as client:
        with pytest.raises(RuntimeError, match="boom"):
            await PlaywrightRenderer.start(client)

    assert stopped == [True]


class _FakeStarter:
    """Stands in for ``async_playwright()``: ``start`` and ``launch`` fail as told."""

    def __init__(
        self,
        start_error: Exception | None,
        launch_error: Exception | None,
        chromium_path: str = sys.executable,
    ) -> None:
        self.stopped = False
        starter = self

        class Chromium:
            executable_path = chromium_path

            async def launch(self, **kwargs: object) -> None:
                assert launch_error is not None
                raise launch_error

        class Driver:
            chromium = Chromium()

            async def stop(self) -> None:
                starter.stopped = True

        self._driver = Driver()
        self._start_error = start_error

    def __call__(self) -> "_FakeStarter":
        return self

    async def start(self) -> object:
        if self._start_error is not None:
            raise self._start_error
        return self._driver


@pytest.mark.parametrize(
    ("start_error", "launch_error", "executable", "expected"),
    [
        (OSError("driver missing"), None, True, "render_unavailable"),
        (PlaywrightError("driver crashed"), None, True, "render_failed"),
        (None, None, False, "render_unavailable"),  # Chromium not downloaded, whatever the text
        (None, PlaywrightError("Executable doesn't exist at /x"), True, "render_failed"),
        (None, PlaywrightError("Browser closed unexpectedly"), True, "render_failed"),
    ],
)
async def test_start_failures_map_to_the_right_reason(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    start_error: Exception | None,
    launch_error: Exception | None,
    executable: bool,
    expected: str,
) -> None:
    path = sys.executable if executable else str(tmp_path / "missing" / "chrome")
    starter = _FakeStarter(start_error, launch_error, path)
    monkeypatch.setattr(browser, "async_playwright", starter)

    async with SafeHttpClient(HttpClientConfig()) as client:
        with pytest.raises(FetchError) as info:
            await PlaywrightRenderer.start(client)

    assert info.value.reason == expected
    assert starter.stopped is (start_error is None)  # the driver is not left running
