"""Safe shared HTTP client for source fetchers.

``SafeHttpClient.get`` follows redirects itself, one *hop* at a time, so every policy applies to
every hop and not only to the URL the caller passed. The ordered steps of one hop are:

1. ``_guard``: scheme check and address validation (the connection is pinned to the validated
   address);
2. ``_check_robots``: robots.txt policy of the target origin (skipped for the robots fetch);
3. ``_slot``: per-origin rate limit, held until the body has been read;
4. ``_build_request``: request pinned to one validated address, with the headers the client
   controls;
5. ``_send_pinned``: send with ``stream=True``, trying the next validated address only when the
   connection cannot be made; transport errors become :class:`FetchError`;
6. ``_read_body``: read the body within the response size limit;
7. ``_classify``: 2xx result, 304 not modified, redirect or ``http_status`` error.

Each step is its own private method so a policy is added by filling in one method.

Fetches made outside the client (a headless browser) reuse the same steps through
:meth:`SafeHttpClient.check_target` (step 1 alone) and :meth:`SafeHttpClient.admission`
(steps 1 to 3, the slot held while the caller fetches).
"""

import asyncio
import codecs
import logging
import re
import zlib
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from http.cookiejar import CookieJar, DefaultCookiePolicy
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network
from types import MappingProxyType
from typing import Any, Final, Self

import httpx2
from pydantic import BaseModel, ConfigDict, model_validator

import invio
from invio.config.settings import (
    HttpBytes,
    HttpContact,
    HttpCount,
    HttpSeconds,
    Settings,
    check_http_timeouts,
    get_settings,
)
from invio.sources.errors import (
    BlockedError,
    BlockReason,
    FetchError,
    RenderUnavailableError,
    TooLargeError,
)
from invio.sources.netguard import (
    GuardedTarget,
    Resolver,
    SystemResolver,
    check_scheme,
    guard_url,
    origin_of,
)
from invio.sources.ratelimit import HostRateLimiter, effective_interval
from invio.sources.robots import ROBOTS_MAX_BYTES, RobotsCache
from invio.sources.urls import redact_url, without_query

__all__ = [
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
]

_log = logging.getLogger("invio.sources.http")

_REPOSITORY_URL: Final = "https://github.com/kaihempel/invio"
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
# Headers the client controls itself; a caller-supplied value is ignored.
_CLIENT_HEADERS: Final = frozenset({"user-agent", "host", "accept-encoding"})
# Caller headers that may follow a redirect to another origin; everything else (credentials,
# cookies, API keys, ...) is dropped.
_CROSS_ORIGIN_HEADERS: Final = re.compile(
    r"accept.*|if-none-match|if-modified-since", re.IGNORECASE
)
_ENCODINGS: Final = {"gzip": "gzip", "x-gzip": "gzip", "deflate": "deflate"}
_CHARSET: Final = re.compile(r"charset\s*=\s*[\"']?([^\s;\"']+)", re.IGNORECASE)
# Conditional-GET validators kept per client; the least recently used are dropped beyond this.
_MAX_VALIDATORS: Final = 10_000


def charset_label(content_type: str) -> str | None:
    """The ``charset`` parameter of a ``Content-Type`` value as written, if there is one."""
    match = _CHARSET.search(content_type)
    return match.group(1) if match is not None else None


def _setting_default(name: str) -> Any:
    """The default of setting ``name``: :class:`Settings` is the one place defaults live."""
    return Settings.model_fields[name].default


class HttpClientConfig(BaseModel):
    """Limits and identity of a :class:`SafeHttpClient`; built from settings in production."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contact: HttpContact = _setting_default("http_contact")
    max_response_bytes: HttpBytes = _setting_default("http_max_response_bytes")
    max_redirects: HttpCount = _setting_default("http_max_redirects")
    connect_timeout: HttpSeconds = _setting_default("http_connect_timeout_seconds")
    read_timeout: HttpSeconds = _setting_default("http_read_timeout_seconds")
    total_timeout: HttpSeconds = _setting_default("http_total_timeout_seconds")
    host_interval: HttpSeconds = _setting_default("http_host_interval_seconds")
    respect_robots: bool = _setting_default("http_respect_robots")

    @model_validator(mode="after")
    def _validate_timeouts(self) -> Self:
        check_http_timeouts(
            self.read_timeout,
            self.total_timeout,
            read_name="read_timeout",
            total_name="total_timeout",
        )
        return self

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Self:
        """Build the config from the ``INVIO_HTTP_*`` settings (global settings by default)."""
        settings = settings if settings is not None else get_settings()
        return cls(
            contact=settings.http_contact,
            max_response_bytes=settings.http_max_response_bytes,
            max_redirects=settings.http_max_redirects,
            connect_timeout=settings.http_connect_timeout_seconds,
            read_timeout=settings.http_read_timeout_seconds,
            total_timeout=settings.http_total_timeout_seconds,
            host_interval=settings.http_host_interval_seconds,
            respect_robots=settings.http_respect_robots,
        )

    @property
    def user_agent(self) -> str:
        """The ``User-Agent`` header value: product, project URL and operator contact."""
        return f"invio/{invio.__version__} (+{_REPOSITORY_URL}; contact: {self.contact})"


@dataclass(frozen=True, slots=True)
class FetchResult:
    """A successful (2xx) response."""

    url: str  # final URL after redirects
    requested_url: str  # URL the caller passed
    status: int
    headers: Mapping[str, str]
    content: bytes  # decoded body, never longer than ``max_response_bytes``
    etag: str | None
    last_modified: str | None

    def text(self, encoding: str | None = None) -> str:
        """Decode the body with ``encoding`` or the header charset (UTF-8 if absent/unknown)."""
        return self.content.decode(encoding or self._header_charset(), errors="replace")

    def _header_charset(self) -> str:
        label = charset_label(self.headers.get("content-type", ""))
        if label is not None:
            try:
                return codecs.lookup(label).name
            except LookupError:
                pass
        return "utf-8"


@dataclass(frozen=True, slots=True)
class NotModified:
    """A 304 answer to a conditional request; carries the validators that were sent."""

    url: str
    etag: str | None
    last_modified: str | None


@dataclass(frozen=True, slots=True)
class _Hop:
    """The raw outcome of one request: status, headers, the logical URL and the body.

    ``body`` is only read for 2xx answers; redirects, 304 and error statuses carry ``b""``.
    """

    status: int
    headers: httpx2.Headers
    url: httpx2.URL  # logical URL (original host name), never the pinned address
    body: bytes


class _Decoder:
    """Incremental body decoder whose output per call is bounded (identity, gzip, deflate).

    A gzip body may consist of several members, which are decoded one after the other; NUL
    padding after the last member is ignored. Any other data after the end of the compressed
    stream is a malformed response.
    """

    def __init__(self, encoding: str | None, url: httpx2.URL) -> None:
        self.encoding = encoding
        self._url = url
        self._inflater: zlib._Decompress | None = None
        # True while the body so far is complete: no compressed stream has been started, or
        # the last one ended cleanly. An empty body is complete whatever its encoding.
        self.finished = True

    def feed(self, data: bytes, max_length: int) -> bytes:
        """Decode ``data``, returning at most ``max_length`` bytes (callers pass limit + 1)."""
        if self.encoding is None:
            return data
        out = bytearray()
        try:
            while data and len(out) < max_length:
                if self._inflater is not None and self._inflater.eof:
                    if not data.strip(b"\0"):
                        break
                    if self.encoding != "gzip":
                        raise FetchError("invalid_response", url=str(self._url))
                    self._inflater = None  # the next gzip member starts here
                if self._inflater is None:
                    self._inflater = zlib.decompressobj(self._wbits(data))
                out += self._inflater.decompress(data, max_length - len(out))
                data = (
                    self._inflater.unused_data
                    if self._inflater.eof
                    else self._inflater.unconsumed_tail
                )
        except zlib.error:
            raise FetchError("invalid_response", url=str(self._url)) from None
        self.finished = self._inflater is None or self._inflater.eof
        return bytes(out)

    def _wbits(self, first: bytes) -> int:
        if self.encoding == "gzip":
            return 16 + zlib.MAX_WBITS
        # "deflate" should be zlib-wrapped, but some servers send a raw deflate stream.
        looks_zlib = (
            len(first) >= 2 and first[0] & 0x0F == 8 and (first[0] * 256 + first[1]) % 31 == 0
        )
        return zlib.MAX_WBITS if looks_zlib else -zlib.MAX_WBITS


def _decoder_for(headers: httpx2.Headers, url: httpx2.URL) -> _Decoder:
    """Pick the decoder for ``Content-Encoding``; stacked or unknown encodings are refused.

    Only one layer of gzip or deflate is accepted (the client asks for nothing else), which
    keeps the expansion bounded by a single, size-limited decoder.
    """
    raw = ",".join(headers.get_list("content-encoding"))
    encodings = [item.strip().lower() for item in raw.split(",") if item.strip()]
    encodings = [item for item in encodings if item != "identity"]
    if not encodings:
        return _Decoder(None, url)
    if len(encodings) > 1 or encodings[0] not in _ENCODINGS:
        raise FetchError("invalid_response", url=str(url))
    return _Decoder(_ENCODINGS[encodings[0]], url)


@dataclass
class _Budget:
    """The deadline of one fetch, shared by all its hops.

    Set when the first request is about to be sent (after the first rate-limit slot), so the
    wait for a slot is not charged to the first hop; later hops spend what is left.
    """

    deadline: float | None = None


@dataclass(frozen=True, slots=True)
class _Validators:
    etag: str | None
    last_modified: str | None


class SafeHttpClient:
    """HTTP GET client for fetching untrusted URLs; one instance per run.

    Always use it as ``async with``, or call :meth:`aclose`. ``allow_networks``, ``resolver``
    and ``transport`` exist for tests only.

    ``total_timeout`` bounds the sending and reading of one :meth:`get` including all
    redirect hops, measured from the first send. It is not a wall-clock limit on :meth:`get`:
    the DNS check of each hop (up to ``connect_timeout``), the wait for a rate-limit slot (up
    to ``max(host_interval, Crawl-delay)``, Crawl-delay capped at 30 s) and the first
    robots.txt fetch of an origin (with its own ``total_timeout``) come on top.

    URL userinfo (``user:password@``) is never sent and never appears in results, errors or
    logs; query strings and fragments are left out of error messages and logs. Cookies are
    never stored, so ``Set-Cookie`` of one response never reaches another request.
    """

    def __init__(
        self,
        config: HttpClientConfig | None = None,
        *,
        allow_networks: Iterable[IPv4Network | IPv6Network] = (),
        resolver: Resolver | None = None,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config if config is not None else HttpClientConfig.from_settings()
        self._allow_networks = tuple(allow_networks)
        self._resolver: Resolver = resolver if resolver is not None else SystemResolver()
        self._robots = RobotsCache(self._fetch_robots)
        self._limiter = HostRateLimiter()
        # By normalised requested URL, least recently used first; never persisted.
        self._validators: OrderedDict[str, _Validators] = OrderedDict()
        # Redirects are followed by ``_fetch`` so each hop is checked; no proxies from the
        # environment; no idle connections, so a pinned connection is never reused for another
        # host name. No cookies (an empty allow list refuses every domain): requests are pinned
        # to an IP address, so a jar would key cookies on the address and hand one site's
        # cookies to another site on the same (shared hosting) address.
        self._client = httpx2.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            cookies=CookieJar(policy=DefaultCookiePolicy(allowed_domains=[])),
            transport=transport,
            timeout=httpx2.Timeout(
                connect=self._config.connect_timeout,
                read=self._config.read_timeout,
                write=self._config.connect_timeout,
                pool=self._config.connect_timeout,
            ),
            limits=httpx2.Limits(max_keepalive_connections=0),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool; calling it again is harmless."""
        await self._client.aclose()

    @property
    def user_agent(self) -> str:
        """The ``User-Agent`` the client sends; a browser driven under the guard sends it too."""
        return self._config.user_agent

    @property
    def max_response_bytes(self) -> int:
        """The largest response body the client accepts (also the cap for rendered pages)."""
        return self._config.max_response_bytes

    async def check_target(self, url: str) -> None:
        """Refuse ``url`` unless its scheme and every address it resolves to are allowed.

        Only the scheme and address guard of :meth:`get` (with the client's resolver and
        ``allow_networks``): nothing is sent, robots.txt and the rate limit are not consulted.
        For requests made outside the client, such as the sub-requests of a browser page.
        Raises :class:`BlockedError`, or :class:`FetchError` (``"invalid_url"``,
        ``"dns_failed"``, ``"timeout"``). Failures are not logged here: a page makes many
        requests, and the caller decides what is worth reporting.

        Unlike :meth:`get`, the connection is not pinned to the checked address, so whoever
        connects resolves the name again (a DNS rebinding window).
        """
        await self._guard(self._parse(url))

    @asynccontextmanager
    async def admission(self, url: str) -> AsyncIterator[None]:
        """Admit a fetch made outside the client (a browser navigation) to ``url``.

        Runs the guard and the robots.txt check of :meth:`get` before yielding (a refusal
        raises before the block runs), then holds the per-origin rate-limit slot while the
        block runs. The slot is held for the whole block, so other fetches of the same origin
        wait for it (up to the block's duration, which callers bound), and slots are not
        re-entrant: do not call :meth:`get` for the same origin inside the block.

        A refusal is logged once, like :meth:`get` does. An error raised inside the block
        is the block's own to report.
        """
        try:
            target = await self._guard(self._parse(url))
            await self._check_robots(target)
        except FetchError as error:
            self._log_failure(error)
            raise
        async with self._slot(target):
            yield

    async def get(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> FetchResult | NotModified:
        """GET ``url``, following redirects; raises :class:`FetchError` on any failure.

        This is the only place that classifies the outcome and logs failures, so each failed
        fetch is logged exactly once.
        """
        requested_url = redact_url(url)
        cache_key = self._cache_key(requested_url)
        sent = self._with_validators(cache_key, headers or {})
        try:
            hop = await self._fetch(
                url,
                sent,
                check_robots=True,
                max_bytes=self._config.max_response_bytes,
                truncate=False,
            )
            return self._classify(hop, requested_url=requested_url, cache_key=cache_key, sent=sent)
        except FetchError as error:
            self._log_failure(error)
            raise

    async def _fetch(
        self,
        url: str,
        headers: Mapping[str, str],
        *,
        check_robots: bool,
        max_bytes: int,
        truncate: bool,
    ) -> _Hop:
        """Follow redirects and return the final raw hop without judging its status.

        Shared by :meth:`get` and the robots.txt fetch, which needs the raw status (a 4xx
        means "allow all", not an error). ``current`` is always the logical URL.
        """
        current = self._parse(url)
        first_origin = origin_of(current)
        budget = _Budget()
        for hop_number in range(self._config.max_redirects + 1):
            hop = await self._hop(
                current,
                self._headers_for(headers, same_origin=origin_of(current) == first_origin),
                budget,
                check_robots=check_robots,
                max_bytes=max_bytes,
                truncate=truncate,
            )
            if hop.status not in _REDIRECT_STATUSES:
                return hop
            location = hop.headers.get("location")
            if not location:
                raise FetchError("missing_location", url=str(current), status=hop.status)
            if hop_number == self._config.max_redirects:
                raise FetchError("too_many_redirects", url=str(current))
            current = self._join(current, location)
        raise AssertionError("unreachable: the loop always returns or raises")  # pragma: no cover

    @staticmethod
    def _headers_for(headers: Mapping[str, str], *, same_origin: bool) -> Mapping[str, str]:
        """Caller headers for a hop; only harmless ones are forwarded to another origin."""
        if same_origin:
            return headers
        return {k: v for k, v in headers.items() if _CROSS_ORIGIN_HEADERS.fullmatch(k)}

    @staticmethod
    def _log_failure(error: FetchError) -> None:
        """Log a refused or failed fetch; the URL carries no credentials, query or fragment."""
        try:
            host = httpx2.URL(error.url).host
        except (httpx2.InvalidURL, ValueError):  # error.url is the unparsable input
            host = ""
        extra = {"url": without_query(error.url), "host": host, "reason": str(error.reason)}
        if isinstance(error, BlockedError):
            _log.warning("http_blocked", extra=extra)
        else:
            _log.warning("http_failed", extra=extra | {"status": error.status})

    # --- URL handling ------------------------------------------------------------------------

    @staticmethod
    def _parse(url: str) -> httpx2.URL:
        """Parse, then check the scheme, then require a host; userinfo is dropped."""
        try:
            parsed = httpx2.URL(url)
        except (httpx2.InvalidURL, ValueError):
            raise FetchError("invalid_url", url=url) from None
        if not parsed.scheme:
            raise FetchError("invalid_url", url=url)
        check_scheme(parsed)
        if not parsed.host:
            raise FetchError("invalid_url", url=url)
        return parsed.copy_with(userinfo=b"")

    @classmethod
    def _join(cls, base: httpx2.URL, location: str) -> httpx2.URL:
        """Resolve a redirect target; a malformed one is the server's fault (invalid_response)."""
        try:
            return cls._parse(str(base.join(location)))
        except BlockedError:
            raise  # a non-web scheme stays a blocked error
        except (httpx2.InvalidURL, ValueError, FetchError):
            raise FetchError("invalid_response", url=str(base)) from None

    # --- the pipeline of one hop -------------------------------------------------------------

    async def _hop(
        self,
        url: httpx2.URL,
        extra_headers: Mapping[str, str],
        budget: _Budget,
        *,
        check_robots: bool,
        max_bytes: int,
        truncate: bool,
    ) -> _Hop:
        """Run one request through the pipeline."""
        target = await self._guard(url)
        if check_robots:
            await self._check_robots(target)  # before the slot: slots are not re-entrant
        async with self._slot(target):
            if budget.deadline is None:
                budget.deadline = asyncio.get_running_loop().time() + self._config.total_timeout
            try:
                async with asyncio.timeout_at(budget.deadline):
                    response = await self._send_pinned(target, extra_headers)
                    try:
                        body = b""
                        if 200 <= response.status_code < 300:
                            body = await self._read_body(
                                response, target.url, max_bytes=max_bytes, truncate=truncate
                            )
                        return _Hop(response.status_code, response.headers, target.url, body)
                    finally:
                        await response.aclose()
            except (TimeoutError, httpx2.TimeoutException):
                raise FetchError("timeout", url=str(url)) from None
            except (httpx2.DecodingError, httpx2.InvalidURL):
                raise FetchError("invalid_response", url=str(url)) from None
            except httpx2.RemoteProtocolError as exc:
                # httpx2 parses the Location of a redirect even when it does not follow it, and
                # reports an unparsable one as a protocol error: that is a malformed response.
                malformed = "location header" in str(exc).lower()
                reason = "invalid_response" if malformed else "connection_failed"
                raise FetchError(reason, url=str(url)) from None
            except httpx2.RequestError:
                raise FetchError("connection_failed", url=str(url)) from None

    async def _guard(self, url: httpx2.URL) -> GuardedTarget:
        """Check scheme and resolved addresses; nothing is sent to a refused target."""
        try:
            async with asyncio.timeout(self._config.connect_timeout):
                return await guard_url(url, self._resolver, self._allow_networks)
        except TimeoutError:
            raise FetchError("timeout", url=str(url)) from None

    async def _check_robots(self, target: GuardedTarget) -> None:
        """Raise :class:`BlockedError` if robots.txt disallows ``target``."""
        if not self._config.respect_robots:
            return
        policy = await self._robots.policy(target.origin)
        if not policy.allows(str(target.url)):
            raise BlockedError(BlockReason.BLOCKED_BY_ROBOTS, url=str(target.url))

    async def _fetch_robots(self, url: httpx2.URL) -> tuple[int, bytes]:
        """Fetch robots.txt through the normal pipeline, minus the robots check itself.

        Returns the raw status (a 4xx is a policy, not an error) and at most 500 KB of body.
        """
        hop = await self._fetch(
            str(url), {}, check_robots=False, max_bytes=ROBOTS_MAX_BYTES, truncate=True
        )
        return hop.status, hop.body

    @asynccontextmanager
    async def _slot(self, target: GuardedTarget) -> AsyncIterator[None]:
        """Hold the per-origin rate-limit slot for one request.

        The interval is raised by the origin's ``Crawl-delay`` once its robots.txt is known; the
        robots.txt request itself (and any hop before that) uses the configured interval.
        """
        policy = self._robots.known(target.origin)
        interval = effective_interval(
            self._config.host_interval, policy.crawl_delay if policy is not None else None
        )
        async with self._limiter.slot(target.origin, interval):
            yield

    def _build_request(
        self,
        target: GuardedTarget,
        address: IPv4Address | IPv6Address,
        extra_headers: Mapping[str, str],
    ) -> httpx2.Request:
        """Build the GET request pinned to ``address``, one of the target's validated addresses.

        The client owns User-Agent, Host and Accept-Encoding. The URL carries the IP; ``Host``
        and, for https, the TLS server name stay the original host name.
        """
        headers = {
            name: value
            for name, value in extra_headers.items()
            if name.lower() not in _CLIENT_HEADERS
        }
        headers["User-Agent"] = self._config.user_agent
        headers["Accept-Encoding"] = "gzip, deflate"
        headers["Host"] = target.url.netloc.decode("ascii")
        extensions: dict[str, str] = {}
        if target.origin.scheme == "https":
            extensions["sni_hostname"] = target.origin.host
        pinned = target.url.copy_with(host=str(address))
        return self._client.build_request("GET", pinned, headers=headers, extensions=extensions)

    async def _send_pinned(
        self, target: GuardedTarget, extra_headers: Mapping[str, str]
    ) -> httpx2.Response:
        """Send to the validated addresses in resolver order; return once headers arrived.

        Only a connection that cannot be made moves on to the next address (a dual-stack host
        on a machine without an IPv6 route, say); nothing has been sent at that point. The last
        address's error is the one that is raised.
        """
        *others, last = target.addresses
        for address in others:
            try:
                return await self._send(self._build_request(target, address, extra_headers))
            except (httpx2.ConnectError, httpx2.ConnectTimeout):
                continue
        return await self._send(self._build_request(target, last, extra_headers))

    async def _send(self, request: httpx2.Request) -> httpx2.Response:
        """Send ``request`` and return as soon as the headers have arrived."""
        return await self._client.send(request, stream=True)

    async def _read_body(
        self, response: httpx2.Response, url: httpx2.URL, *, max_bytes: int, truncate: bool
    ) -> bytes:
        """Read the decoded body; more than ``max_bytes`` raises, or is cut off if ``truncate``."""
        # The raw stream is decoded here, never by httpx2: a single network chunk can inflate to
        # hundreds of MiB, so decoding must stop at the size limit.
        decoder = _decoder_for(response.headers, url)
        # Content-Length counts the encoded bytes, so it only predicts the size when the body
        # is not compressed; a compressed body is judged by its decoded size below.
        announced = response.headers.get("content-length", "")
        if (
            not truncate
            and decoder.encoding is None
            and announced.isascii()
            and announced.isdigit()
            and int(announced) > max_bytes
        ):
            raise TooLargeError(url=str(url), limit=max_bytes)
        body = bytearray()
        async for chunk in response.aiter_raw():
            body += decoder.feed(chunk, max_bytes - len(body) + 1)
            if len(body) > max_bytes:
                if truncate:
                    return bytes(body[:max_bytes])
                raise TooLargeError(url=str(url), limit=max_bytes)
        if not decoder.finished:
            raise FetchError("invalid_response", url=str(url))  # truncated compressed stream
        return bytes(body)

    @staticmethod
    def _cache_key(requested_url: str) -> str:
        """The validator cache key: case, default port and fragment do not make a new URL."""
        try:
            return str(httpx2.URL(requested_url).copy_with(fragment=None))
        except (httpx2.InvalidURL, ValueError):
            return requested_url  # the fetch fails with invalid_url; nothing gets cached

    def _with_validators(self, cache_key: str, headers: Mapping[str, str]) -> Mapping[str, str]:
        """Add cached validators as conditional headers unless the caller set them."""
        cached = self._validators.get(cache_key)
        if cached is None:
            return headers
        self._validators.move_to_end(cache_key)
        names = {name.lower() for name in headers}
        merged = dict(headers)
        if cached.etag and "if-none-match" not in names:
            merged["If-None-Match"] = cached.etag
        if cached.last_modified and "if-modified-since" not in names:
            merged["If-Modified-Since"] = cached.last_modified
        return merged

    def _classify(
        self, hop: _Hop, *, requested_url: str, cache_key: str, sent: Mapping[str, str]
    ) -> FetchResult | NotModified:
        """Map a final (non-redirect) hop to a result, ``NotModified`` or an error.

        A 304 only means "unchanged" as the answer to a conditional request; to a plain GET it
        is an error, since the caller has no copy that could be unchanged.
        """
        if hop.status == 304:
            lowered = {name.lower(): value for name, value in sent.items()}
            etag, last_modified = lowered.get("if-none-match"), lowered.get("if-modified-since")
            if etag is None and last_modified is None:
                raise FetchError("http_status", url=str(hop.url), status=hop.status)
            return NotModified(url=requested_url, etag=etag, last_modified=last_modified)
        if not 200 <= hop.status < 300:
            raise FetchError("http_status", url=str(hop.url), status=hop.status)
        etag, last_modified = hop.headers.get("etag"), hop.headers.get("last-modified")
        if etag or last_modified:
            self._validators[cache_key] = _Validators(etag, last_modified)
            self._validators.move_to_end(cache_key)
            if len(self._validators) > _MAX_VALIDATORS:
                self._validators.popitem(last=False)
        return FetchResult(
            url=str(hop.url),
            requested_url=requested_url,
            status=hop.status,
            headers=MappingProxyType(httpx2.Headers(hop.headers)),
            content=hop.body,
            etag=hop.headers.get("etag"),
            last_modified=hop.headers.get("last-modified"),
        )
