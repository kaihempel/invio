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
from invio.sources.robots import ROBOTS_MAX_BYTES, RobotsCache
from invio.sources.urls import redact

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
# Never forwarded to another origin after a redirect.
_CREDENTIAL_HEADERS: Final = frozenset({"authorization", "cookie", "proxy-authorization"})
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


class SafeHttpClient:
    """HTTP GET client for fetching untrusted URLs; one instance per run.

    Always use it as ``async with``, or call :meth:`aclose`. ``allow_networks``, ``resolver``
    and ``transport`` exist for tests only.

    ``total_timeout`` is a deadline per request (hop), not for a whole redirect chain. URL
    userinfo (``user:password@``) is never sent and never appears in results, errors or logs.
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
        try:
            hop = await self._fetch(
                url,
                headers or {},
                check_robots=True,
                max_bytes=self._config.max_response_bytes,
                truncate=False,
            )
            return self._classify(hop, requested_url=redact(url))
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
        for hop_number in range(self._config.max_redirects + 1):
            hop = await self._hop(
                current,
                self._headers_for(headers, same_origin=origin_of(current) == first_origin),
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
        """Caller headers for a hop; credentials are not forwarded to another origin."""
        if same_origin:
            return headers
        return {k: v for k, v in headers.items() if k.lower() not in _CREDENTIAL_HEADERS}

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
        try:
            return cls._parse(str(base.join(location)))
        except (httpx.InvalidURL, ValueError):
            raise FetchError("invalid_url", url=str(base)) from None

    # --- the pipeline of one hop -------------------------------------------------------------

    async def _hop(
        self,
        url: httpx.URL,
        extra_headers: Mapping[str, str],
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
            try:
                async with asyncio.timeout(self._config.total_timeout):
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
            except httpx.DecodingError:
                raise FetchError("invalid_response", url=str(url)) from None
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
        """Hold the per-origin rate-limit slot for one request (added with the rate limiter)."""
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
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) > max_bytes:
                if truncate:
                    return bytes(body[:max_bytes])
                raise TooLargeError(url=str(url), limit=max_bytes)
        return bytes(body)

    def _classify(self, hop: _Hop, *, requested_url: str) -> FetchResult:
        """Map a final (non-redirect) hop to a result or an ``http_status`` error."""
        if not 200 <= hop.status < 300:
            raise FetchError("http_status", url=str(hop.url), status=hop.status)
        return FetchResult(
            url=str(hop.url),
            requested_url=requested_url,
            status=hop.status,
            headers=MappingProxyType(httpx.Headers(hop.headers)),
            content=hop.body,
            etag=hop.headers.get("etag"),
            last_modified=hop.headers.get("last-modified"),
        )
