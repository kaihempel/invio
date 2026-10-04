---

description: "Task list for the safe shared HTTP client (gh-issue-10)"
---

# Tasks: Safe Shared HTTP Client for Source Fetchers

**Input**: Design documents from `/specs/006-gh-issue-10/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/python-api.md, quickstart.md

**Tests**: Required — the constitution (Principle III) demands a test per acceptance criterion,
and FR-030 / the issue require tests against a local test server with no internet access.
Within each story, write the tests first and confirm they fail before implementing.

**Organization**: Tasks are grouped by user story so each story can be implemented and tested
on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story from spec.md (US1–US5)
- Paths are relative to the repository root (single project: `src/invio/`, `tests/`)

## Conventions for every task

- Python 3.12, `mypy --strict` clean, ruff clean, line length 100, double quotes.
- Module docstrings and comment density like `src/invio/cli/source_check.py`.
- Async tests rely on `asyncio_mode = "auto"` (no `@pytest.mark.asyncio` needed).
- No test may touch the internet. Loopback servers bind `127.0.0.1` only; clients in loopback
  tests are created with `allow_networks=[ip_network("127.0.0.0/8")]`.
- Public names of `invio.sources.http` and their behaviour are fixed by
  `specs/006-gh-issue-10/contracts/python-api.md`; field names, defaults and validation rules by
  `specs/006-gh-issue-10/data-model.md`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Add the dependency and move the shared URL helper.

- [X] T001 Add `"httpx>=0.28"` to `[project.dependencies]` in `pyproject.toml` (keep the list alphabetical) and run `uv lock` to update `uv.lock`; verify `uv sync --locked` succeeds.
- [X] T002 [P] Create `src/invio/sources/urls.py` with `redact(text: str) -> str` moved verbatim from `src/invio/cli/source_check.py` (including the `_USERINFO` regex and its comment), plus `__all__ = ["redact"]`; in `src/invio/cli/source_check.py` delete the local definition and `from invio.sources.urls import redact` (keep `redact` in that module's `__all__` so existing imports keep working). Run `uv run pytest tests/test_source_check.py` to confirm no behaviour change.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Settings, config model, result/error types, test helpers and the client skeleton
with a per-hop pipeline that the stories plug into.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T003 [P] Add to `Settings` in `src/invio/config/settings.py`, under a new `# HTTP client for sources` comment block: `http_contact: str = "admin@example.invalid"` (`min_length=1`, validator rejecting `\r`/`\n`); `http_max_response_bytes: int = Field(default=10_485_760, gt=0)`; `http_max_redirects: int = Field(default=5, ge=0)`; `http_connect_timeout_seconds: float = Field(default=10.0, gt=0, allow_inf_nan=False)`; `http_read_timeout_seconds: float = Field(default=30.0, gt=0, allow_inf_nan=False)`; `http_total_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)`; `http_host_interval_seconds: float = Field(default=1.0, gt=0, allow_inf_nan=False)`; `http_respect_robots: bool = True`.
- [X] T004 [P] Add tests in `tests/test_settings.py`: defaults of all eight `http_*` fields; `INVIO_HTTP_HOST_INTERVAL_SECONDS=0`, `INVIO_HTTP_MAX_RESPONSE_BYTES=0`, `INVIO_HTTP_MAX_REDIRECTS=-1`, `INVIO_HTTP_READ_TIMEOUT_SECONDS=nan` and `INVIO_HTTP_CONTACT` containing a newline each raise a `ValidationError` whose message names the field.
- [X] T005 Create `src/invio/sources/http.py` (module docstring summarising the pipeline from plan.md) with the data types from data-model.md: `HttpClientConfig` (frozen Pydantic model, `extra="forbid"`, fields `contact`, `max_response_bytes`, `max_redirects`, `connect_timeout`, `read_timeout`, `total_timeout`, `host_interval`, `respect_robots` with the defaults and rules from data-model.md, a `model_validator` enforcing "`total_timeout` `>= read_timeout`", classmethod `from_settings(settings: Settings | None = None)` mapping the `http_*` settings, property `user_agent` = `f"invio/{invio.__version__} (+https://github.com/kaihempel/invio; contact: {contact})"`); and, in a new `src/invio/sources/errors.py` (no imports from `http.py`, so `netguard`/`robots` can use it without a cycle; re-exported from `http.py`), `BlockReason(StrEnum)` with `UNSUPPORTED_SCHEME = "unsupported_scheme"`, `NON_PUBLIC_ADDRESS = "non_public_address"`, `BLOCKED_BY_ROBOTS = "blocked_by_robots"`; `FetchError(Exception)` (`url: str` always passed through `redact`, `status: int | None`, `reason: str`, message `f"{reason}: {url}"` plus ` (HTTP {status})` when set); `BlockedError(FetchError)` (`reason: BlockReason`); `TooLargeError(FetchError)` (`limit: int`, reason `"too_large"`); back in `http.py`, frozen slotted dataclasses `FetchResult` (`url`, `requested_url`, `status`, `headers: Mapping[str, str]`, `content: bytes`, `etag: str | None`, `last_modified: str | None`, method `text(encoding: str | None = None) -> str` using the header charset, fallback UTF-8, `errors="replace"`) and `NotModified` (`url`, `etag`, `last_modified`); `__all__` exactly as listed in contracts/python-api.md.
- [X] T006 [P] Create `tests/http_helpers.py`: (a) a `LoopbackServer` fixture factory `loopback_server()` based on the `server` fixture in `tests/test_source_check.py` (same `server_bind` override, daemon threads, clean shutdown) but with a mutable `routes: dict[str, Route]` where `Route` is a dataclass (`status=200`, `headers: dict[str, str]`, `body: bytes = b""`, `chunked: bool = False`, `delay: float = 0.0`, `stall: bool = False`) and a thread-safe request log of `RecordedRequest(path, headers, started: float  # time.monotonic())`; unknown paths answer 404; `/robots.txt` defaults to 404 unless a route is set; expose `base_url`, `origin_port`, `requests`, `paths()`; (b) pytest fixtures `server` and `second_server` (two independent instances = two origins); (c) `FakeResolver` mapping host → list of IP strings (supports a list of answers per host consumed in order, to simulate DNS rebinding) and recording calls; (d) `RecordingTransport` wrapping `httpx.MockTransport` with a handler callable and a `requests: list[httpx.Request]` log; (e) `LOOPBACK = [ip_network("127.0.0.0/8")]` constant.
- [X] T007 Write `tests/test_http_client.py` (foundation behaviour, against `server` with `allow_networks=LOOPBACK`, `respect_robots=False`, `host_interval=0.01`): 200 returns `FetchResult` with body, headers, `url` and `requested_url`; 404 and 503 raise `FetchError` with `reason == "http_status"` and the status; a single 302 to a relative `Location` is followed and `FetchResult.url` is the final URL; redirect without `Location` → `FetchError(reason="missing_location")`; `BlockedError`/`TooLargeError` are subclasses of `FetchError`; `str(FetchError("x", url="http://u:p@h/"))` contains no `u:p`; `HttpClientConfig(foo=1)` is rejected; `HttpClientConfig.from_settings()` maps all fields; the client works as an async context manager and `aclose()` is idempotent. Tests must fail before T008.
- [X] T008 Implement `SafeHttpClient` in `src/invio/sources/http.py` per contracts/python-api.md: constructor (`config`, keyword-only `allow_networks`, `resolver`, `transport`), `__aenter__`/`__aexit__`/`aclose`; an inner `httpx.AsyncClient(follow_redirects=False, trust_env=False, transport=transport, timeout=httpx.Timeout(connect=config.connect_timeout, read=config.read_timeout, write=config.connect_timeout, pool=config.connect_timeout), limits=httpx.Limits(max_keepalive_connections=0))` (pinned IPs: never reuse a connection across hostnames, research R3); `get(url, *, headers=None)` that parses the URL with `httpx.URL` (`httpx.InvalidURL`/`ValueError` → `FetchError(reason="invalid_url")`) and runs a manual redirect loop (301/302/303/307/308; `Location` joined with `current.join(location)`; missing → `missing_location`; more than `config.max_redirects` hops → `FetchError(reason="too_many_redirects")`). Each hop calls a private `async def _hop(self, url: httpx.URL, extra_headers, *, check_robots: bool) -> httpx.Response`-style method that for now only builds and sends the request with `stream=True` and reads the body; structure it as the ordered steps from plan.md "Pipeline per hop" with clearly separated private methods so US1–US5 add steps without restructuring. Map `httpx.ConnectError` → `connection_failed`, any other `httpx.TransportError` → `connection_failed`; 2xx → `FetchResult`; non-2xx/non-3xx → `FetchError(reason="http_status", status=…)`. Ignore caller-supplied `User-Agent` and `Host` headers. Make T007 pass.

**Checkpoint**: `uv run pytest tests/test_http_client.py tests/test_settings.py` green; user story work can start.

---

## Phase 3: User Story 1 — Block requests to internal network resources (Priority: P1) 🎯 MVP

**Goal**: Only http/https; every hop's host is resolved and rejected if any address is
non-public; the connection is pinned to the validated address (FR-005–FR-010).

**Independent Test**: `http://127.0.0.1/`, `http://10.0.0.5/`, `file:///etc/passwd` and a loopback
redirect to `http://10.0.0.5/` each raise `BlockedError`, and no request reaches the blocked
target (RecordingTransport / server log).

### Tests for User Story 1 ⚠️ (write first, must fail)

- [X] T009 [P] [US1] Write `tests/test_http_netguard.py` unit tests for `invio.sources.netguard`: `is_public_address` is False for `127.0.0.1`, `10.0.0.5`, `172.16.0.1`, `192.168.1.1`, `169.254.169.254`, `0.0.0.0`, `100.64.0.1`, `224.0.0.1`, `255.255.255.255`, `::1`, `::`, `fe80::1`, `fc00::1`, `ff02::1`, `::ffff:127.0.0.1`, `::ffff:10.0.0.1`, `2002:7f00:0001::1` (6to4 of 127.0.0.1) and True for `93.184.216.34`, `2606:4700::1111`; a Hypothesis property test: for any `IPv4Address` `a`, `is_public_address(a) == is_public_address(IPv6Address(f"::ffff:{a}"))`; `check_scheme` rejects `file`, `ftp`, `gopher`, `data`, `javascript` and accepts `http`/`https` case-insensitively; `origin_of(httpx.URL(...))` lower-cases the host and fills default ports 80/443; `guard_url` with `FakeResolver` rejects when any of several addresses is private, raises `FetchError(reason="dns_failed")` when the resolver raises `socket.gaierror`, honours `allow_networks` only for listed networks (a `127.0.0.0/8` allowance does not allow `10.0.0.5`), parses standard IP literals (`http://127.0.0.1/`, `http://[::1]/`, `http://[::ffff:10.0.0.1]/`) directly without calling the resolver; and, using the real `SystemResolver` (numeric hosts are converted locally by `getaddrinfo` without any network lookup), raises `BlockedError(NON_PUBLIC_ADDRESS)` for `http://2130706433/` and `http://0x7f.1/`.
- [X] T010 [P] [US1] Write `tests/test_http_ssrf.py` client-level tests: with default `allow_networks` and a `RecordingTransport`, `get("http://127.0.0.1/")` and `get("http://10.0.0.5/")` raise `BlockedError(reason=NON_PUBLIC_ADDRESS)` and the transport recorded zero requests; `get("file:///etc/passwd")` raises `BlockedError(reason=UNSUPPORTED_SCHEME)`; with `server` + `LOOPBACK` allowance a route `/r` answering `302 Location: http://10.0.0.5/admin` raises `BlockedError(NON_PUBLIC_ADDRESS)` (server saw `/r` only) and a route redirecting to `ftp://example.org/` raises `UNSUPPORTED_SCHEME`; pinning: with `FakeResolver({"example.org": ["93.184.216.34"]})` and a `RecordingTransport`, the sent request URL host is `93.184.216.34`, the `Host` header is `example.org`, and `request.extensions["sni_hostname"] == "example.org"` for an https URL; rebinding: resolver answers public then `127.0.0.1` — the request is sent to the public IP and the resolver is consulted once per hop; connection isolation: with `FakeResolver({"a.example": ["93.184.216.34"], "b.example": ["93.184.216.34"]})` and a `RecordingTransport`, fetching `https://a.example/` then `https://b.example/` sends two requests whose `sni_hostname` extensions are `a.example` and `b.example` respectively, and the inner `httpx.AsyncClient` is created with `max_keepalive_connections == 0` (assert via a constructor spy, e.g. `monkeypatch` wrapping `httpx.AsyncClient`); a caller-supplied `Host` header is ignored; `BlockedError` messages and `url` never contain URL userinfo.

### Implementation for User Story 1

- [X] T011 [US1] Create `src/invio/sources/netguard.py`: `Origin(NamedTuple)` (`scheme: Literal["http", "https"]`, `host: str`, `port: int`); `origin_of(url: httpx.URL) -> Origin`; `Resolver` protocol `async def resolve(self, host: str, port: int) -> list[str]`; default `SystemResolver` using `asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)`; `is_public_address(addr: IPv4Address | IPv6Address) -> bool` = `addr.is_global and not addr.is_multicast`, additionally unwrapping `ipv4_mapped` and `sixtofour` and requiring the embedded IPv4 to be public; `check_scheme(url)` raising `BlockedError(UNSUPPORTED_SCHEME)`; `GuardedTarget` dataclass (`url`, `origin`, `address`) and `async def guard_url(url, resolver, allow_networks) -> GuardedTarget` that parses IP literals directly (`ipaddress.ip_address` on the bracket-stripped host); hosts that are not valid standard IP literals, including legacy numeric forms like `2130706433` or `0x7f.1`, go to the resolver, whose returned addresses are then checked like any other; it raises `FetchError(reason="dns_failed")` on `socket.gaierror`/empty result, rejects with `BlockedError(NON_PUBLIC_ADDRESS)` if **any** address is neither public nor inside `allow_networks`, and returns the first address. Import the error types from `invio.sources.errors`. Make T009 pass.
- [X] T012 [US1] Wire the guard into the per-hop pipeline in `src/invio/sources/http.py`: before sending each hop (initial URL and every redirect target) call `check_scheme` and `guard_url`; build the outgoing request with the URL host replaced by the validated IP (`url.copy_with(host=str(address))`, IPv6 handled by httpx), explicit `Host` header from the logical URL (`host` or `host:port` for non-default ports), and `extensions={"sni_hostname": logical_host}` for https; keep the logical URL for redirect joining, `FetchResult.url` and errors. Make T010 pass.
- [X] T013 [US1] Add structured logging in `src/invio/sources/http.py`: logger `logging.getLogger("invio.sources.http")`; on every `BlockedError` log `"http_blocked"` at WARNING with `extra={"url": <redacted>, "host": origin.host, "reason": str(reason)}`; on every other `FetchError` log `"http_failed"` with `extra={"url", "host", "reason", "status"}`. Add tests to `tests/test_http_ssrf.py` using `caplog` asserting the record message, `reason` attribute and that no record contains userinfo (FR-028, FR-029).

**Checkpoint**: Issue acceptance criterion 1 is met; SSRF protection works on its own.

---

## Phase 4: User Story 2 — Enforce resource limits on every fetch (Priority: P1)

**Goal**: Size limit on announced and streamed (decoded) bodies, max 5 redirects, connect/read
timeouts plus a total deadline, honest User-Agent (FR-002–FR-004, FR-008, FR-011, FR-012).

**Independent Test**: Against `server`: oversized `Content-Length`, oversized chunked body,
gzip bomb → `TooLargeError`; 6-hop chain → `too_many_redirects`; stalled route → `timeout`
within the total deadline; recorded `User-Agent` matches the format.

### Tests for User Story 2 ⚠️

- [X] T014 [P] [US2] Write `tests/test_http_limits.py` (client with `LOOPBACK`, `respect_robots=False`, `host_interval=0.01`, `max_response_bytes=1024`): route announcing `Content-Length: 10485760` and streaming that body → `TooLargeError` with `limit == 1024` raised in < 2 s (client-side assertion only); chunked route sending 4096 bytes without `Content-Length` → `TooLargeError`; route announcing `Content-Length: 10` but writing 4096 bytes → `FetchResult` whose `content` is exactly the first 10 bytes (HTTP/1.1 framing; the extra bytes are never read); route with `Content-Encoding: gzip` whose compressed size < 1024 but inflated size 100 KB → `TooLargeError`; a 1024-byte body exactly at the limit succeeds; 6-hop redirect chain with `max_redirects=5` → `FetchError(reason="too_many_redirects")` and a 5-hop chain succeeds; `stall=True` route with `read_timeout=0.2`, `total_timeout=0.5` → `FetchError(reason="timeout")` within 1.5 s; a route dripping 1 byte every 0.1 s with `read_timeout=0.3`, `total_timeout=0.5` → `timeout` (total deadline, R9); the recorded `User-Agent` equals `HttpClientConfig(contact="ops@example.org").user_agent` and matches `r"^invio/\S+ \(\+https://github\.com/kaihempel/invio; contact: ops@example\.org\)$"`; outgoing `Accept-Encoding` is `gzip, deflate`; a caller-supplied `User-Agent` is overridden.

### Implementation for User Story 2

- [X] T015 [US2] In `src/invio/sources/http.py` implement body limits in the hop's read step: if `Content-Length` parses to an int greater than `config.max_response_bytes`, close the response and raise `TooLargeError`; else iterate `response.aiter_bytes()`, accumulate into a `bytearray`, raise `TooLargeError` as soon as the length exceeds the limit (close the response in `finally`). Send `User-Agent: config.user_agent` and `Accept-Encoding: gzip, deflate` on every request, overriding caller values.
- [X] T016 [US2] In `src/invio/sources/http.py` wrap the send+read part of each hop (not the later rate-limiter wait) in `asyncio.timeout(config.total_timeout)`; map `TimeoutError` and `httpx.TimeoutException` to `FetchError(reason="timeout")`; confirm the redirect cap from T008 produces `too_many_redirects` for `max_redirects + 1` hops. Make T014 pass.

**Checkpoint**: US1 + US2 = the full safety contract (MVP).

---

## Phase 5: User Story 3 — Respect robots.txt (Priority: P2)

**Goal**: robots.txt fetched once per origin via the guarded pipeline; disallowed paths (pages
and feeds) are never requested; 4xx → allow all, 5xx/network failure → disallow all for the run
(FR-013–FR-018).

**Independent Test**: `server` with `robots.txt` disallowing `/private/` and `/feed.xml`:
`/private/page` and `/feed.xml` raise `BlockedError(BLOCKED_BY_ROBOTS)` and never appear in the
server log; `/public/page` succeeds; `/robots.txt` was requested exactly once.

### Tests for User Story 3 ⚠️

- [ ] T017 [P] [US3] Write `tests/test_http_robots.py` (client with `LOOPBACK`, `host_interval=0.01`, `respect_robots=True`): disallowed page and disallowed feed path raise `BlockedError(reason=BLOCKED_BY_ROBOTS)` and are absent from `server.paths()`; allowed path succeeds; three sequential and five concurrent (`asyncio.gather`) fetches on one origin cause exactly one `/robots.txt` request; robots 404 and 403 → everything allowed; robots 503 → every page blocked for the rest of the run and robots.txt not retried on later calls; robots route stalled past `total_timeout` → blocked; robots.txt redirecting to `http://10.0.0.5/robots.txt` → host disallowed (guard applies to robots fetch); robots body of 600 KB whose first 500 KB contain `Disallow: /private/` is parsed (not an error); a group `User-agent: invio` with `Disallow: /x/` overrides `User-agent: *` with `Disallow: /`; the robots.txt request itself is not preceded by a robots check (no recursion) and carries the invio `User-Agent`; with `respect_robots=False` no `/robots.txt` request is made; a redirect from origin A to a disallowed path on origin B (`second_server`) is blocked by B's robots.txt.

### Implementation for User Story 3

- [ ] T018 [US3] Create `src/invio/sources/robots.py`: `ROBOTS_AGENT_TOKEN = "invio"`, `ROBOTS_MAX_BYTES = 512_000`; `RobotsPolicy` frozen dataclass (`mode: Literal["parsed", "allow_all", "disallow_all"]`, `parser: RobotFileParser | None`, `crawl_delay: float | None`) with `allows(url: str) -> bool`; `RobotsCache` taking an injected `fetch: Callable[[httpx.URL], Awaitable[tuple[int, bytes]]]` (status, body truncated to 500 KB) with `async def policy(origin: Origin) -> RobotsPolicy` that holds a per-origin `asyncio.Lock` so concurrent callers share one fetch, maps 2xx → `parsed` (decode UTF-8 `errors="replace"`, `parser.parse(text.splitlines())`, `crawl_delay = parser.crawl_delay(ROBOTS_AGENT_TOKEN)` kept only if a finite number `>= 0`), 4xx → `allow_all`, 5xx or any `FetchError` (including `BlockedError`) → `disallow_all`, and caches the result for the cache's lifetime (state transitions in data-model.md).
- [ ] T019 [US3] Integrate in `src/invio/sources/http.py`: create a `RobotsCache` per client whose `fetch` runs the normal hop pipeline (guard, rate limiter once US4 exists, size cap of `ROBOTS_MAX_BYTES` that truncates instead of raising, redirects up to `max_redirects`) with `check_robots=False`; when `config.respect_robots` is true, every hop with `check_robots=True` (initial URL and each redirect target, feeds included) asks `policy(origin).allows(url)` before sending and raises `BlockedError(BLOCKED_BY_ROBOTS)` otherwise (logged as `http_blocked`). Make T017 pass.

**Checkpoint**: robots.txt is respected independently of rate limiting.

---

## Phase 6: User Story 4 — Rate-limit requests per host (Priority: P2)

**Goal**: Per origin at most one request in flight and starts spaced by the effective interval
`max(configured, Crawl-delay)` capped at 30 s; origins independent (FR-019–FR-021, FR-020a).

**Independent Test**: Two concurrent `get`s on `server` with `host_interval=0.2` are recorded
≥ 0.2 s apart; a concurrent `get` on `second_server` is not delayed; `Crawl-delay: 1` raises
spacing to ≥ 1 s.

### Tests for User Story 4 ⚠️

- [ ] T020 [P] [US4] Write `tests/test_http_ratelimit.py`: unit tests for `HostRateLimiter` with a fake clock/sleep (injectable `clock` and `sleep` callables): first slot starts immediately; second slot waits `interval - elapsed`; a slot is exclusive until released (one in flight); waiters are served FIFO; cancelling a waiting task does not block later waiters; `effective_interval(configured=0.2, crawl_delay=1.0) == 1.0`, `(1.0, None) == 1.0`, `(1.0, 0.5) == 1.0`, `(0.2, 120) == 30.0`, `(45.0, None) == 45.0`. Integration tests with real servers (`respect_robots=False` unless stated): two concurrent requests to `server` with `host_interval=0.2` → server `started` timestamps differ by `>= 0.2 - 0.01`; with `host_interval=1.0` default the gap is `>= 0.99`; a request to `second_server` issued alongside finishes before the second `server` request starts; robots.txt + first page on one origin are spaced (robots counts, FR-021); each redirect hop on the same origin is spaced; with `respect_robots=True` and `Crawl-delay: 1` for `invio`, two pages are `>= 0.99` s apart while `host_interval=0.2`.

### Implementation for User Story 4

- [ ] T021 [US4] Create `src/invio/sources/ratelimit.py`: `CRAWL_DELAY_CAP = 30.0`; `effective_interval(configured: float, crawl_delay: float | None) -> float` returning `max(configured, min(crawl_delay or 0.0, CRAWL_DELAY_CAP))`; `HostRateLimiter(clock=time.monotonic, sleep=asyncio.sleep)` with per-origin state (`asyncio.Lock`, `last_start: float | None`) and an async context manager `slot(origin: Origin, interval: float)` that acquires the origin's lock, sleeps until `last_start + interval`, records `last_start = clock()` and releases the lock on exit.
- [ ] T022 [US4] Integrate in `src/invio/sources/http.py`: one `HostRateLimiter` per client; every hop (including robots.txt fetches and each redirect hop) runs send+read inside `limiter.slot(origin, interval)`, where `interval = effective_interval(config.host_interval, policy.crawl_delay)` when a robots policy is known for that origin and `config.host_interval` otherwise (robots fetch itself uses the configured interval); the total-timeout from T016 starts after the slot is acquired. Make T020 pass.

**Checkpoint**: Politeness complete; US1–US4 all independently verified.

---

## Phase 7: User Story 5 — Skip unchanged content with conditional requests (Priority: P3)

**Goal**: Remember `ETag`/`Last-Modified` per URL for the client's lifetime, send them as
`If-None-Match`/`If-Modified-Since`, return `NotModified` on 304 (FR-022–FR-025).

**Independent Test**: First `get` of a route with `ETag: "v1"` returns `FetchResult`; second
`get` sends `If-None-Match: "v1"`, the server answers 304 and the client returns `NotModified`.

### Tests for User Story 5 ⚠️

- [ ] T023 [P] [US5] Write `tests/test_http_conditional.py` (route handler in `tests/http_helpers.py` gains an optional `conditional` mode: answer 304 when `If-None-Match` matches the route's `ETag` or `If-Modified-Since` equals its `Last-Modified`): first fetch returns `FetchResult` with `etag`/`last_modified` set and the request had no conditional headers; second fetch sends both headers and returns `NotModified(url, etag, last_modified)`; a route without validators → second request unconditional; a new `SafeHttpClient` instance sends no conditional headers; caller-supplied `If-None-Match` overrides the cached value; validators are stored under the requested URL even when the content came via a redirect; 304 is not raised as `FetchError`; a 304 without a cached entry still returns `NotModified` with `etag=None`.

### Implementation for User Story 5

- [ ] T024 [US5] In `src/invio/sources/http.py` add the private `Validators(etag: str | None, last_modified: str | None)` dataclass and a per-client `dict[str, Validators]`; in `get`, add `If-None-Match` / `If-Modified-Since` from the cache for the requested URL unless the caller passed them; after a 2xx with at least one validator, store them; on a 304 at the final hop return `NotModified(url=<requested url>, etag=…, last_modified=…)` with the validators that were sent. Make T023 pass.

**Checkpoint**: All five user stories functional.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [ ] T025 [P] Document the client in `README.md`: a "Fetching sources safely" subsection (what is blocked, robots.txt behaviour incl. `blocked_by_robots`, rate limiting incl. `Crawl-delay` cap, size/redirect/timeout limits) and add the eight `INVIO_HTTP_*` settings with defaults to the settings section; add the same keys with defaults and a comment to `.env.example` (ask operators to set `INVIO_HTTP_CONTACT`).
- [ ] T026 [P] Update `specs/006-gh-issue-10/plan.md` "Source Code" tree to list the final files (`errors.py` from T005; tests `test_http_ssrf.py`, `test_http_limits.py`, `test_http_conditional.py`) so plan and code match.
- [ ] T027 [P] Enforce FR-001 in `pyproject.toml`: under `[tool.ruff.lint.flake8-tidy-imports.banned-api]` add `"httpx".msg = "source fetchers must use invio.sources.http.SafeHttpClient (FR-001)"` and `"urllib.request".msg` with the same message; restructure `[tool.ruff.lint.per-file-ignores]` so TID251 is enforced for `src/invio/sources/**` except `src/invio/sources/http.py`, `src/invio/sources/netguard.py`, `src/invio/sources/robots.py` and `src/invio/sources/errors.py`, while the existing `invio.scheduling` bans keep working (the current ignore `"!src/invio/scheduling/**"` inverts scope, so the patterns need reworking). Verify with a throw-away module that importing `httpx` in `src/invio/sources/` fails `ruff check` and that `src/invio/cli/source_check.py` (urllib) still passes. If the ignore patterns become unmanageable, add an import-scanning test to `tests/test_cli_layering.py` instead.
- [ ] T028 Run the full gate set from quickstart.md §1 (`uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`) and fix findings; coverage must stay ≥ 95 % — add tests for any uncovered branches in `src/invio/sources/` (e.g. IPv6 `Host` header with port, `text()` charset fallback, `httpx.TransportError` mapping).
- [ ] T029 Verify the issue's acceptance criteria against the table in `specs/006-gh-issue-10/quickstart.md` §2 by running `uv run pytest tests/test_http_*.py -v` with networking to the internet unavailable (e.g. offline) and confirm all pass; record the httpx justification (research R1) for the PR description.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none. T001 and T002 are independent.
- **Foundational (Phase 2)**: needs T001 (httpx) and T002 (`redact`). T003/T004/T006 in parallel; T005 → T007 → T008.
- **US1 (Phase 3)**: needs Phase 2.
- **US2 (Phase 4)**: needs Phase 2; independent of US1 functionally (tests use `LOOPBACK`), but both edit `http.py`, so run sequentially after US1 in a single-developer flow.
- **US3 (Phase 5)**: needs Phase 2; uses `Origin`/`origin_of` from T011 (US1) → do after US1.
- **US4 (Phase 6)**: needs Phase 2 and `Origin` (T011); the Crawl-delay part needs `RobotsPolicy` (T018, US3).
- **US5 (Phase 7)**: needs Phase 2 only.
- **Polish (Phase 8)**: after all desired stories.

### User Story Dependencies (graph)

```text
Setup ─► Foundational ─► US1 ─┬─► US2
                              ├─► US3 ─► US4 (Crawl-delay)
                              └─► US5 (only needs Foundational)
                                         └──► Polish
```

### Within Each User Story

- Tests first and failing → helper module → integration into `http.py` → logging/edge cases.
- Story checkpoint must be green before moving on.

### Parallel Opportunities

- T001 ∥ T002; T003 ∥ T004 ∥ T006 (then T005 → T007 → T008).
- In each story the test task(s) marked [P] can be written while the helper module is built
  (different files): T009 ∥ T010; T014; T017 ∥ T018; T020 ∥ T021; T023.
- T025 ∥ T026 ∥ T027 in Polish.
- Tasks that edit `src/invio/sources/http.py` (T008, T012, T013, T015, T016, T019, T022, T024)
  are never parallel with each other.

---

## Parallel Example: User Story 1

```bash
Task: "Write tests/test_http_netguard.py unit tests for invio.sources.netguard (T009)"
Task: "Write tests/test_http_ssrf.py client-level SSRF tests (T010)"
# then
Task: "Create src/invio/sources/netguard.py (T011)"
```

## Parallel Example: User Story 3

```bash
Task: "Write tests/test_http_robots.py (T017)"
Task: "Create src/invio/sources/robots.py (T018)"
```

---

## Implementation Strategy

### MVP First (US1 + US2)

1. Phase 1 Setup → Phase 2 Foundational.
2. Phase 3 (US1): SSRF guard — **validate**: acceptance criterion 1 passes.
3. Phase 4 (US2): limits — **validate**: acceptance criterion 2 passes.
   US1 + US2 together are the safety contract and could ship first.

### Incremental Delivery

4. US3 robots.txt → acceptance criterion 3.
5. US4 rate limiting → acceptance criterion 4.
6. US5 conditional GET.
7. Polish: docs, gates, coverage, offline verification (acceptance criterion 5).

---

## Notes

- Commit after each task or checkpoint on branch `gh-issue-10`; reference issue #10.
- The test-only constructor arguments (`allow_networks`, `resolver`, `transport`) must never be
  read from `Settings` or job files (FR-010).
- Rate-limit assertions are lower bounds only (no flaky upper bounds except the cross-origin
  independence check, which uses a generous margin).
