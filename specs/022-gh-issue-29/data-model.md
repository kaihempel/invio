# Data Model: Video Transcript Retrieval (#29)

There is no database schema change: transcripts are stored in the existing `items.raw_content`
column (`LONG_TEXT`), and a fallback description in the existing `items.teaser`. Everything else
is an in-memory value or a setting.

## Transcript (value object, `invio.sources.transcript`)

| Field | Type | Rules |
|---|---|---|
| `text` | `str` | non-empty after stripping; no timestamps, markup or consecutive duplicate lines (subtitle path) |
| `language` | `str` | primary language subtag, lower case (`de`, `en`, `es`); from the chosen track key or Whisper's detected language |
| `method` | `Literal["subtitles", "transcription"]` | how the text was obtained |

Frozen, slotted dataclass. Its `__post_init__` raises `ValueError` for blank text or language.

## SubtitleTrack (internal, `invio.sources.transcript`)

| Field | Type | Notes |
|---|---|---|
| `key` | `str` | yt-dlp language key as offered (`de`, `en-US`, `en-orig`) |
| `language` | `str` | primary subtag of `key` |
| `automatic` | `bool` | `True` for `automatic_captions`, `False` for `subtitles` |
| `original` | `bool` | automatic track that is the video's own ASR track (`-orig`, or `language == info["language"]`) |

Selection order (FR-003, R3), at most 4 tracks: manual job language → original automatic job
language → manual `en` → original automatic `en` → other manual → other original automatic →
automatic translation into the job language (the last subtitle tier). Other machine translations
are never used. Tracks without a VTT format are ignored.

## VideoMetadata (internal)

What the single metadata extraction provides: `video_id`, `duration_seconds: int | None`,
`tracks: list[SubtitleTrack]`, `description: str | None`, `live: bool` (live, upcoming or post-live stream). Built defensively from the yt-dlp
info mapping (wrong types → treated as missing).

## TranscriptUnavailable (error, `invio.sources.errors`)

Subclass of `FetchError`: `reason: str`, `url: str` (redacted, canonical watch URL),
`status: int | None`, plus:

| Attribute | Type | Notes |
|---|---|---|
| `description` | `str \| None` | video description for rating without transcript; never in `str(err)` |
| `INSTALL_HINT` | `ClassVar[str]` | `"install it with: uv sync --extra whisper"`, appended to the message for `whisper_not_installed` |

Message formats:

- `whisper_not_installed: https://www.youtube.com/watch?v=ID (install it with: uv sync --extra whisper)`
- `too_long: https://www.youtube.com/watch?v=ID (95 min > limit 90 min)`
- `<reason>: <url>[ (HTTP <status>)]` for all others (inherited). `<url>` is the full canonical
  watch URL when it matches `https://www.youtube.com/watch?v=<id>`, else the URL without query.
- `transcription_disabled`: the fallback is switched off (`INVIO_WHISPER_MAX_PER_RUN=0`); permanent.

Permanent vs transient: `is_transient_fetch(err)` (unchanged function; R2 table).

## TranscriptionDeferred (signal, `invio.sources.errors`)

Plain `Exception` (not a `FetchError`, so it can never fail an item; it carries no URL) with
`reason: Literal["cap_reached", "not_enough_time"]`. Raised by the adapter when
`may_transcribe()` returns `False` (`cap_reached`), or when the job starts on the transcription
worker and `clock() + (duration or max duration) + 10 min > deadline` (`not_enough_time`,
research R11). Caught only by the `process_item` boundary.

## Settings (additions to `invio.config.settings.Settings`)

| Field | Env | Default | Validation |
|---|---|---|---|
| `whisper_model_size` (existing) | `INVIO_WHISPER_MODEL_SIZE` | `small` | `tiny`, `base`, `small`, `medium`, `large-v3` |
| `whisper_max_duration_minutes` | `INVIO_WHISPER_MAX_DURATION_MINUTES` | `90` | `int`, `>= 1` |
| `whisper_max_per_run` | `INVIO_WHISPER_MAX_PER_RUN` | `3` | `int`, `>= 0` (`0` = fallback off for good: `transcription_disabled`) |

## RunDeps / RunScope (additions, `invio.graph`)

| Where | Field | Type | Default |
|---|---|---|---|
| `RunDeps` | `fetch_transcript` | `TranscriptFetcher` | required in production; test helpers supply a fake that raises `TranscriptUnavailable("video_unavailable")` |
| `RunDeps` | `max_transcriptions_per_run` | `int` | `3`; `__post_init__` rejects `< 0` |
| `RunScope` | `transcriptions` | `set[int]` | empty; item ids holding a transcription slot this run (an item deferred for `not_enough_time` gives its slot back) |
| `RunScope` | `deferred` | `set[int]` | empty; item ids already released by a deferral, so `persist` does not release them again after a budget stop (`RunDraft.deferred`) |

The run deadline passed to the port is the existing `RunScope.token` (no new field).

## Deployment variables (Ansible role `invio`, research R12)

| Variable | Default | Validation | Effect |
|---|---|---|---|
| `invio_whisper_enabled` | `false` | boolean | `true`: venv synced with `--extra whisper`; `false`: plain sync (removes the extra) |
| `invio_whisper_run_due_memory_max` | `3G` | `^[0-9]+[KMGT]?$` | run-due `MemoryMax` drop-in while enabled (replaces `invio_run_due_memory_max`) |

Effective run-due memory limit: `invio_whisper_run_due_memory_max` if enabled, else
`invio_run_due_memory_max` (default `1G` = shipped unit; no drop-in written).

## Item state transitions (video items)

```text
taken (attempts+1) ─► video_path
   ├─ transcript ok ───────────────► status EXTRACTED, raw_content = text ─► score_relevance …
   ├─ permanent unavailable ───────► raw_content None, teaser ← description (if empty) ─► score_relevance …
   ├─ transient (after retries) ───► status FAILED, last_error = "TranscriptUnavailable: …"  (retried next run while attempts < max)
   └─ deferred (cap reached or ────► released: status NEW, attempts−1, run_id None  (picked up next run)
      not enough run time)
```
