---

description: "Task list for gh-issue-36: invio source discover"
---

# Tasks: Source Discovery Command

**Input**: Design documents from `/specs/019-gh-issue-36/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/cli-source-discover.md, contracts/python-api.md, quickstart.md

**Tests**: Required — constitution principle III demands an automated test for every acceptance criterion, including rejection paths. Tests use no network: `SafeHttpClient` with the mock transport / `FakeResolver` / `LoopbackServer` from `tests/http_helpers.py`, `YoutubeSource(extract=fake)` (see `tests/youtube_helpers.py`), and `CliRunner` + SQLite like `tests/test_cli_job*.py`. Write each story's tests first and see them fail.

**Organization**: Tasks are grouped by user story (spec.md) so each story can be implemented and tested on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1–US4 from spec.md
- Paths are relative to the repository root (single project: `src/invio/`, `tests/`)

---

## Phase 1: Setup

**Purpose**: Branch, fixtures and empty modules every story builds on

- [X] T001 Create and switch to branch `gh-issue-36` from current `main` (constitution: Development Workflow); all following work is committed there, never on `main`
- [X] T002 Create fixture directory `tests/fixtures/discovery/` with: `page_rss_link.html` (head has `<link rel="alternate" type="application/rss+xml" title="Blog" href="/blog/feed.xml">`), `page_atom_and_rss.html` (one RSS and one Atom `<link>`, one relative, one absolute, plus a `<base href>` variant), `page_plain.html` (no alternate links, no YouTube links), `page_comments.html` (main feed `/feed/` and `<link … title="Example » Comments Feed" href="/comments/feed/">`), `feed_rss.xml` (title "Example Blog", 3 dated entries, newest 2026-10-01), `feed_atom.xml`, `feed_empty.xml` (valid RSS channel, no items), `feed_broken.xml` (truncated XML, no usable entries), `not_a_feed.html` (HTML served at a feed path), `sitemap_urlset.xml` (3 `<url>` with `lastmod`, newest 2026-09-30), `sitemap_index.xml` (2 child `<sitemap>`), `robots_sitemaps.txt` (`Sitemap: https://example.com/sitemap_index.xml`), `robots_many_sitemaps.txt` (7 `Sitemap:` lines)
- [X] T003 [P] Create `src/invio/sources/discover.py` with module docstring (purpose, layering: imports only `invio.sources.*` and `invio.config.job`), empty `__all__`, and the constants from contracts/python-api.md: `MAX_ANNOUNCED_FEEDS: Final = 20`, `MAX_YOUTUBE_LINKS: Final = 20`, `MAX_ROBOTS_SITEMAPS: Final = 5`, `PROBE_PATHS: Final = ("feed", "rss", "atom.xml", "sitemap.xml")`
- [X] T004 [P] Create `src/invio/cli/commands/source.py` with `app = typer.Typer(help="Find and add sources.", no_args_is_help=True)` (auto-registered as `invio source`, root callback does runtime setup) and the test seams `_make_service() -> JobService` (`JobService.from_settings()`), `_make_prompter() -> Prompter` (`QuestionaryPrompter()`), `_is_interactive() -> bool` (`sys.stdin.isatty() and sys.stdout.isatty()`); `tests/test_cli_layering.py` already scans every CLI module for `invio.db` imports, so no test change is needed

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Types, target parsing, client/adapter wiring and the HTML base-URL helper used by every story

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T005 [P] Write tests for `parse_target` and `DiscoveryTarget.rebased` in `tests/test_discover.py`: `example.com` → `https://example.com/`; `https://example.com/blog/post-1` → root `https://example.com/`, folder `https://example.com/blog/`; `https://example.com/about` and `https://example.com/` → folder `None`; port kept in root; `ftp://x`, `file:///etc/passwd`, `https://user:pw@example.com/`, `""`, `"   "`, `https://` (no host) raise `ValueError` with a message
- [X] T006 Implement in `src/invio/sources/discover.py` the frozen dataclasses from data-model.md: `DiscoveryTarget(url, root, folder)` with `rebased(final_url) -> DiscoveryTarget` (same root/folder rules applied to the start page's final URL), `FindingOrigin` (IntEnum `DIRECT=0, ANNOUNCED=1, PROBED=2, ROBOTS=3, LINKED=4`), private `_Finding(kind: Literal["feed_or_sitemap","youtube_channel","youtube_playlist"], locator, origin, position, hint_title)`, `DiscoveredSource(source: SourceConfig, title: str | None, entry_count: int, newest: datetime | None, sitemap_index: bool, is_comment_feed: bool, origin, position, final_url)`, `Rejection(locator, reason, status: int | None)`, `DiscoveryReport(target, sources: tuple[DiscoveredSource, ...], page_problem: Rejection | None, rejected: tuple[Rejection, ...], skipped: tuple[str, ...])`; and `parse_target(raw: str) -> DiscoveryTarget` per data-model.md ("absolute `http`/`https`; a scheme-less input such as `example.com` gets `https://`"; folder = "`url` path up to its last `/` when that is not `/`, else `None`"; reject non-http(s) scheme, missing host, credentials, whitespace-only — no network)
- [X] T007 [P] Move `_youtube_source` from `src/invio/pipeline/deps.py` to a classmethod `YoutubeSource.from_settings(cls, settings: Settings) -> Self` in `src/invio/sources/youtube.py` (same cookies/proxy/timeout handling, proxy secret unwrapped) and call it from `default_deps`; keep `tests/test_pipeline_deps.py` green and add one test in `tests/test_source_youtube.py` that `from_settings` passes cookies file, proxy and timeout through
- [X] T008 [P] Extract the `<base href>` rule from `src/invio/sources/web.py` (`_base_url`) into a public `html_base_url(tree: LexborHTMLParser, final_url: str) -> str` (in `web.py`, exported in `__all__`) and use it from `_links`; behaviour unchanged (existing `tests/test_source_web*.py` stay green)
- [X] T009 Add `_discovery_deps() -> AbstractAsyncContextManager[tuple[SafeHttpClient, YoutubeSource]]` to `src/invio/cli/commands/source.py`: builds `SafeHttpClient(HttpClientConfig.from_settings())` and `YoutubeSource.from_settings(get_settings())`, yields both, closes the client (`async with`) and calls `youtube.close()` in `finally` (depends on T007)

**Checkpoint**: Foundation ready — user stories can start

---

## Phase 3: User Story 1 - Discover the feed announced by a website (Priority: P1) 🎯 MVP

**Goal**: `invio source discover URL` finds announced RSS/Atom feeds (and a feed entered directly), validates them, and prints numbered entries with title, newest entry date and a paste-ready YAML snippet.

**Independent Test**: Serve `page_rss_link.html` and `feed_rss.xml` through the mock transport; run discovery and the CLI on the page; exactly one validated `rss` candidate with title "Example Blog", newest entry 2026-10-01 and a snippet that `validate_job` accepts as a source.

### Tests for User Story 1 ⚠️

- [X] T010 [P] [US1] Tests for `describe_feed` in `tests/test_source_rss.py`: RSS 2.0 and Atom fixtures return `FeedSummary(title, entry_count, newest)` with newest = max of `published` else `updated`; `feed_empty.xml` → `FeedSummary(title, 0, None)`; `feed_broken.xml`, `not_a_feed.html` and `b""` → `None`; title HTML is stripped/collapsed
- [X] T011 [P] [US1] Engine tests in `tests/test_discover.py`: `test_discover_announced_rss_link_is_offered` (issue AC1: page with RSS `<link>` → one `rss` `DiscoveredSource`, absolute URL resolved from relative href, origin `ANNOUNCED`); RSS + Atom both offered as `rss` (US1-2); `<base href>` respected (US1-3); `test_discover_drops_unreachable_and_invalid_findings` (issue AC4: announced feeds answering 404, 500, timeout, HTML body, broken XML and a redirect to `http://10.0.0.5/` and a body above the client size limit (`too_large`) are absent from `sources` and present in `rejected` with reasons `http_status`/`timeout`/`not_a_feed_or_sitemap`/`non_public_address`/`too_large`) (US1-4); direct feed URL entered → one `DIRECT` candidate, no HTML parsing (US1-5); comment feed listed last with `is_comment_feed=True` (US1-6); a start page on a private address → `page_problem` with `non_public_address`; more than 20 announced feeds → only 20 checked and a `skipped` note; same feed announced twice / redirecting to the same final URL → listed once
- [X] T012 [P] [US1] CLI tests in `tests/test_cli_source.py` (monkeypatch `_discovery_deps` to yield a client on the mock transport and a fake-extract `YoutubeSource`): output format per contracts/cli-source-discover.md (`[1] rss  <url>`, `title: … · newest entry: 2026-10-01 · found: announced`, indented YAML list item); every printed snippet loads through `validate_job` as a job source (SC-003); `no entries` and `unknown` date rendering; `(comments)` marker; rejected findings on stderr as `Not offered: <url>: <reason>`; exit 0 when ≥1 source; "No source found for …" on stderr and exit 1 when none; `ftp://x` and `https://` exit 2 with no request recorded by the transport; URLs in output redacted (no credentials, no query string in stderr)

### Implementation for User Story 1

- [X] T013 [P] [US1] Implement `FeedSummary` and `describe_feed(content: bytes) -> FeedSummary | None` in `src/invio/sources/rss.py` reusing `_parse` and `_entry_date`; rule: "None unless feedparser recognises a feed version and the parse is not broken (same rule as RssFeedSource.fetch); an empty well-formed feed is a FeedSummary(…, 0, None)"; title from `feed.title` via `collapse(html_to_text(...))`, empty → `None`; add to `__all__`
- [X] T014 [US1] Implement in `src/invio/sources/discover.py`: start-page fetch via `client.get(target.url, conditional=False)`; classify body (feed → `DIRECT` finding already validated; HTML per `web._is_html` → parse with `LexborHTMLParser`; else `page_problem` `not_a_feed_or_sitemap`); collect `link[rel~=alternate][href]` with `type` `application/rss+xml` or `application/atom+xml` (case-insensitive, parameters ignored) resolved via `html_base_url` + `http_url_or_none`, keeping the `title` attribute as `hint_title`, capped at `MAX_ANNOUNCED_FEEDS` with a `skipped` note; any `FetchError` on the start page becomes `page_problem` (depends on T006, T008, T013)
- [X] T015 [US1] Implement validation of URL findings in `src/invio/sources/discover.py`: `client.get(url, conditional=False)`, `describe_feed` on the body → `DiscoveredSource(source=RssSource(type="rss", url=final_url), …)`; otherwise `Rejection(reason="not_a_feed_or_sitemap")`; every `FetchError` → `Rejection(redacted url, error.reason, error.status)`; run all findings concurrently with `asyncio.gather`; never raise `FetchError` out of `discover` (depends on T014)
- [X] T016 [US1] Implement `is_comment_feed(final_url: str, *titles: str | None) -> bool` (path ends with `/comments/feed`, `/comments/feed/` or `/comments/`, or any title contains "comments", case-insensitive), de-duplication by `(source.type, canonical_url(final_url))` keeping the lowest `(origin, position)`, ordering by `(is_comment_feed, origin, position)`, and the public `async def discover(target, *, client, youtube) -> DiscoveryReport` in `src/invio/sources/discover.py`; export public names in `__all__` (depends on T015)
- [X] T017 [US1] Implement the `discover` command in `src/invio/cli/commands/source.py`: argument `URL`; `parse_target` → `typer.BadParameter` (exit 2) on `ValueError`; `asyncio.run` of `discover` inside `_discovery_deps()`; render each source as in contracts/cli-source-discover.md (dates `YYYY-MM-DD` UTC, `unknown`, `no entries`, `sitemap index: N sitemaps`, `(comments)` marker, `found: <origin>`), snippet = `yaml.safe_dump([source.model_dump(mode="json", exclude_defaults=True)], sort_keys=False)` indented 4 spaces; stderr lines for `page_problem` (`Start page not usable: …`), each `Rejection` (`Not offered: …`), each `skipped` note; "No source found for <url> (checked: page links, common locations, robots.txt sitemaps, YouTube links)." + exit 1 when `sources` is empty (depends on T009, T016)

**Checkpoint**: US1 is a working MVP — announced feeds are found, validated and printed

---

## Phase 4: User Story 2 - Find feeds and sitemaps at common locations (Priority: P1)

**Goal**: Probe `feed`, `rss`, `atom.xml`, `sitemap.xml` at the site root and the page's folder, take up to 5 `Sitemap:` entries from `robots.txt`, classify every result by content (feed or sitemap).

**Independent Test**: Serve `page_plain.html` at `/` and `sitemap_urlset.xml` at `/sitemap.xml` (404 elsewhere); discovery offers exactly one `sitemap` candidate with 3 URLs and newest 2026-09-30.

### Tests for User Story 2 ⚠️

- [X] T018 [P] [US2] Tests for `describe_sitemap` in `tests/test_source_sitemap.py`: urlset → `SitemapSummary(is_index=False, entry_count=3, newest=2026-09-30)`; index → `is_index=True`, `entry_count=2`, children never fetched; gzip body works; inflated size beyond `limit` → `None` or `too_large` handled as not-a-sitemap; RSS body, HTML body, malformed XML, DTD/entity payload → `None`
- [X] T019 [P] [US2] Tests for `robots.txt` sitemaps in `tests/test_http_robots.py`: `parse_robots` collects `Sitemap:` lines (case-insensitive field, inside or outside groups, file order, absolute http(s) only, others dropped) into `RobotsPolicy.sitemaps`; `SafeHttpClient.robots_sitemaps(url)` returns them, reuses the cached policy (one robots.txt request total when followed by a `get` on the same origin), returns `()` for 404/5xx/blocked robots.txt and also works with `respect_robots=False`
- [X] T020 [P] [US2] Engine tests in `tests/test_discover.py`: `test_discover_probes_sitemap_xml_at_root` (issue AC2, US2-1); sitemap index at `/sitemap.xml` offered (US2-2); 404 / 500 / HTML / unreadable at probe paths not offered (US2-3); feed both announced and at `/feed` (same final URL) listed once as `ANNOUNCED` (US2-4); deep page `https://example.com/blog/post-1` probes `https://example.com/{feed,rss,atom.xml,sitemap.xml}` then `https://example.com/blog/{…}` in that order (US2-5); root page probes each path once (US2-6); `robots_sitemaps.txt` with nothing at `/sitemap.xml` → `sitemap_index.xml` offered with origin `ROBOTS` (US2-7); `robots_many_sitemaps.txt` → 5 checked, `skipped` note for 2 (US2-8); a sitemap served at `/feed` is classified `sitemap`; start page 500 still probes (from the entered URL) and finds the sitemap; start page `https://example.com/` redirecting to `https://www.example.com/home/` probes `https://www.example.com/{…}` and `https://www.example.com/home/{…}` and reads `www.example.com` robots.txt; ordering direct < announced < probed (root before folder, path order) < robots
- [X] T021 [P] [US2] CLI tests in `tests/test_cli_source.py`: sitemap entry renders `sitemap: 3 URLs · newest entry: 2026-09-30 · found: probed` and an index renders `sitemap index: 2 sitemaps`; numbering follows the engine order; snippet `- type: sitemap\n  url: …` validates

### Implementation for User Story 2

- [X] T022 [P] [US2] Implement `SitemapSummary` and `describe_sitemap(content: bytes, *, url: str, limit: int) -> SitemapSummary | None` in `src/invio/sources/sitemap.py` reusing `_parse` (catch `FetchError` → `None`), `_children_text`, `_parse_lastmod`; "None when the body is not a urlset/sitemapindex (malformed XML included); limit bounds gzip inflation (client.max_response_bytes). Children are not fetched."; add to `__all__`
- [X] T023 [P] [US2] Add `sitemaps: tuple[str, ...] = ()` to `RobotsPolicy` and collect `Sitemap:` lines in `parse_robots` in `src/invio/sources/robots.py` (RFC 9309 §2.3.5, group-independent; keep only absolute http(s) URLs); add `async def robots_sitemaps(self, url: str) -> tuple[str, ...]` to `SafeHttpClient` in `src/invio/sources/http.py` using `self._robots.policy(origin)` after `_guard` (refused/failed → `()`), independent of `respect_robots`
- [X] T024 [US2] Extend `src/invio/sources/discover.py`: probe findings (`PROBED`) for `PROBE_PATHS` under the root, then under the folder if set, of `target.rebased(start_page.final_url)` when the start page loaded, else of `target` (research R5), de-duplicated by absolute URL, independent of start-page success; `ROBOTS` findings from `client.robots_sitemaps(<same base>.root)`, first `MAX_ROBOTS_SITEMAPS` with a `skipped` note for the rest; classification order in validation: `describe_feed` → `RssSource`, else `describe_sitemap(content, url=final_url, limit=client.max_response_bytes)` → `SitemapSource(type="sitemap", url=final_url)` with `sitemap_index` set, else `not_a_feed_or_sitemap`; a start page that is itself a sitemap becomes a `DIRECT` sitemap candidate (depends on T016, T022, T023)
- [X] T025 [US2] Extend rendering in `src/invio/cli/commands/source.py` for sitemaps (`sitemap: N URLs` / `sitemap index: N sitemaps`, newest date) (depends on T017, T024)

**Checkpoint**: US1 + US2 — feeds and sitemaps are found via announcements, probes and robots.txt

---

## Phase 5: User Story 3 - Add a discovered source to an existing job (Priority: P2)

**Goal**: `--add-to JOB [--pick N]` appends one validated source to an existing job, re-validates the whole job, and saves only if valid.

**Independent Test**: With a stored valid job and a fixture site offering one feed, run `invio source discover … --add-to news` non-interactively; the job's last source is the new `rss` source, all other settings unchanged, exit 0.

### Tests for User Story 3 ⚠️

- [X] T026 [P] [US3] Service tests in `tests/test_job_service.py` for `JobService.append_source`: appends as last source and persists; other config fields unchanged; `next_run_at` recalculated for enabled jobs like `update`; `JobNotFoundError` for a missing job; `StoredJobConfigError` for an invalid stored config (nothing written); `SourceExistsError` when a source of the same type has the same `canonical_url` (e.g. trailing tracking param) or same YouTube `(kind, token)` (handle case-insensitive); `JobConfigError` when the result fails validation (the job model has no source-count limit, so simulate by monkeypatching `invio.services.jobs._validate` to raise `JobConfigError` on the second call) and the stored config is byte-for-byte unchanged (SC-004); one `job.updated` log line without config contents
- [X] T027 [P] [US3] CLI tests in `tests/test_cli_source.py` (SQLite `JobService` via `_make_service`, `FakePrompter` via `_make_prompter`, `_is_interactive` patched): `test_cli_add_to_appends_and_revalidates` (issue AC3, US3-1); interactive select with several candidates picks one, "Cancel" and Ctrl+C (`FakePrompter` running out of answers) → `Nothing added to job '<job>'.` exit 0 (US3-2, FR-019); non-interactive with 2 candidates and no `--pick` → list + "use --pick N", job unchanged, exit 1 (US3-3); non-interactive with exactly 1 candidate → added, exit 0 (US3-4); `--pick 2` adds candidate 2 without prompting, `--pick 9` with 2 candidates → message naming `1–2`, unchanged, exit 1 (US3-5); unknown job and a job name violating the naming rules (e.g. `"../x"`) → exit 1 and the transport recorded zero requests (US3-6, FR-014, edge case "Job names"); invalid stored job → exit 2, zero requests; source already present → `Job '<job>' already contains this source; nothing changed.` exit 0 (US3-7); `test_cli_add_to_invalid_result_is_not_saved` (monkeypatch `invio.services.jobs._validate` as in T026) → validation errors naming the field on stderr, exit 2, unchanged (US3-8); no candidates → job unchanged, exit 1 (US3-9); `--pick` without `--add-to` and `--pick 0` → exit 2 with no network

### Implementation for User Story 3

- [X] T028 [US3] Implement `SourceExistsError(name: str, source_type: str)` (picklable like the other service errors, exported) and `JobService.append_source(self, name: str, source: SourceConfig) -> JobRecord` in `src/invio/services/jobs.py`: one `session_scope`; `_require` the job; build `JobConfig` via `_record(job).config` (raises `StoredJobConfigError`); duplicate check ("locator equality: `canonical_url(url)` for `rss`/`sitemap`, parsed `(kind, token)` for YouTube (handles case-insensitive)"); append `source.model_dump(mode="json", exclude_defaults=True)` to the stored `sources`; `_validate` the new dict (`JobConfigError`, nothing written); `_apply_update`; `_log_change("job.updated", name)` (`invio.services` may import `invio.sources.urls` and `invio.config.job` parsers — check `tests/test_*layering*.py` and keep imports downward)
- [X] T029 [US3] Add options `--add-to JOB` and `--pick N` (`min=1`) to `discover` in `src/invio/cli/commands/source.py`: `--pick` without `--add-to` → `typer.BadParameter` (exit 2); before discovery, under `mapped_errors(config_exit=2)`, call `_make_service().get_by_name(job)` (missing → exit 1, invalid stored config → exit 2, missing DB URL → exit 2) (depends on T017)
- [X] T030 [US3] Implement selection and append in `src/invio/cli/commands/source.py`: no sources → unchanged, exit 1; `--pick N` out of `1..n` → stderr `--pick must be between 1 and <n>`, exit 1; else `--pick`, else non-interactive single source, else non-interactive multiple → stderr "several sources found; choose one with --pick N", exit 1; else interactive `Prompter.select` over `"[i] <type> <locator>"` + `"Cancel"` (`WizardAborted`/Cancel → `Nothing added …`, exit 0); then `append_source` under `mapped_errors(config_exit=2)`; `SourceExistsError` → "already contains" line, exit 0; success → `Added [i] <type> <locator> to job '<job>'.` (depends on T028, T029)

**Checkpoint**: US1–US3 — discovered sources can be added to jobs safely

---

## Phase 6: User Story 4 - Discover YouTube channels and playlists linked from a site (Priority: P3)

**Goal**: Recognise YouTube channel/playlist links on the page (or entered directly), validate them with the YouTube adapter, and offer them as `youtube_channel` / `youtube_playlist` sources.

**Independent Test**: Serve a page linking to `https://www.youtube.com/@somecreator/videos` and `https://www.youtube.com/playlist?list=PL123`, with `YoutubeSource(extract=fake)`; one `youtube_channel` and one `youtube_playlist` candidate with titles, snippets validate.

### Tests for User Story 4 ⚠️

- [X] T031 [P] [US4] Tests for `YoutubeSource.describe` in `tests/test_source_youtube.py`: uses the injected extractor with `playlistend == 5`; title from `info["title"]`, falling back to `channel`/`uploader`; `entry_count` and `newest` from the listing (undated → `None`); extractor failure/timeout raise `FetchError` like `fetch`; `RuntimeError` after `close()`
- [X] T032 [P] [US4] Engine tests in `tests/test_discover.py`: handle link → `youtube_channel` with `channel_id="@somecreator"` and title (US4-1); playlist URL and `watch?v=…&list=PL…` → `youtube_playlist` (US4-2); `/@x`, `/@x/videos`, `/@X` and channel id link of the same channel collapse to one per identity (handle case-insensitive) (US4-3); lone video link, `/results`, a non-YouTube host, an extractor error and an empty listing are not offered (US4-4); direct entry of a channel or playlist URL → `DIRECT` candidate, zero HTTP requests recorded (no page fetch, probes or robots.txt) (US4-5); more than 20 YouTube links → 20 checked + `skipped` note; YouTube candidates ordered after robots sitemaps
- [X] T033 [P] [US4] CLI test in `tests/test_cli_source.py`: YouTube entries render `[n] youtube_channel  @somecreator` with title and `found: linked`; snippet (`channel_id: '@somecreator'`) validates; `--add-to` with a YouTube candidate appends it

### Implementation for User Story 4

- [X] T034 [P] [US4] Implement `ListingSummary` and `async def describe(self, config, /) -> ListingSummary` in `src/invio/sources/youtube.py`, sharing the extraction path of `fetch` (factor a private `_extract_info(locator, max_items)` used by both; same timeout, cookies, proxy, error classification), `playlistend=5`, newest from `_videos(...)` dates
- [X] T035 [US4] Extend `src/invio/sources/discover.py`: collect `a[href]` and `link[href]` on the start page whose host is in the YouTube host set, try `parse_youtube_channel` then `parse_youtube_playlist` (ignore `ValueError`), identity `(kind, token)` with handles lower-cased, cap `MAX_YOUTUBE_LINKS` after de-duplication with a `skipped` note; when the entered URL itself parses as a channel/playlist, create a `DIRECT` YouTube finding and skip the HTTP start-page fetch, the probes and `robots.txt` for it; validate via `youtube.describe(YoutubeChannelSource(type="youtube_channel", channel_id=locator))` / `YoutubePlaylistSource(...)`; `entry_count == 0` → `Rejection("empty_listing")`; `FetchError` → `Rejection` (depends on T016, T034)
- [X] T036 [US4] Extend rendering in `src/invio/cli/commands/source.py` for YouTube entries (locator instead of URL, title, newest date, `found: linked`/`direct`) (depends on T017, T035)

**Checkpoint**: All four user stories work independently

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T037 [P] Document `invio source discover URL [--add-to JOB] [--pick N]` in `README.md` (new CLI subsection next to the `invio job` docs): what is searched (announced feeds, common locations at root and page folder, robots.txt sitemaps, YouTube links), output format, `--add-to`/`--pick` rules, exit codes 0/1/2, and that all requests go through the safe HTTP client (private addresses refused, robots.txt honoured)
- [X] T038 [P] Add a layering assertion to `tests/test_cli_layering.py` (or a new `tests/test_sources_layering.py`) that `src/invio/sources/discover.py` imports nothing from `invio.cli`, `invio.services`, `invio.pipeline`, `invio.db`
- [X] T039 Add a request-budget test in `tests/test_discover.py` as the automated proxy for SC-005 (30 s at the default 1 s host interval): a typical fixture site (page, 8 probes, robots.txt, 3 feeds) issues at most 13 HTTP requests (1 page + 1 robots.txt + 8 probes + 3 feeds) and no URL twice; the wall-clock bound itself is checked manually in quickstart §3
- [X] T040 Run the gates: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest`; fix findings (narrow, justified `# type: ignore` only)
- [X] T041 Walk through `specs/019-gh-issue-36/quickstart.md` §1–§2 (and §3–§4 if internet is available) and tick the acceptance-criteria table

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none
- **Foundational (Phase 2)**: after Setup — blocks all stories
- **US1 (Phase 3)**: after Foundational — MVP
- **US2 (Phase 4)**: after US1's engine core (T016) and CLI command (T017); its parsers (T018, T019, T022, T023) can start right after Phase 2
- **US3 (Phase 5)**: service part (T026, T028) can start after Phase 2; CLI part needs T017
- **US4 (Phase 6)**: adapter part (T031, T034) can start after Phase 2; engine part needs T016
- **Polish (Phase 7)**: after the desired stories

### Within Each Story

Tests first (must fail) → adapter helpers → engine → CLI.

### Story Graph

```text
Setup → Foundational ─┬─> US1 (T010–T017) ─┬─> US2 engine/CLI (T024–T025)
                      │                     ├─> US3 CLI (T029–T030)
                      │                     └─> US4 engine/CLI (T035–T036)
                      ├─> US2 parsers (T018, T019, T022, T023)
                      ├─> US3 service (T026, T028)
                      └─> US4 adapter (T031, T034)
```

### Parallel Opportunities

- Phase 1: T003, T004 in parallel after T002
- Phase 2: T005, T007, T008 in parallel
- After Phase 2: describe_feed (T010/T013), describe_sitemap (T018/T022), robots sitemaps (T019/T023), append_source (T026/T028), YouTube describe (T031/T034) — five different files, all parallel
- Story test tasks marked [P] are in different files or independent sections and can be written together

## Parallel Example: after Phase 2

```text
Task: "T013 [US1] describe_feed in src/invio/sources/rss.py"
Task: "T022 [US2] describe_sitemap in src/invio/sources/sitemap.py"
Task: "T023 [US2] RobotsPolicy.sitemaps + SafeHttpClient.robots_sitemaps"
Task: "T028 [US3] JobService.append_source in src/invio/services/jobs.py"
Task: "T034 [US4] YoutubeSource.describe in src/invio/sources/youtube.py"
```

## Implementation Strategy

### MVP First (US1)

1. Phases 1–2
2. Phase 3 (US1) → validate with T011/T012 → usable: `invio source discover` prints announced feeds

### Incremental Delivery

1. + US2 → probes and robots.txt sitemaps (covers issue AC2)
2. + US3 → `--add-to` (covers issue AC3)
3. + US4 → YouTube
4. Polish → README, gates, quickstart

Issue acceptance criteria are covered by T011 (AC1, AC4), T020 (AC2), T027 (AC3).
