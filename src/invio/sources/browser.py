"""Headless-Chromium renderer for ``render: js`` web sources.

This is the only module that imports Playwright, an optional dependency (the ``render`` extra,
plus ``playwright install chromium``). :mod:`invio.sources.web` imports it lazily, on the first
``render: js`` fetch, so installations that never render do not need it.

A browser is not the safe HTTP client, so the client's rules are applied around it:

* the page URL passes :meth:`SafeHttpClient.admission` (scheme and address guard, robots.txt,
  per-origin rate limit) before the browser sees it;
* every request the page makes, WebSockets included, is routed through
  :meth:`SafeHttpClient.check_target` and aborted when the target is not allowed. Chromium does
  not route the hops of a redirect, so allowed requests are fetched by Playwright with
  ``max_redirects=0`` and redirects are followed here, each target checked before it is
  requested; the browser receives the final response (only for GET and HEAD: a redirected POST
  is aborted). The page keeps seeing the URL it asked for, and the render reports the final
  URL of the main document;
* service workers (which bypass routing), downloads, proxies and persistent storage are off.

Known limits: the address check resolves the host name, and the connection then resolves it
again (no pinning to the checked address as in ``SafeHttpClient``), so a DNS answer that changes
in between (DNS rebinding) is not caught. Every response, the main document included, is capped
at the client's ``max_response_bytes``; Playwright buffers a body before it can be measured, so
a dishonest server can still make it read more than that before the cap applies (the time
budget bounds it). The body is also decompressed before it is measured, so a small compressed
response can take far more memory than the cap (a "zip bomb"); at most
:data:`MAX_CONCURRENT_RENDERS` renders run at a time to bound how many pages can do so at once.
CORS preflight requests (``OPTIONS``) are sent by Chromium's network stack without being
routed, so a page's script can make one reach a host the guard would refuse; it carries no
body and its answer never reaches the page, and the request that would follow it is checked.
robots.txt is consulted for the page URL only.
"""

import asyncio
import contextlib
import logging
import os
from typing import Any, Final, Self
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import (
    APIResponse,
    Browser,
    Playwright,
    Route,
    WebSocketRoute,
    async_playwright,
)
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from invio.sources.errors import (
    BlockedError,
    BlockReason,
    FetchError,
    RenderUnavailableError,
    TooLargeError,
)
from invio.sources.http import SafeHttpClient
from invio.sources.urls import WEB_SCHEMES, origin, without_query
from invio.sources.web import RenderedPage

__all__ = ["MAX_CONCURRENT_RENDERS", "RENDER_TIMEOUT_SECONDS", "PlaywrightRenderer"]

_log = logging.getLogger("invio.sources.browser")

RENDER_TIMEOUT_SECONDS: Final = 30.0
"""Budget of one render, navigation included; read when a render starts."""

MAX_CONCURRENT_RENDERS: Final = 4
"""Renders (browser contexts) of one renderer that run at the same time; the rest wait."""

_LAUNCH_ARGS: Final = [
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",  # no UDP around the guard
    "--no-proxy-server",  # never a proxy from the environment or the system
    "--dns-prefetch-disable",  # no lookups for links the guard has not seen
    "--disable-features=NetworkPrediction",  # no speculative preconnects
]
# Schemes whose content never leaves the browser; the guard has nothing to check there.
_LOCAL_SCHEMES: Final = frozenset({"data", "blob", "about"})
_SOCKET_SCHEMES: Final = {"ws": "http", "wss": "https"}
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
_MAX_REDIRECTS: Final = 10
_SAFE_METHODS: Final = frozenset({"GET", "HEAD"})
# Credentials the page sent to one origin must not follow a redirect to another.
_CREDENTIAL_HEADERS: Final = frozenset({"authorization", "proxy-authorization", "cookie"})


class PlaywrightRenderer:
    """Renders pages in one headless Chromium shared by all renders.

    Create it with :meth:`start`. Each render uses its own browser context, so cookies and
    storage never carry over from one page to the next.
    """

    def __init__(self, client: SafeHttpClient, driver: Playwright, browser: Browser) -> None:
        self._client = client
        self._driver = driver
        self._browser = browser
        self._closed = False
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_RENDERS)

    @classmethod
    async def start(cls, client: SafeHttpClient) -> Self:
        """Launch Chromium.

        Raises :class:`RenderUnavailableError` when the driver or the Chromium executable is
        missing, and :class:`FetchError` (``"render_failed"``) for any other launch failure.

        ``client`` supplies the address guard, robots.txt policy, rate limit and User-Agent.
        """
        try:
            driver = await async_playwright().start()
        except OSError as error:  # the Playwright driver cannot be run
            raise RenderUnavailableError(url="") from error
        except PlaywrightError as error:
            raise FetchError("render_failed", url="") from error
        try:
            if not os.path.isfile(driver.chromium.executable_path):  # Chromium not downloaded
                raise RenderUnavailableError(url="")
            browser = await driver.chromium.launch(headless=True, args=_LAUNCH_ARGS)
        except BaseException as error:  # do not leave the driver process behind
            await driver.stop()
            if isinstance(error, PlaywrightError):
                raise FetchError("render_failed", url="") from error
            raise
        return cls(client, driver, browser)

    async def aclose(self) -> None:
        """Close the browser and the Playwright driver; calling it again is harmless."""
        if self._closed:
            return
        self._closed = True
        try:
            await self._browser.close()
        finally:
            await self._driver.stop()

    async def render(self, url: str, *, wait_for: str | None) -> RenderedPage:
        """Load ``url`` and return the document once the network has been idle for ~0.5 s.

        With ``wait_for`` (a plain CSS selector) it also waits until a matching element exists. The
        whole render takes at most :data:`RENDER_TIMEOUT_SECONDS`. Raises
        :class:`BlockedError` (scheme, address or robots.txt) before a browser context is
        opened, and :class:`FetchError` with ``"timeout"``, ``"http_status"`` (the main
        response was 4xx/5xx) or ``"render_failed"`` (no response, a download, a crash).

        The per-origin rate-limit slot is held while the page loads, so renders of one origin
        run one after the other; at most :data:`MAX_CONCURRENT_RENDERS` run at all.
        """
        async with self._client.admission(url), self._slots:
            guard = _RequestGuard(self._client)
            try:
                async with asyncio.timeout(RENDER_TIMEOUT_SECONDS):
                    return await self._load(url, wait_for, guard)
            except FetchError as error:
                _log_failure(error)
                raise
            # A main-frame navigation the guard refused explains whatever failed after it: the
            # navigation itself, or a wait on the error page it left behind.
            except (TimeoutError, PlaywrightTimeoutError) as error:
                raise _logged(guard.failure or FetchError("timeout", url=url)) from error
            except PlaywrightError as error:  # Playwright's timeout is caught above
                raise _logged(guard.failure or FetchError("render_failed", url=url)) from error

    async def _load(self, url: str, wait_for: str | None, guard: "_RequestGuard") -> RenderedPage:
        context = await self._browser.new_context(
            user_agent=self._client.user_agent,
            service_workers="block",
            accept_downloads=False,
        )
        try:
            await context.route("**/*", guard.route)
            await context.route_web_socket("**/*", guard.web_socket)
            page = await context.new_page()
            timeout_ms = RENDER_TIMEOUT_SECONDS * 1000
            response = await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
            if response is None:  # pragma: no cover (only a same-document navigation has none)
                raise FetchError("render_failed", url=url)
            if response.status >= 400:
                raise FetchError("http_status", url=url, status=response.status)
            if wait_for is not None:
                try:
                    await page.wait_for_selector(
                        f"css={wait_for}", state="attached", timeout=timeout_ms
                    )
                except PlaywrightError as error:  # a timeout is handled by the caller
                    if "while parsing css selector" not in str(error):
                        raise
                    raise FetchError("invalid_wait_for", url=url) from error
            # A later navigation of the main frame that the guard refused leaves Chromium's
            # error page behind, which must not be taken for the page's content.
            if guard.failure is not None:
                raise guard.failure
            if page.url.startswith("chrome-error:"):  # pragma: no cover (defensive)
                raise FetchError("render_failed", url=url)
            return RenderedPage(guard.document_url or page.url, await page.content())
        finally:
            with contextlib.suppress(PlaywrightError):
                await context.close()


class _RequestGuard:
    """Routes the requests of one render: allowed ones are fetched, the rest are aborted.

    The verdict for a host is cached for the life of the render (one DNS lookup per host, not
    per image) when it is a refusal by policy; a failed lookup may succeed on the next try. A
    handler must never leave a request pending, so any failure aborts it. ``document_url`` is
    the final URL of the latest main-frame navigation, and ``failure`` the error that made the
    guard abort a main-frame navigation (a refused target or redirect, an oversized response),
    for the render to raise instead of returning Chromium's error page.
    """

    def __init__(self, client: SafeHttpClient) -> None:
        self._client = client
        self._verdicts: dict[tuple[str, str | None, int | None], BlockedError | None] = {}
        self.document_url: str | None = None
        self.failure: FetchError | None = None

    async def route(self, route: Route) -> None:
        url = route.request.url
        response: APIResponse | None = None
        try:
            scheme = urlsplit(url).scheme
            if scheme in _LOCAL_SCHEMES:
                await route.continue_()
                return
            if scheme not in WEB_SCHEMES:
                raise BlockedError(BlockReason.UNSUPPORTED_SCHEME, url=url)
            await self._check(url)
            response, final_url = await self._fetch(route)
            if self._is_main_navigation(route):
                self.document_url = final_url
            await route.fulfill(response=response)
        except FetchError as error:  # refused by the guard
            self._refuse(route, error)
            await self._abort(route, url, "blocked")
        except Exception:
            self._refuse(route, FetchError("render_failed", url=url))
            await self._abort(route, url, "failed")
        finally:
            if response is not None:
                await _dispose(response)

    async def _fetch(self, route: Route) -> tuple[APIResponse, str]:
        """Fetch the request, following redirects here so that every hop is checked.

        Returns the final response (which the caller disposes) and its URL. Raises
        :class:`FetchError` when the request is refused: a target the guard blocks, too many
        hops, a redirect of a request that is not GET/HEAD, or a body over the size cap.

        Credentials never reach an origin other than the requested one: from the first hop to
        another origin on, every hop is sent with the request headers minus the credentials,
        also when a later hop stays on (or returns to) one origin. ``headers=None`` would make
        Playwright send the original headers again.
        """
        request = route.request
        url = request.url
        requested = origin(url)
        headers: dict[str, str] | None = None  # the original request headers
        response = await route.fetch(max_redirects=0)
        try:
            for hop in range(_MAX_REDIRECTS + 1):
                location = response.headers.get("location")
                if response.status not in _REDIRECT_STATUSES or location is None:
                    await self._check_size(response, url)
                    return response, url
                if hop == _MAX_REDIRECTS:
                    raise FetchError("too_many_redirects", url=url)
                if request.method not in _SAFE_METHODS:
                    raise FetchError("render_failed", url=url)
                target = urljoin(url, location)
                await self._check(target)
                if headers is None and origin(target) != requested:
                    headers = {
                        name: value
                        for name, value in request.headers.items()
                        if name.lower() not in _CREDENTIAL_HEADERS
                    }
                following = await route.fetch(url=target, headers=headers, max_redirects=0)
                await _dispose(response)
                response, url = following, target
        except BaseException:
            await _dispose(response)
            raise
        raise AssertionError("unreachable")  # pragma: no cover (the loop returns or raises)

    async def _check_size(self, response: APIResponse, url: str) -> None:
        """Raise :class:`TooLargeError` unless the body fits ``max_response_bytes``.

        The announced length is judged first (an oversized body is then never read), then the
        actual one.
        """
        limit = self._client.max_response_bytes
        announced = response.headers.get("content-length", "")
        if announced.isascii() and announced.isdigit() and int(announced) > limit:
            raise TooLargeError(url=url, limit=limit)
        if len(await response.body()) > limit:
            raise TooLargeError(url=url, limit=limit)

    def _refuse(self, route: Route, error: FetchError) -> None:
        """Record ``error`` as the render's failure if ``route`` is a main-frame navigation.

        A sub-resource that is refused does not fail the render. The first failure is kept.
        """
        if self.failure is None and self._is_main_navigation(route):
            self.failure = error

    @staticmethod
    def _is_main_navigation(route: Route) -> bool:
        request = route.request
        return request.is_navigation_request() and request.frame.parent_frame is None

    async def web_socket(self, socket: WebSocketRoute) -> None:
        url = socket.url
        try:
            await self._check(url)
            socket.connect_to_server()
            return
        except FetchError:
            _log_request("blocked", url)
        except Exception:
            _log_request("failed", url)
        with contextlib.suppress(Exception):
            await socket.close()

    async def _check(self, url: str) -> None:
        """Raise unless the client's guard lets ``url`` through (``ws``/``wss`` count as http(s)).

        Raises the client's :class:`BlockedError` (cached per scheme, host and port) or
        :class:`FetchError` (a failed lookup or timeout, which may succeed next time: not
        cached).
        """
        parts = urlsplit(url)
        scheme = _SOCKET_SCHEMES.get(parts.scheme, parts.scheme)
        key = (scheme, parts.hostname, parts.port)
        if key in self._verdicts:
            refusal = self._verdicts[key]
        else:
            try:
                await self._client.check_target(urlunsplit(parts._replace(scheme=scheme)))
                refusal = None
            except BlockedError as error:
                refusal = error
            self._verdicts[key] = refusal
        if refusal is not None:
            raise BlockedError(refusal.reason, url=url)

    @staticmethod
    async def _abort(route: Route, url: str, outcome: str) -> None:
        _log_request(outcome, url)
        with contextlib.suppress(Exception):  # e.g. the route was already handled
            await route.abort("blockedbyclient")


async def _dispose(response: APIResponse) -> None:
    with contextlib.suppress(Exception):
        await response.dispose()


def _log_request(outcome: str, url: str) -> None:
    """Debug-log a sub-request that was not made; the host only, as paths and queries can
    carry tokens."""
    _log.debug("browser_request_" + outcome, extra={"host": urlsplit(url).hostname or ""})


def _logged(error: FetchError) -> FetchError:
    _log_failure(error)
    return error


def _log_failure(error: FetchError) -> None:
    """Log a failed render once, without credentials, query or fragment of the URL."""
    extra: dict[str, Any] = {
        "url": without_query(error.url),
        "host": urlsplit(error.url).hostname or "",
        "reason": str(error.reason),
        "status": error.status,
    }
    _log.warning("render_failed", extra=extra)
