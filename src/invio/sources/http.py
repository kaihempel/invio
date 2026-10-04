"""Safe shared HTTP client for source fetchers.

``SafeHttpClient.get`` follows redirects itself, one *hop* at a time, so every policy applies to
every hop and not only to the URL the caller passed. The ordered steps of one hop are:

1. ``_guard``: scheme check and address validation (the connection is pinned to the validated
   address);
2. ``_check_robots``: robots.txt policy of the target origin (skipped for the robots fetch);
3. ``_slot``: per-origin rate limit, held until the body has been read;
4. ``_build_request``: pinned request with the headers the client controls;
5. ``_send``: send with ``stream=True`` and map transport errors to :class:`FetchError`;
6. ``_read_body``: read the body within the response size limit;
7. ``_classify``: 2xx result, 304 not modified, redirect or ``http_status`` error.

Each step is its own private method so a policy is added by filling in one method.
"""

import asyncio
import codecs
import logging
import re
import zlib
from collections.abc import AsyncIterator, Iterable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from ipaddress import IPv4Network, IPv6Network
from types import MappingProxyType
from typing import Final, Self

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

import invio
from invio.config.settings import Settings, get_settings
from invio.sources.errors import BlockedError, BlockReason, FetchError, TooLargeError
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
from invio.sources.urls import redact, redact_url

__all__ = [
    "BlockReason",
    "BlockedError",
    "FetchError",
    "FetchResult",
    "HttpClientConfig",
    "NotModified",
    "SafeHttpClient",
    "TooLargeError",
    "redact",
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


class HttpClientConfig(BaseModel):
    """Limits and identity of a :class:`SafeHttpClient`; built from settings in production."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contact: str = Field(default="admin@example.invalid", min_length=1)
    max_response_bytes: int = Field(default=10_485_760, gt=0)
    max_redirects: int = Field(default=5, ge=0)
    connect_timeout: float = Field(default=10.0, gt=0, allow_inf_nan=False)
    read_timeout: float = Field(default=30.0, gt=0, allow_inf_nan=False)
    total_timeout: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    host_interval: float = Field(default=1.0, gt=0, allow_inf_nan=False)
    respect_robots: bool = True

    @field_validator("contact")
    @classmethod
    def _validate_contact(cls, value: str) -> str:
        # The contact ends up in the User-Agent header; a line break would allow header injection.
        if not value.isascii() or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError("must be ASCII without control characters")
        return value

    @model_validator(mode="after")
    def _validate_timeouts(self) -> Self:
        if self.total_timeout < self.read_timeout:
            raise ValueError("total_timeout must be >= read_timeout")
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
        match = _CHARSET.search(self.headers.get("content-type", ""))
        if match is not None:
            try:
                return codecs.lookup(match.group(1)).name
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
    headers: httpx.Headers
    url: httpx.URL  # logical URL (original host name), never the pinned address
    body: bytes


class _Decoder:
    """Incremental body decoder whose output per call is bounded (identity, gzip, deflate)."""

    def __init__(self, encoding: str | None, url: httpx.URL) -> None:
        self._encoding = encoding
        self._url = url
        self._inflater: zlib._Decompress | None = None
        self.finished = encoding is None  # True once the compressed stream ended cleanly

    def feed(self, data: bytes, max_length: int) -> bytes:
        """Decode ``data``, returning at most ``max_length`` bytes (callers pass limit + 1)."""
        if self._encoding is None:
            return data
        if self._inflater is None:
            self._inflater = zlib.decompressobj(self._wbits(data))
        out = bytearray()
        try:
            while data and not self._inflater.eof and len(out) < max_length:
                out += self._inflater.decompress(data, max_length - len(out))
                data = self._inflater.unconsumed_tail
        except zlib.error:
            raise FetchError("invalid_response", url=str(self._url)) from None
        self.finished = self._inflater.eof
        return bytes(out)

    def _wbits(self, first: bytes) -> int:
        if self._encoding == "gzip":
            return 16 + zlib.MAX_WBITS
        # "deflate" should be zlib-wrapped, but some servers send a raw deflate stream.
        looks_zlib = (
            len(first) >= 2 and first[0] & 0x0F == 8 and (first[0] * 256 + first[1]) % 31 == 0
        )
        return zlib.MAX_WBITS if looks_zlib else -zlib.MAX_WBITS


def _decoder_for(headers: httpx.Headers, url: httpx.URL) -> _Decoder:
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

    ``total_timeout`` bounds one :meth:`get` including all redirect hops, measured from the
    first send (the robots.txt fetch has its own budget). URL userinfo (``user:password@``)
    is never sent and never appears in results, errors or logs.
    """

    def __init__(
        self,
        config: HttpClientConfig | None = None,
        *,
        allow_networks: Iterable[IPv4Network | IPv6Network] = (),
        resolver: Resolver | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._config = config if config is not None else HttpClientConfig.from_settings()
        self._allow_networks = tuple(allow_networks)
        self._resolver: Resolver = resolver if resolver is not None else SystemResolver()
        self._robots = RobotsCache(self._fetch_robots)
        self._limiter = HostRateLimiter()
        self._validators: dict[str, _Validators] = {}  # by requested URL, never persisted
        # Redirects are followed by ``_fetch`` so each hop is checked; no proxies from the
        # environment; no idle connections, so a pinned connection is never reused for another
        # host name.
        self._client = httpx.AsyncClient(
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            timeout=httpx.Timeout(
                connect=self._config.connect_timeout,
                read=self._config.read_timeout,
                write=self._config.connect_timeout,
                pool=self._config.connect_timeout,
            ),
            limits=httpx.Limits(max_keepalive_connections=0),
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the connection pool; calling it again is harmless."""
        await self._client.aclose()

    async def get(
        self, url: str, *, headers: Mapping[str, str] | None = None
    ) -> FetchResult | NotModified:
        """GET ``url``, following redirects; raises :class:`FetchError` on any failure.

        This is the only place that classifies the outcome and logs failures, so each failed
        fetch is logged exactly once.
        """
        requested_url = redact_url(url)
        sent = self._with_validators(requested_url, headers or {})
        try:
            hop = await self._fetch(
                url,
                sent,
                check_robots=True,
                max_bytes=self._config.max_response_bytes,
                truncate=False,
            )
            return self._classify(hop, requested_url=requested_url, sent=sent)
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
        """Log a refused or failed fetch, with the URL stripped of credentials."""
        try:
            host = httpx.URL(error.url).host
        except (httpx.InvalidURL, ValueError):  # error.url is the unparsable input
            host = ""
        extra = {"url": error.url, "host": host, "reason": str(error.reason)}
        if isinstance(error, BlockedError):
            _log.warning("http_blocked", extra=extra)
        else:
            _log.warning("http_failed", extra=extra | {"status": error.status})

    # --- URL handling ------------------------------------------------------------------------

    @staticmethod
    def _parse(url: str) -> httpx.URL:
        """Parse, then check the scheme, then require a host; userinfo is dropped."""
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, ValueError):
            raise FetchError("invalid_url", url=url) from None
        if not parsed.scheme:
            raise FetchError("invalid_url", url=url)
        check_scheme(parsed)
        if not parsed.host:
            raise FetchError("invalid_url", url=url)
        return parsed.copy_with(userinfo=b"")

    @classmethod
    def _join(cls, base: httpx.URL, location: str) -> httpx.URL:
        """Resolve a redirect target; a malformed one is the server's fault (invalid_response)."""
        try:
            return cls._parse(str(base.join(location)))
        except BlockedError:
            raise  # a non-web scheme stays a blocked error
        except (httpx.InvalidURL, ValueError, FetchError):
            raise FetchError("invalid_response", url=str(base)) from None

    # --- the pipeline of one hop -------------------------------------------------------------

    async def _hop(
        self,
        url: httpx.URL,
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
            request = self._build_request(target, extra_headers)
            if budget.deadline is None:
                budget.deadline = asyncio.get_running_loop().time() + self._config.total_timeout
            try:
                async with asyncio.timeout_at(budget.deadline):
                    response = await self._send(request)
                    try:
                        body = b""
                        if 200 <= response.status_code < 300:
                            body = await self._read_body(
                                response, target.url, max_bytes=max_bytes, truncate=truncate
                            )
                        return _Hop(response.status_code, response.headers, target.url, body)
                    finally:
                        await response.aclose()
            except (TimeoutError, httpx.TimeoutException):
                raise FetchError("timeout", url=str(url)) from None
            except (httpx.DecodingError, httpx.InvalidURL):
                raise FetchError("invalid_response", url=str(url)) from None
            except httpx.RemoteProtocolError as exc:
                # httpx parses the Location of a redirect even when it does not follow it, and
                # reports an unparsable one as a protocol error: that is a malformed response.
                malformed = "location header" in str(exc).lower()
                reason = "invalid_response" if malformed else "connection_failed"
                raise FetchError(reason, url=str(url)) from None
            except httpx.RequestError:
                raise FetchError("connection_failed", url=str(url)) from None

    async def _guard(self, url: httpx.URL) -> GuardedTarget:
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

    async def _fetch_robots(self, url: httpx.URL) -> tuple[int, bytes]:
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
        self, target: GuardedTarget, extra_headers: Mapping[str, str]
    ) -> httpx.Request:
        """Build the GET request pinned to the validated address.

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
        pinned = target.url.copy_with(host=str(target.address))
        return self._client.build_request("GET", pinned, headers=headers, extensions=extensions)

    async def _send(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` and return as soon as the headers have arrived."""
        return await self._client.send(request, stream=True)

    async def _read_body(
        self, response: httpx.Response, url: httpx.URL, *, max_bytes: int, truncate: bool
    ) -> bytes:
        """Read the decoded body; more than ``max_bytes`` raises, or is cut off if ``truncate``."""
        announced = response.headers.get("content-length", "")
        if not truncate and announced.isdigit() and int(announced) > max_bytes:
            raise TooLargeError(url=str(url), limit=max_bytes)
        # The raw stream is decoded here, never by httpx: a single network chunk can inflate to
        # hundreds of MiB, so decoding must stop at the size limit.
        # A response that was read already (``MockTransport``) is decoded by httpx.
        decoder = (
            _Decoder(None, url)
            if response.is_stream_consumed
            else _decoder_for(response.headers, url)
        )
        body = bytearray()
        async for chunk in self._raw_chunks(response):
            body += decoder.feed(chunk, max_bytes - len(body) + 1)
            if len(body) > max_bytes:
                if truncate:
                    return bytes(body[:max_bytes])
                raise TooLargeError(url=str(url), limit=max_bytes)
        if not decoder.finished:
            raise FetchError("invalid_response", url=str(url))  # truncated compressed stream
        return bytes(body)

    @staticmethod
    async def _raw_chunks(response: httpx.Response) -> AsyncIterator[bytes]:
        """The undecoded body; a response that was already read (``MockTransport``) is
        yielded as a whole instead, and httpx has decoded it by then."""
        if response.is_stream_consumed:
            yield response.content
            return
        async for chunk in response.aiter_raw():
            yield chunk

    def _with_validators(self, requested_url: str, headers: Mapping[str, str]) -> Mapping[str, str]:
        """Add cached validators as conditional headers unless the caller set them."""
        cached = self._validators.get(requested_url)
        if cached is None:
            return headers
        names = {name.lower() for name in headers}
        merged = dict(headers)
        if cached.etag and "if-none-match" not in names:
            merged["If-None-Match"] = cached.etag
        if cached.last_modified and "if-modified-since" not in names:
            merged["If-Modified-Since"] = cached.last_modified
        return merged

    def _classify(
        self, hop: _Hop, *, requested_url: str, sent: Mapping[str, str]
    ) -> FetchResult | NotModified:
        """Map a final (non-redirect) hop to a result, ``NotModified`` or an error."""
        if hop.status == 304:
            lowered = {name.lower(): value for name, value in sent.items()}
            return NotModified(
                url=requested_url,
                etag=lowered.get("if-none-match"),
                last_modified=lowered.get("if-modified-since"),
            )
        if not 200 <= hop.status < 300:
            raise FetchError("http_status", url=str(hop.url), status=hop.status)
        etag, last_modified = hop.headers.get("etag"), hop.headers.get("last-modified")
        if etag or last_modified:
            self._validators[requested_url] = _Validators(etag, last_modified)
        return FetchResult(
            url=str(hop.url),
            requested_url=requested_url,
            status=hop.status,
            headers=MappingProxyType(httpx.Headers(hop.headers)),
            content=hop.body,
            etag=hop.headers.get("etag"),
            last_modified=hop.headers.get("last-modified"),
        )
