# Contract: `YoutubeSource` adapter

**Feature**: [../spec.md](../spec.md) · implements `invio.sources.base.Source[…]` for
`YoutubeChannelSource | YoutubePlaylistSource`.

## Construction

```python
YoutubeSource(
    *,
    cookies_file: Path | None = None,
    proxy: str | None = None,          # plain string; the caller unwraps the SecretStr
    timeout: float = 60.0,
    extract: Extractor | None = None,   # tests; default calls yt_dlp
    now: Callable[[], datetime] | None = None,   # tests; aware UTC
)

Extractor = Callable[[str, Mapping[str, object]], Mapping[str, object]]
# (listing_url, yt_dlp_options) -> yt-dlp "info" mapping; blocking
```

## `fetch(config) -> list[Candidate]`

| Aspect | Guarantee |
|---|---|
| Output | ≤ `config.max_items` candidates, `type="video"`, canonical watch URL, `url_hash` set, `teaser=None`, `content_hash=None` |
| Order | dated videos newest first, then undated in listing order |
| Age | dated videos older than `max_age_days` are dropped; undated are kept |
| Duplicates | one candidate per video id |
| Skipped entries | entries lacking `id` or `title`, or with an id outside `[A-Za-z0-9_-]{1,64}`, are skipped silently (logged at debug) |
| Empty result | `[]`, no error |
| Network use | only via the injected extractor; never downloads media; options include `extract_flat="in_playlist"`, `skip_download=True`, `ignoreconfig=True`, `allowed_extractors=["youtube.*"]` |
| Threading | the extractor runs on a bounded thread pool owned by the adapter (`close()` shuts it down); the call is awaited with `timeout` (best effort: a timed-out thread runs on until yt-dlp's socket timeout) |

## Errors (all `FetchError`, `url` = canonical listing URL)

| Reason | When | Transient (`is_transient_fetch`) |
|---|---|---|
| `timeout` | wait timeout or socket timeout | yes |
| `connection_failed` | DNS/connect/proxy failure | yes |
| `http_status` (+status) | 429/403 block or bot check, 404/private/removed | 429 yes; 403/404 no |
| `invalid_response` | extractor returned an unusable shape or failed unexpectedly | yes |
| `cookies_unavailable` | configured cookies file not a readable file | no |

Never contained in a `FetchError` message or a log line: yt-dlp's own message, the proxy URL,
cookie file path or contents.

## Wiring

`default_deps.fetch_source(config)` returns `await youtube.fetch(config)` for
`config.type in {"youtube_channel", "youtube_playlist"}`. `SUPPORTED_SOURCES` includes both types.
