## Summary

Video items get their spoken content as text: subtitles first, optional local transcription as a bounded fallback (closes #29).

- New adapter `invio.sources.transcript.YoutubeTranscripts`: one yt-dlp metadata call, the best subtitle track (job language, then English, then any; manual before automatic; at most 4 tracks tried; YouTube's automatic translation into the job language only as the last subtitle tier), WebVTT converted to clean text (`vtt_to_text`). **No audio is downloaded while any track yields text.**
- Fallback without subtitles (optional `whisper` extra, `uv sync --extra whisper`): audio-only download into a private temp dir, faster-whisper (CPU, int8, model size from settings) on a single-worker pool (one transcription at a time per process), temp files always removed.
- Bounds: `INVIO_WHISPER_MAX_DURATION_MINUTES` (default 90, longer videos are never downloaded), `INVIO_WHISPER_MAX_PER_RUN` (default 3; `0` switches the fallback off for good) and a run-time check (a transcription only starts if `now + duration + 10 min` fits before the run's lock expiry). Videos over the cap or the time are *deferred*: released for the next run (like a budget stop), never failed.
- Graph (amends the #21 contract): `video_path` now stores the transcript as `raw_content` and leads straight to `score_relevance` (edge `video_path -> score_relevance`; `extract_text` would fetch the watch page and overwrite the transcript). New `RunDeps.fetch_transcript` port and `max_transcriptions_per_run`; a permanent `TranscriptUnavailable` rates the item from title and description (stored as teaser if empty), a transient one fails the item for a later run.
- Shared refactor: the yt-dlp plumbing of #28 (null logger, private cookie copy, error classification) moved to the private `invio.sources.ytdlp`; `YoutubeSource` behaviour and tests are unchanged.
- Opt-in deployment switch: `invio_whisper_enabled` (default `false`) installs the extra and gives `invio-run-due.service` `invio_whisper_run_due_memory_max` (`3G`) through the existing drop-in mechanism. With the switch off nothing changes (no drop-in, no sandbox change).
- New settings: `INVIO_WHISPER_MAX_DURATION_MINUTES`, `INVIO_WHISPER_MAX_PER_RUN`; new role variables: `invio_whisper_enabled`, `invio_whisper_run_due_memory_max`.
- Docs: README (video transcripts), `.env.example`, `docs/deployment.md` (local transcription), role contract table.

## Justification

- **New optional runtime dependency `faster-whisper`** (Constitution IV): local, CPU-only speech-to-text without PyTorch or a cloud API (no cost, no data leaving the host). It is an extra (`whisper`), not part of the default or dev install; `uv.lock` is updated, CI keeps installing `--locked` without it, and tests never import it (a fake module is used for the default seam).
- mypy: one narrow `ignore_missing_imports` override for `faster_whisper`, imported lazily and only inside `invio.sources.transcript`.
- No secrets in errors or logs: reason codes, canonical video URLs and class names only (never yt-dlp's text, the proxy URL or the cookie path).

## Known limitations

- A running transcription cannot be interrupted: the process waits for it when it exits (documented in `docs/deployment.md`).
- The run-time check assumes transcription is not slower than the audio; on weak CPUs use a smaller model or a longer `INVIO_RUN_LOCK_SECONDS`. `TimeoutStartSec=3h` of `invio-run-due.service` covers all due jobs of one start while the check only knows its own job's lock deadline.
- YouTube audio formats may need an external JavaScript runtime for yt-dlp (reported as the permanent `no_audio_format` when missing).
- `MemoryMax` 3G is an estimate; measured `MemoryPeak` of a real 90-minute transcription: **not measured yet** (quickstart section 3, task T042 is open).

## Test plan

- [x] `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest` (without the `whisper` extra; coverage >= 95)
- [x] `uv run pytest tests/test_deploy_role.py tests/test_deploy_units.py`
- [x] No network access and no real Whisper in tests (autouse guard extended to the transcript seams)
- [ ] Molecule scenario with the switch on and off (written in `side_effect.yml`, not run locally)
- [ ] quickstart sections 2-4 against real YouTube and a deployed host (not run; needed to confirm the memory figure before release)
