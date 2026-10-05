# Research: Web Page Source with Change Detection (gh-issue-12)

All Technical Context unknowns are resolved below. Each entry: **Decision**, **Rationale**,
**Alternatives considered**.

## R1 — HTML parser and CSS selector engine

- **Decision**: `selectolax` (≥ 1.0; 1.0.0 resolved, Lexbor backend, `LexborHTMLParser`) as a new runtime
  dependency. It parses the page, evaluates `selector` / `a[href]` and removes non-text
  elements.
- **Rationale**: A real HTML5 parser (Lexbor follows the WHATWG tree-building rules, so
  `selector` matches what a browser would match), a full CSS selector engine, ships type stubs
  (mypy strict), C speed, no transitive dependencies. Python ≥ 3.9 < 3.16 fits the project's
  3.12+.
- **Alternatives considered**: `beautifulsoup4` + `soupsieve` (pure Python, two dependencies,
  ~10× slower, the stdlib `html.parser` backend is not HTML5-conformant, `lxml` backend adds a
  third); `lxml` + `cssselect` (XPath translation of CSS, less complete selector support, libxml2
  HTML4 parser); stdlib `html.parser` alone (no CSS selectors).

## R2 — Readable text and normalization (FR-010, FR-011)

- **Decision**: Two steps.
  1. With selectolax: remove `script`, `style`, `noscript`, `template` elements (and their
     content) from the document; pick the region (`body`, or every `selector` match in
     document order).
  2. Run each region node's serialized HTML through the RSS source's existing text extractor
     (stdlib `HTMLParser`: drops comments, decodes character references, block elements
     separate words, inline elements do not), join the regions with a space, apply Unicode NFC,
     then collapse every whitespace run (`str.split()` covers NBSP and all Unicode spaces) to a
     single space and trim.
  The extractor, `collapse()` and the teaser function move from `rss.py` to a shared module
  `invio/sources/text.py`; `noscript` is added to its non-text tags (harmless for feeds).
- **Rationale**: Reusing one extractor gives feeds and pages identical text semantics ("wo<b>rd</b>"
  stays "word", "<p>a</p><p>b</p>" reads "a b"). selectolax's own `text(separator=…)` either
  merges paragraphs (no separator) or splits inline words (with separator). NFC keeps composed
  and decomposed spellings of the same text on one hash.
- **Alternatives considered**: selectolax `text(deep=True, separator=" ")` only (splits inline
  words, so a markup-only edit like adding `<b>` would change the hash); case-folding or
  removing punctuation (would hide real content changes).

## R3 — Fingerprint (FR-012)

- **Decision**: `content_hash = sha256(normalized_text.encode("utf-8")).hexdigest()`; empty text
  hashes the empty string.
- **Rationale**: Same primitive as `url_hash`; stable across processes and machines (no
  `hash()` salt); 64 hex chars fit the existing `content_hash` column.
- **Alternatives considered**: SimHash/fuzzy hashing (would hide small real changes, contradicts
  SC-002).

## R4 — Document decoding (FR-007)

- **Decision**: Encoding precedence: BOM → `charset` in `Content-Type` → `<meta charset>` /
  `<meta http-equiv="Content-Type">` in the first 1024 bytes → UTF-8; decode with
  `errors="replace"` via `FetchResult.text(encoding)`.
- **Rationale**: This is the HTML standard's precedence (minus heuristic sniffing), and the
  scan window matches the spec's prescan limit. The same text in different encodings yields the
  same normalized text, hence the same hash.
- **Alternatives considered**: Passing bytes to selectolax and letting Lexbor detect the
  encoding (no control over precedence, header charset ignored).

## R5 — What counts as HTML (FR-006)

- **Decision**: Accept `text/html` and `application/xhtml+xml`. Without a `Content-Type`, accept
  the body if, after an optional BOM and whitespace, it starts with `<!doctype html` or `<html`
  (case-insensitive). Anything else → `FetchError("not_html")`.
- **Rationale**: Rejects PDFs, JSON and images, which would otherwise produce a meaningless but
  stable hash; tolerates misconfigured servers that omit the header.
- **Alternatives considered**: Full MIME sniffing (overkill); accepting everything (silently
  tracks binary data).

## R6 — Link discovery (FR-014–FR-016)

- **Decision**: `a[href]` inside each region node in document order. Base URL: the first
  `<base href>` of the document resolved against the final page URL, else the final page URL.
  Each href → `urljoin` → `canonical_url()` → keep http(s) with a host (same check as the RSS
  source's `_entry_url`) → drop the page itself (canonical form of the final and of the
  configured URL) → host filter (FR-015) or `re.search(url_pattern, url)` → drop duplicates,
  first wins. Title = collapsed link text, else the URL.
- **Rationale**: Mirrors how a browser resolves links and how the RSS source maps entry links,
  so candidates from both sources share URL identity.
- **Alternatives considered**: Ignoring `<base>` (wrong URLs on sites that use it); matching
  `url_pattern` against the raw href (relative hrefs would make patterns site-specific).

## R7 — Optional browser rendering engine (FR-017–FR-020)

- **Decision**: Playwright for Python (≥ 1.63, Chromium), as an optional extra
  `invio[render]`. It is imported only inside `invio/sources/browser.py`, and that module is
  imported only inside `WebPageSource` when a `render: js` config is fetched. An `ImportError`
  (or a missing browser binary) → `FetchError("render_unavailable")` whose message tells the
  operator to run `uv sync --extra render` and `playwright install chromium`. The development
  group also contains Playwright so mypy and the browser tests can run.
- **Rationale**: Named in the issue; mature async API; type information included. The lazy
  import keeps installs without the extra working (FR-017, SC-005).
- **Alternatives considered**: Selenium (no request interception without a proxy); pyppeteer
  (unmaintained); an external rendering service (network dependency, more SSRF surface).

## R8 — Network guard for the browser (FR-019, clarification Q1)

- **Decision**:
  - Page URL: `SafeHttpClient.admission(url)` (new public context manager: scheme + address
    guard, robots.txt, per-origin rate-limit slot held during navigation). A blocked URL never
    reaches the browser.
  - Every browser request: `context.route("**/*", handler)`. The handler calls
    `SafeHttpClient.check_target(url)` (new public method: scheme + address guard only, using
    the client's resolver and `allow_networks`). Blocked → `route.abort("blockedbyclient")`.
    Allowed → `route.fetch(max_redirects=0)`; redirects are followed by the handler itself,
    each `Location` checked before it is requested, and the browser gets the final response via
    `route.fulfill(response=…)`. (Verified against Chromium 1243: neither `route.continue_()`
    nor fulfilling a 3xx makes Chromium route the redirect hop, so a hop checked only by the
    handler would let a second hop through. Redirected POSTs are aborted. The render reports the
    final URL of the main document; the page itself keeps seeing the URL it asked for.)
  - WebSockets: `context.route_web_socket("**/*", …)` with the same check, closing blocked
    connections. Service workers blocked (`service_workers="block"`, they bypass routing);
    downloads off; WebRTC limited to proxied UDP via the Chromium flag
    `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`.
  - robots.txt and the rate limit are not applied to sub-requests (clarification Q1).
- **Rationale**: Gives the browser the same "no internal targets" guarantee as #10 for every
  request, including redirect hops, using the existing guard code.
- **Residual risk**: The check resolves the host, then the browser's own network stack
  resolves it again (no address pinning as in `SafeHttpClient`), so DNS rebinding between the
  two lookups remains possible. Accepted for an opt-in feature and documented in the README.
  Closing it would mean proxying all browser traffic through `SafeHttpClient`, which is out of
  scope.
- **Alternatives considered**: Checking the page URL only (rejected in clarification Q1);
  a local filtering HTTP proxy (closes the rebinding gap, but much more code and TLS
  complexity).

## R9 — Rendering completion and limits (FR-018, clarification Q4)

- **Decision**: `page.goto(url, wait_until="networkidle")` (Playwright: no network activity for
  500 ms), then `page.wait_for_selector(wait_for, state="attached")` if set, then
  `page.content()`. The whole render, navigation included, runs inside `asyncio.timeout(30)`
  with matching Playwright timeouts; expiry → `FetchError("timeout")`. The page's context is
  closed in `finally`. A main response with status ≥ 400 → `FetchError("http_status",
  status=…)`; no response or a navigation error → `FetchError("render_failed")`. The final URL
  is `page.url`. The rendered HTML is capped at the client's `max_response_bytes` (else
  `TooLargeError`).
- **Rationale**: Matches the clarified rule and reuses the existing error vocabulary.
- **Alternatives considered**: `load` event only (misses script-loaded content); fixed sleeps
  (slow and still flaky).

## R10 — Browser lifecycle

- **Decision**: One Chromium instance per `WebPageSource`, launched on the first `render: js`
  fetch and closed by `WebPageSource.aclose()` (`async with` supported). Each fetch uses a new,
  isolated browser context (no shared cookies or storage) with the client's User-Agent.
  Concurrent renders share the browser.
- **Rationale**: Launching Chromium costs ~1 s; one per run is enough. A fresh context per
  page keeps sites from seeing each other's state, like the cookie-free `SafeHttpClient`.
- **Alternatives considered**: A browser per fetch (slow); a persistent profile (leaks state
  across sites and runs).

## R11 — Configuration validation (FR-001–FR-003)

- **Decision**: New fields on `WebSource`: `selector`, `mode`, `url_pattern`, `render`,
  `wait_for`. Validators: `selector` / `wait_for` compiled by selectolax against an empty
  document (an invalid selector raises at load time); `url_pattern` compiled with `re`
  (`re.error` → field error with the regex message); a model validator rejects `url_pattern`
  without `mode: links` and `wait_for` without `render: js`. `docs/job.schema.json` is
  regenerated (`test_committed_schema_is_current` enforces it).
- **Rationale**: Constitution I (fail fast, name the field). `config` is the lowest layer and
  may use a third-party library; it must not import `invio.sources`.
- **Alternatives considered**: Validating selectors on first fetch (late failure in an
  unattended run, contradicts FR-002).

## R12 — Testing without network or browser

- **Decision**: Static paths use the existing loopback `server` fixture (`tests/http_helpers.py`)
  with `allow_networks=LOOPBACK`. Text, hash and link logic get pure unit tests, plus Hypothesis
  property tests (whitespace and markup-only changes keep the hash; text changes alter it). The
  lazy import is checked in a subprocess (`playwright` not in `sys.modules` after importing
  `invio.sources.web` and fetching a static page). "Engine missing" is simulated by setting
  `sys.modules["playwright"] = None`. Real-browser tests carry a new `browser` marker and skip
  when Chromium cannot be launched. CI installs Chromium (`uv run playwright install
  --with-deps chromium`), so they run there and count toward the 95 % coverage gate. They
  include the SSRF tests: a sub-request and a redirect to a non-allowed private address are
  aborted.
- **Rationale**: Constitution III (no internet, deterministic) while still testing the browser
  guard for real in CI.
- **Alternatives considered**: Mocking Playwright entirely (would not prove interception works);
  omitting `browser.py` from coverage (hides the security-relevant code).
