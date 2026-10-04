# Contract: `invio.sources.http` Python API

The shared client is a library interface used by source fetchers (later issues). It has no CLI
surface. Everything below is exported from `invio.sources.http` (`__all__`); helper modules
(`netguard`, `robots`, `ratelimit`, `urls`) are internal except `redact`.

## Construction and lifetime

```python
from invio.sources.http import HttpClientConfig, SafeHttpClient

async with SafeHttpClient(HttpClientConfig.from_settings()) as client:
    result = await client.get("https://example.org/feed.xml")
```

```python
class SafeHttpClient:
    def __init__(
        self,
        config: HttpClientConfig | None = None,     # None -> HttpClientConfig.from_settings()
        *,
        allow_networks: Iterable[IPv4Network | IPv6Network] = (),  # TEST ONLY (FR-010)
        resolver: Resolver | None = None,            # TEST ONLY; default: loop.getaddrinfo
        transport: httpx.AsyncBaseTransport | None = None,  # TEST ONLY (MockTransport)
    ) -> None: ...
    async def __aenter__(self) -> Self: ...
    async def __aexit__(self, *exc: object) -> None: ...   # closes the pool
    async def aclose(self) -> None: ...

    async def get(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,    # extra request headers
    ) -> FetchResult | NotModified: ...
```

- One instance = one run: robots cache, rate-limiter state and validator cache are per instance.
- `get` is safe to call concurrently from many tasks.
- `allow_networks` addresses are exempt from the non-public check only; scheme, robots,
  rate-limit and size rules still apply. Not wired to settings or job files.
- Caller-provided `User-Agent`, `Host` headers are ignored (client controls them);
  caller-provided `If-None-Match` / `If-Modified-Since` override cached validators.

## `get` outcome table

| Situation | Outcome |
|-----------|---------|
| 2xx | `FetchResult` |
| 304 | `NotModified` |
| other 4xx/5xx (after redirects) | `FetchError(reason="http_status", status=<code>)` |
| scheme not http/https (start or redirect) | `BlockedError(reason=UNSUPPORTED_SCHEME)` |
| host resolves to any non-public address (start or redirect) | `BlockedError(reason=NON_PUBLIC_ADDRESS)` |
| robots.txt disallows path, or robots.txt failed with 5xx/network error | `BlockedError(reason=BLOCKED_BY_ROBOTS)` |
| `Content-Length` > limit, or decoded body exceeds limit | `TooLargeError(limit=…)` |
| > `max_redirects` hops | `FetchError(reason="too_many_redirects")` |
| redirect without `Location` | `FetchError(reason="missing_location")` |
| DNS failure | `FetchError(reason="dns_failed")` |
| connect/read/total timeout | `FetchError(reason="timeout")` |
| other transport error | `FetchError(reason="connection_failed")` |
| malformed URL | `FetchError(reason="invalid_url")` |

Guarantees:
- A `BlockedError` for `UNSUPPORTED_SCHEME`/`NON_PUBLIC_ADDRESS` is raised **before** any
  connection to that target; for `BLOCKED_BY_ROBOTS` no request for the disallowed URL is sent.
- `str(error)` and `error.url` never contain URL userinfo or request headers.
- `isinstance(BlockedError(...), FetchError)` and `isinstance(TooLargeError(...), FetchError)`.

## Requests on the wire

- Method `GET` only; `Accept-Encoding: gzip, deflate`.
- `User-Agent: invio/<version> (+https://github.com/kaihempel/invio; contact: <contact>)`.
- `Host: <original host[:port]>`; TCP connection to the validated IP; TLS SNI/cert check against
  the original host name.
- `If-None-Match` / `If-Modified-Since` when validators for that URL are cached.
- Per origin: at most one in flight; starts spaced by the effective interval
  (configured, raised by `Crawl-delay` up to 30 s). robots.txt and each redirect hop count.

## Logging

Logger `invio.sources.http`, JSON via `invio.log`:

| Event message | Level | `extra` |
|---------------|-------|---------|
| `http_blocked` | WARNING | `url` (redacted), `host`, `reason` |
| `http_failed` | WARNING | `url` (redacted), `host`, `reason`, `status` |

## Settings (`invio.config.settings.Settings`)

`INVIO_HTTP_CONTACT`, `INVIO_HTTP_MAX_RESPONSE_BYTES`, `INVIO_HTTP_MAX_REDIRECTS`,
`INVIO_HTTP_CONNECT_TIMEOUT_SECONDS`, `INVIO_HTTP_READ_TIMEOUT_SECONDS`,
`INVIO_HTTP_TOTAL_TIMEOUT_SECONDS`, `INVIO_HTTP_HOST_INTERVAL_SECONDS`,
`INVIO_HTTP_RESPECT_ROBOTS` — defaults and validation in [data-model.md](../data-model.md).
Documented in README and `.env.example`.
