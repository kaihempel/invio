# Research: Video Transcript Retrieval (#29)

All Technical Context unknowns are resolved below. Format per decision: **Decision**,
**Rationale**, **Alternatives considered**.

## R1 — Module layout and shared yt-dlp plumbing

**Decision**: New adapter module `src/invio/sources/transcript.py` (the issue's
`scout/sources/transcript.py`). The yt-dlp plumbing that `invio.sources.youtube` (#28) already
has and that the transcript adapter needs too moves to a new private module
`src/invio/sources/ytdlp.py`:

- `NullLogger` (swallows yt-dlp output, which can echo the proxy URL or cookie path),
- `private_cookie_copy(path)` (yt-dlp writes the jar back on close; we only ever hand it a
  `0600` copy in a fresh temp dir),
- `base_options(*, timeout, cookies_file, proxy)` (quiet, `ignoreconfig`, `cachedir=False`,
  `socket_timeout` capped at 20 s, `retries=0`, `allowed_extractors=["youtube.*"]`, cookies,
  proxy, logger),
- `classify_safely(exc) -> (reason, status)` and its helpers (`_chain`, `_http_status`,
  `_has_code`, `_network_errors`).

`invio.sources.youtube` imports them from there. It keeps only the names the #28 tests and the
test guard reach through the module (`_yt_dlp_extract`, `_private_cookie_copy`, `_MAX_WORKERS`) so
those tests stay unchanged. `ytdlp.py` also holds the shared `Extractor` type, the default
`extract_info` and `download` functions, the video-id check and canonical watch URL
(`parse_video_url`, `watch_url`) and `failure_text` (internal marker matching).

**Rationale**: Constitution I (contracts defined once) and "simplicity first": both adapters
must apply the same secret-safety rules (no proxy or cookie path in errors and logs), and a
second copy would drift. A neutral private module avoids `transcript.py` importing private names
from `youtube.py`.

**Alternatives considered**: importing `youtube._classify` etc. directly (couples two adapters
through private names); duplicating the code (two places to fix the next yt-dlp change).

## R2 — Public API and error model

**Decision**:

```text
Transcript(text: str, language: str, method: Literal["subtitles", "transcription"])   # frozen
YoutubeTranscripts.get_transcript(video_url, *, language, may_transcribe, deadline) -> Transcript
TranscriptUnavailable(FetchError)        # reason code, permanent or transient
TranscriptionDeferred(Exception)         # reason "cap_reached" | "not_enough_time"; never a failure
```

`TranscriptUnavailable` **subclasses `FetchError`** (in `invio.sources.errors`, next to
`RenderUnavailableError`). Permanence is decided by the existing `is_transient_fetch`, so
`retrying(...)`, `item_failure_message` and `failure_message` work unchanged:

| reason | when | transient? |
|---|---|---|
| `transcription_disabled` | no usable subtitles and the fallback is switched off (`INVIO_WHISPER_MAX_PER_RUN=0`): the video is rated from title and description, never deferred forever | no |
| `whisper_not_installed` | no usable subtitles and the `whisper` extra is missing; message carries `INSTALL_HINT = "install it with: uv sync --extra whisper"` | no |
| `too_long` | no subtitles and duration > limit; message: `too_long: <url> (95 min > limit 90 min)` | no |
| `video_unavailable` | private, deleted, region/age-blocked/members-only, HTTP 404/410, or HTTP 403 confirmed by such a text marker | no |
| `no_audio_format` | yt-dlp offers no audio-only format | no |
| `no_speech` | transcription produced empty text | no |
| `unsupported_url` | the item URL is not a YouTube watch/shorts/youtu.be URL | no |
| `timeout`, `connection_failed`, `invalid_response`, `http_status` 408/425/429/5xx | network trouble (reuses #28's classification) | yes |
| HTTP 403 without a confirming marker | reported as `invalid_response` with `status=403`: YouTube also answers 403 for IP blocks, proxy refusals and PO-token problems, so it is worth another try | yes |

`cookies_unavailable` stays a plain `FetchError` (as in #28): a configuration problem, not a
per-video fact; it fails the item (non-transient, so not retried within the run), and the
YouTube source fetch reports the same problem loudly.

A `TranscriptUnavailable` may carry `description: str | None` (the video description from the
metadata, **not** part of `str(err)`), so the pipeline can rate the item from title and
description (clarification 1).

**Rationale**: Reusing the `FetchError` contract means the transient/permanent split of
clarification 1 needs no new mechanism: transient → retried at the call boundary, then the
item error boundary marks the item `failed`; permanent → caught by the video node.

**Alternatives considered**: a separate exception hierarchy with a `transient: bool` flag (would
need its own retry predicate and message formatting); returning `Transcript | None` (loses the
reason the spec requires).

## R3 — Retrieval flow and subtitle track selection

**Decision**: One yt-dlp metadata extraction per video (`download=False`, not flat) on the
adapter's worker pool under `youtube_timeout_seconds`. It yields `duration`, `subtitles`
(manual), `automatic_captions`, `language` and `description`. Then:

1. Build the ordered candidate list (FR-003): manual in job language → original automatic in
   job language → manual `en` → original automatic `en` → any other manual → any other
   original automatic → automatic translation into the job language. Language keys match by
   primary subtag (`de`, `de-DE`, `de-orig` all count as `de`). An "original" automatic track is
   `<lang>-orig` or the key equal to `info["language"]`; `-orig` tracks come first within a tier.
2. For the first candidate: download it as VTT with yt-dlp (`skip_download`,
   `writesubtitles` or `writeautomaticsub`, `subtitleslangs=[key]`, `subtitlesformat="vtt"`,
   `outtmpl` into a fresh temp dir), read it, convert (R4). Empty text after conversion → try
   the next candidate.
3. If no candidate gives text → fallback (R5).

At most four tracks are tried per video. YouTube's machine translations (automatic tracks that
are not original) are only a candidate as the *last* subtitle tier, and only into the job
language, so that a real recognition in another language wins over a translation, and the audio
fallback follows after that. Other translations are never used.

A transient error while reading metadata or subtitles propagates as transient
`TranscriptUnavailable`; it **never** triggers the audio fallback (spec edge case).

**Rationale**: The metadata call is needed anyway (duration check before any download, AC 3)
and tells us which tracks exist without trying them blindly. The issue explicitly names
`writesubtitles` / `writeautomaticsub`; letting yt-dlp fetch the track keeps proxy and cookies
on one network stack (FR-012).

**Alternatives considered**: fetching the track URL from `info` with yt-dlp's `urlopen` (works,
but bypasses the format negotiation yt-dlp does for VTT); `youtube-transcript-api` (new
dependency, different network stack, ignores our proxy/cookie handling).

## R4 — VTT to plain text

**Decision**: Pure, stdlib-only function `vtt_to_text(vtt: str) -> str` in `transcript.py`:

- drop the `WEBVTT` header block and `NOTE`, `STYLE`, `REGION` blocks;
- drop cue identifier lines and timing lines (anything containing `-->`);
- strip inline tags, including `<c>`, `<v Speaker>`, `<b>`/`<i>`/`<u>`, `<lang …>` and inline
  timestamps `<00:00:01.240>`;
- `html.unescape` entities (`&amp;`, `&#39;`, `&nbsp;`), collapse whitespace per line;
- drop empty lines and any line equal to the previously **kept** line (this removes YouTube's
  rolling-caption repeats, where each cue repeats the previous line plus a new one);
- join kept lines with single spaces into one text.

**Rationale**: VTT is simple enough that a regex/line parser is reliable; pure function → easy
exhaustive tests (AC 2) and Hypothesis property tests (no `-->`, no `<…>`, no two equal
consecutive lines). No new dependency (`webvtt-py` would add one for ~40 lines).

**Alternatives considered**: `webvtt-py` (extra dependency, still needs our dedupe); asking
yt-dlp for `srt`/`json3` (YouTube-specific; VTT is what the issue specifies and what most
extractors offer).

## R5 — Audio fallback, temp files and the one-at-a-time limit

**Decision**:

- **Order of checks before any download**: fallback switched off (`whisper_max_per_run=0`) →
  `transcription_disabled`; duration known and > `whisper_max_duration_minutes`
  → `too_long`; `importlib.util.find_spec("faster_whisper") is None` → `whisper_not_installed`;
  `may_transcribe()` false → `TranscriptionDeferred("cap_reached")`; then, **inside the worker
  when the job actually starts**, the run-time check of R11 →
  `TranscriptionDeferred("not_enough_time")`. So a long video, a missing extra, a full cap or a
  near deadline never downloads audio.
- **One job at a time**: every transcription (download + transcribe) runs on a dedicated
  `ThreadPoolExecutor(max_workers=1)` owned by the adapter. The single worker is the limit: at
  most one job runs per process, others queue (FR-009). If the waiting coroutine is cancelled,
  a queued job is cancelled before it starts; a running job finishes in its thread and cleans up
  there, so a cancelled caller can never cause two parallel transcriptions.
- **Temp files**: the worker function opens `tempfile.TemporaryDirectory(prefix="invio-audio-",
  ignore_cleanup_errors=True)` and does download and transcription inside it, so the directory
  is removed in the same thread on success, error or interruption (FR-008). The cookie copy
  uses the same pattern (R1).
- **Download**: yt-dlp with `format="bestaudio"` (no `best` fallback: audio only),
  `outtmpl=<tmp>/audio.%(ext)s`, no post-processors (no ffmpeg needed). "Requested format is
  not available" → `no_audio_format`.
- **Unknown duration**: `faster_whisper` reports `info.duration` before the segments are
  consumed; if it is above the limit, stop before iterating segments → `too_long`.
- **Transcription**: `WhisperModel(settings.whisper_model_size, device="cpu",
  compute_type="int8")`, cached per size for the process (loading takes seconds). `language=None`
  (auto-detect), `vad_filter=True` (skips silence; also makes music-only videos produce empty
  text → `no_speech`). Segments are joined with spaces. Returned language: `info.language`.
- No `asyncio.wait_for` around the transcription: it is bounded by the duration cap and the
  socket timeout of the download; a hard timeout cannot stop the thread anyway (same reasoning
  as #28's module docstring), and it would release the slot while the thread is still busy.

**Rationale**: Meets AC 1, 3, 4, 5 and FR-009 with the simplest primitives; mirrors #28's
"worker pool, lazy import, temp copy" style.

**Alternatives considered**: `asyncio.Semaphore(1)` (does not cover a thread that keeps running
after its awaiter is cancelled); a process pool (heavier, model loaded per process); running
ffmpeg to convert to WAV (extra system dependency; PyAV inside faster-whisper decodes directly).

## R6 — Optional `whisper` extra

**Decision**: `[project.optional-dependencies] whisper = ["faster-whisper>=1.2"]`; `uv.lock`
updated. Not added to the `dev` group, so the default test environment has no faster-whisper
(spec FR-016). `faster_whisper` is imported only inside `transcript.py`'s default transcriber,
lazily. mypy override `module = ["faster_whisper", "faster_whisper.*"]
ignore_missing_imports = true`. Tests inject a fake transcriber; the "not installed" path is
tested by injecting `whisper_available=lambda: False` (and once with the real `find_spec` in
the default environment).

The first transcription downloads the model from the Hugging Face hub into the default cache
(~0.5 GB for `small`). Documented in the README/deployment notes. No new setting.

**Rationale**: Same pattern as the `render` extra (Playwright, #26). Constitution IV: new
runtime dependency justified in the PR description (local speech-to-text, CPU-only, int8).

**Alternatives considered**: `openai-whisper` (PyTorch, far heavier); a cloud speech-to-text API
(costs, data leaves the host; out of scope).

## R7 — Settings

**Decision**: Reuse `whisper_model_size` (already in `Settings`, `Literal["tiny", "base",
"small", "medium", "large-v3"]`, default `small` — matches clarification 3; unknown values are
already rejected). Add:

| Setting | Env | Type / default | Rule |
|---|---|---|---|
| `whisper_max_duration_minutes` | `INVIO_WHISPER_MAX_DURATION_MINUTES` | `int = 90` | `ge=1` |
| `whisper_max_per_run` | `INVIO_WHISPER_MAX_PER_RUN` | `int = 3` | `ge=0`; `0` switches the fallback off for good (the adapter raises the permanent `transcription_disabled`; the item is rated from title and description) |

Proxy, cookies and timeout are the existing `youtube_*` settings (FR-012).

**Rationale**: Grouping under `whisper_` matches the existing name; both limits only apply to
the audio fallback.

## R8 — Pipeline integration (`video_path`)

**Decision**:

- A new port in `invio.graph.ports`: `TranscriptFetcher` Protocol
  `(url, *, language, may_transcribe, deadline) -> Transcript` (`deadline` = the run's lock
  token `scope.token`, see R11), and a new `RunDeps` field
  `fetch_transcript: TranscriptFetcher`. Production wiring in `invio.pipeline.deps` creates
  `YoutubeTranscripts.from_settings(settings)` and closes it on exit; tests pass fakes.
- **Topology change**: `video_path → score_relevance` instead of `→ extract_text`. Otherwise
  `extract_text` would fetch the YouTube watch page and overwrite the transcript in
  `raw_content`. The node is wrapped with `_recording("extract_text", …)` (it is the item's
  text-acquisition stage, so `RunStage`, `STAGE_ORDER` and stored stats stay unchanged). The
  #21 contract ("in ItemState, out ItemState, `raw_content` set or left unchanged") is kept.
- New stage function `stages.video_transcript(state, deps, scope)`:
  - calls `deps.fetch_transcript(item.url, language=config.language, may_transcribe=…)`
    through `retrying(policy=deps.retry, retry_on=is_transient_fetch)`;
  - success → `ItemRepository.set_extracted(item, transcript.text)`; logs
    `extract.transcript` with `item_id`, `method`, `language`, `chars`;
  - permanent `TranscriptUnavailable` → logs `extract.transcript_unavailable` (`item_id`,
    `reason`); if `item.teaser` is empty and the error carries a description, stores it as the
    teaser (collapsed, capped at 2 000 chars) via new `ItemRepository.set_teaser`; returns
    normally, so the item is rated from title + description (clarification 1);
  - transient errors (after retries) and `cookies_unavailable` propagate → the existing error
    boundary marks the item `failed` (retried by a later run up to `max_attempts`).
- **Per-run cap** (clarification 4): `RunScope` gains `transcriptions: set[int]` (item ids that
  hold a transcription slot). `may_transcribe` is a closure called on the event-loop thread: it
  returns `True` if the item is already in the set (retries do not count twice), else adds it
  if `len(set) < settings.whisper_max_per_run`. The cap is passed to `RunDeps` as
  `max_transcriptions_per_run: int` (no settings import in the graph).
- **Deferral**: `TranscriptionDeferred` propagates out of the subgraph; `process_item`'s
  boundary catches it next to `BudgetExceeded`, calls `ItemRepository.release([item])` (attempt
  undone, back to `new`, `run_id` cleared — the #19 mechanism) and returns `{}`; logs
  `item.transcription_deferred` with `item_id` and `reason` (`cap_reached` or
  `not_enough_time`). The released item id is recorded in `RunScope.deferred`, and `persist`
  (`RunDraft.deferred`, read by `unprocessed()`) does not release it a second time after a
  budget stop (otherwise `attempts` would go down twice). For `not_enough_time` the item's slot is also removed from
  `scope.transcriptions`: the cap counts transcriptions that actually ran. The item has no `ItemResult`, so it is neither counted as
  processed nor failed.

**Rationale**: Ports keep `invio.graph` free of adapter construction (existing pattern for
`fetch_page`). Releasing on deferral reuses the budget-stop semantics the spec asks for
("left unchanged, like a budget-stopped item").

**Known limit (accepted)**: an item waiting for the single transcription worker holds one of
the run's `max_parallel_items` slots. With the default cap of 3 and 4 parallel items, at least
one slot always stays free for other items. The README notes that `INVIO_WHISPER_MAX_PER_RUN`
should stay below `INVIO_MAX_PARALLEL_ITEMS`.

**Alternatives considered**: keeping `video_path → extract_text` and making `extract_text` skip
items with `raw_content` (hides the video path's behaviour in another node); a new `RunStage`
`video_transcript` (touches stats, stored errors and progress output for no user benefit).

## R9 — Deployment risk: YouTube JavaScript challenges

**Finding**: Current yt-dlp releases need an external JavaScript runtime (e.g. Deno) to solve
YouTube's player challenges for full format lists; without it some or all audio formats can be
missing. Subtitles and metadata are usually still available.

**Decision**: No code change. Missing audio formats map to the permanent `no_audio_format`
(not retried forever). The deployment docs mention installing a JS runtime when the whisper
fallback is enabled.

## R10 — Testing approach

**Decision**: All tests offline (Constitution III):

- `vtt_to_text`: table tests with real-world-shaped fixtures (manual VTT, YouTube rolling auto
  captions, tags, entities, NOTE/STYLE blocks) plus Hypothesis properties.
- Track selection: pure function over `subtitles` / `automatic_captions` mappings.
- Adapter: injected `extract`, `download_subtitle`, `download_audio`, `transcribe` and
  `whisper_available` callables. Assertions: no audio call when subtitles exist (AC 1); too-long
  and not-installed raise before any download (AC 3, 5); temp dir gone after success and after
  a raising transcriber (AC 4, via a recorded `tmp` path); concurrency 1 (fake transcriber
  records max parallel entries across 3 concurrent calls).
- Graph: `test_graph_build.py`/`test_graph_extract.py` style tests with a fake
  `fetch_transcript` for success, permanent (teaser set, not failed), transient (failed,
  retried), deferral (released, attempts unchanged) and the cap of 3 with 5 videos.
- `test_pipeline_deps.py`: wiring builds and closes the adapter.
- Run-time check (R11): fake clock + deadline; 60-min video with 40 min left → deferred
  `not_enough_time`, no download, slot freed; the check uses the time at job start (a fake
  first job advances the clock while a second waits).
- Deployment (R12): `test_deploy_role.py` (defaults match the contract table; switch off → no
  drop-in, switch on → `3G` drop-in; validation of the new variables; `--extra whisper` only when
  enabled) and the Molecule verify step.

## R11 — Run-time check before a transcription starts (FR-009b, clarification 2026-10-11)

**Finding**: a run's deadline is its lock token `scope.token` (`locked_until`, set to claim time +
`INVIO_RUN_LOCK_SECONDS`, default 2 h). `stages.ensure_lock_held` raises `LockExpiredError`
(run-fatal) at the next item node or guarded stage once `deps.clock() >= scope.token`. One
transcription running past the deadline therefore fails the whole run.

**Decision**:

- The port receives `deadline: datetime | None` (`scope.token`; `None` = no check, for
  direct use outside a run) and the adapter gets a `clock: Callable[[], datetime]` (default
  `datetime.now(UTC)`; production passes `deps.clock`'s equivalent, tests a fake).
- The check runs **at the start of the worker job**, i.e. after the job waited for the single
  transcription worker (R5), before the temp dir is created and before any download:
  `needed = (duration_seconds or max_duration_minutes * 60) + DEADLINE_RESERVE`, start only if
  `clock() + needed <= deadline`; otherwise raise `TranscriptionDeferred("not_enough_time")`.
- `DEADLINE_RESERVE = 10 min` (module constant): time kept for the stages after the items
  (scoring/summarizing the transcribed item, synthesis, persist, notify), so that "fits in the
  remaining time" (spec) also means "the run still finishes before its deadline" (SC-011).
- Real-time assumption (transcription no slower than the audio's duration): `small`/int8 on a
  multi-core CPU is several times faster than real time, so this is conservative; with
  `medium`/`large-v3` on a weak CPU it may not hold. That case is documented (README: choose a
  smaller model or raise `INVIO_RUN_LOCK_SECONDS`).

**Known limit**: the check only knows the run's own lock deadline. The run-due service's
`TimeoutStartSec=3h` covers every due job of one service start, so several transcription-heavy
jobs run one after another can still exceed it. This is documented in `docs/deployment.md`
(keep `INVIO_WHISPER_MAX_PER_RUN` low or stagger schedules); no code change.

**Rationale**: checking when the job starts (not when it is queued) accounts for the time spent
waiting behind another transcription. Raising inside the worker keeps the decision next to the
single place that knows when the job really starts. Reusing `TranscriptionDeferred` gives the
spec's "defer exactly as FR-009a" for free.

**Alternatives considered**: checking when queuing (ignores waiting time); measuring transcription
speed and extrapolating (non-deterministic, hard to test); extending the lock while transcribing
(rejected in clarification, option D).

## R12 — Opt-in deployment switch (FR-018, clarification 2026-10-11)

**Findings** (`deploy/`):

- `invio-run-due.service` ships `MemoryMax=1G`, `TimeoutStartSec=3h`, `PrivateTmp=yes`,
  `ProtectSystem=strict`, `ReadWritePaths=/var/lib/invio` and
  `XDG_CACHE_HOME=/var/lib/invio/cache`. The unit has no `MemoryDenyWriteExecute` or
  `SystemCallFilter`, so CTranslate2/onnxruntime run unchanged.
- The Hugging Face hub cache defaults to `$XDG_CACHE_HOME/huggingface`, i.e.
  `/var/lib/invio/cache/huggingface`, which is writable and persistent. Temp audio lives in
  the unit's private `/tmp`. **No sandbox change is needed.**
- The role already writes `MemoryMax` drop-ins (`50-invio-role.conf`) through
  `invio_memory_overrides` only when the value differs from the shipped default, and removes the
  drop-in otherwise. `uv sync --locked --no-dev --no-editable` installs the venv.
- `tests/test_deploy_role.py` checks role defaults against the table in
  `specs/015-gh-issue-24/contracts/ansible-role.md`, and the Molecule `verify.yml` checks the
  effective `MemoryMax`.

**Decision**:

| Role variable | Default | Effect |
|---|---|---|
| `invio_whisper_enabled` | `false` | `true`: `uv sync … --extra whisper`; `false`: plain sync, which also removes a previously installed extra |
| `invio_whisper_run_due_memory_max` | `3G` | used as the run-due memory limit **instead of** `invio_run_due_memory_max` when enabled |

- `vars/main.yml`: the run-due entry of `invio_memory_overrides` becomes
  `value: "{{ invio_whisper_run_due_memory_max if invio_whisper_enabled | bool else invio_run_due_memory_max }}"`.
  With the switch off and default variables, no drop-in is written: the deployment is unchanged
  (SC-010).
- `validate.yml`: `invio_whisper_enabled` must be a boolean; `invio_whisper_run_due_memory_max`
  uses the existing `^[0-9]+[KMGT]?$` rule.
- `install.yml`: the `uv sync` argv gets `--extra whisper` when enabled. It stays `--locked`.
- The `INVIO_WHISPER_*` settings are set through the existing `invio_env_extra` (no new managed
  env keys).
- Contract table in `specs/015-gh-issue-24/contracts/ansible-role.md`, `docs/deployment.md`
  (switch, memory, model cache location `/var/lib/invio/cache/huggingface`, ~0.5 GB first
  download, JS-runtime note from R9), and Molecule: a scenario variable run with the switch on,
  checking `MemoryMax=3G` and that `faster_whisper` is importable in the venv.
- **Memory figure**: 3G for the default model (`small`, int8). This is an estimate: the model
  needs roughly 1 GB at peak, the invio process a few hundred MB, and there is headroom for yt-dlp
  and decoding. The quickstart includes a measurement (`MemoryPeak` after a real transcription)
  to confirm the figure before release. Documented guidance: `medium` → 4G, `large-v3` → 6G.

**Rationale**: reuses the role's existing drop-in mechanism, so "off" is byte-for-byte today's
deployment. One switch (spec) covers both install and limit.

**Alternatives considered**: raising `MemoryMax` in the shipped unit (changes every deployment);
a separate whisper systemd unit (more surface, needs IPC); docs only (rejected in clarification,
option A).
