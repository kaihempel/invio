# Contract: YouTube sources in the job file

**Feature**: [../spec.md](../spec.md) · extends the job-file contract of earlier features
(`docs/job.schema.json` is regenerated from the models).

```yaml
sources:
  - type: youtube_channel
    channel_id: "@somechannel"        # or UCxxxxxxxxxxxxxxxxxxxxxx, or https://www.youtube.com/@somechannel
    max_age_days: 14                   # optional, >= 1; default: no age limit
    max_items: 20                      # optional, 1..200; default 20
    name: Some Channel                 # optional
    enabled: true                      # optional
  - type: youtube_playlist
    playlist_id: PLxxxxxxxxxxxxxxxxxxxx   # or https://www.youtube.com/playlist?list=PLxxx
    max_items: 10
```

## Accepted `channel_id` values

| Form | Example | Listing URL built by invio |
|---|---|---|
| channel id | `UCabc_123-x` | `https://www.youtube.com/channel/UCabc_123-x/videos` |
| handle | `@some.channel` | `https://www.youtube.com/@some.channel/videos` |
| channel URL | `https://www.youtube.com/channel/UCabc…`, `https://www.youtube.com/@handle` (hosts `youtube.com`, `www.`, `m.`, `music.`) | as above |

## Accepted `playlist_id` values

| Form | Example | Listing URL |
|---|---|---|
| playlist id | `PLabc_123-x` | `https://www.youtube.com/playlist?list=PLabc_123-x` |
| playlist URL | `https://www.youtube.com/playlist?list=PLabc…` (also a watch URL carrying `list=`) | as above |

## Rejected (load-time error naming the field)

- empty value, value with whitespace, any character outside the patterns above
- non-`https` URL, host not in the allowed YouTube host list, channel URL without a channel id or
  handle, playlist URL without `list=`
- `max_items` < 1 or > 200, `max_age_days` < 1, non-integers (strict types), unknown keys

Existing job files with `channel_id: UC123` / `playlist_id: PL123` stay valid and unchanged.

## Environment (not in the job file)

| Variable | Meaning |
|---|---|
| `INVIO_YOUTUBE_COOKIES_FILE` | Netscape-format cookies file, readable by the service user (yt-dlp gets a private copy per listing; the file is never written) |
| `INVIO_YOUTUBE_PROXY` | proxy URL for YouTube listing requests (secret) |
| `INVIO_YOUTUBE_TIMEOUT_SECONDS` | hard timeout per listing, default 60 |
