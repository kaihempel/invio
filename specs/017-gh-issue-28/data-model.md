# Data Model: YouTube Channel and Playlist Source

**Feature**: [spec.md](spec.md) · **Research**: [research.md](research.md)

No database change and no migration. The entities are configuration models and in-memory
records.

## YoutubeChannelSource (job file, existing model extended)

| Field | Type | Rule |
|---|---|---|
| `type` | `"youtube_channel"` | discriminator |
| `channel_id` | string | non-empty; one of: channel id `UC[A-Za-z0-9_-]+`-style token (`[A-Za-z0-9_-]+`), `@handle` (`@[A-Za-z0-9._-]+`), or `https` YouTube channel URL (R1, R3). Surrounding whitespace is rejected. |
| `name` | string \| null | existing |
| `enabled` | bool | existing, default true |
| `max_age_days` | int \| null | ≥ 1; null = no age limit (new) |
| `max_items` | int | 1..200, default 20 (new) |

## YoutubePlaylistSource (existing model extended)

Same as above with `type = "youtube_playlist"` and `playlist_id`: a playlist id
(`[A-Za-z0-9_-]+`) or an `https` YouTube URL carrying `?list=<id>`.

Validation failures name the field (`channel_id` / `playlist_id`, `max_items`, `max_age_days`)
and the violated rule; unknown keys stay rejected.

## Settings (existing, one field added)

| Variable | Type | Notes |
|---|---|---|
| `INVIO_YOUTUBE_COOKIES_FILE` | path \| unset | existing; must be a readable file when set |
| `INVIO_YOUTUBE_PROXY` | secret string \| unset | existing; may embed credentials, never logged |
| `INVIO_YOUTUBE_TIMEOUT_SECONDS` | float > 0, default 60 | new; hard limit for one listing |

## Internal records (module-private in `sources/youtube.py`)

- **`_Locator`**: frozen; `kind` (`channel_id` \| `handle` \| `playlist_id`), `token`; method
  `listing_url()` returns the canonical YouTube URL handed to yt-dlp (R3).
- **`_Video`**: frozen; `id`, `title`, `published: datetime | None` (aware UTC). Built from one
  yt-dlp entry; entries without `id` or `title` never become a `_Video`.

## Candidate (existing, `invio.domain.Candidate`)

| Field | Value for a video |
|---|---|
| `url` | `https://www.youtube.com/watch?v=<id>` |
| `url_hash` | `url_hash(url)` |
| `title` | video title (whitespace-normalized) |
| `published_at` | `_Video.published` |
| `type` | `"video"` |
| `teaser` | `None` |
| `content_hash` | `None` |

## Ordering / lifecycle

`yt-dlp entries` → `_Video` (skip invalid, drop duplicate ids) → age filter (`clamp_or_expire`)
→ sort: dated newest first, then undated in listing order → cut to `max_items` → `Candidate`.
No state is kept between runs.
