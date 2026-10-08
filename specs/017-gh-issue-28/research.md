# Research: YouTube Channel and Playlist Source

**Feature**: [spec.md](spec.md) · **Plan**: [plan.md](plan.md) · **Date**: 2026-10-08

The Technical Context has no open `NEEDS CLARIFICATION` items. The items below are the design
questions the plan had to settle, checked against `src/invio/config/job.py`,
`src/invio/config/settings.py`, `src/invio/sources/{sitemap,rss,freshness,errors,base}.py`,
`src/invio/graph/stages.py` and `src/invio/pipeline/deps.py`.

---

## R1 — Job-file locator: keep `channel_id` / `playlist_id`, widen what they accept

**Decision**: Keep the existing fields `channel_id` and `playlist_id` on `YoutubeChannelSource`
and `YoutubePlaylistSource`. Their value may be:
- channel: a channel id (`UC…`), an `@handle`, or an `https` YouTube channel URL
  (`/channel/UC…`, `/@handle`);
- playlist: a playlist id (`PL…`, `UU…`, `OL…`, …) or an `https` YouTube playlist URL
  (`…/playlist?list=ID`).

Add two optional fields to both models: `max_age_days` (int ≥ 1, default none, same as RSS and
sitemap) and `max_items` (int 1..200, default 20).

**Rationale**: The issue says "channel and playlist URLs … resolve `@handle` URLs", but the job
file contract (saved files, `docs/job.schema.json`, wizard, ~10 tests) already fixes the
locator as `channel_id` / `playlist_id`, and the models forbid unknown keys. Renaming would
invalidate saved job files. Widening the accepted values satisfies the issue and breaks nothing:
every existing value (`UC123`, `PL123`) stays valid. `max_age_days` follows the other sources.
`max_items` is the "per-source maximum (default 20)" of the issue; it bounds what is *listed*.
The job-wide `limits.max_items_per_source` (default 20) keeps cutting afterwards in
`fetch_sources`, so the effective cap is the smaller of the two.

**Alternatives considered**:
- *New `url` field next to the id*: two locators for one source, "exactly one of" validation,
  more schema surface. Rejected.
- *Only a URL field, drop ids*: breaks saved files. Rejected.
- *Reuse only `limits.max_items_per_source`*: it is job-wide, but the issue and spec ask for a
  per-source value, and it is applied after the adapter has already listed everything.

## R2 — `yt-dlp` as a library, flat extraction

**Decision**: Add `yt-dlp` as a runtime dependency (latest release at implementation time,
`uv add yt-dlp`, lock committed). Call `yt_dlp.YoutubeDL(opts).extract_info(url, download=False)`
with:

```
extract_flat = "in_playlist"   # entries are not resolved one by one
skip_download = True
playlistend = max_items         # bounds the listing itself
quiet = True; no_warnings = True; noprogress = True
ignoreconfig = True             # never read the host's yt-dlp.conf
cachedir = False                # no cache directory writes
socket_timeout = <seconds>      # bounds a stalled connection (R5)
retries = 0; extractor_retries = 0   # invio retries transient errors itself (fetch_sources)
allowed_extractors = ["youtube.*"]   # R3
cookiefile / proxy             # only when configured (R7)
```

Result handling: `info["entries"]` is the list; each entry is read defensively (`id`, `title`,
`url`/`webpage_url`, `upload_date` `YYYYMMDD`, `timestamp`). The result of `yt-dlp` is an
untyped dict → converted once into a frozen `_Video` dataclass; entries without `id` or `title`
are skipped (FR-012).

`yt-dlp` has partial type information; strict mypy is satisfied by confining the import and
call to one small function and adding a narrow `[[tool.mypy.overrides]]` for `yt_dlp.*`
(`ignore_missing_imports`), commented like the existing `feedparser` override. Tests inject a
fake extractor callable (`Callable[[str, dict[str, object]], dict[str, object]]`), so only one
integration-style test imports `yt_dlp` (to assert the real option names are accepted).

**Rationale**: Required by the issue. Flat mode is the metadata-only, fast path.

**Alternatives considered**: shelling out to the `yt-dlp` binary (process management, no typed
errors); the YouTube Data API (needs an API key and quota, not in the issue).

**Known limitation**: flat channel listings often omit `upload_date`. By the clarification,
undated videos are kept (ranked after dated ones, inside `max_items`) and no per-video request is
made to find the date. `timestamp` is used when present.

## R3 — Keep `yt-dlp` pointed at YouTube only (SSRF / scope)

**Decision**: The adapter never passes the user's raw string to `yt-dlp`. It first parses the
locator (R1) into a typed form and **builds the URL itself**:
- id → `https://www.youtube.com/channel/<id>/videos`
- handle → `https://www.youtube.com/@<handle>/videos`
- playlist → `https://www.youtube.com/playlist?list=<id>`

A URL given in the job file is accepted only when its scheme is `https` and its host is
`youtube.com`, `www.youtube.com`, `m.youtube.com` or `music.youtube.com`; id/handle are extracted
from the path/query and must match a strict pattern (`[A-Za-z0-9_-]+`, handle `@[A-Za-z0-9._-]+`).
`allowed_extractors = ["youtube.*"]` is a second barrier inside yt-dlp (it would otherwise use
its generic extractor for any URL and follow redirects).

**Rationale**: `yt-dlp` bypasses `SafeHttpClient` (SSRF guard, robots, per-host rate limit). The
user-controlled part is reduced to a validated token inside a fixed YouTube URL, so a job file
cannot make the server fetch internal addresses. Validation lives in `config/job.py` (fails at
load time with the field name) and is reused by the adapter.

**Alternatives considered**: running `yt-dlp` behind `SafeHttpClient`'s transport — not possible,
yt-dlp uses its own network stack.

## R4 — Ordering, age filter, count

**Decision**: After conversion:
1. `clamp_or_expire(published, now, cutoff)` (existing helper) drops dated videos older than
   `max_age_days`; a date in the future is clamped to now; undated videos are kept.
2. Stable sort: dated videos newest first, then undated videos in listing order.
3. Cut to `max_items`.

`published_at` = `timestamp` (UTC) if present, else `upload_date` as midnight UTC, else `None`.
Duplicate video ids inside one listing are dropped (first wins). `url_hash` of the canonical
watch URL `https://www.youtube.com/watch?v=<id>` is the identity, so the same video from a
channel and a playlist deduplicates in the existing pipeline stage.

**Rationale**: Matches the clarification and the RSS/sitemap semantics (`clamp_or_expire`
already keeps undated entries). `now` is injectable for tests, like the sitemap adapter.

## R5 — Thread executor and timeout

**Decision**: `await asyncio.wait_for(loop.run_in_executor(None, extract), timeout)`, where the
extract callable is the only code touching `yt-dlp`. Timeout = new setting
`INVIO_YOUTUBE_TIMEOUT_SECONDS` (default 60, > 0), passed to the adapter constructor. yt-dlp's
`socket_timeout` is set to `min(timeout, 20)` so a hung connection ends the worker thread soon
after the wait is abandoned.
On `asyncio.TimeoutError` the adapter raises `FetchError("timeout", …)`.

**Rationale**: A Python thread cannot be killed; the wait is abandoned at the timeout and the
thread ends when yt-dlp's own socket timeout trips. Because retries are disabled inside
yt-dlp, the thread is bounded. Cancellation of the run cancels the awaiting task only; the
leftover thread is short-lived and holds no run resources.

**Alternatives considered**: a process pool (isolation but heavy start-up, pickling results);
`asyncio.to_thread` (equivalent; `run_in_executor` kept to make the executor explicit/testable).

## R6 — Error mapping without leaking details

**Decision**: Catch `yt_dlp.utils.DownloadError` / `ExtractorError` (and `OSError`, `ValueError`
from the library) and map by exception class + a small set of message substrings to existing
reasons so the retry policy (`is_transient_fetch`) works unchanged:

| yt-dlp situation | `FetchError.reason` | status | transient? |
|---|---|---|---|
| `wait_for` timeout, socket timeout | `timeout` | – | yes |
| connection refused/reset, DNS, proxy failure (`URLError`, `TransportError`) | `connection_failed` | – | yes |
| HTTP 429 / "Sign in to confirm you're not a bot" / HTTP 403 block | `http_status` | 429 / 403 | 429 yes, 403 no |
| channel/playlist not found, private, removed (HTTP 404, "does not exist", "private") | `http_status` | 404 | no |
| result not a mapping / no `entries` of the expected shape | `invalid_response` | – | yes |
| cookies file missing/unreadable (R7) | `cookies_unavailable` | – | no |
| anything else from yt-dlp | `invalid_response` | – | yes |

The `FetchError` is built from the **canonical URL only**; the yt-dlp message is never copied
into it (it can contain the proxy URL with credentials or cookie paths). The log line carries
the source key and the exception class name, as `fetch_sources` already does.

**Rationale**: Constitution V; keeps `FetchError` the single failure type (issue: "map failures
to `FetchError`"); unknown reasons are permanent in `is_transient_fetch`, so reusing the
existing reason vocabulary makes network trouble retryable and 404 permanent with no change to
the retry code.

## R7 — Cookies and proxy

**Decision**: The adapter is constructed with `cookies_file: Path | None`, `proxy: str | None`
(from `Settings.youtube_cookies_file`, `settings.youtube_proxy.get_secret_value()`), both already
in `Settings`. When set they are passed as `cookiefile` / `proxy`. A cookies path that is not a
readable file raises `FetchError("cookies_unavailable", url=…)` at fetch time with the message
"cookies_unavailable" (the path is not echoed). Empty values are treated as unset.

yt-dlp may write refreshed cookies back to the cookie file. invio does not copy the file; the
README states that the service user must be able to write it.

**Rationale**: Spec FR-010/FR-011. Failing at fetch (not at `Settings` load) keeps other
commands usable without YouTube setup, consistent with "credentials are checked when used"
(constitution V).

## R8 — Wiring and the `SUPPORTED_SOURCES` gate

**Decision**: `graph/stages.py` `SUPPORTED_SOURCES` adds the two types; `pipeline/deps.py`
builds one `YoutubeSource` from settings and dispatches both types to it. The existing
`fetch_source` wrapper already retries transient `FetchError`s and turns a persistent one into a
per-source failure that does not stop the others (SC-004 is met by existing machinery; the new
tests prove it for YouTube errors). The existing "unsupported" test in
`tests/test_pipeline_run.py` is updated to use a different still-unsupported case or replaced by
a positive test.

**Rationale**: Smallest change that makes the source usable end-to-end.
