# Implementation Plan: Web Page Source with Change Detection

**Branch**: `gh-issue-12` | **Date**: 2026-10-05 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/008-gh-issue-12/spec.md`

## Summary

Add the `web` source adapter `WebPageSource` (`invio.sources.web`). It fetches a page through
`SafeHttpClient`, or optionally renders it in headless Chromium. It takes the readable text of
the `selector` region (or `<body>`) and reports either one page candidate with a SHA-256
`content_hash` and a teaser (`mode: page`), or the linked article URLs filtered by
`url_pattern` / same host (`mode: links`).

HTML parsing and CSS selection use `selectolax`. Text extraction is shared with the RSS source
(moved to `invio.sources.text`). Playwright is an optional extra, imported only for
`render: js`. Every browser request goes through the existing address guard, using two new
public `SafeHttpClient` methods. The `WebSource` config gains `selector`, `mode`,
`url_pattern`, `render` and `wait_for`, all validated when the job file is loaded. Details:
[research.md](research.md).

## Technical Context

**Language/Version**: Python 3.12+ (uv)

**Primary Dependencies**: existing: pydantic v2, httpx2 (only via `SafeHttpClient`), feedparser.
New runtime: `selectolax>=1.0`. New optional extra `render`: `playwright>=1.63` (also in the
dev group, for mypy and tests).

**Storage**: N/A. Candidates are returned, not stored; the existing `content_hash` field is reused.

**Testing**: pytest + pytest-asyncio + Hypothesis, the loopback HTTP test server
(`tests/http_helpers.py`), and a new `browser` marker for real-Chromium tests (skipped when
Chromium is unavailable; run in CI).

**Target Platform**: Linux/macOS, run unattended by cron/systemd.

**Project Type**: CLI application / library (single project).

**Performance Goals**: Parsing and hashing a 10 MB page (the client's size cap) takes well
under 1 s. Network time is bounded by the client's per-host interval and timeouts. A render
takes at most 30 s.

**Constraints**: No internet in tests. `invio.sources` must not import `httpx2`,
`urllib.request`, `invio.db` or `invio.services` (ruff TID251). Playwright must not be imported
unless `render: js` is fetched. Hashes must be deterministic across machines. mypy strict.

**Scale/Scope**: A few to a few dozen `web` sources per job; links mode returns at most the
links on one page, capped later by `max_items_per_source`.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How |
|-----------|--------|-----|
| I. Strict contracts at boundaries | ✅ | New `WebSource` fields are typed and validated at load time (selector, regex, cross-field rules); unknown keys are still rejected; `schema_version` unchanged (additive, optional keys); `Candidate` reused, not redeclared. Page HTML is untrusted input, handled defensively (encoding, size cap, not-HTML check). |
| II. CLI-first | ✅ | No new command. The new configuration is reached through job files (`invio job validate/create/edit`); configuration errors exit 2 via the existing error path. Like the RSS adapter (#11), `WebPageSource` itself is consumed by the run pipeline, which is wired to sources in a later M2 issue; until then it is exercised by tests only. |
| III. Test-covered behaviour | ✅ | Every acceptance criterion maps to tests (quickstart §3–6); rejection paths are covered; no internet; Chromium tests use the loopback server and run in CI. |
| IV. Quality gates mirror CI | ✅ | `uv.lock` updated; CI adds `playwright install --with-deps chromium`; no new `type: ignore` expected (selectolax and playwright ship types). |
| V. Secrets / observability | ✅ | URLs in errors go through the existing redaction (`FetchError`); `admission()` logs failures once, like `get()`; the browser context does not persist cookies or storage. |
| Layering | ✅ | `config` → selectolax only (no `invio.sources` import); `sources.web` → `sources.http` / `text` / `urls` / `domain`; `sources.browser` → playwright + `sources.http`; nothing imports upward. |
| New runtime dependencies justified | ✅ | selectolax: R1. playwright is optional only: R7. Both are to be repeated in the PR description. |
| Docs updated | ✅ | README (web source keys, `render` extra, DNS-rebinding note), `docs/job.example.yaml`, `docs/job.schema.json`. |

**Post-design re-check (after Phase 1)**: still passes. The two new public `SafeHttpClient`
methods reuse its private guard, robots and slot steps (no policy duplicated). The one
deliberate gap (browser DNS re-resolution) is documented in R8 and the README.

## Project Structure

### Documentation (this feature)

```text
specs/008-gh-issue-12/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── job-file.md      # web source keys and load-time errors
│   └── python-api.md    # WebPageSource, text helpers, renderer, SafeHttpClient additions
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
src/invio/
├── config/job.py            # WebSource: + selector, mode, url_pattern, render, wait_for (+ validators)
└── sources/
    ├── text.py              # NEW: collapse, html_to_text (+noscript), teaser, TEASER_MAX_CHARS (moved from rss.py)
    ├── rss.py               # imports the shared text helpers; re-exports TEASER_MAX_CHARS
    ├── http.py              # SafeHttpClient.check_target(), .admission()
    ├── web.py               # NEW: WebPageSource, PageRenderer protocol, decoding, region/text/hash, links
    └── browser.py           # NEW: PlaywrightRenderer (only module importing playwright)

tests/
├── test_source_text.py      # NEW: shared text helpers (collapse, html_to_text, teaser)
├── test_web_text.py         # NEW: normalization, hash, decoding, HTML check, Hypothesis properties
├── test_source_web.py       # NEW: static fetch via loopback server (page, selector, links, errors)
├── test_source_web_render.py# NEW: lazy import, render_unavailable, fake renderer (no Chromium needed)
├── test_source_web_browser.py# NEW: @browser real Chromium: rendering, wait_for, timeout, SSRF guard
├── test_http_admission.py   # NEW: check_target / admission (guard, robots, rate slot)
├── test_job_sources.py      # + new keys, validation errors, back-compat
└── test_source_rss.py       # unchanged behaviour after the text-helper move

docs/job.schema.json, docs/job.example.yaml, README.md   # updated
pyproject.toml / uv.lock     # selectolax; [project.optional-dependencies] render; dev: playwright; marker "browser"
.github/workflows/ci.yml     # install Chromium before pytest
```

**Structure Decision**: Single project, following the existing `src/invio/sources/` adapter
layout from #10/#11. A new module for the shared text helpers avoids `web` importing `rss`
internals. The Playwright code sits alone in `browser.py`, so the lazy-import guarantee is
checked at one import site.

## Implementation Notes (order)

1. `text.py` extraction refactor; RSS tests stay green.
2. `WebSource` fields and validators; schema regeneration; job tests.
3. `web.py` static path: decode → not-HTML check → strip non-text → region → text/hash/teaser
   → page candidate; then links mode.
4. `SafeHttpClient.check_target` / `admission` with tests.
5. `browser.py` renderer (route guard, redirect handling, WebSocket and service-worker
   blocking, 30 s budget), wired lazily into `WebPageSource`; lazy-import and unavailable
   tests; `browser`-marked Chromium tests; CI step.
6. README / docs.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Browser sub-requests are checked by DNS lookup, but not pinned to the checked address (DNS-rebinding window) | `render: js` must keep the browser away from internal targets (clarification Q1), and Playwright offers no address pinning | A filtering local proxy for all browser traffic would close the window, but adds TLS-aware proxy code far beyond this issue. The feature is opt-in, and the gap is documented. |
| Playwright in the dev dependency group, as well as the optional extra | mypy strict over `src/` must resolve `invio.sources.browser`, and the browser guard must be tested for real in CI | Omitting `browser.py` from type checking or coverage would leave the security-relevant code unchecked |
