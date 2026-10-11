# Implementation Plan: Video Transcript Retrieval (Subtitles First, Local Transcription Fallback)

**Branch**: `gh-issue-29` | **Date**: 2026-10-11 (updated after the 2026-10-11 clarifications) | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/022-gh-issue-29/spec.md`

## Summary

Video items (from the YouTube source of #28) get their spoken content as text. A new adapter
`invio.sources.transcript.YoutubeTranscripts` reads the video's metadata once with yt-dlp, picks
the best subtitle track (job language → English → any; manual before automatic), downloads it
as VTT and converts it to clean text. Only if no track yields text does it fall back to local
transcription: audio-only download into a temp dir and `faster-whisper` (model size from
settings) on a single-worker pool. The fallback is an optional extra (`uv sync --extra
whisper`), bounded by a 90-minute duration cap, a per-run cap of 3, and a run-time check: a
transcription only starts if the video still fits before the run's deadline (lock expiry) minus
a 10-minute reserve, otherwise the video is deferred. Failures use the existing
`FetchError` contract: permanent ones let the item be rated from title + description, transient
ones fail the item for a later retry, and the per-run cap releases the item to the next run.
The `video_path` node of the #21 `process_item` subgraph calls the adapter through a new
`RunDeps` port and now leads straight to `score_relevance`. The Ansible role gains an opt-in
switch (`invio_whisper_enabled`, default off) that installs the extra and raises the run-due
service's `MemoryMax` to `3G` through the role's existing drop-in mechanism; with the switch off
the deployment is unchanged.

## Technical Context

**Language/Version**: Python 3.12+ (uv-managed)

**Primary Dependencies**: yt-dlp (existing), faster-whisper ≥ 1.2 (new, optional extra
`whisper`), LangGraph (existing), Pydantic v2 / pydantic-settings (existing)

**Storage**: existing `items` table (`raw_content` for the transcript, `teaser` for the fallback
description); no migration

**Testing**: pytest (+ pytest-asyncio, Hypothesis); fakes for yt-dlp, subtitle and audio
download and the transcriber; default test environment without faster-whisper

**Target Platform**: Linux server (cron/systemd, CPU only); macOS for development

**Project Type**: CLI tool / single Python package (`src/invio`)

**Performance Goals**: subtitle path a few seconds per video (one metadata call + one small VTT
download); fallback bounded by the duration cap (≤ 90 min audio) and one job at a time

**Constraints**: at most 1 concurrent transcription per process; ≤ 3 transcriptions per run
(default); a transcription starts only if `duration + 10 min` fits before the run deadline;
no audio download when any subtitles exist; temp files always removed; no secrets in logs or
errors; tests offline; run-due service `MemoryMax` 1G unless the deployment switch is on (3G);
systemd sandbox unchanged

**Scale/Scope**: up to ~20 videos per source per run (#28 default); 2 new source modules, 1
changed source module, graph port/stage/topology change, 2 settings, Ansible role switch
(2 variables), docs

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
|---|---|---|
| I. Strict contracts at boundaries | yt-dlp info mappings are parsed defensively into internal records (`VideoMetadata`, `SubtitleTrack`); new settings are typed and validated (`ge=1`, `ge=0`); the video id is validated and the URL rebuilt before yt-dlp sees it; errors reuse the single `FetchError` contract | ✅ |
| II. CLI-first | no new command; behaviour reached through `invio job run` / `run-due`; logs to stderr | ✅ |
| III. Test-covered behaviour | every AC and clarification mapped to offline tests (quickstart table); no network, no real Whisper; rejection paths (too long, not installed, invalid settings) tested | ✅ |
| IV. Quality gates mirror CI | mypy strict: faster-whisper and yt-dlp only behind narrow, commented `ignore_missing_imports` overrides; `uv.lock` updated, CI installs `--locked` | ✅ |
| V. Secrets & observability | proxy stays `SecretStr` until the adapter; yt-dlp output swallowed; errors carry reason codes and canonical URLs only; structured log events with `item_id` inside the run context | ✅ |
| Layering | `invio.sources.transcript` imports `config` and `sources.*` only (no `db`, enforced by ruff TID251); `invio.graph` gets the adapter through a port; `invio.pipeline.deps` wires it | ✅ |
| New runtime dependency | `faster-whisper` is optional (extra), justified in the PR (local CPU speech-to-text, int8) | ✅ (justify in PR) |
| Deployment hardening (#24) | switch off → no drop-in, no sandbox or unit change (SC-010); switch on → only `MemoryMax` via the existing drop-in; model cache and temp audio stay inside `/var/lib/invio` and the private `/tmp` (R12) | ✅ |
| Docs for user-facing changes | README (video transcripts, `whisper` extra, settings, run-time check), `.env.example`, `docs/deployment.md` (switch, memory guidance, model cache, JS runtime), ansible-role contract table | ✅ (task) |

No violations, so Complexity Tracking stays empty.

**Post-design re-check (after Phase 1, repeated 2026-10-11)**: still passes. The design adds
one port field and one scope field; the run deadline reuses the existing lock token, and the
deployment switch reuses the role's drop-in mechanism. It keeps `RunStage` unchanged and adds no DB schema change. The only deviation from
the #21 contract is the edge `video_path → score_relevance`; the node's in/out contract is kept.
The amendment is recorded in [contracts/graph-video-path.md](contracts/graph-video-path.md).

## Project Structure

### Documentation (this feature)

```text
specs/022-gh-issue-29/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0: R1–R12
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── transcript-adapter.md   # YoutubeTranscripts, vtt_to_text, errors, logging
│   ├── graph-video-path.md     # port, topology change, node outcomes, deferral, deadline
│   └── deployment-whisper.md   # opt-in Ansible switch, memory limit, checks W1–W6
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
src/invio/
├── config/settings.py            # + whisper_max_duration_minutes, whisper_max_per_run
├── sources/
│   ├── errors.py                 # + TranscriptUnavailable(FetchError), TranscriptionDeferred
│   ├── ytdlp.py                  # NEW: shared yt-dlp plumbing (options, cookie copy, null logger, classify)
│   ├── youtube.py                # uses ytdlp.py; old private names kept as aliases
│   └── transcript.py             # NEW: Transcript, YoutubeTranscripts, track selection, vtt_to_text, defaults
├── db/repositories.py            # + ItemRepository.set_teaser
├── graph/
│   ├── ports.py                  # + TranscriptFetcher, RunDeps.fetch_transcript / max_transcriptions_per_run
│   ├── scope.py                  # + RunScope.transcriptions
│   ├── stages.py                 # + video_transcript stage
│   ├── nodes/extract.py          # placeholder video_path removed (stage moves to stages.py)
│   └── build.py                  # video_path wired, edge → score_relevance, TranscriptionDeferred boundary
└── pipeline/deps.py              # builds/closes YoutubeTranscripts, passes the cap

tests/
├── test_source_transcript_vtt.py      # NEW: vtt_to_text tables + Hypothesis properties
├── test_source_transcript.py          # NEW: adapter (selection, fallback, guards, temp, concurrency, errors)
├── test_graph_video_path.py           # NEW: node outcomes, cap/deferral, topology
├── youtube_helpers.py / pipeline_helpers.py   # + fake transcript fetcher, info builders
├── test_source_youtube*.py            # unchanged, must still pass after the ytdlp.py move
├── test_settings.py, test_pipeline_deps.py, test_graph_build.py   # extended

tests/test_deploy_role.py              # + switch defaults, drop-in on/off, --extra, validation

deploy/ansible/roles/invio/
├── defaults/main.yml             # + invio_whisper_enabled, invio_whisper_run_due_memory_max
├── vars/main.yml                 # run-due memory override picks the whisper limit when enabled
└── tasks/{validate,install}.yml  # validate the new variables; --extra whisper in uv sync
deploy/ansible/molecule/          # verify W6 with the switch on

pyproject.toml   # + [project.optional-dependencies] whisper; mypy override faster_whisper
uv.lock          # regenerated
README.md, .env.example, docs/deployment.md, specs/015-gh-issue-24/contracts/ansible-role.md
```

**Structure Decision**: Single package layout as defined in the constitution. The transcript
adapter sits next to the YouTube source in `invio.sources`. The graph reaches it only through
`RunDeps`, and production wiring stays in `invio.pipeline.deps`.

## Complexity Tracking

No constitution violations to justify.
