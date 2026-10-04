# Research: Safe Shared HTTP Client (gh-issue-10)

All Technical Context unknowns are resolved below. Each entry: Decision / Rationale / Alternatives.

## R1 — HTTP library

- **Decision**: Add **httpx** (`>=0.28`) as a runtime dependency and use `httpx.AsyncClient` with
  `follow_redirects=False` and `trust_env=False`.
- **Rationale**: The issue names it; it is async-native, supports streaming bodies with a
  per-chunk iterator, granular timeouts (`httpx.Timeout(connect=…, read=…)`), and ships
  `httpx.MockTransport` for transport-level tests without an extra test dependency. The
  stdlib `urllib` used by `invio.cli.source_check` is blocking and has no async API.
  `trust_env=False` stops `HTTP(S)_PROXY`/`.netrc` from routing requests around the SSRF guard.
- **Alternatives**: `aiohttp` (heavier, own server/client stack, no advantage here);
  stdlib `urllib` in a thread pool (no streaming limits per chunk without re-implementing
  what `HttpSourceChecker` already does, no async rate limiting integration).
- **Constitution**: new runtime dependency → justified in the PR description (Technology
  constraints). Verified: httpx 0.28.1 / httpcore 1.0.9.

## R2 — SSRF guard: which addresses are "non-public"

- **Decision**: Resolve the host with `loop.getaddrinfo(host, port, type=SOCK_STREAM)` (IP
  literals are parsed directly, so `0x7f.1`, `2130706433` etc. are normalised by the resolver).
  Reject the request if **any** resolved address fails `ipaddress.ip_address(a).is_global`, or
  is multicast; IPv4-mapped / 6to4 / NAT64-embedded IPv6 addresses are unwrapped
  (`ipv4_mapped`, `sixtofour`) and the embedded IPv4 address is checked too.
- **Rationale**: `is_global` is `False` for loopback, RFC 1918, link-local, unspecified,
  shared address space (100.64/10), documentation and reserved ranges in both families, which
  covers FR-006 in one rule maintained by the stdlib. Multicast is `is_global` for some
  ranges, hence the explicit extra check. "Any address bad → reject" prevents a host that
  resolves to one public and one private address from being used as a pivot.
- **Alternatives**: hand-maintained CIDR deny-list (drifts, easy to miss IPv6 forms);
  allow-list of public ranges (impractical).

## R3 — Preventing DNS rebinding (FR-007)

- **Decision**: **Pin the connection to the validated address.** For each hop the guard returns
  the first validated IP; the request URL's host is replaced by that IP, the original
  `Host` header is set explicitly, and the request extension `sni_hostname=<original host>`
  is passed so TLS SNI and certificate verification still use the hostname.
- **Rationale**: Only public httpx/httpcore API is used (httpcore reads
  `request.extensions["sni_hostname"]` for `server_hostname`; verified in httpcore 1.0.9).
  No second resolution happens inside the transport, so check and connect use the same IP.
- **Alternatives**: custom `httpcore.AsyncNetworkBackend` that validates in `connect_tcp`
  (cleaner, but httpx's `AsyncHTTPTransport` does not accept a backend parameter, so it needs a
  hand-written transport or private-attribute patching); resolving twice and comparing
  (still racy).
- **Connection reuse**: because requests are pinned to an IP, httpx's pool groups connections
  by IP rather than by hostname. A keep-alive connection opened for `a.example` (TLS name and
  certificate for `a.example`) could otherwise be reused for `b.example` on the same IP, so
  TLS verification for `b.example` would never happen. The client therefore disables
  keep-alive (`httpx.Limits(max_keepalive_connections=0)`): every hop opens a new connection
  whose TLS name matches its own `Host`. That costs little at >= 1 s per host. Keeping a
  separate pool per hostname is the alternative if reuse is ever needed.

## R4 — Redirect handling

- **Decision**: Manual redirect loop in the client: on 301/302/303/307/308 read `Location`,
  resolve it relative to the current URL (`httpx.URL.join`), then run scheme check → SSRF
  guard → robots check → rate limiter for the new URL before sending. Stop with
  `FetchError("too many redirects")` after `max_redirects` (default 5). Missing `Location` →
  `FetchError`. Method stays `GET` for all codes (only GET is in scope).
- **Rationale**: httpx's built-in redirect following cannot run an async check per hop.
- **Alternatives**: httpx event hooks (`request` hook runs per hop but cannot pin the IP or
  rate-limit cleanly).

## R5 — Response size limit (FR-011/FR-012)

- **Decision**: Use `client.send(request, stream=True)`. If `Content-Length` > limit → close and
  raise `TooLargeError` before reading. Otherwise iterate `response.aiter_bytes()` (decoded
  content), sum lengths, raise `TooLargeError` as soon as the sum exceeds the limit. Send
  `Accept-Encoding: gzip, deflate` only.
- **Rationale**: Covers announced, unannounced and lying sizes and applies the limit to decoded
  content. Restricting encodings avoids `br`/`zstd` decoders that are not always installed and
  keeps the decompression path to zlib.
- **Accepted risk**: httpx decodes per network chunk, so one decoded chunk of a highly
  compressed body can exceed the remaining budget before the check fires (bounded by
  zlib's ratio on one raw chunk). SC-002's "limit plus one chunk" is interpreted as one decoded
  chunk. A streaming `zlib.decompressobj(max_length=…)` decoder is a possible later hardening.
- **Default**: `10 * 1024 * 1024` bytes (10 MiB).

## R6 — robots.txt

- **Decision**: `urllib.robotparser.RobotFileParser`, fed via `.parse(lines)` with content
  fetched by the safe client itself (internal path that skips the robots check, FR-018).
  Cache key = origin `(scheme, host, port)`. Status mapping (RFC 9309 + clarifications):
  2xx → parse (first 500 KB, decoded as UTF-8 with `errors="replace"`); 4xx → allow all;
  5xx, network error, timeout, blocked or too many redirects → disallow all for the run.
  A body over 500 KB is not an error: reading stops at 500 KB and that prefix is parsed.
  Redirects of robots.txt are followed (max 5) under the same guard. User-agent token for
  matching: `invio`. `crawl_delay("invio")` feeds R7. Concurrent first requests to one host
  share one robots fetch (per-origin `asyncio.Lock`).
- **Rationale**: Stdlib, no new dependency; `.parse()` lets us keep fetching inside the guarded
  client instead of `RobotFileParser.read()`, which uses unguarded `urllib`.
- **Note**: `RobotFileParser` does not support RFC 9309 longest-match precedence perfectly
  (it uses first-match within a group). Acceptable for this iteration; tests use rule sets
  where both semantics agree. Recorded in plan risks.
- **Alternatives**: `protego` (better RFC compliance; extra dependency — revisit if real-world
  mismatches appear).

## R7 — Per-host rate limiter

- **Decision**: `HostRateLimiter` keyed by origin. Each origin has an `asyncio.Lock` (FIFO-fair,
  so waiting fetches queue in order) and the monotonic start time of its last request.
  `async with limiter.slot(origin, interval):` acquires the lock, sleeps until
  `last_start + interval`, records the new start, and releases after the response body is
  read — giving "one in flight" and "spacing between starts" (FR-019). Effective interval =
  `max(configured, min(crawl_delay or 0, 30.0))` (FR-020a): `Crawl-delay` can only raise the
  interval, by at most 30 s, and a configured interval is never capped.
  Cancellation while waiting releases nothing it does not hold (lock semantics).
- **Rationale**: Simple, no background tasks; robots fetches and every redirect hop go through
  the same slot (FR-021). Different origins never share a lock (FR-020).
- **Key = origin, not bare hostname**: tests run several local servers on `127.0.0.1` with
  different ports and must be treated as distinct hosts; in production, origin and hostname
  are almost always equivalent.
- **Alternatives**: token bucket (allows bursts, not wanted); `asyncio.Semaphore(1)` +
  timestamp (equivalent; Lock is clearer for a single slot).

## R8 — Conditional GET cache

- **Decision**: In-memory `dict[str, Validators]` per client instance keyed by the requested URL
  (before redirects). Stored only after a 2xx response that has `ETag` and/or `Last-Modified`.
  Sent as `If-None-Match` / `If-Modified-Since`. A 304 returns `NotModified(url, etag,
  last_modified)`. Caller-supplied conditional headers win over cached ones.
- **Rationale**: Matches FR-022–FR-025 and the issue's "in-memory per run" scope.

## R9 — Timeouts

- **Decision**: `httpx.Timeout(connect=10, read=30, write=10, pool=10)` plus a **total per-request (per-hop)
  deadline** (`asyncio.timeout`, default 60 s) around sending and reading (excluding the
  rate-limiter wait). Timeout → `FetchError(reason="timeout")`.
- **Rationale**: Read timeout is per socket read, so a server dripping one byte every 29 s would
  never trip it; the total deadline makes SC-006 hold.
- **Scope**: a whole `get()` is therefore bounded by `(max_redirects + 2) × total_timeout`
  plus rate-limit waits (initial hop, up to `max_redirects` redirects, and one robots.txt
  fetch). This is deliberate: a single budget across hops would need wait-time accounting
  for little gain.

## R10 — Configuration

- **Decision**: New `Settings` fields (all `INVIO_HTTP_*`, validated `gt=0` where numeric):
  `http_contact` (default `"admin@example.invalid"`), `http_max_response_bytes`
  (10 MiB), `http_max_redirects` (5, `ge=0`), `http_connect_timeout_seconds` (10),
  `http_read_timeout_seconds` (30), `http_total_timeout_seconds` (60),
  `http_host_interval_seconds` (1.0), `http_respect_robots` (True). A frozen
  `HttpClientConfig` (Pydantic, `extra="forbid"`) is built from them via
  `HttpClientConfig.from_settings()`; tests construct it directly.
  User-Agent: `invio/<version> (+https://github.com/kaihempel/invio; contact: <http_contact>)`.
- **Test-only allowance (FR-010)**: constructor argument `allow_networks: Iterable[IPv4Network |
  IPv6Network]` on the client — not a setting, not reachable from job files. Tests pass
  `127.0.0.0/8` and `::1/128` only, so private-range redirects stay blocked even in tests.

## R11 — Errors, logging, URL redaction

- **Decision**: `FetchError(Exception)` with `url`, `status: int | None`, `reason: str`;
  `BlockedError(FetchError)` with `reason: BlockReason` (`StrEnum`: `unsupported_scheme`,
  `non_public_address`, `blocked_by_robots`); `TooLargeError(FetchError)` with `limit`.
  Messages and log fields use a redacted URL (userinfo removed). The existing `redact()` in
  `invio.cli.source_check` moves to `invio.sources.urls` (cli re-imports it) because
  `sources` must not import `cli` (dependency direction).
  Logger `invio.sources.http`, events `http_blocked` (WARNING) and `http_failed` (WARNING),
  `extra={"host", "reason", "url", "status"}`.

## R12 — Test strategy

- **Decision**: Two layers, no internet:
  1. **Loopback server tests** — a threaded `http.server` fixture (pattern from
     `tests/test_source_check.py`, extended with configurable routes and a request log with
     monotonic timestamps) in `tests/http_helpers.py`; two server instances give two origins.
     Covers size limits, redirects, robots, rate limiting, conditional GET, timeouts,
     User-Agent. Client is created with `allow_networks=[127.0.0.0/8]`.
  2. **Guard tests with fakes** — an injectable `Resolver` (fake mapping host → IPs) and
     `httpx.MockTransport` recording requests prove: blocked targets never reach the transport,
     multi-address hosts are rejected, connections are pinned to the checked IP with correct
     `Host` header and `sni_hostname` extension (rebinding scenario: resolver returns a public
     IP first, a private one afterwards).
  Rate-limit tests use small intervals (0.2 s) and assert `>=` on server-side timestamps;
  Hypothesis property test for the address classifier over generated IPv4/IPv6 addresses.
