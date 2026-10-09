# Data Model: Source Discovery Command (gh-issue-36)

All types are in-memory, frozen dataclasses (no database change). They live in
`invio.sources.discover` unless noted. Names avoid `Candidate`, which is the pipeline's domain
record in `invio.domain`.

## DiscoveryTarget

The normalised address the operator entered.

| Field | Type | Rule |
|---|---|---|
| `url` | `str` | absolute `http`/`https`; a scheme-less input such as `example.com` gets `https://` |
| `root` | `str` | `scheme://host[:port]/` of `url` |
| `folder` | `str \| None` | `url` path up to its last `/` when that is not `/`, else `None` |

`DiscoveryTarget.rebased(final_url) -> DiscoveryTarget` returns the same structure for the
start page's final URL; discovery probes and reads `robots.txt` from the rebased target when
the start page loaded (research R5).

Validation (`parse_target(raw) -> DiscoveryTarget`, raises `ValueError` with a message):
non-http(s) scheme, missing host, credentials in the URL, or whitespace-only input → rejected
before any network access (FR-002). The address guard (private addresses) is **not** applied
here; it happens in the client on fetch, so the message names the guard's reason.

## FindingOrigin (enum)

`direct` (0) · `announced` (1) · `probed` (2) · `robots` (3) · `linked` (4). The value is the
sort rank (research R8).

## Finding

A possible source before validation.

| Field | Type | Notes |
|---|---|---|
| `kind` | `"feed_or_sitemap" \| "youtube_channel" \| "youtube_playlist"` | feeds and sitemaps are told apart only by content (R3) |
| `locator` | `str` | absolute URL; for YouTube the channel/playlist value to put into the job (`@handle`, channel id or playlist id) |
| `origin` | `FindingOrigin` | |
| `position` | `int` | order within its origin (page order, probe order, robots file order) |
| `hint_title` | `str \| None` | `title` attribute of an announcing `<link>`; used for comment detection |

Identity before validation: `(kind, locator)` for URLs (after `canonical_url`), `(kind, parsed
token)` for YouTube (handle lower-cased). Duplicates keep the lowest `(origin, position)`.

## DiscoveredSource

A validated finding; what the operator sees and may add.

| Field | Type | Notes |
|---|---|---|
| `source` | `SourceConfig` (`RssSource \| SitemapSource \| YoutubeChannelSource \| YoutubePlaylistSource`) | built with required fields only; always passes the job model |
| `title` | `str \| None` | feed title, YouTube listing title; `None` for sitemaps |
| `entry_count` | `int` | feed entries, sitemap URLs/child sitemaps, videos seen (YouTube: up to 5) |
| `newest` | `datetime \| None` | aware UTC; `None` → printed "unknown" (or "no entries" when `entry_count == 0`) |
| `sitemap_index` | `bool` | `True` for a `sitemapindex` |
| `is_comment_feed` | `bool` | rule in research R8; only for `rss` |
| `origin` / `position` | as in `Finding` | for ordering |
| `final_url` | `str` | after redirects (URL types); the address shown and stored |

Identity after validation (FR-009): `(source.type, canonical_url(final_url))`, or
`(source.type, kind, token)` for YouTube. Ordering: `(is_comment_feed, origin, position)`.

## Rejection

Why a finding or the start page was not offered (stderr only).

| Field | Type | Notes |
|---|---|---|
| `locator` | `str` | redacted URL or YouTube locator |
| `reason` | `str` | `FetchError.reason` (`timeout`, `http_status`, `blocked_by_robots`, `non_public_address`, …) or `not_a_feed_or_sitemap`, `empty_listing` |
| `status` | `int \| None` | HTTP status if any |

## DiscoveryReport

Return value of `discover(...)`.

| Field | Type | Notes |
|---|---|---|
| `target` | `DiscoveryTarget` | |
| `sources` | `tuple[DiscoveredSource, ...]` | ordered, de-duplicated, numbered 1..n by the CLI |
| `page_problem` | `Rejection \| None` | start page failed or was neither HTML, feed nor sitemap |
| `rejected` | `tuple[Rejection, ...]` | invalid findings (unreachable, blocked, unreadable) |
| `skipped` | `tuple[str, ...]` | human-readable "N more announced feeds not checked" etc. (FR-013) |

## Summaries returned by the adapters (new, public)

| Type | Module | Fields |
|---|---|---|
| `FeedSummary` | `invio.sources.rss` | `title: str \| None`, `entry_count: int`, `newest: datetime \| None` |
| `SitemapSummary` | `invio.sources.sitemap` | `is_index: bool`, `entry_count: int`, `newest: datetime \| None` |
| `ListingSummary` | `invio.sources.youtube` | `title: str \| None`, `entry_count: int`, `newest: datetime \| None` |

`RobotsPolicy` (`invio.sources.robots`) gains `sitemaps: tuple[str, ...] = ()`.

## Job changes (existing entity)

`JobService.append_source(name, source)` appends one `SourceConfig` to `JobConfig.sources`.
State rules:

- job must exist (`JobNotFoundError`) and its stored config must validate
  (`StoredJobConfigError`);
- no source with the same type and locator may exist (`SourceExistsError`, new, in
  `invio.services.jobs`); locator equality: `canonical_url(url)` for `rss`/`sitemap`, parsed
  `(kind, token)` for YouTube (handles case-insensitive);
- the resulting config must pass `validate_job` (`JobConfigError`); otherwise nothing is
  written;
- on success: config persisted, `next_run_at` recalculated if enabled (same as `update`),
  `job.updated` change logged (contents never logged).
