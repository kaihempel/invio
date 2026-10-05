# Feature Specification: Web Page Source with Change Detection

**Feature Branch**: `gh-issue-12`

**Created**: 2026-10-05

**Status**: Draft

**Input**: User description: "GitHub issue #12: [FEAT] Add web page source with change detection. Depends on #10 (safe HTTP client). Context: Many relevant pages (e.g. developer news pages) have no feed. The source tracks a page and reports it as new when its relevant content changes. Implementation steps: 1) Implement `WebSource` fetching the page with the safe client. 2) If `selector` is set, extract that CSS region; otherwise use the full body. 3) Normalize text (collapse whitespace, strip scripts/styles) and compute `content_hash`. 4) Return one candidate for the page itself with `content_hash`; optionally `mode: links` to return linked article URLs matching an optional `url_pattern`. 5) Optional `render: js` flag, with the browser engine loaded lazily so it is not a hard dependency. Acceptance criteria: unchanged page content produces the same `content_hash` across runs; a changed paragraph in the selected region changes the hash; changes outside the selector do not change the hash; `mode: links` returns only URLs matching `url_pattern`; the browser engine is not loaded unless `render: js` is used."

## Clarifications

### Session 2026-10-05

- Q: For `render: js`, how should invio stop the headless browser from contacting internal addresses? → A: Every request the browser makes (page, scripts, images, API calls) is checked by the existing scheme/address guard and cancelled if blocked; robots.txt and rate limiting apply to the page URL only.
- Q: When a configured `selector` matches nothing, should the fetch fail or report an empty-text fingerprint? → A: Fail this source's fetch with reason `selector_not_found`; no candidate is returned and other sources are unaffected.
- Q: In `mode: links` without `url_pattern`, should links to any website be returned, or only same-site links? → A: Without `url_pattern`, only links on the same host as the final page URL; with `url_pattern`, any host the pattern matches.
- Q: With `render: js`, when is the page considered finished rendering? → A: When the network has been idle for ~0.5 s, capped at 30 s total; an optional `wait_for` CSS selector additionally waits until that element exists; a timeout fails the fetch with reason `timeout`.
- Q: In page mode, should the candidate carry a teaser of the tracked text? → A: Yes — the first 500 characters of the normalized region text, cut with an ellipsis (`…`) if longer (same limit as the RSS source); empty text means no teaser.

## User Scenarios & Testing *(mandatory)*

The "users" of this feature are (a) the operator who adds web pages without a feed to a research job and runs it unattended, and (b) the pipeline, which receives the page (or the articles it links to) as candidates and decides, using the content fingerprint, whether something is new.

### User Story 1 - Detect when a tracked page's content changes (Priority: P1)

The operator adds a page such as a vendor's "developer news" page to a job as a `web` source. On every run invio fetches the page, reduces it to its readable text and computes a content fingerprint (`content_hash`). It reports exactly one candidate for the page carrying that fingerprint. As long as the readable text does not change, the fingerprint stays the same, so the pipeline treats the page as already known; when the text changes, the fingerprint changes and the page is reported as new.

**Why this priority**: This is the core value of the issue — following sources that have no feed. Without a stable fingerprint, every run would either miss updates or report the same page again and again.

**Independent Test**: Serve a fixed HTML page from a local test server, run the source twice and compare fingerprints; then change one paragraph and run again.

**Acceptance Scenarios**:

1. **Given** a `web` source without selector and a page whose content does not change, **When** the source is fetched in two separate runs, **Then** each run returns one candidate for the page and both carry the same `content_hash`.
2. **Given** the same page, **When** a paragraph in the body text is changed and the source is fetched again, **Then** the `content_hash` differs from the previous run.
3. **Given** two versions of the page that differ only in scripts, styles, HTML comments, markup/attributes or whitespace (indentation, line breaks, repeated spaces), **When** both are fetched, **Then** they produce the same `content_hash`.
4. **Given** a page with a `<title>`, **When** it is fetched, **Then** the candidate's title is the page title and its URL is the page's canonical URL, and its teaser is the start of the tracked text (at most 500 characters); a page without a usable title uses the URL as title.

---

### User Story 2 - Track only the relevant region of a page (Priority: P1)

Many pages contain parts that change on every visit — dates, "trending" boxes, ads, cookie banners, footers. The operator sets a `selector` (a CSS selector) that names the region worth watching, for example the list of news posts. Only the text inside that region is fingerprinted, so noise elsewhere on the page does not trigger false "new" reports.

**Why this priority**: Without a selector most real pages would change on every run, making change detection useless; it is part of the issue's acceptance criteria.

**Independent Test**: Serve a page with a selected region and a non-selected region; change each region separately and compare fingerprints.

**Acceptance Scenarios**:

1. **Given** a `web` source with `selector`, **When** a paragraph inside the selected region changes, **Then** the `content_hash` changes.
2. **Given** a `web` source with `selector`, **When** only content outside the selected region changes (header, sidebar, footer, scripts), **Then** the `content_hash` stays the same.
3. **Given** a selector that matches several elements, **When** the page is fetched, **Then** the text of all matches, in document order, is fingerprinted.
4. **Given** a selector that matches no element on the fetched page, **When** the page is fetched, **Then** the fetch fails with a clear, machine-readable reason (e.g. `selector_not_found`) instead of reporting an empty or unchanged page.

---

### User Story 3 - Discover linked articles on an index page (Priority: P2)

Some pages are lists of articles (a blog index, a release-notes overview) without a feed. The operator sets `mode: links` and optionally a `url_pattern`. Instead of one candidate for the page, invio returns one candidate per linked article whose URL matches the pattern, so each article enters the pipeline individually, just like feed entries.

**Why this priority**: Valuable for index pages, but the page-level change detection (Stories 1–2) already covers the basic need, so this is a second increment.

**Independent Test**: Serve an index page with a mix of article links, navigation links and external links; fetch with `mode: links` and a pattern and check the returned URLs.

**Acceptance Scenarios**:

1. **Given** `mode: links` and a `url_pattern`, **When** the page is fetched, **Then** only links whose absolute URL matches the pattern are returned, one candidate each, in page order.
2. **Given** `mode: links` without `url_pattern`, **When** the page is fetched, **Then** every http(s) link of the (selected region of the) page that points to the same host as the page is returned, except links back to the page itself; links to other hosts are dropped.
3. **Given** `mode: links` and a `selector`, **When** the page is fetched, **Then** only links inside the selected region are considered.
4. **Given** relative links, duplicate links (also after URL canonicalization, e.g. differing only by tracking parameters or fragment) and non-http links (`mailto:`, `javascript:`, `#anchor`), **When** the page is fetched, **Then** relative links are resolved against the page URL, each article URL appears once (first occurrence wins), and non-http(s) links are dropped.
5. **Given** a link, **When** it becomes a candidate, **Then** its title is the link text (whitespace collapsed), falling back to the URL; it carries no `content_hash`.

---

### User Story 4 - Track pages that need JavaScript to show their content (Priority: P3)

Some pages render their content in the browser. The operator sets `render: js` on such a source; invio then loads the page in a headless browser and applies the same selector, normalization, fingerprinting and link discovery to the rendered page. Installations that never use this option do not need the browser engine at all.

**Why this priority**: Only a minority of pages need it, and it brings a heavy optional dependency; the static path must work first.

**Independent Test**: Verify that fetching a static `web` source (and importing the source module) does not load the browser engine; with the engine available, a page whose text is inserted by script yields that text in its fingerprint.

**Acceptance Scenarios**:

1. **Given** sources without `render: js`, **When** they are configured and fetched, **Then** the browser engine is never loaded.
2. **Given** `render: js` and the browser engine is not installed, **When** the source is fetched, **Then** the fetch fails with a clear reason that names the missing optional component and how to install it; other sources of the run are unaffected.
3. **Given** `render: js` and a page whose content is produced by script, **When** the source is fetched, **Then** the fingerprint (or link list) reflects the rendered content.
4. **Given** `render: js`, **When** the page URL or any request the browser makes points to a forbidden target (non-http(s) scheme, private/loopback/link-local address), **Then** that request is refused, just as the shared HTTP client would refuse it.

---

### Edge Cases

- **HTTP 304 (unchanged)**: the shared client returns "not modified" for a page fetched earlier in the run with validators; the source then returns no candidates, consistent with the other sources.
- **Non-HTML response** (e.g. PDF, JSON, image): the fetch fails with a clear reason (e.g. `not_html`) instead of fingerprinting binary or unrelated data.
- **Empty readable text** (no selector, body without text): a candidate with the fingerprint of the empty text is returned; an empty page is a legitimate, stable state.
- **Invalid selector syntax**: rejected when the job file is loaded (configuration error), not on the first run.
- **Invalid `url_pattern`** (not a valid regular expression): rejected when the job file is loaded.
- **`url_pattern` without `mode: links`**, **`wait_for` without `render: js`**: rejected when the job file is loaded, because they would have no effect.
- **Rendered page never settles** (endless polling, `wait_for` element never appears): the fetch fails with reason `timeout` after 30 s; other sources of the run continue.
- **Character encoding**: the page's declared encoding (HTTP header or `<meta charset>`) is honored; the same text in different encodings produces the same fingerprint.
- **Redirects**: the candidate URL in page mode is the canonical form of the configured page URL, so a redirect target that varies between runs does not create a "new" page; relative links in links mode are resolved against the final (post-redirect) URL.
- **Blocked by robots.txt, SSRF guard, size limit, timeout**: surfaced as the shared client's typed fetch errors; the source adds no special handling.
- **Very many links**: the source returns all matches; capping per source is done by the existing per-run limits (`max_items_per_source`).

## Requirements *(mandatory)*

### Functional Requirements

**Configuration**

- **FR-001**: The `web` source configuration MUST accept, in addition to `type`, `url`, `name` and `enabled`: an optional `selector` (non-empty CSS selector), an optional `mode` (`page` (default) or `links`), an optional `url_pattern` (regular expression), an optional `render` (`static` (default) or `js`) and an optional `wait_for` (non-empty CSS selector, only meaningful with `render: js`).
- **FR-002**: Unknown keys, an invalid `selector` or `wait_for`, an invalid `url_pattern`, a `url_pattern` without `mode: links`, a `wait_for` without `render: js`, and unknown `mode`/`render` values MUST be rejected when the job file is loaded, with an error naming the field.
- **FR-003**: Existing job files with `web` sources that only use `type`, `url`, `name` and `enabled` MUST remain valid and behave as `mode: page`, `render: static`, no selector.

**Fetching**

- **FR-004**: Static pages MUST be fetched only through the shared safe HTTP client, so the SSRF guard, robots.txt, rate limiting, size limit and conditional GET apply unchanged.
- **FR-005**: When the shared client reports "not modified", the source MUST return no candidates.
- **FR-006**: A response that is not an HTML document MUST fail with a machine-readable reason.
- **FR-007**: The page's declared character encoding MUST be honored when decoding the document.

**Region selection and normalization**

- **FR-008**: Without `selector`, the region is the document body; with `selector`, the region is every element matching the selector, in document order.
- **FR-009**: A `selector` that matches no element MUST fail the fetch with the reason `selector_not_found` and return no candidate; it MUST NOT fall back to the whole body or to an empty-text fingerprint. This applies in both page and links mode.
- **FR-010**: The readable text of the region MUST exclude the content of script, style, noscript and template elements and HTML comments.
- **FR-011**: Text normalization MUST collapse every run of whitespace (including line breaks and non-breaking spaces) to a single space, apply Unicode NFC normalization (so composed and decomposed spellings of the same text are equal), trim leading/trailing whitespace and separate the text of block-level elements so that adjacent paragraphs do not merge into one word. Case and punctuation are kept.
- **FR-012**: `content_hash` MUST be a deterministic fingerprint (SHA-256, hex) of the normalized text only: identical normalized text always yields the identical hash, across runs and machines; any change of the normalized text yields a different hash.

**Page mode (default)**

- **FR-013**: In page mode the source MUST return exactly one candidate per successful fetch, with the canonical page URL, its URL hash, the page title (whitespace collapsed; fallback: the URL), `published_at` empty, type `article`, a teaser, and the `content_hash` from FR-012. The teaser is the normalized region text (FR-011), at most 500 characters including a trailing ellipsis (`…`) when cut, the same limit as the RSS source; there is no teaser when the region text is empty. The teaser does not influence the `content_hash`.

**Links mode**

- **FR-014**: In links mode the source MUST collect the targets of all links in the region, resolve relative links against the final page URL, keep only http(s) URLs, canonicalize them with the existing URL canonicalization, drop links to the page itself and drop duplicates (first occurrence wins), preserving page order.
- **FR-015**: If `url_pattern` is set, only URLs for which the pattern matches (anywhere in the canonical absolute URL) MUST be returned, on any host. If `url_pattern` is not set, only URLs whose host equals the host of the final (post-redirect) page URL MUST be returned (compared case-insensitively; subdomains such as `www.` count as different hosts).
- **FR-016**: Each link candidate MUST carry its canonical URL and URL hash, the link text as title (fallback: the URL), `published_at` empty, type `article`, no teaser and no `content_hash`.

**JavaScript rendering**

- **FR-017**: The browser engine MUST be an optional dependency: it MUST NOT be loaded when the source module is imported or when sources without `render: js` are configured or fetched.
- **FR-018**: With `render: js`, the rendered document MUST pass through the same region selection, normalization, fingerprinting and link discovery as a static page. The document is read once the network has been idle for about 0.5 s and, if `wait_for` is set, an element matching `wait_for` exists. Rendering a page MUST take at most 30 s in total; on expiry the fetch fails with reason `timeout` and the browser resources for that page are released.
- **FR-019**: With `render: js`, the page URL MUST pass the same scheme, network-address and robots.txt checks and the per-host rate limit as static fetches. Every request the browser makes while loading the page (redirects, scripts, styles, images, frames, API calls) MUST be checked by the same scheme and network-address guard and cancelled if it fails; robots.txt and the rate limit are not applied to these sub-requests. The check resolves the host name itself; because the browser resolves it again when connecting, a DNS answer that changes between the two lookups (DNS rebinding) is a known, documented residual risk of `render: js` (see Assumptions).
- **FR-020**: With `render: js` and the browser engine unavailable, the fetch MUST fail with a clear reason naming the missing optional component; this failure MUST NOT affect other sources.

**General**

- **FR-021**: Every failure MUST surface as the existing typed fetch error with a short machine-readable reason; the source MUST NOT store anything or fetch linked articles' content.

### Key Entities

- **Web source configuration**: the operator's description of a tracked page — page URL, optional name, enabled flag, optional region selector, mode (`page`/`links`), optional link pattern, rendering (`static`/`js`), optional element to wait for when rendering.
- **Page candidate**: one item representing the tracked page itself, identified by its canonical URL and carrying the fingerprint of its relevant text plus a short teaser of it; the pipeline compares the fingerprint with the stored one to decide whether the page is new.
- **Link candidate**: one item per article discovered on an index page, identified by its canonical URL, titled by its link text; no fingerprint.
- **Content fingerprint (`content_hash`)**: a stable digest of the normalized readable text of the selected region.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Fetching an unchanged page 10 times in a row yields the same fingerprint every time (0 false "changed" reports).
- **SC-002**: Every single-paragraph text change inside the tracked region changes the fingerprint (0 missed changes across the test corpus).
- **SC-003**: Changes confined to content outside the selected region, to scripts/styles, or to whitespace and markup produce 0 fingerprint changes.
- **SC-004**: In links mode, 100% of returned URLs match the configured pattern and 0 matching links in the region are missed.
- **SC-005**: An installation without the optional browser component can configure and run jobs with static `web` sources with no error and without that component being loaded.
- **SC-006**: Every acceptance criterion of issue #12 is covered by at least one automated test that runs without internet access.

## Assumptions

- "New" is decided downstream: the source only reports the candidate with its fingerprint; comparing it with the previously stored fingerprint and marking the page as new is the pipeline's job (outside this issue).
- Page mode reuses the existing item type `article`; no new item type is introduced.
- `url_pattern` is a regular expression matched with search semantics against the canonical absolute URL; operators wanting a full match anchor it themselves.
- The browser engine for `render: js` is installed as an optional extra; rendered fetches do not use conditional GET (a rendered page is always fetched in full).
- The page title is taken from the document `<title>` even when a selector is set.
- `render: js` is opt-in and does not pin the browser's connections to the checked addresses; the remaining DNS-rebinding window is accepted for this issue and documented for operators. Closing it (e.g. a filtering proxy for all browser traffic) is out of scope.
- Depends on #10 (shared safe HTTP client) and reuses the URL canonicalization and `Source` contract introduced with #11.
