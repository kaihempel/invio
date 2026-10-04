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
from invio.sources.netguard import Resolver
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

_REPOSITORY_URL: Final = "https://github.com/kaihempel/invio"
_REDIRECT_STATUSES: Final = frozenset({301, 302, 303, 307, 308})
# Headers the client controls itself; a caller-supplied value is ignored.
_CLIENT_HEADERS: Final = frozenset({"user-agent", "host", "accept-encoding"})
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
        if "\r" in value or "\n" in value:
            raise ValueError("must not contain line breaks")
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


class SafeHttpClient:
    """HTTP GET client for fetching untrusted URLs; one instance per run.

    Always use it as ``async with``, or call :meth:`aclose`. ``allow_networks``, ``resolver``
    and ``transport`` exist for tests only.
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
        self._resolver = resolver
        # Redirects are followed by ``get`` so each hop is checked; no proxies from the
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
        """GET ``url``, following redirects; raises :class:`FetchError` on any failure."""
        current = self._parse(url)
        for hop in range(self._config.max_redirects + 1):
            response = await self._hop(current, headers or {}, check_robots=True)
            if response.status_code not in _REDIRECT_STATUSES:
                return self._classify(response, requested_url=url)
            location = response.headers.get("location")
            if not location:
                raise FetchError("missing_location", url=str(current), status=response.status_code)
            if hop == self._config.max_redirects:
                raise FetchError("too_many_redirects", url=str(current))
            current = self._join(current, location)
        raise AssertionError("unreachable: the loop always returns or raises")  # pragma: no cover

    # --- URL handling ------------------------------------------------------------------------

    @staticmethod
    def _parse(url: str) -> httpx.URL:
        try:
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, ValueError):
            raise FetchError("invalid_url", url=url) from None
        if not parsed.host:
            raise FetchError("invalid_url", url=url)
        return parsed

    @classmethod
    def _join(cls, base: httpx.URL, location: str) -> httpx.URL:
        try:
            return cls._parse(str(base.join(location)))
        except (httpx.InvalidURL, ValueError):
            raise FetchError("invalid_url", url=str(base)) from None

    # --- the pipeline of one hop -------------------------------------------------------------

    async def _hop(
        self, url: httpx.URL, extra_headers: Mapping[str, str], *, check_robots: bool
    ) -> httpx.Response:
        """Run one request through the pipeline; the returned response has its body read."""
        target = await self._guard(url)
        if check_robots:
            await self._check_robots(target)
        async with self._slot(target):
            request = self._build_request(target, extra_headers)
            try:
                async with asyncio.timeout(self._config.total_timeout):
                    response = await self._send(request)
                    try:
                        await self._read_body(response, url)
                    finally:
                        await response.aclose()
            except (TimeoutError, httpx.TimeoutException):
                raise FetchError("timeout", url=str(url)) from None
            except httpx.RequestError:
                raise FetchError("connection_failed", url=str(url)) from None
        return response

    async def _guard(self, url: httpx.URL) -> httpx.URL:
        """Validate the scheme and target address of ``url`` (added with the SSRF guard)."""
        return url

    async def _check_robots(self, target: httpx.URL) -> None:
        """Raise :class:`BlockedError` if robots.txt disallows ``target`` (added with robots)."""

    @asynccontextmanager
    async def _slot(self, target: httpx.URL) -> AsyncIterator[None]:
        """Hold the per-origin rate-limit slot for one request (added with the rate limiter)."""
        yield

    def _build_request(self, target: httpx.URL, extra_headers: Mapping[str, str]) -> httpx.Request:
        """Build the GET request; the client owns User-Agent, Host and Accept-Encoding."""
        headers = {
            name: value
            for name, value in extra_headers.items()
            if name.lower() not in _CLIENT_HEADERS
        }
        headers["User-Agent"] = self._config.user_agent
        headers["Accept-Encoding"] = "gzip, deflate"
        return self._client.build_request("GET", target, headers=headers)

    async def _send(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` and return as soon as the headers have arrived."""
        return await self._client.send(request, stream=True)

    async def _read_body(self, response: httpx.Response, url: httpx.URL) -> None:
        """Read the (decoded) body into ``response`` (size limit added with the limits)."""
        await response.aread()

    def _classify(self, response: httpx.Response, *, requested_url: str) -> FetchResult:
        """Map a final (non-redirect) response to a result or an ``http_status`` error."""
        status = response.status_code
        final_url = str(response.url)
        if not 200 <= status < 300:
            raise FetchError("http_status", url=final_url, status=status)
        return FetchResult(
            url=final_url,
            requested_url=requested_url,
            status=status,
            headers=MappingProxyType(httpx.Headers(response.headers)),
            content=response.content,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
        )
