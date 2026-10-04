# Implementation Plan: Safe Shared HTTP Client for Source Fetchers

**Branch**: `gh-issue-10` | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/006-gh-issue-10/spec.md`

## Summary

Add `invio.sources.http.SafeHttpClient`, an async wrapper around `httpx.AsyncClient` that every
source fetcher will use. Each `get()` runs through one pipeline per redirect hop:

1. **Scheme + SSRF guard** — only `http`/`https`; resolve the host; reject if any address is not
   globally routable (or is multicast); pin the TCP connection to the validated IP via URL
   rewrite + `Host` header + `sni_hostname` extension (no DNS rebinding window).
2. **robots.txt** — fetched once per origin through the same guarded client, parsed with
   `urllib.robotparser`; 4xx → allow all, 5xx/network failure → disallow all for the run;
   applies to every fetch including feeds.
3. **Per-origin rate limiter** — FIFO lock, one request in flight, starts spaced by
   `max(configured interval, Crawl-delay)` capped at 30 s.
4. **Send + stream** — manual redirects (max 5), connect/read timeouts plus a total deadline,
   size limit enforced on `Content-Length` and on decoded bytes while streaming.
5. **Result** — 2xx → `FetchResult`, 304 → `NotModified` (validators cached in memory per
   client), other 4xx/5xx → `FetchError`; typed `BlockedError` / `TooLargeError` subclasses.

Limits come from new `INVIO_HTTP_*` settings. Tests use loopback `http.server` instances and
`httpx.MockTransport` with a fake resolver — no internet. Details: [research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12+ (uv-managed)

**Primary Dependencies**: **httpx `>=0.28`** (new runtime dependency, justified in R1); stdlib
`ipaddress`, `asyncio`, `urllib.robotparser`; Pydantic v2 / pydantic-settings (existing).

**Storage**: N/A — all caches in memory per client instance; no schema change.

**Testing**: pytest + pytest-asyncio (`asyncio_mode = "auto"`, existing), Hypothesis
(existing) for the address classifier; threaded `http.server` on `127.0.0.1` and
`httpx.MockTransport`; no new test dependency.

**Target Platform**: Linux server (cron/systemd) and macOS for development.

**Project Type**: Single Python package (CLI application); this feature is an internal library
layer in the `sources` adapter package.

**Performance Goals**: Not throughput-bound; politeness dominates (≥ 1 s between requests per
host). Overhead per request: one `getaddrinfo` + robots lookup (cached).

**Constraints**: ≤ 10 MiB decoded body in memory per fetch (bounded decoder); total
deadline 60 s per get(); no internet in tests; mypy strict; coverage ≥ 95 %.

**Scale/Scope**: Tens of URLs per job run, a handful of hosts; ~4 small modules (~400–500 LOC)
plus tests.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Assessment | Status |
|-----------|------------|--------|
| I. Strict contracts at boundaries | URLs are parsed with `httpx.URL` and checked before use; `HttpClientConfig` is a frozen Pydantic model with `extra="forbid"`; new settings validated (`gt=0`, finite) and fail fast with field names. Errors/results are typed and defined once in `invio.sources.http`. | ✅ |
| II. CLI-first | No user-facing command is added; this is an adapter used by future fetchers that are reached via `invio` jobs. Settings are documented. | ✅ (N/A for commands) |
| III. Test-covered behaviour | Every acceptance criterion and FR maps to tests (quickstart table); unit tests use loopback servers / MockTransport only; rate-limit assertions are lower bounds on small intervals to stay deterministic. | ✅ |
| IV. Quality gates mirror CI | ruff, ruff format, mypy strict over `src/`, pytest; `uv.lock` updated with httpx, CI installs `--locked`. No `Any`/`type: ignore` planned except a justified, narrow one if `RobotFileParser` typing requires it. | ✅ |
| V. Secrets & observability | URLs are redacted (userinfo stripped) in errors and logs; request headers never logged; blocked/failed fetches logged as structured events with `host`, `reason`, `url`; `trust_env=False` keeps proxy credentials out of play. | ✅ |
| Dependency direction | `sources` imports only `config`, `log` and stdlib/httpx. `redact()` moves from `cli.source_check` to `sources.urls` so `sources` never imports `cli`. | ✅ |
| New runtime dependency justified | httpx — see R1; to be repeated in the PR description. | ✅ |
| Simplicity first | No retries, no persistent cache, no proxy support, no HEAD/POST; four focused modules instead of one 500-line file. | ✅ |
| Docs updated with user-facing config | README "Settings" section and `.env.example` get the `INVIO_HTTP_*` keys. | ✅ |

**Post-design re-check (after Phase 1)**: unchanged — all ✅. The test-only constructor
arguments (`allow_networks`, `resolver`, `transport`) are not reachable from settings or job
files, satisfying FR-010 without weakening Principle I.

## Project Structure

### Documentation (this feature)

```text
specs/006-gh-issue-10/
├── plan.md              # This file
├── research.md          # Phase 0: decisions R1–R12
├── data-model.md        # Phase 1: config, results, errors, per-origin state
├── quickstart.md        # Phase 1: validation guide
├── contracts/
│   └── python-api.md    # Phase 1: SafeHttpClient public API and outcome table
├── checklists/
│   └── requirements.md  # spec quality checklist
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
src/invio/
├── config/
│   └── settings.py          # + INVIO_HTTP_* fields (validated)
├── cli/
│   └── source_check.py      # redact() now imported from invio.sources.urls
└── sources/
    ├── __init__.py
    ├── urls.py              # NEW: redact(), redact_url() helpers (no network)
    ├── errors.py            # NEW: BlockReason, FetchError, BlockedError, TooLargeError
    ├── netguard.py          # NEW: Origin, origin_of(), Resolver, SystemResolver,
    │                        #      is_public_address(), check_scheme(), guard_url()
    ├── robots.py            # NEW: RobotsPolicy, RobotsCache (fetch via injected callable)
    ├── ratelimit.py         # NEW: HostRateLimiter (per-origin lock + spacing)
    └── http.py              # NEW: HttpClientConfig, SafeHttpClient, FetchResult, NotModified
                             #      (re-exports the error types)

tests/
├── http_helpers.py          # NEW: loopback server fixture (routes, request log w/ timestamps),
│                            #      FakeResolver, recording MockTransport
├── test_http_netguard.py    # NEW: scheme/address classification (+ Hypothesis), pinning
├── test_http_client.py      # NEW: result mapping, errors, redirects, config, lifecycle
├── test_http_ssrf.py        # NEW: blocked targets, pinning, rebinding, userinfo, logging
├── test_http_limits.py      # NEW: size/decompression limits, redirect cap, timeouts, UA
├── test_http_conditional.py # NEW: ETag / Last-Modified, NotModified
├── test_http_robots.py      # NEW: allow/deny, 404/503 semantics, single fetch, feeds, Crawl-delay
├── test_http_ratelimit.py   # NEW: spacing, one-in-flight, cross-origin independence
├── test_settings.py         # + INVIO_HTTP_* validation cases
└── test_source_check.py     # unchanged behaviour; redact import path still works
```

**Structure Decision**: Single-project layout as established. The issue's
`scout/sources/http.py` maps to `src/invio/sources/http.py` ("scout" is the legacy name, as
decided for #9). Helpers are split by concern so each can be tested in isolation;
`invio.sources.http` is the only public entry point for fetchers.

## Implementation Notes (for /speckit-tasks)

- **Order**: settings + `HttpClientConfig` → `urls`/`netguard` (pure, unit-tested first) →
  `ratelimit` → `http.py` core (send/stream/size/timeouts/errors) → redirects with per-hop
  guard → `robots` integration → conditional GET → logging → docs.
- **Pipeline per hop** (inside `SafeHttpClient`): `guard_url` → (robots unless internal robots
  fetch) → `limiter.slot(origin, interval)` → build pinned request → `send(stream=True)` →
  status handling → read body with limit → release slot.
- **robots fetch** reuses the same per-hop pipeline with `check_robots=False` and a 500 KB cap
  that truncates instead of raising.
- **Concurrency**: robots fetch per origin guarded by its own `asyncio.Lock` so concurrent first
  requests trigger exactly one robots.txt request.
- **Timeouts**: `asyncio.timeout(total_timeout)` wraps send+read for one hop, not the limiter
  wait; `httpx.TimeoutException` and `TimeoutError` both map to `FetchError("timeout")`.

## Risks

| Risk | Mitigation |
|------|------------|
| `RobotFileParser` uses first-match rather than RFC 9309 longest-match | Accept for now; tests use unambiguous rules; swap to `protego` later if needed (R6). |
| Decompression chunk can exceed the remaining budget | Only gzip/deflate accepted; documented as accepted risk (R5). |
| Timing-based rate-limit tests flaky on slow CI | Assert lower bounds only, intervals ≥ 0.2 s, no upper-bound assertions except a generous one for cross-origin independence. |
| `sni_hostname` relies on an httpcore extension | Verified in httpcore 1.0.9; covered by a MockTransport test asserting the extension is set; declare `httpx>=0.28` and rely on `uv.lock` for the exact version. |

## Complexity Tracking

No constitution violations; section intentionally empty.
