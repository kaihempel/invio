# Research: Source Discovery Command (gh-issue-36)

All Technical Context unknowns are resolved below. Each entry: decision, rationale, alternatives.

## R1 — Where discovery logic lives

**Decision**: A new adapter module `src/invio/sources/discover.py` holds the whole discovery
pipeline (collect findings → validate → order). It depends only on `invio.sources.*` and
`invio.config.job`. The CLI command `src/invio/cli/commands/source.py` (auto-registered as
`invio source`) wires it up, prints the results and performs `--add-to` through `JobService`.

**Rationale**: Constitution layering is `cli` → orchestration → adapters → `config`/domain.
Discovery is fetching and parsing untrusted web content, which is exactly what `sources` does.
Keeping it free of Typer and the database makes it unit-testable against the loopback server
and the mock transport the other adapters already use.

**Alternatives considered**: Putting it in `invio.cli` like the wizard's `source_check.py`.
Rejected: that module uses `urllib` without the SSRF guard (its docstring limits it to local
operator checks), and FR-012 requires the safe client. A new `invio.services.discovery` was
rejected because there is no unit of work or database state to coordinate.

## R2 — HTTP access

**Decision**: Every request goes through one `SafeHttpClient` built with
`HttpClientConfig.from_settings()` (the same client the pipeline uses), opened for the duration
of the command. Requests use `conditional=False`, so nothing is answered 304.

**Rationale**: FR-012 (address guard, robots.txt, rate limit, timeouts, size limit) is then
inherited, not re-implemented. `conditional=False` keeps the per-run validator cache out of
the picture; a discovery run has no earlier fetch to compare against.

**Alternatives considered**: Reusing `HttpSourceChecker` (no SSRF guard, no robots, rejected) or
a second lightweight client (duplicated policy, rejected).

## R3 — Content classification ("by what it is, not what it claims")

**Decision**: One fetch per candidate URL, then classify the body in this order:

1. **Feed**: a new public `describe_feed(content) -> FeedSummary | None` in `sources/rss.py`
   reuses the module's `_parse` (feedparser over a byte stream). A body is a feed when
   feedparser reports a version and the parse is not broken (the same rule `RssFeedSource.fetch`
   applies before raising `malformed_feed`; an empty but well-formed feed is still a feed).
   `FeedSummary` carries the feed title (`feed.title`, collapsed to plain text), the entry count
   and the newest entry date (the existing `_entry_date` rule: `published`, else `updated`).
2. **Sitemap**: a new public `describe_sitemap(content, *, url, limit) -> SitemapSummary | None`
   in `sources/sitemap.py` reuses `_parse` (defusedxml, gzip-aware) and accepts a root whose
   local name is `urlset` or `sitemapindex`. For a `urlset` it counts `<url>` entries and takes
   the newest `lastmod`; for an index it counts child `<sitemap>` entries and their newest
   `lastmod`. Children are **not** fetched (discovery only proves the document is a sitemap).
3. Anything else is not a candidate (for the start page, it is parsed as HTML instead, R4).

**Rationale**: FR-008 and the edge case "wrong content type" require classification by content.
Feed first, because feedparser is lenient and a sitemap never has a feed version, while the
reverse check (XML root name) is cheap. Reusing the adapters' parsers guarantees "valid in
discovery" means "fetchable by a job run".

**Alternatives considered**: Calling `RssFeedSource.fetch` / `SitemapUrlSource.fetch` with a
synthetic config. Rejected: a second request per candidate, no feed title, and the sitemap
adapter would fetch every child of an index.

## R4 — Reading the start page

**Decision**: The start page is fetched once. If its body is a feed or sitemap (R3), it becomes
the "direct" candidate (FR-006). Otherwise, when the body is HTML (same `_is_html` rule
`web.py` uses), it is parsed with selectolax (`LexborHTMLParser`, already a dependency) for:

- `link[rel~=alternate][href]` whose `type` is `application/rss+xml` or `application/atom+xml`
  (case-insensitive, parameters ignored); `title` attribute kept as a hint for comment detection;
- `a[href]` and `link[href]` pointing to YouTube (R6).

Relative URLs resolve against the first valid `<base href>`, else the final page URL (same rule
as `web._base_url`; the helper is promoted to a small shared function in `sources/urls.py` or
reused via import). A start page that fails (fetch error, blocked, non-HTML non-feed) is recorded
as a page problem; probing continues (spec edge case "unreachable start page").

**Rationale**: Uses the HTML parser and URL helpers the project already has.

**Alternatives considered**: Regex over HTML (fragile), BeautifulSoup (new dependency).

## R5 — Probe locations and `robots.txt` sitemaps

**Decision**:

- Probe bases: the site root (`scheme://host[:port]/`) and, when the page path up to its last
  `/` differs from `/`, that folder. Locations per base in fixed order: `feed`, `rss`,
  `atom.xml`, `sitemap.xml` (FR-004, clarification Q1). Bases are taken from the **final** start-page URL
  (after redirects) when the page loaded, else from the entered URL (normalised), so probing
  follows `example.com` → `www.example.com` and still works when the page itself failed. Duplicate absolute URLs
  are tried once.
- `robots.txt` sitemaps: `parse_robots` gains collection of `Sitemap:` lines (RFC 9309 §2.3.5:
  group-independent, absolute URLs) into `RobotsPolicy.sitemaps: tuple[str, ...]`. A new
  `SafeHttpClient.robots_sitemaps(url) -> tuple[str, ...]` returns them for the URL's origin via
  the existing `RobotsCache` (fetching robots.txt once if not yet known, even when
  `respect_robots` is off). Non-http(s) or unparsable entries are dropped; the first 5 are used,
  the rest reported as skipped (FR-004a, FR-013).

**Rationale**: The robots cache already fetches and parses robots.txt for every origin the
client touches, so the hints cost no extra request (clarification Q3 rationale). Keeping parsing
in `robots.py` keeps one robots parser.

**Alternatives considered**: Fetching `/robots.txt` again with `client.get` (duplicate request
and a second parser, rejected).

## R6 — YouTube links: recognition and validation

**Decision**:

- Recognition: every collected link on an allowed YouTube host is tried with the existing
  `parse_youtube_channel` and `parse_youtube_playlist` from `invio.config.job` (channel URL
  `/channel/<id>[/tab]`, `/@handle[/tab]`, any URL with exactly one `list=`). A link that
  parses as neither (a lone video, `/results`, `/user/...`) is ignored. Identity for
  de-duplication is `(kind, token)` as returned by the parsers (handles compared
  case-insensitively).
- Validation: a new `YoutubeSource.describe(config) -> ListingSummary` reuses the adapter's
  extractor, options, timeout, cookies and proxy with `playlistend` = 5, and returns the listing
  title (`info["title"]`, else `info["channel"]`/`info["uploader"]`), the number of videos seen
  and the newest upload date. A listing with no videos, or any `FetchError`, means "not
  offered" (spec US4 scenario 4).
- The candidate keeps the locator form found on the page (`@handle` or channel id, playlist id);
  both forms are valid `channel_id` / `playlist_id` values in the job file.
- A factory `YoutubeSource.from_settings(settings)` moves out of `pipeline/deps.py`
  (`_youtube_source`) so both the pipeline and the CLI build the adapter the same way.

**Rationale**: FR-012 requires the same mechanism job runs use. yt-dlp is already a core
dependency, and the injectable `extract` seam makes tests offline.

**Alternatives considered**: YouTube's public `feeds/videos.xml?channel_id=` RSS (needs the
channel id; handles would require scraping YouTube HTML; and the job would then run through a
different mechanism than discovery validated). Rejected.

## R7 — Concurrency and bounds

**Decision**: Findings are validated concurrently with `asyncio.gather` (the client's per-origin
rate limiter serialises same-host requests anyway; YouTube uses the adapter's 4-thread pool).
Caps: 20 announced feeds, 20 YouTube links (counted after de-duplication by identity), 5
`robots.txt` sitemaps; overflow is reported as skipped.

**Rationale**: SC-005 (≤30 s typical): with the default 1 s host interval, 1 page + 8 probes +
robots.txt + a few feeds stay around 10–15 s.

## R8 — Ordering, numbering and comment feeds

**Decision**: Each finding carries an origin rank: direct (0), announced (1, page order),
probed (2, root before folder, then `feed`, `rss`, `atom.xml`, `sitemap.xml`), robots (3, file
order), YouTube (4, page order). Validated results are sorted by `(is_comment_feed, rank,
position)` (stable). De-duplication keeps the first (lowest rank) occurrence by
`(type, final URL)` for feeds/sitemaps and `(kind, token)` for YouTube; it runs on the final
URL after redirects, so `/feed` redirecting to an announced feed collapses into one.

A feed is a comment feed when its final URL path ends with `/comments/feed`, `/comments/feed/`
or `/comments/`, or when its announced `title` attribute or parsed feed title contains
"comments" (case-insensitive) (clarification Q4, FR-010).

## R9 — Appending to a job

**Decision**: `JobService.append_source(name, source: SourceConfig) -> JobRecord`, in one unit
of work:

1. load the job (`JobNotFoundError`); build its `JobConfig` from the stored config
   (`StoredJobConfigError` when it no longer validates — refused, exit 2);
2. if a source of the same `type` with the same locator already exists
   (`url` compared after `invio.sources.urls.canonical_url` for rss/sitemap; parsed `(kind,
   token)` for YouTube), raise `SourceExistsError` (CLI: exit 0, "already present");
3. append `source.model_dump(mode="json", exclude_defaults=True)` to the stored `sources`
   list, validate the whole config with `validate_job` (`JobConfigError` → exit 2, nothing
   written), and persist via the existing `_apply_update` (next run recalculated as `update`
   does).

The CLI checks the job exists (`get_by_name`) **before** any network access (FR-014), then calls
`append_source` after the selection.

**Rationale**: Read-modify-write in one session avoids a lost update if the job is edited
concurrently; the service stays the only writer of jobs (constitution, #9).

**Alternatives considered**: CLI builds the new config and calls `update` (two sessions; a
concurrent edit between them would be overwritten). Rejected.

## R10 — Selection and exit codes

**Decision**:

| Situation | Behaviour | Exit |
|---|---|---|
| Invalid URL argument / `--pick` without `--add-to` / `--pick < 1` | usage error, no network | 2 |
| `--add-to` job missing | message, no network | 1 |
| `--add-to` job stored config invalid | errors listed, no network | 2 |
| No validated candidate | "no source found" + tried checks on stderr | 1 |
| Candidates found, no `--add-to` | list + snippets | 0 |
| `--add-to`, interactive, no `--pick` | `Prompter.select` over the numbered list + "Cancel" | 0 (added or cancelled) |
| `--add-to`, non-interactive, no `--pick`, exactly 1 candidate | added | 0 |
| `--add-to`, non-interactive, no `--pick`, ≥2 candidates | list + "use --pick N" | 1 |
| `--pick N` out of range | "valid range 1–M", nothing changed | 1 |
| Selected source already in job | "already present", nothing changed | 0 |
| Re-validation fails | errors naming fields, nothing changed | 2 |

Interactivity uses the same `_is_interactive()` rule as `invio job` (stdin and stdout are TTYs).
The prompter is the existing `Prompter` protocol (`QuestionaryPrompter` in production,
`FakePrompter` in tests).

## R11 — Output format

**Decision**: stdout, per candidate:

```text
[1] rss  https://example.com/feed.xml
    title: Example Blog · newest entry: 2026-10-01 · found: announced
    - type: rss
      url: https://example.com/feed.xml
```

The snippet is the `SourceConfig` dumped with `exclude_defaults=True` as a YAML list item,
indented so it can be pasted under `sources:`. Comment feeds get a `(comments)` marker after the
type. Skipped items, page problems and per-candidate rejection reasons go to stderr (one line
each; reasons use `FetchError.reason`, URLs redacted like the rest of the code base). A test
asserts every snippet loads back through `validate_job` (SC-003).

## R12 — Test strategy (no internet)

- `discover.py`: `SafeHttpClient` with the existing `RecordingTransport`/mock transport and
  `FakeResolver` from `tests/http_helpers.py`, or the `LoopbackServer` with
  `allow_networks=[127.0.0.0/8]`; fixtures under `tests/fixtures/discovery/` (page with RSS
  `<link>`, page with nothing, sitemap, sitemap index, robots.txt with `Sitemap:` lines, comment
  feed, broken feed, HTML at `/feed`).
- YouTube: `YoutubeSource(extract=fake)`.
- CLI: `typer.testing.CliRunner` with monkeypatched seams (`_make_service`, `_discovery_deps`,
  `_make_prompter`, `_is_interactive`) like `tests/test_cli_job*.py`; SQLite job store.
- Each issue acceptance criterion maps to at least one named test (see quickstart.md).
