# Quickstart: validating video transcript retrieval

**Feature**: [spec.md](spec.md) · **Contracts**: [contracts/](contracts/) · **Data model**: [data-model.md](data-model.md)

## Prerequisites

```bash
uv sync --locked                    # base install: no faster-whisper (the default test setup)
```

No network, YouTube account or Whisper model is needed for the automated checks.

## 1. Automated checks (mirror CI)

```bash
uv run ruff check && uv run ruff format --check
uv run mypy
uv run pytest tests/test_source_transcript_vtt.py tests/test_source_transcript.py \
  tests/test_source_youtube.py tests/test_source_youtube_errors.py \
  tests/test_graph_video_path.py tests/test_graph_build.py \
  tests/test_settings.py tests/test_pipeline_deps.py
uv run pytest                       # full suite, still without the whisper extra
```

Expected: all green. Mapping of acceptance criteria to tests:

| Acceptance criterion / story | Expected |
|---|---|
| Subtitles never trigger an audio download (AC 1, US1) | the fake audio downloader is never called when any track yields text |
| Clean VTT text (AC 2, US1) | no `-->`, no tags, no consecutive duplicates; rolling captions collapse; see the example in [contracts/transcript-adapter.md](contracts/transcript-adapter.md) |
| Track order (US1, FR-003) | job language > `en` > other; manual before automatic within a language; Spanish-only → `language="es"` |
| Too long skipped with clear error (AC 3, US3) | `TranscriptUnavailable` `too_long`, message names minutes and limit; no download |
| Temp files removed (AC 4, US2) | the recorded temp dir does not exist after success and after a raising transcriber |
| No extra → `TranscriptUnavailable` (AC 5, US3) | `whisper_not_installed` with the `uv sync --extra whisper` hint; subtitles still work |
| One transcription at a time (US2) | 3 concurrent fallback calls → max 1 inside the fake transcriber |
| Per-run cap 3 (US2, clarification 4) | 5 subtitle-less videos → 3 transcribed, 2 released (`new`, attempts unchanged) |
| Permanent vs transient (US4, clarification 1) | permanent → rated with description as teaser, not failed; transient → `failed`, retried next run |
| Not enough run time (US2 scenario 7, FR-009b) | fake clock, 60-min video, 40 min to the deadline → `TranscriptionDeferred("not_enough_time")` at job start, no download, item released, slot freed |
| Deployment switch (FR-018, SC-010) | `tests/test_deploy_role.py`: off → no drop-in, on → `3G` drop-in and `--extra whisper`; checks W1–W5 in [contracts/deployment-whisper.md](contracts/deployment-whisper.md) |

Deployment tests in addition (`uv sync --group deploy` for the linters and Molecule):

```bash
uv run pytest tests/test_deploy_role.py tests/test_deploy_units.py
```

## 2. Optional: real run with the whisper extra

Needs network access; the first transcription downloads the `small` model (~0.5 GB).
Current yt-dlp versions may need a JavaScript runtime (e.g. Deno) for YouTube audio formats
(research R9).

```bash
uv sync --extra whisper
export INVIO_WHISPER_MODEL_SIZE=small          # default
export INVIO_WHISPER_MAX_DURATION_MINUTES=90   # default
export INVIO_WHISPER_MAX_PER_RUN=3             # default; 0 disables the fallback
uv run invio job run <job-with-a-youtube-source> --dry-run --max-items 3 -v
```

Expected on stderr (JSON log lines): `extract.transcript` with `method` `subtitles` for videos
with captions, `transcription` for others; `item.transcription_deferred` once more than three
videos need the fallback. The digest on stdout summarizes the videos from their transcripts.
After the run, no `invio-audio-*` directory remains in the temp directory:

```bash
ls "${TMPDIR:-/tmp}" | grep invio-audio- || echo "clean"
```

## 3. Optional: deployed with the switch on (confirms the memory figure)

On a test host deployed by the role with `invio_whisper_enabled: true` (Molecule check W6
covers the install and the limit):

```bash
systemctl show -p MemoryMax invio-run-due.service          # MemoryMax=3221225472
sudo -u invio /opt/invio/.venv/bin/python -c "import faster_whisper"
sudo systemctl start invio-run-due.service                  # with a due job whose video has no subtitles
systemctl show -p MemoryPeak,Result invio-run-due.service   # Result=success; MemoryPeak well below 3G
ls /var/lib/invio/cache/huggingface                         # the downloaded model
```

Expected: the run succeeds and `MemoryPeak` confirms the 3G figure from research R12 (record the
value in the PR). With `invio_whisper_enabled: false` and a redeploy: `MemoryMax=1073741824`, no
`50-invio-role.conf` under `invio-run-due.service.d/`, and `import faster_whisper` fails.

## 4. Optional: without the extra

```bash
uv sync --locked                                # removes faster-whisper again
uv run invio job run <same-job> --dry-run --max-items 3 -v
```

Expected: videos with subtitles behave as above; others log
`extract.transcript_unavailable` with `reason` `whisper_not_installed` and are still rated
(from title and description). The run does not fail.
