---

description: "Task list for video transcript retrieval: subtitles first, Whisper fallback (#29)"
---

# Tasks: Video Transcript Retrieval (Subtitles First, Local Transcription Fallback)

**Input**: Design documents from `specs/022-gh-issue-29/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/transcript-adapter.md](contracts/transcript-adapter.md),
[contracts/graph-video-path.md](contracts/graph-video-path.md),
[contracts/deployment-whisper.md](contracts/deployment-whisper.md), [quickstart.md](quickstart.md)

**Tests**: Included. Constitution III and spec FR-016 require an automated test for every
acceptance scenario, including the rejection paths, offline and without the `whisper` extra.
Write each story's tests first and confirm they fail before implementing.

**Organization**: Tasks are grouped by user story in spec priority order:
- P1: US1 (subtitles as transcript), US4 (video items through the pipeline)
- P2: US2 (local transcription fallback), US3 (bounded and optional, incl. deployment switch)

`R<n>` refers to the decisions in [research.md](research.md), `W<n>` to the checks in
[contracts/deployment-whisper.md](contracts/deployment-whisper.md).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story the task belongs to (US1–US4)

## Conventions used by every task

- Source goes in `src/invio/`, tests in `tests/`. mypy runs strict over `src/`; the only new
  suppression is the `faster_whisper` override (T002). No `Any` leaks out of
  `invio.sources.transcript`.
- Tests never touch the network, never run real yt-dlp extractions or downloads and never load a
  Whisper model. The adapter's seams (`extract`, `download_subtitle`, `download_audio`,
  `transcribe`, `whisper_available`, `clock` in
  [contracts/transcript-adapter.md](contracts/transcript-adapter.md)) are injected. The default
  test environment does **not** have faster-whisper installed.
- `yt_dlp` and `faster_whisper` are imported lazily inside the default seam implementations
  only; importing `invio.sources.transcript` must work without the extra.
- Layering: `invio.sources.transcript` and `invio.sources.ytdlp` import only `invio.config`,
  `invio.domain` and `invio.sources.*` (ruff TID251 forbids `invio.db`). `invio.graph` reaches
  the adapter only through `RunDeps.fetch_transcript`.
- Secrets: never put a yt-dlp message, the proxy URL or the cookie path into an exception or a
  log line (R1, Constitution V). Log fields: ids, reasons, class names.
- Run `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest` before
  finishing a phase.

---

## Phase 1: Setup

- [x] T001 Add the optional extra `whisper = ["faster-whisper>=1.2"]` under `[project.optional-dependencies]` in `pyproject.toml` (next to `render`), **not** to the `dev` group, and regenerate `uv.lock` with `uv lock`; confirm `uv sync --locked` (without the extra) still works and `python -c "import faster_whisper"` fails in that environment (R6)
- [x] T002 Add a commented `[[tool.mypy.overrides]]` with `module = ["faster_whisper", "faster_whisper.*"]` and `ignore_missing_imports = true` in `pyproject.toml`, next to the `yt_dlp` override; comment: imported lazily and only inside `invio.sources.transcript` (R6)

---

## Phase 2: Foundational (blocks all user stories)

**Purpose**: shared yt-dlp plumbing, the error types, the settings and the `Transcript` value
every story uses.

- [x] T003 Create `src/invio/sources/ytdlp.py` (private shared plumbing, R1) by moving from `src/invio/sources/youtube.py`: `NullLogger` (was `_NullLogger`), `private_cookie_copy` (was `_private_cookie_copy`), the error classification `classify_safely` (was `_classify_safely`) with its helpers `_classify`, `_chain`, `_http_status`, `_has_code`, `_network_errors`, `_NetworkErrors`, `_CHAIN_DEPTH`, and a new `base_options(*, timeout: float, cookies_file: Path | None, proxy: str | None) -> dict[str, object]` returning the shared keys of `YoutubeSource._options` (`quiet`, `no_warnings`, `noprogress`, `ignoreconfig`, `cachedir=False`, `socket_timeout=min(timeout, 20.0)`, `retries=0`, `extractor_retries=0`, `allowed_extractors=["youtube.*"]`, `logger`, and `cookiefile`/`proxy` only when set). Module docstring explains why the code is shared (secret-safety rules once). In `src/invio/sources/youtube.py` import them and keep the old private names as aliases (`_NullLogger = NullLogger`, etc.) so `tests/test_source_youtube*.py` stay unchanged; `YoutubeSource._options` builds on `base_options` and adds `extract_flat`, `skip_download`, `playlistend`
- [x] T004 Run `uv run pytest tests/test_source_youtube.py tests/test_source_youtube_errors.py tests/test_source_youtube_default_extractor.py` after T003 and fix any regression; no test file may be edited for the move (pure refactor)
- [x] T005 [P] Add to `src/invio/sources/errors.py` (and `__all__`): `class TranscriptUnavailable(FetchError)` with `__init__(self, reason: str, *, url: str, status: int | None = None, description: str | None = None, detail: str | None = None)`, `INSTALL_HINT: ClassVar[str] = "install it with: uv sync --extra whisper"`; `description` is stored as an attribute and **never** appears in `str(err)`; for reason `whisper_not_installed` the message is `"whisper_not_installed: <url> (install it with: uv sync --extra whisper)"`; `detail` (e.g. `"95 min > limit 90 min"`) is appended as `" (<detail>)"`. Add `class TranscriptionDeferred(Exception)` (not a `FetchError`) with `reason: Literal["cap_reached", "not_enough_time"]` and `url`. Permanence stays with the unchanged `is_transient_fetch` (R2, data-model)
- [x] T006 [P] In `tests/test_source_errors.py` (create if missing, else extend the existing errors tests) cover T005: message formats for `whisper_not_installed`, `too_long` with detail, plain reasons with `status`; `description` absent from `str(err)` and `repr`; `is_transient_fetch` is false for `whisper_not_installed`, `too_long`, `video_unavailable`, `no_audio_format`, `no_speech`, `unsupported_url`, and true for `timeout`, `connection_failed`, `invalid_response`, `http_status` 429/503; `TranscriptionDeferred` is not a `FetchError`
- [x] T007 [P] Add to `Settings` in `src/invio/config/settings.py`, in the `# Transcription` block after `whisper_model_size`: `whisper_max_duration_minutes: int = Field(default=90, ge=1)` and `whisper_max_per_run: int = Field(default=3, ge=0)` (`0` disables the fallback). Cover in `tests/test_settings.py`: defaults, env overrides `INVIO_WHISPER_MAX_DURATION_MINUTES` / `INVIO_WHISPER_MAX_PER_RUN`, rejection of `0`/negative duration and negative per-run value, and that the existing `whisper_model_size` default is `small` and rejects `huge` (R7)
- [x] T008 Create `src/invio/sources/transcript.py` skeleton: module docstring (subtitles first, fallback bounded and optional, no download when subtitles exist, temp-dir and single-worker rules, secret safety; R3–R5, R11); frozen slotted `Transcript(text: str, language: str, method: Literal["subtitles", "transcription"])` whose `__post_init__` raises `ValueError` for blank `text` or `language`; internal frozen `SubtitleTrack(key, language, automatic, original)` and `VideoMetadata(video_id, duration_seconds: int | None, tracks: list[SubtitleTrack], description: str | None)`; seam type aliases `Extractor`, `SubtitleDownloader`, `AudioDownloader`, `Transcriber`; constant `DEADLINE_RESERVE: Final = timedelta(minutes=10)`; `__all__ = ["DEADLINE_RESERVE", "Transcript", "YoutubeTranscripts", "vtt_to_text"]`

**Checkpoint**: `ytdlp.py` shared, YouTube tests green, errors and settings in place, transcript module importable without the extra.

---

## Phase 3: User Story 1 - Use existing subtitles as the transcript (Priority: P1) 🎯 MVP

**Goal**: For a video with any usable subtitle track, return clean text, its language and
`method="subtitles"`, without ever downloading audio.

**Independent Test**: fake metadata with manual/automatic tracks in several languages; check the
chosen track, language, method, text cleanliness and that the audio seam is never called.

### Tests for User Story 1

- [x] T009 [P] [US1] Create `tests/test_source_transcript_vtt.py`: table tests for `vtt_to_text` covering the example in [contracts/transcript-adapter.md](contracts/transcript-adapter.md) (→ `"hello world this is & more"`), `WEBVTT` header with `Kind:`/`Language:` lines, `NOTE`/`STYLE`/`REGION` blocks, numeric and named cue identifiers, cue settings after the timing (`align:start position:0%`), inline timestamps `<00:00:00.500>`, `<c>`, `<v Speaker>`, `<b>`/`<i>`/`<u>`, `<lang de>`, entities `&amp;` `&#39;` `&nbsp;`, multi-line cues joined, YouTube rolling auto-captions (each cue repeats the previous line) collapsing to one occurrence, empty input and header-only input → `""`; plus Hypothesis properties over generated cue lists: output never contains `-->`, `<` followed by a tag, or two equal consecutive words-lines (AC 2, FR-004)
- [x] T010 [P] [US1] Create `tests/test_source_transcript.py` with fakes (`FakeExtract` returning a canned info mapping with `duration`, `subtitles`, `automatic_captions`, `language`, `description`; `FakeSubtitles` returning VTT per track key and recording calls; `FakeAudio`/`FakeTranscriber` that record calls) and US1 tests: track order per FR-003 — manual job language > automatic job language > manual `en` > automatic `en` > other manual > other automatic (original ASR `-orig` or `info["language"]` before translations); primary-subtag matching (`de-DE`, `de-orig` count as `de`); job `de` + manual `en` + automatic `de` → automatic `de`; job `de` + only Spanish → `language="es"` (US1 scenario 5); a track whose VTT converts to empty text → next track; tracks without a `vtt` format ignored; `method="subtitles"`; **the audio seam and `whisper_available` are never called** in any of these cases (AC 1, SC-001)
- [x] T011 [US1] In `tests/test_source_transcript.py` add URL and error tests: watch, `shorts/<id>` and `youtu.be/<id>` URLs map to the canonical `https://www.youtube.com/watch?v=<id>` given to the seams; a non-YouTube URL or invalid id → `TranscriptUnavailable("unsupported_url")` without any seam call; metadata seam raising a 429-style error → transient `TranscriptUnavailable` (`is_transient_fetch` true) and **no** audio call; "Video unavailable"/"This video is private"/404 → `video_unavailable` (permanent) carrying `description` when the info had one; metadata stalling past `timeout=0.05` → `timeout`; a cookies file that is missing → plain `FetchError("cookies_unavailable")`; no message contains the proxy URL, the cookie path or the raw library text; `close()` then `get_transcript` → `RuntimeError`

### Implementation for User Story 1

- [x] T012 [US1] Implement `vtt_to_text(vtt: str) -> str` in `src/invio/sources/transcript.py` per R4: drop the `WEBVTT` header block and `NOTE`/`STYLE`/`REGION` blocks, cue identifiers and every line containing `-->`; strip all `<…>` tags including inline timestamps; `html.unescape`; collapse whitespace per line; drop empty lines and lines equal to the previously kept line; join with single spaces. Stdlib only
- [x] T013 [US1] Implement metadata parsing and track selection in `src/invio/sources/transcript.py`: `_video_id(url) -> str | None` (watch `v=`, `/shorts/<id>`, `youtu.be/<id>`; id `[A-Za-z0-9_-]{1,64}`; hosts `youtube.com`, `www.youtube.com`, `m.youtube.com`, `youtu.be`); `_metadata(info) -> VideoMetadata` built defensively (wrong types = missing; `duration` int/float ≥ 0 only; tracks only when a `vtt` entry exists); `_ordered_tracks(tracks, job_language, video_language) -> list[SubtitleTrack]` implementing the FR-003 order of R3
- [x] T014 [US1] Implement `YoutubeTranscripts` construction and the subtitle path in `src/invio/sources/transcript.py` per [contracts/transcript-adapter.md](contracts/transcript-adapter.md): `__init__` with all seams and `clock`; `from_settings(settings, *, clock=None)` reading `youtube_cookies_file`, `youtube_proxy` (`.get_secret_value()`), `youtube_timeout_seconds`, `whisper_model_size`, `whisper_max_duration_minutes`; a metadata/subtitle `ThreadPoolExecutor(4, thread_name_prefix="invio-transcript")`; `close()`; `async get_transcript(video_url, *, language, may_transcribe=lambda: True, deadline=None)`: validate URL → `unsupported_url`; check cookies readable → `FetchError("cookies_unavailable")`; one metadata extraction (`download=False`, options from `ytdlp.base_options`) under `asyncio.wait_for(timeout)`; map failures with `ytdlp.classify_safely` → `TranscriptUnavailable(reason, url=…, status=…)` (404/403/410 and "unavailable/private" texts → `video_unavailable`), never the library text; then try tracks in order through `download_subtitle` (same pool and timeout; transient errors propagate, the fallback is not tried) and return the first non-empty `vtt_to_text` result as `Transcript(text, track.language, "subtitles")`; log `transcript.subtitles` (`video_id`, `language`, `automatic`). Until US2 lands, "no usable track" raises `TranscriptUnavailable("whisper_not_installed")` (temporary stub, replaced by the fallback in T030)
- [x] T015 [US1] Implement the default seams for US1 in `src/invio/sources/transcript.py`: `_yt_dlp_extract(url, options)` (lazy `import yt_dlp`, private cookie copy via `ytdlp.private_cookie_copy`, `extract_info(url, download=False)`, mapping check) and `_yt_dlp_subtitle(url, track, options, tmp)` (options `skip_download=True`, `writesubtitles` or `writeautomaticsub` per `track.automatic`, `subtitleslangs=[track.key]`, `subtitlesformat="vtt"`, `outtmpl=str(tmp / "sub.%(ext)s")`; read the single `*.vtt` file as UTF-8 with `errors="replace"`), the temp dir created with `tempfile.TemporaryDirectory(prefix="invio-sub-", ignore_cleanup_errors=True)` inside the worker function. Add a test in `tests/test_source_transcript.py` that checks the option names against a real `yt_dlp.YoutubeDL(params)` construction without network (as #28 T010a did)

**Checkpoint**: subtitle-backed videos produce clean transcripts; ACs 1 and 2 pass.

---

## Phase 4: User Story 4 - Video items flow through the pipeline (Priority: P1)

**Goal**: The `video_path` node stores the transcript as the item's content and continues to
scoring; permanent unavailability rates from title + description; transient failures fail the
item; deferral releases it.

**Independent Test**: item subgraph with a fake `fetch_transcript` for success, permanent,
transient and deferred outcomes.

### Tests for User Story 4

- [x] T016 [US4] Add a `FakeTranscripts` port to `tests/pipeline_helpers.py` (records `(url, language, deadline)`, calls `may_transcribe` when configured, returns a `Transcript` or raises a configured exception per URL; default: raise `TranscriptUnavailable("video_unavailable", url=url)`) and pass it as `fetch_transcript` in `make_deps` (new keyword `transcripts`, plus `max_transcriptions_per_run: int = 3`); update the other direct `RunDeps(...)` constructions in `tests/test_graph_state.py` and `tests/test_pipeline_concurrency.py`
- [x] T017 [P] [US4] Create `tests/test_graph_video_path.py`: (a) transcript → `raw_content == text`, status `extracted`, the page fetcher is **not** called, the item reaches `score_relevance` and the scorer sees the transcript; (b) permanent `TranscriptUnavailable` with `description="About X"` and empty teaser → `raw_content is None`, `teaser == "About X"`, not failed, rated, log `extract.transcript_unavailable` with `item_id` and `reason`; existing teaser is not overwritten; description longer than 2 000 chars is cut and whitespace collapsed; (c) transient `TranscriptUnavailable("timeout")` → retried per `deps.retry` (recording sleep), then status `failed`, `last_error` starts with `"TranscriptUnavailable: timeout"`, other items unaffected; (d) `TranscriptionDeferred("cap_reached")` → item released (status `new`, `attempts` back to the value before the take, `run_id` None), no `ItemResult`, not counted as processed or failed, log `item.transcription_deferred`; (e) the port receives `language == job.language` and `deadline == scope.token`; (f) an article item still goes through `extract_text` unchanged; (g) a plain `FetchError("cookies_unavailable")` from the port → item `failed` (spec FR-014 configuration case), not retried within the run
- [x] T018 [P] [US4] Update existing tests that relied on the placeholder: `tests/test_graph_extract.py` (video placeholder test near line 104 → remove or move to T017), `tests/test_graph_build.py` (video path near lines 139–183: video items now get their content from the fake transcript port, not from the page fetcher; node order `route_kind → video_path → score_relevance`), `tests/test_pipeline_run.py` (YouTube videos near line 258 configure `FakeTranscripts`)
- [x] T019 [P] [US4] In `tests/test_pipeline_deps.py` add: `default_deps` builds a `YoutubeTranscripts` from settings, passes `max_transcriptions_per_run == settings.whisper_max_per_run`, and closes the adapter on exit (also when the body raises)

### Implementation for User Story 4

- [x] T020 [US4] In `src/invio/graph/ports.py` add `TranscriptFetcher` Protocol `async __call__(self, url: str, *, language: str, may_transcribe: Callable[[], bool], deadline: datetime | None) -> Transcript` (import `Transcript` from `invio.sources.transcript`), add to `__all__`; add `RunDeps.fetch_transcript: TranscriptFetcher` (required, placed after `fetch_page`) and `max_transcriptions_per_run: int = 3`, with `__post_init__` raising `ValueError` for `< 0`. In `src/invio/graph/scope.py` add `transcriptions: set[int] = field(default_factory=set)` with a comment (item ids holding a transcription slot this run)
- [x] T021 [US4] In `src/invio/db/repositories.py` add `ItemRepository.set_teaser(item, teaser: str) -> None` (sets `teaser`, flushes; does not change the status) and a test in `tests/test_db_items.py`
- [x] T022 [US4] In `src/invio/graph/stages.py` add `async def video_transcript(state, deps, scope) -> ItemUpdate` per [contracts/graph-video-path.md](contracts/graph-video-path.md): load the item; `may_transcribe` closure (item id already in `scope.transcriptions` → `True`; else add it while `len(scope.transcriptions) < deps.max_transcriptions_per_run` → `True`; else `False`); call `deps.fetch_transcript(item.url, language=config.language, may_transcribe=…, deadline=scope.token)` through `retrying(policy=deps.retry, retry_on=is_transient_fetch, what="transcript", sleep=deps.sleep)`; success → `ItemRepository.set_extracted(item, transcript.text)` and log `extract.transcript` (`item_id`, `method`, `language`, `chars`); permanent `TranscriptUnavailable` (`not is_transient_fetch(err)`) → log `extract.transcript_unavailable` (`item_id`, `reason`), and if `item.teaser` is empty and `err.description` is set, `set_teaser(item, collapse(description)[:2000])`; transient errors and other exceptions propagate; return `{"stage": "extract_text"}`
- [x] T023 [US4] In `src/invio/graph/build.py`: remove the placeholder import, bind `video_path` to `stages.video_transcript` wrapped in `_recording("extract_text", deps, scope, …)`, change the edge `video_path → extract_text` into `video_path → score_relevance`, update the topology docstring; in `_process` catch `TranscriptionDeferred` next to `BudgetExceeded`: release the item with `ItemRepository(scope.session).release([item])` (a DB error → `_fatal` like `_fail_item`), for reason `not_enough_time` also `scope.transcriptions.discard(item_id)`, log `item.transcription_deferred` (`item_id`, `reason`), return `{}`. Remove `video_path` and its `__all__` entry from `src/invio/graph/nodes/extract.py` and update that module's docstring
- [x] T024 [US4] In `src/invio/pipeline/deps.py` build `transcripts = YoutubeTranscripts.from_settings(settings, clock=utcnow)` next to `YoutubeSource`, pass `fetch_transcript=transcripts.get_transcript` and `max_transcriptions_per_run=settings.whisper_max_per_run` to `RunDeps`, and close it in the same `finally` as the other adapters; update the module docstring list of closed resources
- [x] T025 [US4] Amend `specs/014-gh-issue-21/contracts/graph.md`: the `process_item` diagram and the sentence about the placeholder now point to [contracts/graph-video-path.md](contracts/graph-video-path.md) (edge `video_path → score_relevance`)

**Checkpoint**: MVP complete (US1 + US4): videos with subtitles are scored and summarized from their transcripts end to end.

---

## Phase 5: User Story 2 - Transcribe audio locally when no subtitles exist (Priority: P2)

**Goal**: Audio-only download into a temp dir, faster-whisper on a single worker, temp files
always removed, per-run cap and run-time check defer instead of fail.

**Independent Test**: fake metadata without subtitles, fake audio downloader and transcriber;
check method, cleanup on success and failure, max one concurrent job, cap and deadline deferral.

### Tests for User Story 2

- [x] T026 [P] [US2] In `tests/test_source_transcript.py` add fallback tests (fakes as in T010, `whisper_available=lambda: True`): no tracks → audio seam called once with the canonical URL and a temp dir, transcriber called with `model_size` from the constructor and the audio path → `Transcript(text, detected_language, "transcription")`; the temp dir (recorded by the fake) does not exist afterwards, after a raising downloader and after a raising transcriber (AC 4, FR-008); a transcriber returning `""`/whitespace → `no_speech` (permanent); "Requested format is not available" from the audio seam → `no_audio_format` (permanent); an audio-seam network error → transient `TranscriptUnavailable`
- [x] T027 [US2] In `tests/test_source_transcript.py` add concurrency and cancellation tests: three concurrent `get_transcript` calls needing the fallback → the fake transcriber records at most one active call at any time and all three succeed (FR-009, SC-006); cancelling a waiting caller cancels its queued job before it starts (no download recorded); cancelling the running caller lets the job finish in its thread and its temp dir is still removed; the next job starts only after that
- [x] T028 [US2] In `tests/test_source_transcript.py` add guard tests: `may_transcribe` returning `False` → `TranscriptionDeferred("cap_reached")`, no download, `may_transcribe` called exactly once and only when no subtitle track yields text; run-time check with a fake clock (R11): duration 60 min, deadline in 40 min → `TranscriptionDeferred("not_enough_time")` and no download (US2 scenario 7); duration 30 min, deadline in 41 min → runs (30 + 10 reserve ≤ 41); unknown duration with `max_duration_minutes=90` needs 100 min; `deadline=None` → no check; the check uses the clock **when the job starts**: a first job advances the fake clock by 30 min while a second (20 min, deadline 55 min from the original start) waits, so the second is deferred
- [x] T029 [P] [US2] In `tests/test_graph_video_path.py` add the per-run cap scenario with the real `may_transcribe` closure and a fake port that calls it for subtitle-less URLs: 5 such videos with `max_transcriptions_per_run=3` → 3 extracted, 2 released (`new`, attempts unchanged, not failed) and picked up by `list_pending` in the next run (US2 scenario 6); a retried item (transient error, then success) uses one slot only; a `not_enough_time` deferral frees its slot; `max_transcriptions_per_run=0` → every subtitle-less video deferred

### Implementation for User Story 2

- [x] T030 [US2] Implement the fallback in `src/invio/sources/transcript.py` (R5, R11): guards in order before any download — duration known and `> max_duration_minutes * 60` → `too_long` (US3), `not whisper_available()` → `whisper_not_installed` (US3), `not may_transcribe()` → `TranscriptionDeferred("cap_reached")`; then submit one job to a dedicated `ThreadPoolExecutor(max_workers=1, thread_name_prefix="invio-whisper")`. The job function, in the worker thread: run-time check (`deadline` set and `clock() + timedelta(seconds=duration or max_duration*60) + DEADLINE_RESERVE > deadline` → `TranscriptionDeferred("not_enough_time")`); `with tempfile.TemporaryDirectory(prefix="invio-audio-", ignore_cleanup_errors=True) as tmp:` download audio, transcribe with `max_seconds = max_duration*60`, return `(text, language)`. Await the future without `wait_for` (no hard timeout, R5); on cancellation call `future.cancel()` (stops a queued job only). Empty text → `no_speech`; map seam errors (format not available → `no_audio_format`, others via `ytdlp.classify_safely`). Log `transcript.transcribed` (`video_id`, `language`, `seconds`), `transcript.deferred` (`video_id`, `reason`). `close()` shuts this pool down too
- [x] T031 [US2] Implement the default audio and transcription seams in `src/invio/sources/transcript.py`: `_yt_dlp_audio(url, options, tmp) -> Path` (options from `ytdlp.base_options` plus `format="bestaudio"`, `outtmpl=str(tmp / "audio.%(ext)s")`, no post-processors; cookie copy inside `tmp`; return the single downloaded file); `_whisper_transcribe(path, model_size, max_seconds) -> tuple[str, str]` (lazy `from faster_whisper import WhisperModel`; model cached per size with `functools.cache`, `device="cpu"`, `compute_type="int8"`; `segments, info = model.transcribe(str(path), language=None, vad_filter=True)`; if `info.duration > max_seconds` raise `TranscriptUnavailable("too_long", …)` before consuming segments; join stripped segment texts with spaces; return `(text, info.language)`); `_whisper_available()` = `importlib.util.find_spec("faster_whisper") is not None`

**Checkpoint**: subtitle-less videos are transcribed when the extra is present, one at a time, within the per-run cap and the run deadline.

---

## Phase 6: User Story 3 - Bounded and optional transcription (Priority: P2)

**Goal**: Too-long videos and a missing extra never download audio and degrade to
`TranscriptUnavailable`; operators can enable the fallback on a deployed host with one switch.

**Independent Test**: subtitle-less videos over the limit and with the extra missing; role tests
with the switch off and on.

### Tests for User Story 3

- [x] T032 [P] [US3] In `tests/test_source_transcript.py` add: subtitle-less 120-min video with default limit → `TranscriptUnavailable("too_long")` whose message contains `"120 min > limit 90 min"`, no audio call, `may_transcribe` not called (AC 3, SC-003); limit 30 + 45-min video → same; a 120-min video **with** subtitles → subtitles returned (scenario 5); unknown duration and audio reported longer than the limit by the transcriber → `too_long`; `whisper_available=lambda: False` and no subtitles → `whisper_not_installed` with the install hint, no download (AC 5); `whisper_available=lambda: False` with subtitles → subtitles returned (scenario 4); one test with the real default `_whisper_available` in the default test environment (extra absent) → `whisper_not_installed`
- [x] T033 [P] [US3] In `tests/test_deploy_role.py` add checks W1–W5 of [contracts/deployment-whisper.md](contracts/deployment-whisper.md): defaults `invio_whisper_enabled: false` and `invio_whisper_run_due_memory_max: 3G` match the contract table (W1); the effective run-due override value equals `1G` with defaults, so no drop-in is written (W2, SC-010); with the switch on it is `3G`, and a custom `4G` is respected (W3); the `uv sync` argv in `tasks/install.yml` contains `--extra whisper` only when enabled (W4); validation rejects a non-boolean switch and `"3 GB"` naming the variable (W5), using the existing `_validate` helpers of that file

### Implementation for User Story 3

- [x] T034 [US3] In `src/invio/sources/transcript.py` add the helper `_too_long(url, seconds, limit_minutes, description) -> TranscriptUnavailable` (minutes rounded up, `detail=f"{minutes} min > limit {limit} min"`) used by both the pre-download guard of T030 and the post-download check in T031, and make sure the guard order is `too_long` → `whisper_not_installed` → `may_transcribe`, producing exactly the messages of [data-model.md](data-model.md): `"too_long: https://www.youtube.com/watch?v=ID (95 min > limit 90 min)"` (minutes rounded up) and `"whisper_not_installed: https://www.youtube.com/watch?v=ID (install it with: uv sync --extra whisper)"`; both carry `description`
- [x] T035 [US3] Ansible role (R12): in `deploy/ansible/roles/invio/defaults/main.yml` add `invio_whisper_enabled: false` and `invio_whisper_run_due_memory_max: 3G` with a comment (opt-in local transcription, raises run-due `MemoryMax`); in `deploy/ansible/roles/invio/vars/main.yml` set the run-due entry of `invio_memory_overrides` to `value: "{{ invio_whisper_run_due_memory_max if invio_whisper_enabled | bool else invio_run_due_memory_max }}"` (default stays `1G`); in `tasks/validate.yml` assert `invio_whisper_enabled is boolean` and `invio_whisper_run_due_memory_max | string is match('^[0-9]+[KMGT]?$')` with messages naming the variable; in `tasks/install.yml` append `--extra whisper` to the `uv sync` argv only when `invio_whisper_enabled | bool` (keep `--locked`)
- [x] T036 [US3] Add the two variables to the defaults table in `specs/015-gh-issue-24/contracts/ansible-role.md` (the role tests parse it) and to `deploy/ansible/playbook.example.yml` as commented optional variables
- [x] T037 [US3] Molecule (W6): in `deploy/ansible/molecule/default/` add a converge/verify path with `invio_whisper_enabled: true` (scenario variable or a second converge in `side_effect.yml`, following how `side_effect.yml` already re-converges) that checks `MemoryMax=3221225472` for `invio-run-due.service` and that `/opt/invio/.venv/bin/python -c "import faster_whisper"` succeeds; then re-converge with the switch off and check `MemoryMax=1073741824`, no `50-invio-role.conf`, and the import fails

**Checkpoint**: all four stories work; the default deployment is unchanged and the opt-in switch is tested.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T038 [P] Update `README.md`: video transcripts (subtitles first, track order, Spanish-only example), the optional `whisper` extra (`uv sync --extra whisper`, first-use model download ~0.5 GB), settings `INVIO_WHISPER_MODEL_SIZE` / `INVIO_WHISPER_MAX_DURATION_MINUTES` / `INVIO_WHISPER_MAX_PER_RUN`, per-run cap and run-time check (deferral, 10-min reserve; keep the cap below `INVIO_MAX_PARALLEL_ITEMS`; on slow CPUs prefer a smaller model or a longer `INVIO_RUN_LOCK_SECONDS`), behaviour without the extra, permanent vs transient failures, YouTube JS-runtime note (R9)
- [x] T039 [P] Add `INVIO_WHISPER_MAX_DURATION_MINUTES=90` and `INVIO_WHISPER_MAX_PER_RUN=3` next to `INVIO_WHISPER_MODEL_SIZE` in `.env.example` and in `deploy/env/invio.env.example` if it lists optional settings; keep the env-example tests green
- [x] T040 [P] Update `docs/deployment.md` per [contracts/deployment-whisper.md](contracts/deployment-whisper.md): the switch and its two variables, memory guidance (`small` 3G, `medium` 4G, `large-v3` 6G), model cache `/var/lib/invio/cache/huggingface`, outbound HTTPS to the Hugging Face hub on first use, temp audio in the private `/tmp`, sandbox unchanged, JS runtime for yt-dlp, `INVIO_WHISPER_*` via `invio_env_extra`; note that `TimeoutStartSec=3h` of `invio-run-due.service` covers all due jobs of one start, while each job's run-time check only knows its own lock deadline, so operators with several transcription-heavy jobs keep `INVIO_WHISPER_MAX_PER_RUN` low or stagger the schedules
- [x] T041 Run the full gate `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest` (without the extra), plus `uv run pytest tests/test_deploy_role.py tests/test_deploy_units.py`; walk through [quickstart.md](quickstart.md) section 1 and fix findings
- [ ] T042 Manual validation per [quickstart.md](quickstart.md) sections 2–4, **required before release** (SC-010 has no automated check for the memory figure): real run with the extra; deployed host with the switch on: record `MemoryPeak` for a real 90-minute transcription and adjust `invio_whisper_run_due_memory_max` and the docs if it is not comfortably below 3G; switch off again
- [x] T043 Prepare `specs/022-gh-issue-29/pr-description.md` (same structure as earlier features): reference issue #29, justify the new optional runtime dependency `faster-whisper` (Constitution IV), list new settings and role variables, the amended #21 graph contract (edge `video_path → score_relevance`), the shared `invio.sources.ytdlp` refactor, the measured `MemoryPeak` from T042; work on branch `gh-issue-29`

---

## Dependencies & Execution Order

- **Phase 1 → Phase 2 → stories → Polish.** T001 before any test that imports `faster_whisper`
  expectations (T032 real-default check); T002 before `mypy` runs on the transcript module.
- **Foundational**: T003 → T004 (refactor, then regression run). T005 ∥ T007 ∥ T003; T006 after
  T005; T008 after T005 (imports the errors).
- **US1** (T009–T015) after Phase 2. Tests T009–T011 first; T012 → T013 → T014 → T015 (same
  module). T014 temporarily ends "no usable track" with `whisper_not_installed` until T030.
- **US4** (T016–T025) after US1's T014 (the port imports `Transcript`; production wiring needs
  `YoutubeTranscripts`). T016 before T017/T018 (helpers). T020 → T022 → T023 → T024. T021 ∥ T020.
  US4 tests use a fake port, so US4 can also start right after T008 if T024 is done last.
- **US2** (T026–T031) after US1 (extends `get_transcript`); T029 needs US4 (T022/T023). T030 → T031.
- **US3** (T032–T037): T032/T034 after T030 (guards live in the fallback); T033 ∥ T035–T037 are
  independent of the Python code and can run any time after Phase 2.
- **Polish** after all stories; T038 ∥ T039 ∥ T040.

Delivery order: US1 → US4 (MVP) → US2 → US3. The transcript module is edited by US1, US2 and US3,
so those phases run sequentially; the deployment tasks of US3 can run in parallel with US1/US2.

## Parallel Opportunities

- Phase 2: T003 ∥ T005 ∥ T007; then T004 ∥ T006 ∥ T008.
- `[P]` marks only tasks in different files; tasks without it that share a file with a `[P]` task (T010/T011, T026/T027/T028) run after it.
- US1 tests: T009 ∥ T010, then T011 (same file as T010).
- US4: T016 first, then T017 ∥ T018 ∥ T019; T021 ∥ T020.
- US2 tests: T026 ∥ T029, then T027 → T028 (same file as T026).
- US3: T033, T035, T036, T037 (deployment) ∥ the Python work of US1/US2.
- Polish: T038 ∥ T039 ∥ T040.

```text
# Example: start US1 tests together
T009 tests/test_source_transcript_vtt.py
T010 tests/test_source_transcript.py (selection)
# then T011 in tests/test_source_transcript.py (same file as T010)
```

## Implementation Strategy

- **MVP**: Phases 1–4 (US1 + US4). Videos with subtitles are transcribed, scored and summarized
  end to end; videos without subtitles are rated from title and description
  (`whisper_not_installed`). Validate with quickstart section 1 (US1/US4 rows).
- **Increment 2**: US2 — the local fallback with cleanup, single worker, per-run cap and run-time
  check.
- **Increment 3**: US3 — final guard messages and the opt-in deployment switch (role, contract
  table, Molecule).
- **Polish**: docs, full gate, optional real-host validation, PR description.
- Total: 43 tasks (Setup 2, Foundational 6, US1 7, US4 10, US2 6, US3 6, Polish 6).

## Amendments made while implementing

The architect review decisions below override the task text above; contracts, research and the
data model were updated to match.

- **Double release (D1):** `RunScope.deferred` and `RunDraft.deferred` keep `persist` from releasing
  a deferred item again after a budget stop (`unprocessed()` excludes them). T023 releases through a
  small `_release_deferred` helper in `build.py`.
- **`fetch_transcript` and `get_transcript`** take the URL positionally-only (`url, /, *, ...`) and
  `may_transcribe` is required (D5). T016 `FakeTranscripts` follows that signature.
- **`whisper_max_per_run=0`** means the fallback is off for good (D2): the adapter raises the
  permanent `transcription_disabled` (new `transcription_enabled` constructor argument, set by
  `from_settings`); it is not "defer forever". The graph-level cap tests still cover a cap of 0 with a
  port that asks for a slot (T029).
- **Test seams (D9):** a frozen `TranscriptBackends` dataclass replaces the individual constructor
  arguments of T014 (`extract`, `subtitle`, `audio`, `transcribe`, `whisper_available`); `Extractor` is
  defined once in `ytdlp.py`. The autouse network guard (D3) replaces `transcript._yt_dlp_extract`,
  `_yt_dlp_subtitle`, `_yt_dlp_audio` and `_whisper_transcribe`.
- **Subtitle loop (D4):** at most 4 tracks; machine translations only in the job-language and English
  tiers. **Errors (D10, D11):** the message keeps the full canonical watch URL; text markers refine
  `invalid_response` into `video_unavailable` / `no_audio_format`. **Temp dirs (D12):** created by the job
  wrapper in the worker thread; a failed cleanup is logged as `transcript.cleanup_failed`.
- **ytdlp.py (D6):** `youtube.py` keeps only the aliases the tests use (`_yt_dlp_extract`,
  `_private_cookie_copy`, `_MAX_WORKERS`); `ytdlp.py` also holds `extract_info`, `download`, the
  video-id check (`parse_video_url`, `watch_url`) and `failure_text`.
- **Review fixes:** HTTP 403 is transient unless a marker confirms it; the `cleanup_failed` check runs in a
  `finally`; a seam's `no_audio_format` gets the video description; only the missing `faster_whisper` module is
  `whisper_not_installed`; the track order puts automatic translations into the job language last;
  `whisper_not_installed` is logged at warning; `TranscriptionDeferred` has no `url`; `MAX_WORKERS` is shared
  from `ytdlp.py`.
- **T039:** `deploy/env/invio.env.example` lists only the keys the role manages, so it is unchanged.
- **T042** (real run and `MemoryPeak` on a deployed host) needs YouTube access, the whisper model
  download and a systemd host; it is left open. The Molecule scenario of T037 is written but was not run.
