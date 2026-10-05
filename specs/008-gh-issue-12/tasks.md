---

description: "Task list for the web page source with change detection (gh-issue-12)"
---

# Tasks: Web Page Source with Change Detection

**Input**: Design documents from `/specs/008-gh-issue-12/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/job-file.md,
contracts/python-api.md, quickstart.md

**Tests**: Required. Constitution Principle III demands at least one test per acceptance
criterion, including rejection paths, with no internet access. Within each story, write the
tests first and confirm they fail before implementing.

**Organization**: Tasks are grouped by user story so each story can be implemented and tested
on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story from spec.md (US1–US4)
- Paths are relative to the repository root (single project: `src/invio/`, `tests/`)

## Conventions for every task

- Python 3.12, `mypy --strict` clean, ruff clean (incl. TID251 layering), line length 100,
  double quotes. Match the docstring and comment style of `src/invio/sources/rss.py`.
- Async tests rely on `asyncio_mode = "auto"`. Static-fetch tests use the loopback `server`
  fixture, `Route` and `LOOPBACK` from `tests/http_helpers.py`, and create the client with
  `SafeHttpClient(allow_networks=LOOPBACK)`. Add the test module to the `F811` per-file ignore
  in `pyproject.toml`, like `tests/test_source_rss.py`.
- Public names and signatures are fixed by `specs/008-gh-issue-12/contracts/python-api.md`.
  Config keys and error messages are fixed by `contracts/job-file.md`. Candidate field values
  and the outcome/error table are fixed by `data-model.md`. Decisions are in `research.md`
  (R1–R12).
- `invio.sources.web` must never import `playwright` at module level. Only
  `src/invio/sources/browser.py` imports it.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependencies, test marker, CI.

- [X] T001 Update `pyproject.toml`:
  - add `"selectolax>=1.0"` to `[project].dependencies`;
  - add `[project.optional-dependencies]` with `render = ["playwright>=1.63"]`;
  - add `"playwright>=1.63"` to `[dependency-groups].dev`;
  - add the pytest marker `"browser: real headless-Chromium tests (skipped when Chromium cannot be launched)"`;
  - extend the `F811` per-file ignore to `tests/test_source_web*.py` and `tests/test_http_admission.py`.

  Then run `uv lock` and commit `uv.lock`.
- [X] T002 [P] In `.github/workflows/ci.yml`, add a step `uv run playwright install --with-deps chromium` after `uv sync --locked` and before the pytest step of the main test job (not the `-m db` job).

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared text helpers and the extended `WebSource` config. Every story depends on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T003 [P] Write `tests/test_source_text.py` for the new `invio.sources.text` module:
  - `collapse()` collapses all Unicode whitespace including NBSP and trims;
  - `html_to_text()` drops comments and the content of `script`/`style`/`template`/`noscript`, decodes character references, separates block elements (`"<p>a</p><p>b</p>"` → `"a b"` after collapse) and keeps inline words together (`"wo<b>rd</b>"` → `"word"`);
  - `teaser()` returns `None` for empty or whitespace-only text, returns short text unchanged, and cuts long text at a word boundary to at most `TEASER_MAX_CHARS` (500) characters, ending in `"…"`.
- [X] T004 Create `src/invio/sources/text.py` (research R2): move `TEASER_MAX_CHARS`, `_ELLIPSIS`, `_NON_TEXT_TAGS`, `_BLOCK_TAGS`, `_collapse`, `_TextExtractor`, `_html_to_text` and `_teaser` from `src/invio/sources/rss.py`. Expose them publicly as `TEASER_MAX_CHARS`, `collapse`, `html_to_text` and `teaser` (`__all__`), and add `"noscript"` to the non-text tags. Update `src/invio/sources/rss.py` to import them, keep `TEASER_MAX_CHARS` importable from `invio.sources.rss` (keep it in `__all__`), and update `tests/test_source_rss.py` to import `html_to_text` from `invio.sources.text` (no aliases are left in `rss.py`). `uv run pytest tests/test_source_rss.py tests/test_source_text.py` must pass. (Depends on T003)
- [X] T005 [P] Extend `tests/test_job_sources.py` (contracts/job-file.md):
  - a `web` entry with all new keys loads, and the defaults are `selector=None`, `mode="page"`, `url_pattern=None`, `render="static"`, `wait_for=None`;
  - an old entry with only `type`/`url`/`name`/`enabled` loads (FR-003);
  - each row of the "Load-time errors" table yields exactly the documented `<loc>: <message>` through `validate_job` and exits 2 via `invio job validate`: `selector: "div["`, `wait_for: "div["`, `url_pattern: "("`, `url_pattern` with `mode: page`, `wait_for` with `render: static`, `mode: feed`, `render: browser`, empty strings for `selector`/`url_pattern`/`wait_for`, unknown key;
  - add a YAML round-trip case with all keys in `tests/test_job_yaml.py`.
- [X] T006 Extend `WebSource` in `src/invio/config/job.py` (data-model.md, research R11). Add, in this order after `enabled`:
  - `selector: str | None` — "min length 1; must be a valid CSS selector";
  - `mode: Literal["page", "links"] = "page"`;
  - `url_pattern: str | None` — "min length 1; must compile as a Python regular expression; only with `mode: links`";
  - `render: Literal["static", "js"] = "static"`;
  - `wait_for: str | None` — "min length 1; valid CSS selector; only with `render: js`".

  Add field validators: CSS selectors are checked by `selectolax.lexbor.LexborHTMLParser("").css(value)`, any exception → `ValueError("invalid CSS selector")`; the regex by `re.compile`, `re.error` → `ValueError(f"invalid regular expression: {exc}")`. Add a model validator for the two cross-field rules, with errors located at `url_pattern` / `wait_for` and messages `only allowed with mode: links` / `only allowed with render: js` (use `PydanticCustomError` or the existing pattern in the file so the location names the field). Update the class docstring. Do not import `invio.sources`. (Depends on T001)
- [X] T007 Regenerate `docs/job.schema.json` with `uv run python -m invio.config.job`, then confirm `uv run pytest tests/test_job_schema.py tests/test_job_sources.py tests/test_job_yaml.py` passes. (Depends on T005, T006)

**Checkpoint**: Config contract done; text helpers shared; RSS behaviour unchanged.

---

## Phase 3: User Story 1 - Detect when a tracked page's content changes (Priority: P1) 🎯 MVP

**Goal**: A `web` source without a selector returns one page candidate with a stable
`content_hash` over the normalized body text, plus title and teaser.

**Independent Test**: Serve a fixed HTML page from the loopback server, fetch twice: same
hash. Change a paragraph: different hash. Change only scripts, styles, comments, markup or
whitespace: same hash.

### Tests for User Story 1 ⚠️

- [X] T008 [P] [US1] Write `tests/test_web_text.py` for the pure page functions in `invio.sources.web`:
  - `region_text` over `body` excludes `script`/`style`/`noscript`/`template`/comments, applies NFC (composed and decomposed "é" give equal text) and collapses whitespace;
  - `content_hash` is `sha256(text.encode()).hexdigest()`, and empty text hashes `""`;
  - Hypothesis properties: inserting arbitrary whitespace between tags, adding attributes, wrapping words in `<span>`, or adding `<script>`/`<style>`/comments leaves the hash unchanged; changing any word of a paragraph changes it;
  - decoding: header charset beats `<meta charset>`, `<meta charset>` and `<meta http-equiv="Content-Type" content="…; charset=…">` within the first 1024 bytes are honoured, a BOM beats both, the default is UTF-8, and the same text in UTF-8 and ISO-8859-1 hashes equal (research R4);
  - `is_html`: `text/html` and `application/xhtml+xml` are accepted; with no Content-Type, `<!doctype html`/`<html` (case-insensitive, after BOM/whitespace) is accepted; `application/pdf` and `application/json` are rejected (R5).
- [X] T009 [P] [US1] Write `tests/test_source_web.py` (page mode, static, loopback server):
  - 10 fetches of an unchanged page (fresh client each time, so no 304) → one candidate each, all with the same `content_hash` (AC 1, SC-001);
  - a changed paragraph → different hash (AC 2);
  - candidate fields per data-model.md "Page mode": canonical configured URL even after a redirect, `url_hash`, collapsed `<title>` or fallback URL, `published_at=None`, `type="article"`, teaser ≤ 500 chars ending in `…` for long text and `None` for empty text, teaser excluded from the hash;
  - an empty body → a candidate with `content_hash == sha256(b"").hexdigest()` and `teaser=None`;
  - a second fetch answered with 304 (client validators) → `[]`;
  - a PDF or JSON response → `FetchError` with `reason == "not_html"`;
  - `isinstance(WebPageSource(client), Source)` holds.

### Implementation for User Story 1

- [X] T010 [US1] Create `src/invio/sources/web.py` with a module docstring describing the flow (fetch → decode → HTML check → strip non-text → region → text/hash/teaser → candidates). Implement the private helpers:
  - `_decode(result: FetchResult) -> str` (R4: BOM → header charset → meta prescan of the first 1024 bytes → UTF-8, via `FetchResult.text(encoding)`);
  - `_is_html(headers, content) -> bool` (R5);
  - `_parse(html) -> LexborHTMLParser` with `script, style, noscript, template` removed (`strip_tags` / `decompose`);
  - `_region_text(nodes) -> str` (each node's `.html` through `text.html_to_text`, joined by `" "`, `unicodedata.normalize("NFC", …)`, then `text.collapse`);
  - `_content_hash(text) -> str`;
  - `_title(tree, fallback) -> str`.

  Expose the names the T008 tests use (keep them module-private with a leading underscore, as `tests/test_source_rss.py` does for `_entry_date`). (Depends on T004)
- [X] T011 [US1] In `src/invio/sources/web.py`, implement `class WebPageSource` per contracts/python-api.md (`__init__(client, *, renderer=None)`, `fetch`, `aclose`, `__aenter__`/`__aexit__`). Static page mode in `fetch`:
  - `await client.get(str(config.url))`; `NotModified` → `[]`;
  - not HTML → `FetchError("not_html", url=…)`;
  - region = `body` (or the whole document if there is no body);
  - return `[Candidate(...)]` per data-model.md "Page mode" (`url=canonical_url(str(config.url))`, `teaser=text.teaser(region_text)`, `content_hash=_content_hash(region_text)`).

  Leave clear extension points for selector (US2), links (US3) and render (US4). `uv run pytest tests/test_web_text.py tests/test_source_web.py` must pass. (Depends on T010)

**Checkpoint**: MVP. Page change detection works for whole pages (acceptance criteria 1–2).

---

## Phase 4: User Story 2 - Track only the relevant region of a page (Priority: P1)

**Goal**: `selector` limits the fingerprint to the matching elements. No match is an error.

**Independent Test**: A page with a selected and a non-selected region. Editing inside the
selection changes the hash, editing outside does not.

### Tests for User Story 2 ⚠️

- [X] T012 [P] [US2] Add to `tests/test_source_web.py`:
  - with `selector`, a changed paragraph inside the region → different hash (AC 2);
  - changes only in header, sidebar, footer or scripts outside the region → same hash (AC 3);
  - a selector matching several elements → text of all matches in document order (swapping two matches' contents changes the hash);
  - a selector with no match → `FetchError` with `reason == "selector_not_found"`, and no fallback to body;
  - the title still comes from `<title>` when a selector is set.

### Implementation for User Story 2

- [X] T013 [US2] In `src/invio/sources/web.py`, add `_regions(tree, selector) -> list[Node]`: `body` when `selector` is `None`, else `tree.css(selector)` in document order; an empty match list raises `FetchError("selector_not_found", url=…)` (FR-009). Use it in `fetch` for page mode. (Depends on T011)

**Checkpoint**: Acceptance criteria 1–3 pass.

---

## Phase 5: User Story 3 - Discover linked articles on an index page (Priority: P2)

**Goal**: `mode: links` returns one candidate per qualifying link in the region.

**Independent Test**: An index page with article links, navigation, external, relative,
duplicate and `mailto:`/`javascript:`/`#` links. Only the expected canonical URLs come back,
in page order.

### Tests for User Story 3 ⚠️

- [X] T014 [P] [US3] Add links-mode tests to `tests/test_source_web.py`:
  - with `url_pattern`, only matching URLs, on any host, in page order (AC 4, SC-004);
  - without `url_pattern`, only same-host links (host compared case-insensitively; `www.` subdomain counts as a different host);
  - relative links are resolved against the final (post-redirect) URL, and against `<base href>` when present;
  - duplicates after canonicalization (tracking params, fragment) appear once, first wins;
  - the page itself (configured and final URL) is dropped;
  - `mailto:`, `javascript:`, `#frag`-only and hostless links are ignored;
  - with `selector`, only links inside the region count; a selector with no match → `selector_not_found`;
  - candidate fields per data-model.md "Links mode": collapsed link text or fallback URL, `teaser=None`, `content_hash=None`, `type="article"`;
  - a page with no qualifying links → `[]`.

### Implementation for User Story 3

- [X] T015 [US3] In `src/invio/sources/web.py`, implement `_links(tree, regions, *, final_url, page_urls, url_pattern) -> list[Candidate]` per research R6 / data-model.md filter pipeline:
  - `a[href]` per region in order; base = first `<base href>` resolved against `final_url`, else `final_url`;
  - `urljoin` → `canonical_url` → http(s) with hostname and valid port (move the check from `rss._entry_url` into a new `http_url_or_none(raw: str, base: str) -> str | None` in `src/invio/sources/urls.py`, and use it from both `rss.py` and `web.py`; `tests/test_source_rss.py` must stay green);
  - drop URLs in `page_urls`; keep if `re.search(url_pattern, url)` matches, or, without a pattern, if `urlsplit(url).hostname == urlsplit(final_url).hostname` (both lower-cased);
  - deduplicate, first wins.

  Dispatch on `config.mode` in `fetch`. (Depends on T013)

**Checkpoint**: Acceptance criteria 1–4 pass for static pages.

---

## Phase 6: User Story 4 - Track pages that need JavaScript (Priority: P3)

**Goal**: `render: js` renders the page in headless Chromium under the address guard.
Playwright is loaded only then.

**Independent Test**: Importing and using static sources never loads Playwright. With
Chromium, script-inserted text reaches the hash, and requests to private addresses are aborted.

### Tests for User Story 4 ⚠️

- [X] T016 [P] [US4] Write `tests/test_http_admission.py` for the new `SafeHttpClient` methods (contracts/python-api.md):
  - `check_target` raises `BlockedError` for `file:///etc/passwd`, `http://10.0.0.5/` and `http://127.0.0.1/` without `allow_networks`; it passes for loopback with `allow_networks=LOOPBACK`; it raises `FetchError("dns_failed")` with a failing fake resolver; it never fetches robots.txt or sends a request (assert the server recorded nothing);
  - `admission` raises `BlockedError(BLOCKED_BY_ROBOTS)` for a disallowed path before yielding; two `admission` blocks on the same origin are spaced by `host_interval` (use the clock/limiter technique of `tests/test_http_ratelimit.py`); failures are logged once.
- [X] T017 [P] [US4] Write `tests/test_source_web_render.py` (runs without Chromium):
  - **lazy import**: in a subprocess (`sys.executable -c …`), import `invio.sources.web`, `invio.config.job` and `invio.sources.rss`, run a static `WebPageSource.fetch` against an in-process loopback server, and assert `"playwright" not in sys.modules` (AC 5, SC-005);
  - **unavailable**: with `monkeypatch.setitem(sys.modules, "playwright", None)` (and `playwright.async_api`), a `render: js` fetch raises `FetchError` with `reason == "render_unavailable"`, a message mentioning `uv sync --extra render` and `playwright install chromium`, and a static fetch on the same `WebPageSource` still works;
  - **fake renderer**: inject a `PageRenderer` fake returning `RenderedPage(url, html)`; page mode, selector, `selector_not_found` and links mode all work on rendered HTML; the base URL for links is `RenderedPage.url`; `wait_for` is passed through; `aclose()` closes the renderer once; the renderer is created only once across fetches; HTML longer than `max_response_bytes` (encoded UTF-8) → `TooLargeError`.
- [X] T018 [P] [US4] Write `tests/test_source_web_browser.py`, every test marked `@pytest.mark.browser`, with a module fixture that skips when `PlaywrightRenderer.start` raises `render_unavailable`. Use the loopback server and `allow_networks=LOOPBACK`:
  - no test relies on wall-clock sleeps: timing is driven by the test server (a route that answers only when the test sets a `threading.Event`, or never), so results do not depend on machine speed (Constitution III);
  - text inserted by a script from the response of a `fetch('/data')` appears in the hash;
  - `wait_for` waits for an element the script adds only after a second, server-held request completes;
  - a page that polls a never-answering route (no network idle), or whose `wait_for` never appears, fails with `reason == "timeout"` (monkeypatch `RENDER_TIMEOUT_SECONDS` down to 2 s), and afterwards the browser has no open contexts (FR-018 resource release);
  - a sub-request (`<img>`, `fetch()`) to `http://10.255.255.1/` (or another private address outside `LOOPBACK`) is aborted, recorded as never reaching the network, and the page still renders;
  - a sub-request that redirects (302 from the loopback server) to a private address outside `LOOPBACK` is aborted at the redirect hop;
  - a page URL disallowed by robots.txt is never opened (server records no page request) → `blocked_by_robots`;
  - a main response of 404 → `reason == "http_status"`, `status == 404`;
  - a WebSocket to a private address is closed.

### Implementation for User Story 4

- [X] T019 [US4] In `src/invio/sources/http.py`, add the public `SafeHttpClient.check_target(url)` (parse, `_guard`, no robots, no slot) and `@asynccontextmanager admission(url)` (parse → `_guard` → `_check_robots` → `_slot(target)` held during the block). Both log failures once via `_log_failure`, re-raise `FetchError`, strip userinfo like `get()`, and have docstrings per contracts/python-api.md. Update the module docstring to mention them. (Depends on T016)
- [X] T020 [US4] In `src/invio/sources/web.py`, add `RenderedPage(NamedTuple)` (`url`, `html`), the `PageRenderer` Protocol, and the lazy wiring:
  - on the first `render: js` fetch, `from invio.sources import browser` inside a `try` (`ImportError` → `FetchError("render_unavailable", url=…)` raised as the new `RenderUnavailableError(url=…)` from `src/invio/sources/errors.py`, re-exported by `invio.sources.http`, per contracts/python-api.md);
  - then `await browser.PlaywrightRenderer.start(self._client)`, guarded by an `asyncio.Lock` so concurrent fetches start one renderer;
  - rendered path: `renderer.render(str(config.url), wait_for=config.wait_for)`, size check against the client's `max_response_bytes` → `TooLargeError`, then the same region/text/links code with `final_url=rendered.url`;
  - `aclose()` closes the renderer if started.

  T017 must pass. (Depends on T015, T017)
- [X] T021 [US4] Create `src/invio/sources/browser.py` with `PlaywrightRenderer` (research R8–R10). Requirements:
  - `start(client)` launches Chromium headless with `--force-webrtc-ip-handling-policy=disable_non_proxied_udp`; launch or import failure → `render_unavailable`;
  - `render(url, *, wait_for)`, all inside `asyncio.timeout(RENDER_TIMEOUT_SECONDS)` (`Final = 30.0`):
    - `async with client.admission(url)`;
    - new context (`user_agent=client`'s UA, `service_workers="block"`, `accept_downloads=False`, `java_script_enabled=True`);
    - `context.route("**/*", handler)`: `await client.check_target(request.url)` → on `FetchError` `route.abort("blockedbyclient")`, else `response = await route.fetch(max_redirects=0)` and `route.fulfill(response=response)`;
    - `context.route_web_socket("**/*", ws_handler)` with the same check, closing blocked sockets and `connect_to_server()` otherwise;
    - `page.goto(url, wait_until="networkidle", timeout=…)`; no response → `render_failed`; status ≥ 400 → `FetchError("http_status", status=…)`; then `wait_for_selector(wait_for, state="attached")` if set;
    - return `RenderedPage(page.url, await page.content())`;
    - always close the context in `finally`;
    - `TimeoutError` or Playwright `TimeoutError` → `FetchError("timeout")`;
  - `aclose()` closes browser and Playwright.

  Add the read-only `SafeHttpClient.user_agent` property (contracts/python-api.md) in `src/invio/sources/http.py` and use it for the context. T018 must pass with Chromium installed. (Depends on T019, T020)

**Checkpoint**: All five acceptance criteria and all four stories pass.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T022 [P] Update `README.md`:
  - document the `web` source keys (`selector`, `mode`, `url_pattern`, `render`, `wait_for`) with one page example and one links example;
  - the `render` extra installation (`uv sync --extra render`, `uv run playwright install chromium`);
  - the `selector_not_found` behaviour;
  - the DNS-rebinding limitation of `render: js` (plan Complexity Tracking).
- [X] T023 [P] Add `selector: "main"` to the `web` entry of `docs/job.example.yaml`, and confirm `tests/test_job_schema.py::test_example_validates_against_schema` and `uv run invio job validate docs/job.example.yaml` pass.
- [X] T024 Run all gates: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest --cov` (coverage ≥ 95 % with Chromium installed), plus `uv run pytest -m browser`. Fix any finding.
- [X] T025 Walk through `specs/008-gh-issue-12/quickstart.md` §2–§6 and tick the spec's acceptance criteria. In the PR description, justify `selectolax` (R1) and the optional `playwright` extra (R7), and note the DNS-rebinding limitation.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (T001–T002)**: none. T002 is independent of T001.
- **Foundational (T003–T007)**: T004 needs T003. T006 needs T001. T007 needs T005 and T006. Blocks all stories.
- **US1 (T008–T011)**: needs Foundational.
- **US2 (T012–T013)**: needs T011 (extends `fetch`).
- **US3 (T014–T015)**: needs T013 (uses `_regions`).
- **US4 (T016–T021)**: T016–T018 (tests) can start after Foundational. T019 is independent of US1–US3. T020 needs T015. T021 needs T019 and T020.
- **Polish (T022–T025)**: after the stories you ship. T024–T025 come last.

### Story dependencies

US1 → US2 → US3 build on the same `fetch` in `web.py`, so they run in sequence. Each one is
still testable on its own at its checkpoint. US4's client additions (T016, T019) can run in
parallel with US1–US3.

### Parallel opportunities

- T002 ∥ T001; T003 ∥ T005.
- T008 ∥ T009 (different test files).
- T012, T014 can be written alongside earlier implementation (same file `tests/test_source_web.py`, so coordinate or append sequentially).
- T016 ∥ T017 ∥ T018 (different test files), and T019 ∥ US1–US3 implementation.
- T022 ∥ T023.

### Parallel example: User Story 4

```text
Task: "T016 tests/test_http_admission.py"
Task: "T017 tests/test_source_web_render.py"
Task: "T018 tests/test_source_web_browser.py"
then: "T019 SafeHttpClient.check_target/admission"  (parallel with US1–US3 work in web.py)
```

---

## Implementation Strategy

### MVP first (User Story 1)

1. Phase 1 + Phase 2.
2. Phase 3 (US1): whole-page change detection. Stop and validate quickstart §3 (without `-k selector`).

### Incremental delivery

1. + US2 (selector) → acceptance criteria 1–3. This is the minimum shippable state for real-world pages.
2. + US3 (links mode) → acceptance criterion 4.
3. + US4 (JS rendering) → acceptance criterion 5 plus the browser guard.
4. Polish → docs, gates, PR.

Each checkpoint leaves the suite green, so the work can stop or be split into PRs at any one
of them.
