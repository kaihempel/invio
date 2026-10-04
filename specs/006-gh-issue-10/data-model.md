# Data Model: Safe Shared HTTP Client (gh-issue-10)

All entities are in-memory and live for one `SafeHttpClient` instance (≈ one job run).
No database tables or migrations. Module locations are under `src/invio/sources/`.

## HttpClientConfig (`http.py`)

Frozen Pydantic model, `extra="forbid"`. Built from `Settings` via `from_settings()`.

| Field | Type | Default | Validation | Source setting |
|-------|------|---------|------------|----------------|
| `contact` | `str` | `"admin@example.invalid"` | non-empty, no CR/LF | `INVIO_HTTP_CONTACT` |
| `max_response_bytes` | `int` | `10_485_760` | `> 0` | `INVIO_HTTP_MAX_RESPONSE_BYTES` |
| `max_redirects` | `int` | `5` | `>= 0` | `INVIO_HTTP_MAX_REDIRECTS` |
| `connect_timeout` | `float` | `10.0` | `> 0`, finite | `INVIO_HTTP_CONNECT_TIMEOUT_SECONDS` |
| `read_timeout` | `float` | `30.0` | `> 0`, finite | `INVIO_HTTP_READ_TIMEOUT_SECONDS` |
| `total_timeout` | `float` | `60.0` | `> 0`, finite, `>= read_timeout` | `INVIO_HTTP_TOTAL_TIMEOUT_SECONDS` |
| `host_interval` | `float` | `1.0` | `> 0`, finite | `INVIO_HTTP_HOST_INTERVAL_SECONDS` |
| `respect_robots` | `bool` | `True` | — | `INVIO_HTTP_RESPECT_ROBOTS` |

`total_timeout` is the deadline of one `get()` including all redirect hops, measured from the first send (see research R9); the robots.txt fetch has its own.

Derived: `user_agent` = `invio/<version> (+https://github.com/kaihempel/invio; contact: <contact>)`.

Constants (not configurable): `ROBOTS_MAX_BYTES = 512_000`, `CRAWL_DELAY_CAP = 30.0`,
`ROBOTS_AGENT_TOKEN = "invio"`.

## Origin (`netguard.py`)

`NamedTuple(scheme: Literal["http","https"], host: str, port: int)` — lower-cased host,
default port filled in (80/443). Key for robots cache, rate limiter.

## GuardedTarget (`netguard.py`)

Result of a successful guard check for one hop.

| Field | Type | Meaning |
|-------|------|---------|
| `url` | `httpx.URL` | logical URL (original hostname) |
| `origin` | `Origin` | key for policies |
| `address` | `IPv4Address \| IPv6Address` | validated address the connection is pinned to |

## FetchResult (`http.py`)

Frozen dataclass (slots) returned for a 2xx response.

| Field | Type | Notes |
|-------|------|-------|
| `url` | `str` | final URL after redirects (logical, hostname form) |
| `requested_url` | `str` | URL the caller passed |
| `status` | `int` | 2xx |
| `headers` | `Mapping[str, str]` | response headers (immutable copy, case-insensitive lookup) |
| `content` | `bytes` | decoded body, `len <= max_response_bytes` |
| `etag` | `str \| None` | from `ETag` |
| `last_modified` | `str \| None` | from `Last-Modified` |

Convenience: `text(encoding: str | None = None) -> str` (charset from headers, fallback UTF-8,
`errors="replace"`).

## NotModified (`http.py`)

Frozen dataclass: `url: str`, `etag: str | None`, `last_modified: str | None` (the validators
that were sent).

## Errors (`http.py`)

```
FetchError(Exception)            url: str (redacted), status: int | None, reason: str
├── BlockedError(FetchError)     reason: BlockReason
└── TooLargeError(FetchError)    limit: int
```

`BlockReason(StrEnum)`: `UNSUPPORTED_SCHEME = "unsupported_scheme"`,
`NON_PUBLIC_ADDRESS = "non_public_address"`, `BLOCKED_BY_ROBOTS = "blocked_by_robots"`.

`FetchError.reason` values used: `"timeout"`, `"connection_failed"`, `"dns_failed"`,
`"too_many_redirects"`, `"missing_location"`, `"http_status"` (with `status` set), `"invalid_url"`,
`"invalid_response"` (corrupt, truncated or stacked/unsupported `Content-Encoding`, malformed redirect target).

## RobotsPolicy (`robots.py`)

Per origin, cached for the client's lifetime.

| Field | Type | Meaning |
|-------|------|---------|
| `mode` | `Literal["parsed","allow_all","disallow_all"]` | from robots.txt fetch outcome |
| `parser` | `RobotFileParser \| None` | set when `mode == "parsed"` |
| `crawl_delay` | `float \| None` | `parser.crawl_delay("invio")`, ignored if negative/invalid |

State transition (per origin): `unknown → fetching → {parsed | allow_all | disallow_all}`;
terminal for the run, never re-fetched (FR-014, FR-016).

| robots.txt outcome | mode |
|--------------------|------|
| 2xx | `parsed` (first 500 KB) |
| 4xx | `allow_all` |
| 5xx, timeout, connection/DNS error, blocked, too many redirects | `disallow_all` |

## HostRateLimiter state (`ratelimit.py`)

Per origin: `lock: asyncio.Lock`, `last_start: float | None` (monotonic seconds).
Effective interval per origin = `max(config.host_interval, min(crawl_delay or 0, 30.0))`:
`Crawl-delay` can only raise the interval, by at most 30 s; a configured interval is never capped.

## ValidatorCache (`http.py`, private)

`dict[str, Validators]` keyed by requested URL; `Validators(etag: str | None,
last_modified: str | None)`. Written only on 2xx with at least one validator; never persisted.
