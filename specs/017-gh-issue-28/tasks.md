---

description: "Task list for the YouTube channel and playlist source (#28)"
---

# Tasks: YouTube Channel and Playlist Source

**Input**: Design documents from `specs/017-gh-issue-28/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/job-file.md](contracts/job-file.md),
[contracts/youtube-source.md](contracts/youtube-source.md), [quickstart.md](quickstart.md)

**Tests**: Included. Constitution III and spec FR-014 require an automated test for every
acceptance scenario, including the rejection paths, with no network access. Write each story's
tests first and confirm they fail before implementing.

**Organization**: Tasks are grouped by user story in spec priority order:
- P1: US1 (channel videos), US2 (playlist videos)
- P2: US3 (age and count limits), US4 (failures, cookies, proxy)

`R<n>` refers to the decisions in [research.md](research.md).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story the task belongs to (US1–US4)

## Conventions used by every task

- Source goes in `src/invio/`, tests in `tests/`. mypy runs in strict mode; the only new
  suppression allowed is the `yt_dlp` override in `pyproject.toml` (T002). No `Any` leaks out of
  `invio.sources.youtube`.
- Tests never touch the network and never run a real `yt-dlp` extraction: inject a fake extractor
  (`Extractor` in [contracts/youtube-source.md](contracts/youtube-source.md)) and a fixed `now`.
  Tests may import `yt_dlp.utils` for exception types (T018) and construct `yt_dlp.YoutubeDL`
  once to check option names (T010a); the adapter imports `yt_dlp` and `yt_dlp.utils` lazily
  inside the default extractor and the error mapper, so importing `invio.sources.youtube` never
  requires them.
- Layering: `invio.sources.youtube` imports only `invio.config`, `invio.domain` and
  `invio.sources.*`. Never put the yt-dlp message, proxy URL or cookie path into a `FetchError`
  or a log line (R6).
- Run `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest` before
  finishing a phase.

---

## Phase 1: Setup

- [x] T001 Add the runtime dependency with `uv add yt-dlp` (updates `pyproject.toml` and `uv.lock`); confirm `uv sync --locked` works (R2)
- [x] T002 Add a commented `[[tool.mypy.overrides]]` for `module = ["yt_dlp", "yt_dlp.*"]` with `ignore_missing_imports = true` in `pyproject.toml`, next to the existing `feedparser` override (R2)

---

## Phase 2: Foundational (blocks all user stories)

**Purpose**: the validated locator, the timeout setting and the adapter skeleton every story uses.

- [x] T003 [P] Add `youtube_timeout_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)` to `Settings` in `src/invio/config/settings.py` (variable `INVIO_YOUTUBE_TIMEOUT_SECONDS`) and cover default, override and rejection of `0`/negative/`nan` in `tests/test_settings.py`
- [x] T004 Add locator parsing to `src/invio/config/job.py`: a `field_validator` on `YoutubeChannelSource.channel_id` accepting a channel id (`[A-Za-z0-9_-]+`), an `@handle` (`@[A-Za-z0-9._-]+`) or an `https` URL on host `youtube.com`/`www.youtube.com`/`m.youtube.com`/`music.youtube.com` with path `/channel/<id>` or `/@<handle>`; and on `YoutubePlaylistSource.playlist_id` accepting a playlist id (`[A-Za-z0-9_-]+`) or such a URL carrying `list=<id>`. Reject empty values, whitespace, other schemes/hosts, URLs without id/handle/`list=`; error messages name the field and rule (Constitution I, R1, R3). Put the parsing in two public functions in `src/invio/config/job.py`, `parse_youtube_channel(value) -> tuple[Literal["channel_id", "handle"], str]` and `parse_youtube_playlist(value) -> str` (returning the validated token), used by the validators and by `invio.sources.youtube` (single implementation, no drift). Existing values such as `UC123`, `UC-_x`, `PL123` must stay valid
- [x] T005 [P] Add tests in `tests/test_job_sources.py` for T004: accepted forms (id, `@handle`, channel URL, playlist URL, watch URL with `list=`) and rejected forms (`file:///etc/passwd`, `http://youtube.com/...`, `https://169.254.169.254/`, `https://evil.example/@x`, `" UC1"`, `""`, URL without `list=`)
- [x] T005a Add to both `YoutubeChannelSource` and `YoutubePlaylistSource` in `src/invio/config/job.py`: `max_age_days: StrictInt | None = Field(default=None, ge=1)` and `max_items: StrictInt = Field(default=20, ge=1, le=200)`, placed after `enabled` (field order as in `SitemapSource`); regenerate the schema with `uv run python -m invio.config.job` (writes `docs/job.schema.json`; `tests/test_job_schema.py` fails while it is stale) (R1)
- [x] T005b [P] In `tests/test_job_sources.py` add config tests for both source models: `max_age_days` accepts `1`, rejects `0`, `-1`, `1.5`, `"7"`, defaults to `None`; `max_items` defaults to `20`, accepts `1` and `200`, rejects `0`, `201`, `-1`, `True`, `"5"`; unknown keys still rejected
- [x] T006 Create `src/invio/sources/youtube.py` skeleton: module docstring (explain the deliberate bypass of `SafeHttpClient`, R3); `Extractor` type alias; frozen `_Locator` (`kind`, `token`, `listing_url()` building `https://www.youtube.com/channel/<id>/videos`, `https://www.youtube.com/@<handle>/videos`, `https://www.youtube.com/playlist?list=<id>`) with a `from_config` factory that calls `parse_youtube_channel` / `parse_youtube_playlist` from T004 (no second parser); frozen `_Video` (`id`, `title`, `published`); `YoutubeSource.__init__(*, cookies_file=None, proxy=None, timeout=60.0, extract=None, now=None)` per [contracts/youtube-source.md](contracts/youtube-source.md); `__all__ = ["YoutubeSource"]`. The default extractor is one small function that imports `yt_dlp` lazily and calls `YoutubeDL(opts).extract_info(url, download=False)` (R2)
- [x] T007 Wire the adapter: add `"youtube_channel"` and `"youtube_playlist"` to `SUPPORTED_SOURCES` in `src/invio/graph/stages.py`; in `src/invio/pipeline/deps.py` build one `YoutubeSource` from `settings.youtube_cookies_file`, `settings.youtube_proxy` (`.get_secret_value()` when set, else `None`) and `settings.youtube_timeout_seconds`, and dispatch both types in `fetch_source` (R8)

**Checkpoint**: config accepts the new locator forms and limit fields; adapter class exists but `fetch` is not implemented yet.

---

## Phase 3: User Story 1 - Collect recent videos from a channel (Priority: P1) 🎯 MVP

**Goal**: A channel (id, `@handle` or URL) yields `type="video"` candidates with id, title, date, URL.

**Independent Test**: fake extractor returns a channel listing; `fetch` returns one video candidate per entry.

### Tests for User Story 1

- [x] T008 [P] [US1] In `tests/test_source_youtube.py` add a `FakeExtractor` helper (records `(url, options)` calls, returns a canned `{"entries": [...]}` mapping or raises a configured exception) and tests: channel id, `@handle` and channel URL each call the extractor with the canonical listing URL from T006; each entry becomes `Candidate(type="video", url="https://www.youtube.com/watch?v=<id>", url_hash=url_hash(url), title=..., teaser=None, content_hash=None)`; `upload_date` `"20260301"` → `2026-03-01T00:00Z`; `timestamp` wins over `upload_date`; empty channel → `[]`; options contain `extract_flat="in_playlist"`, `skip_download=True`, `ignoreconfig=True`, `allowed_extractors=["youtube.*"]`; adapter satisfies `Source[YoutubeChannelSource]`
- [x] T009 [P] [US1] In `tests/test_pipeline_deps.py` replace the "youtube is unsupported" expectation with a test that `fetch_source` dispatches `youtube_channel` to the adapter; in `tests/test_pipeline_run.py` update the case at the `youtube_channel` "unsupported" lines (around 240–256) to a positive end-to-end test: a fake extractor feeds two videos into a run and they reach the pipeline as `video` items

### Implementation for User Story 1

- [x] T010 [US1] Implement `YoutubeSource.fetch` in `src/invio/sources/youtube.py` for channels: build `_Locator`, run the extractor on the adapter's own bounded pool (`loop.run_in_executor`; deviates from the default executor so hung listings cannot exhaust it, see the module docstring) with `asyncio.wait_for(..., timeout)`, set the option dict from R2 (`extract_flat`, `skip_download`, `quiet`, `no_warnings`, `noprogress`, `ignoreconfig`, `cachedir=False`, `socket_timeout=min(timeout, 20)`, `retries=0`, `extractor_retries=0`, `allowed_extractors`, `playlistend=max_items`), convert `info["entries"]` into `_Video` (date from `timestamp` else `upload_date` as midnight UTC, whitespace-normalized title), build `Candidate`s. Depends on T005a and T006
- [x] T010a [US1] In `tests/test_source_youtube.py` add one test that constructs `yt_dlp.YoutubeDL(options)` with exactly the option dict the adapter builds (capture it through the fake extractor) and asserts no exception and no network access, so a misspelled yt-dlp option name fails here (R2)
- [x] T011 [US1] Update `src/invio/cli/wizard.py` prompts `Q_CHANNEL`/`Q_PLAYLIST` to "YouTube channel (id, @handle or URL)" / "YouTube playlist (id or URL)" and adjust `tests/test_wizard.py` only if it asserts the old text

**Checkpoint**: US1 works end to end with a fake extractor; this is the MVP.

---

## Phase 4: User Story 2 - Collect videos from a playlist (Priority: P1)

**Goal**: A playlist (id or URL) yields video candidates; unusable entries are skipped.

**Independent Test**: fake extractor returns a playlist listing including a private/deleted entry.

### Tests for User Story 2

- [x] T012 [P] [US2] In `tests/test_source_youtube.py` add tests: playlist id and playlist URL call the extractor with `https://www.youtube.com/playlist?list=<id>`; entries become video candidates; an entry without `id`, one without `title`, one with an id outside `[A-Za-z0-9_-]{1,64}` and a `None` entry (deleted/private) are skipped while the others are returned; duplicate ids yield one candidate (first wins); adapter satisfies `Source[YoutubePlaylistSource]`

### Implementation for User Story 2

- [x] T013 [US2] In `src/invio/sources/youtube.py` make `_Video` conversion defensive (R2, FR-012): skip non-mapping entries, entries lacking `id`/`title`, invalid ids; drop duplicate ids; log skips at debug with the count only. Ensure the playlist locator path of `_Locator.listing_url()` is used by `fetch`

**Checkpoint**: US1 and US2 both work independently.

---

## Phase 5: User Story 3 - Limit by age and count (Priority: P2)

**Goal**: `max_age_days` and `max_items` (default 20; config fields exist since T005a) are honoured; undated videos are kept after dated ones.

**Independent Test**: fake listing with mixed ages, undated entries and more entries than the limit.

### Tests for User Story 3

- [x] T015 [P] [US3] In `tests/test_source_youtube.py` add tests with a fixed `now`: `max_age_days=7` keeps a 2-day-old and drops a 30-day-old video; a future date is clamped to now; 50 eligible videos with default config → exactly 20, newest first; `max_items=5` → 5; undated videos are kept, placed after all dated ones in listing order, and count toward `max_items` (spec US3 scenario 4); the extractor option `playlistend` equals `max_items`; no extractor call is made per video (exactly one call)

### Implementation for User Story 3

- [x] T017 [US3] In `YoutubeSource.fetch` apply `clamp_or_expire` from `src/invio/sources/freshness.py` with `cutoff = now - timedelta(days=max_age_days)`, then the stable sort "dated newest first, undated after in listing order", then slice to `max_items` (R4). `now` defaults to `datetime.now(UTC)`

**Checkpoint**: limits and ordering verified; all of spec FR-006/FR-007 covered.

---

## Phase 6: User Story 4 - Survive blocks and failures; cookies and proxy (Priority: P2)

**Goal**: Every failure is a `FetchError` for that source only; cookies/proxy are used when configured.

**Independent Test**: the fake extractor raises/returns each failure; other sources in the same run still complete.

### Tests for User Story 4

- [x] T018 [P] [US4] In `tests/test_source_youtube.py` add failure tests, each asserting `FetchError` type, `reason`, `status`, that `err.url` is the canonical listing URL, and that the message contains none of: a proxy URL with credentials, the cookie path, the raw yt-dlp text: extractor sleeps past `timeout=0.05` → `timeout` (and `is_transient_fetch` true); `yt_dlp.utils.DownloadError("... HTTP Error 429 ...")` and "Sign in to confirm you're not a bot" → `http_status` 429; "HTTP Error 404"/"does not exist"/"private" → `http_status` 404 (not transient); `URLError`/`OSError`/`TimeoutError` → `connection_failed`/`timeout` (transient); a non-mapping result → `invalid_response`; unexpected `RuntimeError` → `invalid_response`
- [x] T018a [P] [US4] In `tests/test_source_youtube.py` add a cancellation test: start `fetch` as a task while the fake extractor blocks on a `threading.Event`, cancel the task, assert it ends with `CancelledError` within 1 s and the event can then be released so no thread leaks (spec Edge Cases, R5)
- [x] T019 [P] [US4] In `tests/test_source_youtube.py` add access tests: with `cookies_file=<tmp file>` and `proxy="http://u:secret@proxy:8080"` the extractor options contain `cookiefile` and `proxy`; with neither, the keys are absent; empty-string proxy treated as unset; a `cookies_file` path that does not exist or is a directory → `FetchError("cookies_unavailable")` before the extractor is called, message without the path
- [x] T020 [P] [US4] In `tests/test_pipeline_run.py` add an end-to-end test: a job with a failing YouTube source (fake extractor raising a block error) and a working second source completes; the failure is in `runs.stats["errors"]` with `source` key `"<index>:youtube_channel"`, the other source's items are processed, and the transient 429 is retried by the existing `fetch_sources` retry path (use the recording `env.sleep` fixture as in `tests/test_pipeline_retries.py::test_s9_a_transient_source_failure_is_retried`, so no real waiting) while 404 is attempted exactly once with no sleep

### Implementation for User Story 4

- [x] T021 [US4] In `src/invio/sources/youtube.py` implement the error mapping table of R6: wrap the executor call, convert `asyncio.TimeoutError` → `timeout`; classify `yt_dlp.utils.DownloadError`/`ExtractorError` by exception chain (`urllib.error.HTTPError.code`, `URLError`, `TransportError`, socket timeouts) and a small case-insensitive substring set (`429`, `not a bot`, `403`, `404`, `does not exist`, `private`, `unavailable`) into `FetchError(reason, url=listing_url, status=...)`; anything else → `invalid_response`; never include the library message. Log `source.youtube_failed` with the exception class name only
- [x] T022 [US4] In `YoutubeSource` pass `cookiefile`/`proxy` only when set (empty string = unset) and raise `FetchError("cookies_unavailable", url=listing_url)` when `cookies_file` is set but not a readable regular file (R7); verify the proxy and path never reach logs or messages

**Checkpoint**: all four stories work independently; spec SC-004/SC-005 covered.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T023 [P] Update `README.md`: YouTube source section (locator forms, `max_age_days`, `max_items`, env variables `INVIO_YOUTUBE_COOKIES_FILE`, `INVIO_YOUTUBE_PROXY`, `INVIO_YOUTUBE_TIMEOUT_SECONDS`, cookies file must be writable by the service user, Netscape format, operator responsible for terms of use, undated videos behaviour, flat listing never downloads media) and fix the "Known limitations" sentence near line 606 so only `youtube_*` no longer appears among source types without an adapter (sitemap already has one)
- [x] T024 [P] Update `docs/job.example.yaml` YouTube entries to show an `@handle` channel with `max_age_days`/`max_items`, and keep `tests/test_job_yaml.py`/`tests/test_job_schema.py` green; add `INVIO_YOUTUBE_TIMEOUT_SECONDS=60` to `.env.example` (the cookies and proxy variables are already there)
- [x] T025 Run the full gate: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`, then walk through [quickstart.md](quickstart.md) sections 1–2; fix findings
- [x] T026 Create the branch `gh-issue-28` from `main` if the work is not already on it (plan.md, F6) and prepare the PR description (`specs/017-gh-issue-28/pr-description.md`, same structure as earlier features): reference issue #28, justify the new `yt-dlp` runtime dependency and the documented `SafeHttpClient` bypass (plan → Complexity Tracking), list env variables and config additions

---

## Dependencies & Execution Order

- **Phase 1 → Phase 2 → stories → Polish.** T001 before everything that imports `yt_dlp` (T010a, T010, T018); T002 before `mypy` runs.
- **Foundational**: T003 ∥ T004; T005 after T004; T005a after T004 (same file), T005b after T005a; T006 after T004 and T005a (re-uses the parser, reads `max_items`); T007 after T006.
- **US1** (T008–T011) after Phase 2; T010 is the first real `fetch`. T008/T009 are written before T010 and fail until it lands.
- **US2** (T012–T013) after US1's T010 (extends the same `fetch`/conversion code); the story is independently testable.
- **US3** (T015, T017): config fields already exist (T005a); builds on T010.
- **US4** (T018, T018a, T019–T022): builds on T010; T021 before T022 (same function area), tests T018–T020 first.
- **Polish** after all stories; T023 ∥ T024.

Story order for delivery: US1 → US2 → US3 → US4. US2, US3 and US4 are independent of each other
once US1's `fetch` exists, but they edit the same module, so run them sequentially or coordinate.

## Parallel Opportunities

- Phase 2: T003 ∥ T004, then T005 ∥ T005a, then T005b ∥ T006.
- Within each story the test tasks are [P] (T008 ∥ T009, T018 ∥ T018a ∥ T019 ∥ T020); they touch different files or independent test functions.
- Polish: T023 ∥ T024.

## Implementation Strategy

- **MVP**: Phases 1–3 (US1): channel listing works end to end with default limits (the job-wide
  `max_items_per_source` already caps results). Stop and validate with quickstart section 1.
- **Increment 2**: US2 (playlists) — small, mostly tests and defensive parsing.
- **Increment 3**: US3 (per-source limits) — age filter, ordering and count cut (the config fields come with Phase 2).
- **Increment 4**: US4 (failures, cookies, proxy) — required before enabling the source for
  unattended production runs on a server IP.
- Total: 28 tasks (Setup 2, Foundational 7, US1 5, US2 2, US3 2, US4 6, Polish 4). T005a, T005b, T010a and T018a were added after the analysis so the other task IDs stay stable.
