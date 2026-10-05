# Feature Specification: Safe Shared HTTP Client for Source Fetchers

**Feature Branch**: `gh-issue-10`

**Created**: 2026-10-05

**Status**: Draft

**Input**: User description: "GitHub issue #10: Add safe HTTP client (SSRF guard, robots.txt, rate limiting, conditional GET). Context: All fetchers in scout/sources share one HTTP layer; it must be polite to target sites and must not allow access to internal network resources through user-supplied URLs. Depends on #2. Requirements: (1) Create scout/sources/http.py wrapping httpx.AsyncClient with sane timeouts, max response size (10 MB), max redirects (5) and an honest User-Agent including a contact placeholder. (2) SSRF guard: allow only http/https schemes; resolve the host and reject loopback, private, link-local and multicast addresses; re-check after every redirect. (3) robots.txt handling with per-host caching (urllib.robotparser); skipped URLs are reported as blocked_by_robots. (4) Per-host rate limiter (default 1 request/second) with async semaphore. (5) Conditional GET: store/send ETag and Last-Modified; return a NotModified result on 304 (in-memory per run cache; persistent cache can follow later). (6) Typed errors: FetchError, BlockedError, TooLargeError. Acceptance criteria: http://127.0.0.1, http://10.0.0.5, file:///etc/passwd and a redirect to a private IP are rejected; responses larger than the limit abort with TooLargeError; disallowed paths in robots.txt are not fetched; two requests to the same host are spaced by the configured interval; tests use a local test server (no real internet access)."

## Clarifications

### Amendment 2026-10-05 (PR #48 review)

- A robots.txt fetch that fails with a timeout, connection or DNS error disallows the origin for 5 minutes, then robots.txt is fetched again; one network blip must not block a site for a whole long run. 5xx, blocked and malformed answers still disallow for the rest of the run.
- robots.txt rules are matched by invio's own RFC 9309 matcher instead of `urllib.robotparser`, which ignores `*` and `$` before Python 3.14.
- A 304 to a request that carried no validators is an `http_status` error, not `NotModified`.

### Session 2026-10-05

- Q: When a site answers with an error status (4xx/5xx), should the client raise a fetch error or return the response for the fetcher to decide? → A: Raise a fetch error carrying the status code and final URL for every 4xx/5xx response; only 2xx returns a fetch result and 304 returns a not-modified result.
- Q: If a host's robots.txt cannot be retrieved due to a 5xx or network failure, should all its pages be skipped for the run? → A: Yes — treat the host as fully disallowed for the rest of the run (no retry) and report each skipped URL as `blocked_by_robots`.
- Q: If a site's robots.txt sets a `Crawl-delay`, should invio wait that long between requests to the site? → A: Yes — use the larger of the configured interval and `Crawl-delay`, capped at 30 seconds.
- Q: Should the robots.txt check also apply to RSS/Atom feed URLs explicitly added to a job? → A: Yes — every fetch through the client is checked against robots.txt, feeds included; there is no per-request or per-source exemption.

## User Scenarios & Testing *(mandatory)*

The "users" of this feature are (a) the operator who configures research jobs with source URLs and runs them unattended, (b) the owners of the websites that invio fetches from, and (c) the developers of the individual source fetchers (RSS, web pages, etc.) who all fetch through this one shared layer.

### User Story 1 - Block requests to internal network resources (Priority: P1)

An operator (or anyone able to edit a job file) can put arbitrary URLs into a job's sources. invio runs on a server that may have access to internal services (databases, admin panels, cloud metadata endpoints). Every fetch therefore passes a safety check: only web addresses (`http`/`https`) are allowed, the host name is resolved, and the request is refused if the host points to a loopback, private, link-local, multicast or otherwise non-public address. The same check is applied again to every redirect target, so a public site cannot bounce the fetch into the internal network.

**Why this priority**: This is the security core of the issue. Without it a single malicious or mistyped URL can read internal resources, and the result could be mailed out in a digest. All other features are politeness and efficiency.

**Independent Test**: With the safety check active, ask the client to fetch `http://127.0.0.1/`, `http://10.0.0.5/`, `file:///etc/passwd`, and a URL on a local test server that redirects to a private address; each request is refused with a "blocked" error and no connection to the forbidden target is made.

**Acceptance Scenarios**:

1. **Given** the default configuration, **When** a fetch of `http://127.0.0.1/` is requested, **Then** it is refused with a blocked error naming the reason (non-public address) and no connection is opened.
2. **Given** the default configuration, **When** a fetch of `http://10.0.0.5/` is requested, **Then** it is refused with a blocked error.
3. **Given** any configuration, **When** a fetch of `file:///etc/passwd` (or `ftp://`, `gopher://`, `data:` etc.) is requested, **Then** it is refused with a blocked error naming the unsupported scheme.
4. **Given** a permitted start URL that answers with a redirect to `http://192.168.1.1/admin`, **When** the fetch follows redirects, **Then** the redirect target is checked, refused with a blocked error, and never contacted.
5. **Given** a host name that resolves to several addresses of which at least one is non-public, **When** a fetch is requested, **Then** it is refused.
6. **Given** a host name whose address changes between the check and the connection (DNS rebinding), **When** the fetch is performed, **Then** the connection is only ever made to an address that passed the check.

---

### User Story 2 - Enforce resource limits on every fetch (Priority: P1)

Unattended runs must not hang or exhaust memory because a source is slow, huge or misbehaving. Every fetch has bounded connect and read time, follows at most 5 redirects, and aborts as soon as the response body exceeds the size limit (default 10 MB) — whether or not the server announced the size in advance. Each request identifies invio honestly with a descriptive client identifier that includes a contact placeholder, so site owners know who is fetching and how to reach the operator.

**Why this priority**: A single oversized or endless response can crash or stall a whole run; limits are part of the safety contract alongside the SSRF guard and are named in the acceptance criteria.

**Independent Test**: Against a local test server, fetch a response larger than the configured limit (once with an announced size, once streamed without one), a redirect chain of 6 hops, and an endpoint that never answers; observe a too-large error, a fetch error for too many redirects, and a fetch error for timeout respectively, each within the configured bounds.

**Acceptance Scenarios**:

1. **Given** a size limit of 10 MB, **When** the server announces a body larger than the limit, **Then** the fetch is aborted with a too-large error before the body is downloaded.
2. **Given** a size limit of 10 MB, **When** the server streams more than the limit without announcing a size (or announces a smaller size than it sends), **Then** the fetch is aborted with a too-large error as soon as the limit is crossed, and at most limit-plus-one-chunk bytes are held in memory.
3. **Given** a compressed response, **When** the decompressed content exceeds the limit, **Then** the fetch is aborted with a too-large error (the limit applies to the content the fetcher receives).
4. **Given** a chain of more than 5 redirects, **When** the fetch follows it, **Then** it stops with a fetch error stating too many redirects.
5. **Given** a server that accepts the connection but never answers, **When** the read timeout elapses, **Then** the fetch ends with a fetch error stating a timeout.
6. **Given** any request sent by the client, **When** the test server inspects it, **Then** it carries invio's client identifier with version and contact placeholder.

---

### User Story 3 - Respect robots.txt (Priority: P2)

Site owners can tell crawlers which paths they do not want fetched. Before fetching a page, invio consults the host's robots.txt (fetched once per host and remembered for the rest of the run) and does not fetch paths that are disallowed for invio's client identifier. Such skips are reported with the distinct reason `blocked_by_robots`, so the operator can see why a source produced nothing.

**Why this priority**: Politeness toward target sites is a stated goal and acceptance criterion, but a missing robots check does not endanger the operator's own infrastructure, hence P2.

**Independent Test**: Serve a robots.txt from a local test server that disallows `/private/`; fetching `/private/page` is refused with reason `blocked_by_robots` and the server never receives that request; fetching `/public/page` succeeds; robots.txt itself was requested once.

**Acceptance Scenarios**:

1. **Given** a robots.txt disallowing `/private/` for all agents, **When** `/private/page` is requested, **Then** it is not fetched and a blocked error with reason `blocked_by_robots` is reported.
2. **Given** the same host, **When** several pages are fetched in one run, **Then** robots.txt is retrieved only once for that host.
3. **Given** a host without robots.txt (not-found response), **When** pages are requested, **Then** all paths are treated as allowed.
4. **Given** a host whose robots.txt cannot be retrieved because of a server error or network failure, **When** pages are requested, **Then** the host is treated as fully disallowed for this run and pages are reported as `blocked_by_robots`.
5. **Given** a robots.txt disallowing `/feed.xml`, **When** a feed source at `/feed.xml` is requested, **Then** it is not fetched and is reported as `blocked_by_robots`, exactly like a page.
6. **Given** a robots.txt with rules specific to invio's client identifier, **When** a path is checked, **Then** those specific rules take precedence over the generic rules.

---

### User Story 4 - Rate-limit requests per host (Priority: P2)

To avoid overloading a site, requests to the same host are spaced out: by default at most one request per second per host, and never more than one in flight to the same host at a time. Requests to different hosts are not slowed down by each other.

**Why this priority**: Required for politeness and named in the acceptance criteria; less critical than safety because the number of URLs per job is small.

**Independent Test**: Issue two concurrent fetches to the same local test server and one to a second host; the server records the two same-host requests at least the configured interval apart while the other host is served without waiting.

**Acceptance Scenarios**:

1. **Given** the default interval of 1 second, **When** two requests to the same host are issued at the same time, **Then** the second starts no earlier than 1 second after the first.
2. **Given** a configured interval of 0.2 seconds, **When** two requests to the same host are issued, **Then** they are spaced by at least 0.2 seconds.
3. **Given** requests to two different hosts, **When** they are issued concurrently, **Then** neither waits for the other host's interval.
4. **Given** robots.txt retrieval for a host, **When** the subsequent page request is made, **Then** the robots.txt request counts toward that host's spacing.
5. **Given** a configured interval of 0.2 seconds and a robots.txt with `Crawl-delay: 1` for invio, **When** two pages on that host are requested, **Then** they are spaced by at least 1 second.
6. **Given** a robots.txt with `Crawl-delay: 120`, **When** pages on that host are requested, **Then** they are spaced by the 30-second cap, not 120 seconds.

---

### User Story 5 - Skip unchanged content with conditional requests (Priority: P3)

Many sources (feeds, pages) change rarely. When a URL is fetched again during the same run (or with the same client), invio sends the change markers it received last time (entity tag and last-modified date). If the server says the content is unchanged, the fetcher receives a distinct "not modified" result instead of the full content, saving bandwidth for both sides.

**Why this priority**: An efficiency improvement; the issue explicitly allows an in-memory per-run store with persistence to follow later.

**Independent Test**: A local test server returns content with an entity tag on the first request and answers "not modified" when the matching tag is sent back; the second fetch returns the not-modified result and the server confirms it received the tag.

**Acceptance Scenarios**:

1. **Given** a first successful fetch whose response carried an entity tag and/or last-modified date, **When** the same URL is fetched again with the same client, **Then** the request carries the corresponding conditional headers.
2. **Given** the server answers "not modified", **When** the fetch completes, **Then** the fetcher receives a not-modified result (not an error and not an empty body).
3. **Given** a response without any change markers, **When** the URL is fetched again, **Then** a normal unconditional request is made.
4. **Given** a new client instance (new run), **When** a URL is fetched, **Then** no change markers from earlier runs are sent.

---

### Edge Cases

- URL with credentials (`http://user:pass@host/`), unusual ports, or IP literals in alternative notations (`http://2130706433/`, `http://0x7f.1/`, `http://[::1]/`, `http://[::ffff:127.0.0.1]/`): the resolved address is checked, so these are refused when they map to a non-public address.
- Unspecified (`0.0.0.0`, `::`), shared/carrier-grade NAT (`100.64.0.0/10`), reserved and documentation ranges are treated as non-public and refused.
- Host name that does not resolve: fetch error (not a blocked error).
- Redirect without a target, with a relative target, or to a non-web scheme: relative targets are resolved against the current URL and checked; missing targets are a fetch error; non-web schemes are blocked.
- Redirect to a different host: robots.txt and rate limit of the new host apply to the redirected request.
- robots.txt itself redirects, is larger than its own limit (500 KB), or is served from a non-public address: the same safety rules apply; an over-size robots.txt is truncated to the first 500 KB and parsed as far as possible.
- Server responds with an error status (4xx/5xx other than "not modified"): fetch error that carries the status code and final URL.
- Many concurrent fetches to the same host: they queue fairly and each waits its turn; cancelling one waiting fetch does not block the others.
- A test environment that must talk to a local test server: loopback access is possible only through an explicit, off-by-default allowance that cannot be enabled from job files.

## Requirements *(mandatory)*

### Functional Requirements

**General**

- **FR-001**: All source fetchers MUST perform outgoing web requests through one shared client; no fetcher may open its own unrestricted connection.
- **FR-002**: The client MUST identify itself on every request with a descriptive identifier containing the product name, version and a contact placeholder that the operator can configure.
- **FR-003**: The client MUST apply a connect timeout and a read timeout to every request (defaults: 10 s connect, 30 s read), configurable per client.
- **FR-004**: Limits (size limit, redirect limit, timeouts, per-host interval, client identifier/contact) MUST have defaults and be configurable through application settings; invalid values (e.g. negative or zero limits) MUST be rejected at startup.

**SSRF guard**

- **FR-005**: The client MUST refuse any URL whose scheme is not `http` or `https`.
- **FR-006**: The client MUST resolve the target host before connecting and refuse the request if any resolved address is loopback, private, link-local, multicast, unspecified, shared address space, or otherwise reserved/non-globally-routable, for both IPv4 and IPv6 (including IPv4-mapped IPv6 addresses).
- **FR-007**: The client MUST connect only to an address that passed the check in FR-006 (no second, unchecked resolution).
- **FR-008**: The client MUST follow redirects itself, applying FR-005 to FR-007 to every redirect target before contacting it, and MUST stop after 5 redirects (configurable) with a fetch error.
- **FR-009**: Refusals under FR-005/FR-006/FR-008 MUST raise a blocked error that states the reason (unsupported scheme or non-public address) and the offending URL, without contacting the target.
- **FR-010**: Loopback/private targets MUST only be reachable when an explicit allowance is enabled programmatically (intended for tests); it MUST be off by default and MUST NOT be settable from job files.

**Response size**

- **FR-011**: The client MUST abort with a too-large error when the announced body size exceeds the size limit (default 10 MB) without downloading the body.
- **FR-012**: The client MUST read bodies incrementally and abort with a too-large error as soon as the received (decoded) content exceeds the size limit.

**robots.txt**

- **FR-013**: Before every fetch (web pages and feeds alike, with no per-request exemption), the client MUST check the host's robots.txt rules for its client identifier and MUST NOT fetch disallowed paths.
- **FR-014**: robots.txt MUST be retrieved at most once per host (scheme + host + port) per client instance and reused for subsequent checks.
- **FR-015**: robots.txt retrieval MUST obey the same SSRF guard, rate limit and timeouts as other requests; content beyond 500 KB is ignored.
- **FR-016**: A not-found (or other 4xx) robots.txt MUST mean "everything allowed"; a server error or network failure while retrieving it MUST mean "everything disallowed" for that host for the rest of the run, without retrying robots.txt, and each skipped URL is reported as `blocked_by_robots`.
- **FR-017**: Skipped URLs MUST be reported via a blocked error whose reason is exactly `blocked_by_robots`.
- **FR-018**: robots.txt checking MUST be enabled by default; retrieving robots.txt itself MUST NOT trigger a robots check.

**Rate limiting**

- **FR-019**: The client MUST space the start of consecutive requests to the same host by at least the configured interval (default 1 second) and allow at most one in-flight request per host.
- **FR-020**: Spacing MUST be tracked per host; requests to different hosts MUST NOT delay each other.
- **FR-020a**: If the host's robots.txt specifies a `Crawl-delay` applicable to invio's client identifier, the effective interval for that host MUST be the larger of the configured interval and the `Crawl-delay`, capped at 30 seconds; invalid or negative values are ignored.
- **FR-021**: Each redirect hop and each robots.txt retrieval MUST count as a request for the host it targets.

**Conditional requests**

- **FR-022**: After a successful response carrying an entity tag and/or last-modified date, the client MUST remember them per URL for the lifetime of the client instance.
- **FR-023**: On a later fetch of the same URL, the client MUST send the remembered markers as conditional request headers.
- **FR-024**: On a "not modified" answer, the client MUST return a distinct not-modified result identifying the URL; it MUST NOT raise an error.
- **FR-025**: The remembered markers MUST be kept in memory only; persistence across runs is out of scope.

**Results and errors**

- **FR-026**: A successful (2xx) fetch MUST return the final URL (after redirects), status code, response headers, and body content.
- **FR-027**: The client MUST expose three error types: a general fetch error (network failure, timeout, unresolvable host, too many redirects, every 4xx/5xx response — carrying the status code and final URL when available), a blocked error (SSRF guard or robots.txt, carrying the reason) and a too-large error (size limit exceeded, carrying the limit). Blocked and too-large errors MUST be distinguishable from, and catchable as, fetch errors.
- **FR-028**: Error messages MUST NOT include request headers or credentials embedded in URLs.
- **FR-029**: Blocked and failed fetches MUST be logged as structured events including host, reason and URL (credentials stripped).

**Testing**

- **FR-030**: All behaviours in this specification MUST be verifiable with automated tests that use a local test server and require no internet access.

### Key Entities

- **Fetch result**: Outcome of a successful fetch — final URL, status, headers, body content, and the change markers received.
- **Not-modified result**: Outcome when the server confirms the content is unchanged — requested URL and the markers that were sent.
- **Fetch error / Blocked error / Too-large error**: Typed failure outcomes; blocked errors carry a reason (`unsupported_scheme`, `non_public_address`, `blocked_by_robots`); too-large errors carry the limit; fetch errors carry the status code if any.
- **Host policy state**: Per-host data held for one client instance — parsed robots.txt rules, effective request interval (including any capped `Crawl-delay`), time of the last request, and the one-at-a-time slot.
- **Validator cache**: Per-URL entity tag and last-modified date for one client instance.
- **Client settings**: Size limit, redirect limit, timeouts, per-host interval, client identifier and contact, robots checking on/off, and the test-only allowance for non-public addresses.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the issue's rejection cases (`http://127.0.0.1`, `http://10.0.0.5`, `file:///etc/passwd`, redirect to a private address) are refused, and the forbidden targets receive zero connection attempts.
- **SC-002**: No fetch keeps more than the size limit plus one read chunk in memory; oversized responses are aborted in 100% of tested cases, with and without an announced size.
- **SC-003**: Paths disallowed by robots.txt receive zero requests from invio, and robots.txt is requested at most once per host per run.
- **SC-004**: Consecutive requests to the same host are never closer together than the effective interval — the configured interval or a larger, capped `Crawl-delay` — (measured at the test server), while requests to different hosts proceed without waiting.
- **SC-005**: A re-fetch of unchanged content with change markers transfers no body and returns a not-modified result.
- **SC-006**: No single request (one redirect hop or one robots.txt retrieval) runs longer than the configured total timeout, so one fetch is bounded by the total timeout (shared by all redirect hops) plus per-host waiting time; a stalled server never stalls a run indefinitely.
- **SC-007**: The full test suite for this feature runs without internet access and passes deterministically in CI.

## Assumptions

- The issue's path `scout/sources/http.py` refers to the legacy working name; the client lives in invio's existing `sources` package (consistent with the clarification made for issue #9 that "scout" is a legacy name).
- Dependency #2 (project skeleton, settings and logging) is already merged; the client's defaults are exposed through the existing application settings.
- A suitable asynchronous HTTP library and a robots.txt parser are chosen in `/speckit-plan`; adding the HTTP library as a new runtime dependency is justified in the PR as required by the constitution. Library names from the issue were deliberately left out of the requirements.
- The contact placeholder is a configurable setting with a neutral default (e.g. a placeholder address or project URL); the operator is expected to set a real contact.
- robots.txt semantics follow the current robots exclusion standard (RFC 9309): 4xx → allow all, 5xx/unreachable → disallow all, 500 KB parse limit. A `Crawl-delay` directive is honoured up to a 30-second cap (see FR-020a).
- Only GET requests (including conditional GET) are in scope; HEAD/POST, retries with backoff, proxies, cookies and authentication are out of scope.
- The validator cache and robots cache live for the lifetime of one client instance, which corresponds to one job run; a persistent cache is a later issue.
- Error status codes (4xx/5xx) are reported as fetch errors; fetchers decide how to present them to the operator.
- Integrating the client into concrete fetchers (RSS, web pages, YouTube) is done in their own issues; this feature delivers the shared client and its tests.
