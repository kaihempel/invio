# Context: RSS/Atom Source (gh-issue-11)

**Branch**: `gh-issue-11` · **Issue**: #11 · **Milestone**: M2 Pipeline · **Depends on**: #10 (safe HTTP client, merged)

## Goal

Turn each entry of an RSS 2.0 or Atom feed into a `Candidate` (title, URL, publish date) so the
pipeline can process it.

## Existing building blocks

| What | Where | Notes |
|------|-------|-------|
| `Candidate`, `url_hash()` | `src/invio/domain.py` | frozen kw-only dataclass; `url_hash` = SHA-256 hex of the URL string |
| `SafeHttpClient.get()` | `src/invio/sources/http.py` | returns `FetchResult` (2xx) or `NotModified` (304); raises `FetchError` |
| `FetchError` | `src/invio/sources/errors.py` | `FetchError(reason, *, url, status=None)`; URL is redacted |
| URL helpers | `src/invio/sources/urls.py` | redaction only; `normalize_url()` goes here |
| `RssSource` config | `src/invio/config/job.py` | `type`, `url`, `name`, `enabled`; **no `max_age_days` yet** |
| Loopback test server | `tests/http_helpers.py` | `server` fixture, `Route`, `LOOPBACK` |

Layering (ruff `TID251`): `invio.sources` must not import `invio.db`, `invio.services` or
`httpx2`; network access only through `SafeHttpClient`.

## Design

1. **`src/invio/sources/base.py`**: a `Source` protocol, `async fetch(config) -> list[Candidate]`.
2. **`normalize_url()`** in `src/invio/sources/urls.py`: lower-case the scheme and host, drop
   the default port, the fragment and tracking parameters (`utm_*`, plus common ones such as
   `fbclid`, `gclid`, `mc_cid`, `mc_eid`), keep the order of the remaining parameters. Candidates
   store the normalized URL and `url_hash(normalized)`.
3. **`src/invio/sources/rss.py`**: `RssSource` (adapter) takes a `SafeHttpClient`, fetches
   `config.url`, parses `content` with `feedparser` (bytes, so the XML declaration's encoding
   wins), and maps entries:
   - no `link` → skipped
   - link resolved against the feed URL (relative links), only http(s) kept
   - `title` stripped, fallback to the URL
   - `published_parsed` → `updated_parsed` → `None`, converted to an aware UTC `datetime`;
     any parse error → `None`
   - `teaser` from `summary` (plain text, stripped, truncated), else `None`
   - `type="article"`, `content_hash=None`
   - duplicate URLs (after normalization) within one feed are kept once
4. **`max_age_days`**: new optional field on the `RssSource` config
   (`StrictInt | None`, `ge=1`, default `None` = no age limit). Entries whose date is older than
   `now - max_age_days` are dropped; entries without a date are kept. Regenerate
   `docs/job.schema.json` with `uv run python -m invio.config.job`.
5. **Errors**: `NotModified` → `[]`. A malformed feed (`bozo` set and no entries, or no
   feed-version detected and no entries) raises `FetchError("malformed_feed", url=...)`. A
   well-formed but empty feed returns `[]`. HTTP errors propagate from the client as
   `FetchError`.

## Dependencies

- `feedparser` (runtime) — add with `uv add feedparser`.

## Acceptance criteria → tests

| Criterion | Test |
|-----------|------|
| RSS 2.0 and Atom fixtures yield correct candidates | `tests/test_source_rss.py` with fixtures under `tests/fixtures/feeds/` served by the loopback server |
| URLs differing only by `utm_*` get the same hash | `tests/test_source_urls.py` (`normalize_url`) and an RSS test |
| Entries older than `max_age_days` are dropped | `tests/test_source_rss.py` with a fixed `now` |
| Malformed feeds produce a `FetchError`, not a crash | `tests/test_source_rss.py` (HTML page, truncated XML, binary garbage) |

## Quality gates

```bash
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

Coverage must stay ≥ 95 %.

## Implementation decisions (deviations from the design above)

- The body is passed to `feedparser.parse()` as `io.BytesIO`: given raw `bytes`, feedparser first
  tries to open them as a file name or URL, which must never happen for untrusted input.
- Malformed = no feed version detected, or a real parse error (not the harmless
  `NonXMLContentType` / `CharacterEncodingOverride` notes) with no usable entry. A valid empty
  feed returns `[]`; a feed truncated after a complete item still yields that item.
- `max_age_days` follows `enabled` in the config model (type and locator stay first in saved
  files); entries exactly `max_age_days` old are kept.
- The adapter class is `RssFeedSource` (`Source[RssSource]`) to avoid clashing with the config
  model `RssSource`.
