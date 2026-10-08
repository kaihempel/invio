## Summary

Adds the `youtube_channel` and `youtube_playlist` sources (closes #28).

- New adapter `invio.sources.youtube.YoutubeSource`: metadata-only flat listing through `yt-dlp` (no media download), emitting `video` candidates.
- Locators: channel id, `@handle`, channel/playlist URLs (https, youtube hosts only); the listing URL is rebuilt from a validated token.
- Per-source `max_age_days` and `max_items` (default 20, max 200); undated videos kept after dated ones.
- Settings: `INVIO_YOUTUBE_COOKIES_FILE`, `INVIO_YOUTUBE_PROXY`, `INVIO_YOUTUBE_TIMEOUT_SECONDS` (default 60).
- All failures map to `FetchError` for that source only; no proxy URL, cookie path or yt-dlp message is leaked. The cookies file is copied per call so yt-dlp never rewrites the original.

## Justification

- **New runtime dependency `yt-dlp`**: the only maintained way to list channels/playlists without an API key.
- **Documented `SafeHttpClient` bypass**: yt-dlp does its own networking; mitigated by `allowed_extractors=["youtube.*"]`, locator validation and a rebuilt listing URL (see plan → Complexity Tracking).

## Known limitations

- `max_age_days` only applies when yt-dlp supplies a date (flat listings often lack it).
- `playlistend` caps a playlist before the newest-first sort.
- Timeouts are best effort; a timed-out worker thread ends at yt-dlp's socket timeout.

## Test plan

- [x] `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest` (5010 passed, 35 skipped)
- [x] No network access in tests (guard fixture)
- [ ] quickstart.md walkthrough against real YouTube (not run)
