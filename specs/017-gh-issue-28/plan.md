# Implementation Plan: YouTube Channel and Playlist Source

**Branch**: `gh-issue-28` (constitution naming; the issue text suggests `issue/28-add-youtube-channel-and-playlist`, which is not used, and the spec scripts currently report `017-gh-issue-28`) | **Date**: 2026-10-08 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/017-gh-issue-28/spec.md`

## Summary

This plan adds the missing source adapter for the two YouTube source types that the job file
already accepts (`youtube_channel`, `youtube_playlist`). Today such sources are skipped with a
`source.unsupported` log line.

- **Adapter.** A new `invio.sources.youtube.YoutubeSource` lists a channel's uploads or a
  playlist with `yt-dlp` used as a library in flat, metadata-only mode. It returns one
  `Candidate(type="video")` per video: canonical watch URL, title, upload date. Nothing is
  downloaded, no transcript is fetched (that is #29).
- **Config.** The existing locator fields keep their names (`channel_id`, `playlist_id`) so saved
  job files stay valid, but now also accept an `@handle` or a YouTube URL. Both source models
  gain optional `max_age_days` (like RSS/sitemap) and `max_items` (default 20) (research R1).
- **Limits and order.** Age filter reuses `clamp_or_expire`; results are ordered dated-newest-first,
  then undated videos in listing order, then cut to `max_items` (clarification, research R4).
- **Execution.** `yt-dlp` is synchronous, so it runs in a worker thread under an
  `asyncio.wait_for` timeout plus yt-dlp's own socket timeout. Every failure becomes a
  `FetchError` with a short reason code and the canonical URL, never yt-dlp's raw message
  (research R5, R6).
- **Access settings.** `INVIO_YOUTUBE_COOKIES_FILE` and `INVIO_YOUTUBE_PROXY` already exist in
  `Settings`; the adapter now consumes them (research R7).
- **Wiring.** `default_deps.fetch_source` dispatches the two types to the adapter and
  `SUPPORTED_SOURCES` gains them. README, wizard prompts, example job file and the JSON schema
  are updated.

## Technical Context

**Language/Version**: Python 3.12+ (uv-managed)

**Primary Dependencies**: Pydantic v2 (existing); **new runtime dependency `yt-dlp`**, used only
inside `invio.sources.youtube` (justification in research R2 and the PR description)

**Storage**: N/A (candidates only; persistence is the pipeline's job)

**Testing**: pytest + pytest-asyncio; `yt-dlp` replaced by a fake extractor injected into the
adapter, so no network and no `yt-dlp` import is needed by most tests

**Target Platform**: Linux server (systemd timer), developed on macOS

**Project Type**: CLI application / library (`src/invio`)

**Performance Goals**: listing a source of ≤20 videos in under 10 s typical (spec SC-002); one
slow source never delays the run beyond its timeout (SC-005)

**Constraints**: metadata only; no media download; hard listing timeout (default 60 s,
settings-adjustable); secrets (proxy URL, cookie file contents) never in logs or error text;
mypy strict over `src/`

**Scale/Scope**: a handful of YouTube sources per job; ≤ `max_items` (default 20, upper bound 200)
videos each

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle / rule | Assessment |
|---|---|
| I. Strict contracts at boundaries | **Pass.** New config fields are validated in the strict Pydantic models (unknown keys still rejected, locator forms checked with a named-field message). `yt-dlp` output is untrusted: it is parsed into an internal frozen `_Video` record at one place before use. |
| II. CLI-first | **Pass.** No new command; sources run through `invio job run` / `run-due`. |
| III. Test-covered behaviour | **Pass.** Every acceptance criterion and rejection path (invalid locator, bad limits, missing cookies file, timeout, block, not found) gets a test with a faked extractor. |
| IV. Quality gates | **Pass with note.** `yt-dlp` has no complete type information; its use is confined to one function and a narrow, commented mypy override (research R2). `uv.lock` updated. |
| V. Secrets / observability | **Pass.** Proxy is a `SecretStr` already; errors carry only a reason code and the canonical URL; logs name the source key and error class only (R6). |
| Architecture: dependency direction | **Pass.** `sources` imports only `config` and `domain`. |
| Architecture: sources reach the network "only through `SafeHttpClient`" (`sources/base.py`) | **Deviation, justified** — see Complexity Tracking. |
| New runtime dependency justified | **Pass**, in research R2 and to be repeated in the PR. |
| README/docs updated for user-facing config change | **Planned** (tasks). |

**Post-design re-check**: unchanged; the one deviation is mitigated by the host allow-list and
canonical-URL construction (R3).

## Project Structure

### Documentation (this feature)

```text
specs/017-gh-issue-28/
├── plan.md              # This file (/speckit-plan command output)
├── research.md          # Phase 0 output (/speckit-plan command)
├── data-model.md        # Phase 1 output (/speckit-plan command)
├── quickstart.md        # Phase 1 output (/speckit-plan command)
├── contracts/           # Phase 1 output (/speckit-plan command)
│   ├── job-file.md
│   └── youtube-source.md
└── tasks.md             # Phase 2 output (/speckit-tasks command - NOT created by /speckit-plan)
```

### Source Code (repository root)

```text
src/invio/
├── config/
│   ├── job.py            # YoutubeChannelSource / YoutubePlaylistSource: locator forms, max_age_days, max_items
│   └── settings.py       # (existing youtube_cookies_file / youtube_proxy) + youtube_timeout_seconds
├── sources/
│   └── youtube.py        # NEW: YoutubeSource adapter, locator → canonical URL, error mapping
├── graph/stages.py       # SUPPORTED_SOURCES += youtube_channel, youtube_playlist
├── pipeline/deps.py      # fetch_source dispatches the two types
└── cli/wizard.py         # prompts say "id, @handle or URL"

tests/
├── test_source_youtube.py   # NEW: adapter tests with a fake extractor
├── test_job_sources.py      # locator + limit validation
├── test_settings.py         # timeout setting
├── test_pipeline_deps.py    # dispatch
└── test_pipeline_run.py     # end-to-end with a fake youtube extractor (replaces "unsupported" case)

docs/job.schema.json, docs/job.example.yaml, README.md   # updated
pyproject.toml, uv.lock                                  # yt-dlp dependency, mypy override
```

**Structure Decision**: Single project. One new module `sources/youtube.py` mirroring
`sitemap.py`/`rss.py`; changes elsewhere are small extensions of existing files.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Source adapter reaches the network via `yt-dlp`, bypassing `SafeHttpClient` (SSRF guard, robots, rate limit) | Listing YouTube channels/handles/playlists needs YouTube's extractor logic (consent pages, continuation tokens, bot checks); the issue mandates `yt-dlp` as a library | Re-implementing channel listing over `SafeHttpClient` is fragile and breaks whenever YouTube changes. Mitigated: only `https` YouTube hosts are accepted, the URL given to `yt-dlp` is built by invio from a validated id/handle (never the raw user string), and `allowed_extractors` restricts yt-dlp to its YouTube extractors (R3) |
