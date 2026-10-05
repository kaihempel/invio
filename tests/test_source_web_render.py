"""Tests for ``render: js`` that need no Chromium: lazy import, missing engine, fake renderer."""

import asyncio
import importlib
import importlib.abc
import subprocess
import sys
import textwrap
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from invio.config.job import WebSource
from invio.sources.http import (
    FetchError,
    HttpClientConfig,
    RenderUnavailableError,
    SafeHttpClient,
    TooLargeError,
)
from invio.sources.web import PageRenderer, RenderedPage, WebPageSource
from tests.http_helpers import (  # noqa: F401
    HTML,
    LOOPBACK,
    LoopbackServer,
    Route,
    server,
    web_source,
)

ROOT = Path(__file__).resolve().parents[1]
PAGE_URL = "https://example.org/dir/page"


class FakeRenderer:
    """A :class:`PageRenderer` that returns canned HTML and records how it is used."""

    def __init__(self, html: str, url: str = PAGE_URL) -> None:
        self.html = html
        self.url = url
        self.calls: list[tuple[str, str | None]] = []
        self.closed = 0

    async def render(self, url: str, *, wait_for: str | None) -> RenderedPage:
        self.calls.append((url, wait_for))
        return RenderedPage(self.url, self.html)

    async def aclose(self) -> None:
        self.closed += 1


@pytest.fixture
async def client() -> AsyncIterator[SafeHttpClient]:
    config = HttpClientConfig(respect_robots=False, host_interval=0.01)
    async with SafeHttpClient(config, allow_networks=LOOPBACK) as instance:
        yield instance


def js_config(url: str = PAGE_URL, **fields: object) -> WebSource:
    return web_source(url, render="js", **fields)


def doc(body: str, head: str = "") -> str:
    return f"<html><head><title>T</title>{head}</head><body>{body}</body></html>"


# --- the engine is only loaded when needed (acceptance criterion 5) --------------------------

LAZY_SCRIPT = """
import asyncio, http.server, sys, threading

import invio.config.job
import invio.sources.rss
import invio.sources.web
from invio.config.job import WebSource
from invio.sources.http import HttpClientConfig, SafeHttpClient
from invio.sources.web import WebPageSource
from tests.http_helpers import LOOPBACK


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"<html><body><p>static page</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=httpd.serve_forever, daemon=True).start()


async def main():
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    config = HttpClientConfig(respect_robots=False)
    async with SafeHttpClient(config, allow_networks=LOOPBACK) as client:
        async with WebPageSource(client) as source:
            [candidate] = await source.fetch(WebSource(type="web", url=url))
    assert candidate.teaser == "static page", candidate


asyncio.run(main())
httpd.shutdown()
loaded = sorted(name for name in sys.modules if name.split(".")[0] == "playwright")
assert not loaded, loaded
assert "invio.sources.browser" not in sys.modules
print("ok")
"""


def test_static_use_never_loads_playwright() -> None:
    done = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(LAZY_SCRIPT)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
        cwd=ROOT,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "ok"


# --- the engine is missing -------------------------------------------------------------------


class _NoPlaywright(importlib.abc.MetaPathFinder):
    """Finds no ``playwright`` package, like an installation without the ``render`` extra."""

    def find_spec(self, name: str, path: object, target: object = None) -> None:
        if name == "playwright":
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)


@pytest.fixture
def no_playwright(monkeypatch: pytest.MonkeyPatch) -> None:
    # As if the package were not installed: any import of it fails on the name "playwright".
    for name in [name for name in sys.modules if name.partition(".")[0] == "playwright"]:
        monkeypatch.delitem(sys.modules, name)
    monkeypatch.setattr(sys, "meta_path", [_NoPlaywright(), *sys.meta_path])
    # A module imported by an earlier test would hide the missing engine.
    monkeypatch.delitem(sys.modules, "invio.sources.browser", raising=False)


async def test_render_without_playwright_is_render_unavailable(
    no_playwright: None, client: SafeHttpClient
) -> None:
    async with WebPageSource(client) as source:
        with pytest.raises(FetchError) as info:
            await source.fetch(js_config())

    error = info.value
    assert isinstance(error, RenderUnavailableError)
    assert error.reason == "render_unavailable"
    assert "uv sync --extra render" in str(error)
    assert "playwright install chromium" in str(error)
    assert error.url == PAGE_URL


async def test_render_unavailable_does_not_stop_static_fetches(
    no_playwright: None, client: SafeHttpClient, server: LoopbackServer
) -> None:
    server.routes["/page"] = Route(headers=HTML, body=b"<html><body>Static</body></html>")
    static = WebSource(type="web", url=f"{server.base_url}/page")

    async with WebPageSource(client) as source:
        with pytest.raises(RenderUnavailableError):
            await source.fetch(js_config())
        [candidate] = await source.fetch(static)
        with pytest.raises(RenderUnavailableError):  # remembered, but not as "started"
            await source.fetch(js_config())

    assert candidate.teaser == "Static"


async def test_a_missing_engine_is_not_looked_for_again(
    monkeypatch: pytest.MonkeyPatch, client: SafeHttpClient
) -> None:
    browser = importlib.import_module("invio.sources.browser")
    attempts: list[int] = []

    async def start(client: SafeHttpClient) -> FakeRenderer:
        attempts.append(1)
        raise RenderUnavailableError()  # Chromium is not downloaded

    monkeypatch.setattr(browser.PlaywrightRenderer, "start", start)

    async with WebPageSource(client) as source:
        for _ in range(3):
            with pytest.raises(RenderUnavailableError) as info:
                await source.fetch(js_config())
            assert info.value.url == PAGE_URL

    assert len(attempts) == 1


async def test_a_broken_playwright_installation_is_not_reported_as_missing(
    monkeypatch: pytest.MonkeyPatch, client: SafeHttpClient
) -> None:
    real_import = importlib.import_module

    def broken(name: str, package: str | None = None) -> object:
        if name == "invio.sources.browser":
            raise ModuleNotFoundError("No module named 'playwright._impl'", name="playwright._impl")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", broken)

    async with WebPageSource(client) as source:
        with pytest.raises(ModuleNotFoundError, match=r"playwright\._impl"):
            await source.fetch(js_config())


async def test_unrelated_import_errors_are_not_swallowed(
    monkeypatch: pytest.MonkeyPatch, client: SafeHttpClient
) -> None:
    real_import = importlib.import_module

    def broken(name: str, package: str | None = None) -> object:
        if name == "invio.sources.browser":
            raise ImportError("cannot import name 'x'", name="somewhere_else")
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", broken)

    async with WebPageSource(client) as source:
        with pytest.raises(ImportError, match="cannot import name"):
            await source.fetch(js_config())


def test_render_unavailable_error_redacts_the_url() -> None:
    error = RenderUnavailableError(url="https://user:secret@example.org/a?token=1")

    assert error.reason == "render_unavailable"
    assert "secret" not in str(error)
    assert "token" not in str(error)


# --- a fake renderer: the same pipeline on rendered HTML -------------------------------------


def test_fake_satisfies_the_protocol() -> None:
    renderer: PageRenderer = FakeRenderer("")

    assert renderer is not None


async def test_page_mode_uses_the_rendered_text(client: SafeHttpClient) -> None:
    renderer = FakeRenderer(doc("<p>Rendered by script</p><script>x()</script>"))

    async with WebPageSource(client, renderer=renderer) as source:
        [candidate] = await source.fetch(js_config())

    assert candidate.teaser == "Rendered by script"
    assert candidate.title == "T"
    assert candidate.url == PAGE_URL
    assert candidate.content_hash is not None
    assert renderer.calls == [(PAGE_URL, None)]


async def test_selector_and_selector_not_found_apply_to_rendered_html(
    client: SafeHttpClient,
) -> None:
    renderer = FakeRenderer(doc('<nav>menu</nav><div class="posts">One</div>'))

    async with WebPageSource(client, renderer=renderer) as source:
        [candidate] = await source.fetch(js_config(selector=".posts"))
        with pytest.raises(FetchError) as info:
            await source.fetch(js_config(selector=".missing"))

    assert candidate.teaser == "One"
    assert info.value.reason == "selector_not_found"


async def test_links_mode_resolves_against_the_rendered_page_url(client: SafeHttpClient) -> None:
    renderer = FakeRenderer(
        doc('<a href="item">Item</a><a href="https://elsewhere.example/x">Out</a>'),
        url="https://final.example/dir/page",
    )

    async with WebPageSource(client, renderer=renderer) as source:
        candidates = await source.fetch(js_config(mode="links"))

    assert [c.url for c in candidates] == ["https://final.example/dir/item"]


async def test_rendered_and_static_copies_of_a_page_give_the_same_candidate(
    client: SafeHttpClient, server: LoopbackServer
) -> None:
    html = doc("<header>Menu</header><div class='posts'><p>One</p>\n  <p>Two</p></div>")
    server.routes["/page"] = Route(headers=HTML, body=html.encode())
    url = f"{server.base_url}/page"
    renderer = FakeRenderer(html, url=url)

    async with WebPageSource(client, renderer=renderer) as source:
        [static] = await source.fetch(WebSource(type="web", url=url, selector=".posts"))
        [rendered] = await source.fetch(js_config(url, selector=".posts"))

    assert rendered == static
    assert static.teaser == "One Two"


class FailingRenderer(FakeRenderer):
    """Fails the first render with ``reason``, then renders normally."""

    def __init__(self, reason: str, html: str) -> None:
        super().__init__(html)
        self.reason = reason

    async def render(self, url: str, *, wait_for: str | None) -> RenderedPage:
        if not self.calls:
            self.calls.append((url, wait_for))
            raise FetchError(self.reason, url=url)
        return await super().render(url, wait_for=wait_for)


@pytest.mark.parametrize("reason", ["timeout", "render_failed", "non_public_address"])
async def test_a_render_error_passes_through_and_the_source_stays_usable(
    client: SafeHttpClient, server: LoopbackServer, reason: str
) -> None:
    server.routes["/page"] = Route(headers=HTML, body=b"<html><body>Static</body></html>")
    renderer = FailingRenderer(reason, doc("<p>Later</p>"))

    async with WebPageSource(client, renderer=renderer) as source:
        with pytest.raises(FetchError) as info:
            await source.fetch(js_config(wait_for=".never"))
        [static] = await source.fetch(WebSource(type="web", url=f"{server.base_url}/page"))
        [rendered] = await source.fetch(js_config())
        assert renderer.closed == 0  # one failed page does not drop the shared browser

    assert info.value.reason == reason
    assert info.value.url == PAGE_URL
    assert static.teaser == "Static"
    assert rendered.teaser == "Later"


async def test_wait_for_is_passed_to_the_renderer(client: SafeHttpClient) -> None:
    renderer = FakeRenderer(doc("<p>x</p>"))

    async with WebPageSource(client, renderer=renderer) as source:
        await source.fetch(js_config(wait_for=".ready"))

    assert renderer.calls == [(PAGE_URL, ".ready")]


async def test_rendered_html_larger_than_the_response_limit_is_refused() -> None:
    config = HttpClientConfig(respect_robots=False, max_response_bytes=1000)
    renderer = FakeRenderer(doc("<p>" + "ä" * 600 + "</p>"))  # 1200 bytes in UTF-8, 600 chars

    async with SafeHttpClient(config) as client, WebPageSource(client, renderer=renderer) as source:
        with pytest.raises(TooLargeError) as info:
            await source.fetch(js_config())

    assert info.value.limit == 1000
    assert info.value.url == PAGE_URL


async def test_rendered_html_at_the_limit_is_accepted() -> None:
    html = doc("<p>ok</p>")
    config = HttpClientConfig(respect_robots=False, max_response_bytes=len(html.encode()))
    renderer = FakeRenderer(html)

    async with SafeHttpClient(config) as client, WebPageSource(client, renderer=renderer) as source:
        assert len(await source.fetch(js_config())) == 1


async def test_aclose_closes_the_renderer_once(client: SafeHttpClient) -> None:
    renderer = FakeRenderer(doc("<p>x</p>"))
    source = WebPageSource(client, renderer=renderer)

    await source.fetch(js_config())
    await source.aclose()
    await source.aclose()

    assert renderer.closed == 1


async def test_a_closed_source_refuses_to_render_but_still_fetches_static(
    client: SafeHttpClient, server: LoopbackServer
) -> None:
    server.routes["/page"] = Route(headers=HTML, body=b"<html><body>S</body></html>")
    renderer = FakeRenderer(doc("<p>x</p>"))
    source = WebPageSource(client, renderer=renderer)
    await source.aclose()

    with pytest.raises(RuntimeError, match="closed"):
        await source.fetch(js_config())
    [candidate] = await source.fetch(WebSource(type="web", url=f"{server.base_url}/page"))

    assert renderer.calls == []
    assert candidate.teaser == "S"


async def test_aclose_waits_for_a_renderer_that_is_still_starting(
    client: SafeHttpClient, start_counter: "StartCounter"
) -> None:
    source = WebPageSource(client)
    fetching = asyncio.create_task(source.fetch(js_config()))
    async with asyncio.timeout(5):
        while start_counter.starts == 0:  # the start is under way, holding the lock
            await asyncio.sleep(0)

    await source.aclose()
    await fetching

    assert start_counter.renderer.closed == 1  # not leaked
    with pytest.raises(RuntimeError):
        await source.fetch(js_config())


async def test_a_failed_start_without_a_url_is_reported_for_the_page(
    monkeypatch: pytest.MonkeyPatch, client: SafeHttpClient
) -> None:
    browser = importlib.import_module("invio.sources.browser")

    async def start(client: SafeHttpClient) -> FakeRenderer:
        raise FetchError("render_failed", url="")

    monkeypatch.setattr(browser.PlaywrightRenderer, "start", start)

    async with WebPageSource(client) as source:
        with pytest.raises(FetchError) as info:
            await source.fetch(js_config())

    assert info.value.reason == "render_failed"
    assert info.value.url == PAGE_URL


async def test_a_start_failure_with_a_url_passes_through(
    monkeypatch: pytest.MonkeyPatch, client: SafeHttpClient
) -> None:
    browser = importlib.import_module("invio.sources.browser")

    async def start(client: SafeHttpClient) -> FakeRenderer:
        raise FetchError("render_failed", url="https://other.example/")

    monkeypatch.setattr(browser.PlaywrightRenderer, "start", start)

    async with WebPageSource(client) as source:
        with pytest.raises(FetchError) as info:
            await source.fetch(js_config())

    assert info.value.url == "https://other.example/"


async def test_static_fetches_do_not_touch_the_renderer(
    client: SafeHttpClient, server: LoopbackServer
) -> None:
    server.routes["/page"] = Route(headers=HTML, body=b"<html><body>S</body></html>")
    renderer = FakeRenderer("<html></html>")

    async with WebPageSource(client, renderer=renderer) as source:
        await source.fetch(WebSource(type="web", url=f"{server.base_url}/page"))

    assert renderer.calls == []
    assert server.paths() == ["/page"]


async def test_async_context_manager_returns_the_source(client: SafeHttpClient) -> None:
    source = WebPageSource(client)

    async with source as entered:
        assert entered is source


# --- the lazily started renderer -------------------------------------------------------------


class StartCounter:
    """Stands in for ``PlaywrightRenderer.start``: counts starts and hands out one fake."""

    def __init__(self) -> None:
        self.starts = 0
        self.renderer = FakeRenderer(doc("<p>started</p>"))

    async def __call__(self, client: SafeHttpClient) -> FakeRenderer:
        self.starts += 1
        await asyncio.sleep(0.05)  # let concurrent fetches pile up behind the lock
        return self.renderer


@pytest.fixture
def start_counter(monkeypatch: pytest.MonkeyPatch) -> StartCounter:
    browser = importlib.import_module("invio.sources.browser")
    counter = StartCounter()
    monkeypatch.setattr(browser.PlaywrightRenderer, "start", counter)
    return counter


async def test_the_renderer_is_started_once_across_concurrent_fetches(
    client: SafeHttpClient, start_counter: StartCounter
) -> None:
    async with WebPageSource(client) as source:
        results = await asyncio.gather(*(source.fetch(js_config()) for _ in range(4)))
        await source.fetch(js_config())

    assert start_counter.starts == 1
    assert all(len(r) == 1 for r in results)
    assert start_counter.renderer.closed == 1  # closed by aclose, which the block ended with


async def test_a_failed_start_is_retried_on_the_next_fetch(
    monkeypatch: pytest.MonkeyPatch, client: SafeHttpClient
) -> None:
    browser = importlib.import_module("invio.sources.browser")
    fake = FakeRenderer(doc("<p>second try</p>"))
    attempts: list[int] = []

    async def start(client: SafeHttpClient) -> FakeRenderer:
        attempts.append(1)
        if len(attempts) == 1:
            raise FetchError("render_failed", url=PAGE_URL)  # e.g. a crash during the launch
        return fake

    monkeypatch.setattr(browser.PlaywrightRenderer, "start", start)

    async with WebPageSource(client) as source:
        with pytest.raises(FetchError, match="render_failed"):
            await source.fetch(js_config())
        [candidate] = await source.fetch(js_config())

    assert candidate.teaser == "second try"
    assert len(attempts) == 2
