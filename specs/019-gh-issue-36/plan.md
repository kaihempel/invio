# Implementation Plan: Source Discovery Command

**Branch**: `gh-issue-36` | **Date**: 2026-10-09 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/019-gh-issue-36/spec.md`

## Summary

Add `invio source discover URL [--add-to JOB] [--pick N]`. Given a website address it finds
RSS/Atom feeds, sitemaps and YouTube channels/playlists, validates each by fetching and parsing
it, and prints numbered results with title, newest-entry date and a paste-ready YAML snippet.
With `--add-to`, one result is appended to an existing job and the whole job is validated again
before it is saved.

- **Discovery engine.** A new adapter module, `invio.sources.discover`, collects findings from
  five places: the entered address itself, `<link rel="alternate">` feeds on the page, four
  probe paths at the site root and in the page's folder, `Sitemap:` lines in `robots.txt`, and
  YouTube links on the page.
- **Validation.** Every finding is validated through the shared `SafeHttpClient` and the
  existing parsers (feedparser, the defusedxml sitemap parser, the yt-dlp YouTube adapter).
  Results are classified by their content, de-duplicated by final URL or YouTube identity, and
  sorted with comment feeds last.
- **Adapter additions.** The adapters gain small public "describe" helpers: `describe_feed`,
  `describe_sitemap` and `YoutubeSource.describe`. `robots.txt` parsing keeps `Sitemap:` lines.
- **Saving to a job.** `JobService.append_source` does the read-check-append-validate-save in
  one unit of work.

## Technical Context

**Language/Version**: Python 3.12+ (uv-managed)

**Primary Dependencies**: All of these already exist; no new runtime dependency.
- Typer, plus questionary through the existing `Prompter`.
- `httpx2` through `SafeHttpClient`.
- feedparser, defusedxml, selectolax and yt-dlp.
- Pydantic v2 and PyYAML.

**Storage**: The existing `jobs` table via `JobService`. No schema change.

**Testing**: pytest.
- `SafeHttpClient` runs with the existing mock transport (`RecordingTransport`), `FakeResolver`
  and loopback server from `tests/http_helpers.py`.
- `YoutubeSource(extract=fake)`.
- `typer.testing.CliRunner` with monkeypatched seams, and the SQLite job store.
- New fixtures go in `tests/fixtures/discovery/`. No internet is used.

**Target Platform**: Linux server and developer machines (CLI).

**Project Type**: Single-project CLI tool (`src/invio`, `tests`).

**Performance Goals**: SC-005: a typical site (1 page, up to 8 probes, robots.txt, a few feeds)
finishes in ≤ 30 s with the default 1 s per-host interval. Findings are validated concurrently.

**Constraints**:
- Every request must go through the SSRF guard, the robots.txt policy and the rate limit
  (FR-012).
- Caps: 20 announced feeds, 20 YouTube links, 5 `robots.txt` sitemaps (FR-013).
- `--add-to` must check that the job exists before any network access (FR-014).
- No partial writes.

**Scale/Scope**: One site per invocation. At most about 50 HTTP requests per run plus up to 20
YouTube listings.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How |
|---|---|---|
| I. Strict contracts at boundaries | ✅ | The URL argument is parsed into `DiscoveryTarget` before anything else happens. Remote bodies are parsed by the existing hardened parsers. Every offered source is built as a `SourceConfig` model, and the job is re-validated with `validate_job`. There is no second source schema. |
| II. CLI-first | ✅ | `src/invio/cli/commands/source.py` is auto-discovered, so no other file is edited for registration. Results go to stdout and diagnostics to stderr. Exits are non-zero on failure, and configuration or validation errors exit 2 (contract). |
| III. Test-covered behaviour | ✅ | Each acceptance criterion maps to a named test (quickstart §2), rejection paths included. No network, real YouTube or production database is used. |
| IV. Quality gates mirror CI | ✅ | ruff, ruff format, strict mypy and pytest. No new dependencies; `uv.lock` is unchanged. |
| V. Secrets and observability | ✅ | No credentials are involved. The YouTube proxy and cookies are reused from settings without being logged. URLs in output are redacted, as in `FetchError`. The job is logged as `job.updated` with no contents. |
| Layering | ✅ | `discover.py` (sources) imports only `sources.*` and `config.job`. The CLI imports `sources` and `services` but never `db` (`test_cli_layering`). The YouTube factory moves from `pipeline.deps` down into `sources.youtube`, so the CLI does not depend on the pipeline. |
| Docs | ✅ | The README's CLI section gets `invio source discover` (FR-020). |

There are no violations, so Complexity Tracking is empty. The post-design re-check below also
passes.

## Project Structure

### Documentation (this feature)

```text
specs/019-gh-issue-36/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── cli-source-discover.md
│   └── python-api.md
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks (not created here)
```

### Source Code (repository root)

```text
src/invio/
├── cli/commands/
│   └── source.py            # NEW  `invio source discover`; seams; output; selection; --add-to
├── sources/
│   ├── discover.py          # NEW  target parsing, findings, validation, ordering, report
│   ├── rss.py               # +FeedSummary, describe_feed()
│   ├── sitemap.py           # +SitemapSummary, describe_sitemap()
│   ├── robots.py            # RobotsPolicy.sitemaps; parse `Sitemap:` lines
│   ├── http.py              # +SafeHttpClient.robots_sitemaps()
│   ├── youtube.py           # +ListingSummary, YoutubeSource.describe(), .from_settings()
│   └── web.py               # base-URL helper shared with discover (no behaviour change)
├── services/jobs.py         # +SourceExistsError, JobService.append_source()
└── pipeline/deps.py         # use YoutubeSource.from_settings (no behaviour change)

tests/
├── fixtures/discovery/      # NEW  pages, feeds, sitemaps, robots.txt variants
├── test_discover.py         # NEW  engine: US1, US2, US4, edge cases
├── test_discover_parsers.py # NEW  describe_feed / describe_sitemap / robots sitemaps / describe
├── test_cli_source.py       # NEW  CLI: output, exit codes, --add-to, --pick, prompt
└── test_db_jobs.py          # +append_source cases (or test_jobs_append_source.py)
README.md                    # CLI docs
```

**Structure Decision**: This is the existing single-project layout. The engine is an adapter in
`invio.sources`, the job write goes in the service, and presentation and selection go in the
CLI command module.

## Implementation Notes (for /speckit-tasks)

1. **Foundations first**, each with its own tests:
   - `describe_feed` and `describe_sitemap`.
   - `RobotsPolicy.sitemaps` and `robots_sitemaps`.
   - `YoutubeSource.describe` and `from_settings`, then switch `pipeline/deps.py` over to it.
   - `JobService.append_source`.
2. **The engine (`discover.py`)**, in this order:
   - `parse_target`;
   - the start page (classify the content; parse the HTML for alternate links and YouTube links);
   - probe URLs (root, then folder, de-duplicated);
   - `robots.txt` sitemaps;
   - concurrent validation with `asyncio.gather` (wrap every finding so each failure becomes a
     `Rejection`);
   - de-duplication by final identity, comment detection, sorting, and caps with "skipped" notes.
3. **CLI** (`source.py`):
   - validate options;
   - pre-check the `--add-to` job (`mapped_errors(config_exit=2)`);
   - run discovery with `asyncio.run` inside `_discovery_deps()` (client and YouTube adapter,
     both closed on exit);
   - print the results;
   - pick a source (`--pick`, a single source when not interactive, or the prompt);
   - call `append_source`, mapping `SourceExistsError` to exit 0 and `JobConfigError` to exit 2.
4. **Snippet rendering**: dump `source.model_dump(mode="json", exclude_defaults=True)` as a
   one-item YAML list with `yaml.safe_dump`, indented under the summary. A test loads every
   snippet back through `validate_job`.
5. **US order for MVP**:
   - US1 + US2 (engine + printing) are the P1 MVP;
   - US3 (`--add-to`) next;
   - US4 (YouTube) last. It plugs into the same finding and validation path.

## Risks

- **feedparser leniency.** feedparser may report a version for HTML that contains RSS-like tags.
  Mitigation: also require a non-broken parse. The fixture "HTML at `/feed`" must be rejected.
- **YouTube handle validation needs network** in production (yt-dlp). If it is slow, the
  adapter's timeout bounds it. Results are only shown, and a failure means "not offered".
- **Concurrent edit during `--add-to`.** The read-modify-write runs in one session
  transaction. The last writer wins at the row level, the same as `update`.
- **Cross-host `robots.txt` sitemaps** (CDN) are allowed. They are still subject to the address
  guard and to their own host's robots policy.

## Complexity Tracking

No constitution violations.
