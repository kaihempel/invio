# Contract: `YoutubeTranscripts` adapter and `vtt_to_text`

**Feature**: [../spec.md](../spec.md) · module `invio.sources.transcript` · research R1–R6.

## Construction

```python
YoutubeTranscripts(
    *,
    cookies_file: Path | None = None,
    proxy: str | None = None,                 # plain string; the caller unwraps the SecretStr
    timeout: float = 60.0,                    # metadata + subtitle steps (hard timeout)
    model_size: WhisperModelSize = "small",
    max_duration_minutes: int = 90,
    transcription_enabled: bool = True,       # False: the fallback is off (INVIO_WHISPER_MAX_PER_RUN=0)
    backends: TranscriptBackends | None = None,   # test seams, see below
    clock: Callable[[], datetime] | None = None,  # aware UTC; default datetime.now(UTC)
)

@dataclass(frozen=True, slots=True)
class TranscriptBackends:       # every field None = the real implementation, resolved in __init__
    extract: Extractor | None                       # (video_url, options) -> info mapping
    subtitle: SubtitleDownloader | None             # (video_url, track, options, tmp) -> VTT text
    audio: AudioDownloader | None                   # (video_url, options, tmp) -> Path
    transcribe: Transcriber | None                  # (audio_path, model_size, max_seconds) -> (text, language)
    whisper_available: Callable[[], bool] | None    # default: find_spec("faster_whisper") is not None

DEADLINE_RESERVE: Final = timedelta(minutes=10)   # kept free for the stages after the items

@classmethod
def from_settings(cls, settings: Settings, *, clock=None) -> Self   # youtube_* + whisper_* settings

def close(self) -> None   # shuts both pools down without waiting
```

All seams are blocking callables; the adapter runs them on its pools. The defaults are looked up
when the adapter is built (not at import), so the autouse network guard of `tests/conftest.py`
(which replaces `transcript._yt_dlp_extract`, `_yt_dlp_subtitle`, `_yt_dlp_audio` and
`_whisper_transcribe`) takes effect. `Extractor` is defined once in `invio.sources.ytdlp` and
shared with `invio.sources.youtube`. A transcriber that finds the audio longer than `max_seconds`
raises `AudioTooLong(seconds)`, which the adapter maps to `too_long`; an `ImportError` from it
maps to `whisper_not_installed`.

## `async get_transcript(video_url, /, *, language, may_transcribe, deadline=None) -> Transcript`

`video_url` is positional-only; `may_transcribe` is required (no silent default that would let a
caller bypass the per-run cap). `deadline: datetime | None` is the run's deadline (aware); `None`
disables the run-time check.

| Step | Guarantee |
|---|---|
| URL | `video_url` must be a YouTube watch, shorts or `youtu.be` URL; the id is validated (`[A-Za-z0-9_-]{1,64}`) and the canonical `https://www.youtube.com/watch?v=<id>` is what yt-dlp receives. Otherwise `TranscriptUnavailable("unsupported_url")`. |
| Metadata | one extraction (`download=False`) under `timeout`; failure → `TranscriptUnavailable` with the #28 classification (transient or permanent, R2). |
| Subtitles | tracks tried in FR-003 order, **at most 4**; order: manual job language, original automatic job language, manual `en`, original automatic `en`, other manual, other original automatic, and last the automatic translation into the job language (other machine translations are never used); each converted with `vtt_to_text`; the first non-empty one is returned with `method="subtitles"` and the track's language. |
| No audio when subtitles exist | if any track yields text, no audio downloader call happens (AC 1). |
| Transient error on subtitles | raised as transient `TranscriptUnavailable`; the fallback is **not** tried. |
| Fallback guards (in order, before any download) | fallback switched off (`transcription_enabled=False`) → permanent `transcription_disabled` (rated from title and description, never deferred); a live, upcoming or still-processing stream (`is_live`, or `live_status` `is_live`/`is_upcoming`/`post_live`) → permanent `live_stream` (its audio has no end); `duration > max_duration` → `too_long`; extra missing → `whisper_not_installed` (message has the install hint; logged as a warning once per adapter, then at INFO); `may_transcribe()` false → `TranscriptionDeferred("cap_reached")`. |
| Run-time check (when the job starts on the worker, before the temp dir and any download) | with `deadline` set: `clock() + (duration or max_duration) + DEADLINE_RESERVE > deadline` → `TranscriptionDeferred("not_enough_time")`. |
| Fallback | on the single-worker transcription pool: temp dir → `bestaudio` download capped at `max_filesize = max_duration × 48 kB/s` (a larger format leaves no file: `no_audio_format`) → transcribe with `model_size`; unknown duration checked on the audio before transcription (`too_long`); empty text → `no_speech`; result `method="transcription"`, language = detected. |
| Concurrency | at most one fallback job (download + transcription) runs per adapter at any time; others wait, never fail. The cached Whisper model is shared process-wide and only used under a module lock, so several adapters stay safe. |
| Temp files | the job wrapper creates `invio-sub-*` / `invio-audio-*` temp dirs inside the worker thread and removes them (and the cookie copy) on success, error and cancellation; a removal that fails is logged as `transcript.cleanup_failed`, never raised. |
| Secrets | no proxy URL, cookie path or yt-dlp message text in any error or log line. |
| After `close()` | `RuntimeError`. |
| Errors | no `__cause__`/context of the library is kept (`raise ... from None`); the message shows the full canonical watch URL (the id is its query) and codes only. Text markers of the library message (internal, never printed) refine `invalid_response`: age/country/members-only/private → `video_unavailable`, "requested format is not available" → `no_audio_format`; HTTP 404/410 → `video_unavailable`; HTTP 403 → `video_unavailable` only when such a marker confirms it, otherwise transient (`invalid_response`, status 403); a seam's `TranscriptUnavailable` without `description` gets the video's; only `ModuleNotFoundError` for `faster_whisper` maps to `whisper_not_installed`, any other `ImportError` is a broken install (`invalid_response`, logged as `transcript.failed`); `TimeoutError` → `timeout`. |

`may_transcribe` is called on the event-loop thread, at most once per `get_transcript` call, and
only when the fallback is actually needed.

## Errors

See [../data-model.md](../data-model.md) (`TranscriptUnavailable`, `TranscriptionDeferred`) and
the reason table in [../research.md](../research.md) R2.

## `vtt_to_text(vtt: str) -> str`

Pure, stdlib only. Output contains no `-->`, no `<…>` tag, no `WEBVTT`/`NOTE`/`STYLE`/`REGION`
block, no cue identifier, decoded entities, single spaces, and no line equal to the previously
kept line. Empty input or input without cue text → `""`.

Example:

```text
WEBVTT
Kind: captions
Language: en

00:00:00.000 --> 00:00:02.000 align:start position:0%
hello<00:00:00.500><c> world</c>

00:00:02.000 --> 00:00:02.010
hello world

00:00:02.010 --> 00:00:04.000
hello world
this is &amp; more
```

→ `hello world this is & more`

## Logging (`invio.sources.transcript`)

| Event | Level | Fields |
|---|---|---|
| `transcript.subtitles` | info | `video_id`, `language`, `automatic` |
| `transcript.transcribed` | info | `video_id`, `language`, `seconds` |
| `transcript.unavailable` | info (warning for `whisper_not_installed`) | `video_id`, `reason` |
| `transcript.deferred` | info | `video_id`, `reason` (`cap_reached` / `not_enough_time`) |
| `transcript.failed` | warning | `video_id`, `error` (class name only) |
| `transcript.cleanup_failed` | warning | `prefix` (`invio-sub-` / `invio-audio-`) |
