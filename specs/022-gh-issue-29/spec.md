# Feature Specification: Video Transcript Retrieval (Subtitles First, Local Transcription Fallback)

**Feature Branch**: `gh-issue-29`

**Created**: 2026-10-10

**Status**: Draft

**Input**: User description: "GitHub issue #29: [FEAT] Add video transcript retrieval (subtitles first, Whisper fallback). Depends on #28 and #21 (plugs the video path into the process_item subgraph). Context: Subtitles are fast and free; Whisper is only needed when none exist. Local Whisper is resource-heavy, so it must be bounded and optional. Requirements: 1) Create `get_transcript(video_url) -> Transcript(text, language, method)`. 2) Step 1: fetch manual subtitles, then auto-generated subtitles via yt-dlp, preferring `job.language` and English; convert VTT to plain text and remove duplicate caption lines. 3) Step 2 (fallback): download audio only (bestaudio, max duration limit, default 90 min) to a temp dir; transcribe with faster-whisper (model size from settings); delete temp files in finally. 4) Make Whisper an optional extra (`uv sync --extra whisper`); raise `TranscriptUnavailable` if not installed and no subtitles exist. 5) Limit concurrent Whisper jobs to 1. 6) Plug into the `process_item` subgraph of #21 as the video path. Acceptance criteria: videos with subtitles never trigger an audio download; VTT conversion yields clean text without timestamps or duplicates; videos longer than the limit are skipped with a clear error; temp audio files are removed after success and failure; without the `whisper` extra, behavior degrades to `TranscriptUnavailable`."

## Clarifications

### Session 2026-10-10

- Q: When a video's transcript can't be obtained, what should happen to that item? → A: Split by cause: permanent causes (too long, transcription component missing, private/deleted/region-blocked video, empty result) are rated from title and description without a transcript, like a too-short article page; transient causes (network error, timeout, IP block) mark the item `failed` so a later run retries it.
- Q: If a video has subtitles but none in the job's language or English, should the system use those subtitles or transcribe the audio instead? → A: Use any available subtitle track (manual before automatic) and record its language; audio is never downloaded when any subtitles exist.
- Q: Which local transcription model size should be the default when the operator hasn't set one? → A: `small` (multilingual, CPU-friendly); operators can choose another size in settings.
- Q: Should the time a run spends on local transcription be limited beyond the 90-minute video length cap? → A: Yes, by a per-run cap on transcriptions (configurable, default 3); videos beyond the cap are deferred to the next run (left unchanged, like a budget-stopped item), not failed, and their retry attempts are not consumed.

### Session 2026-10-11

- Q: How should the standard deployment (systemd units and the Ansible role) handle the optional Whisper fallback? → A: Opt-in deployment variable (default off) that installs the transcription component and raises the service memory limit to a documented value; the default deployment stays unchanged.
- Q: Should the system avoid starting a local transcription when there isn't enough run time left to finish it? → A: Yes: start it only if the video's duration (assuming transcription is no slower than real time) fits in the run's remaining time; otherwise defer the video to the next run. Videos of unknown duration need at least the maximum duration left.

## User Scenarios & Testing *(mandatory)*

The primary user is the operator who runs research jobs that include video sources (channels and
playlists from #28). They want each relevant video to be represented by its spoken content as
text so it can be scored and summarized like an article. The secondary user is the item
processing pipeline (#21), whose video path currently passes items through unchanged and must
now supply the transcript text as the item's content.

### User Story 1 - Use existing subtitles as the transcript (Priority: P1)

When a video candidate is processed, the system first looks for subtitles published with the
video: manually created subtitles first, then automatically generated ones. Among the available
tracks it prefers the job's language, then English. The chosen subtitles are converted into
clean, readable plain text (no timing marks, no formatting markup, no repeated caption lines) and
returned as the transcript together with its language and the method "subtitles". No audio is
ever downloaded when subtitles are available.

**Why this priority**: Most videos have subtitles, and this path is fast, free and needs no extra
components. On its own it already makes most video candidates usable by the pipeline (MVP).

**Independent Test**: With a stubbed video backend offering manual and/or automatic subtitle
tracks in various languages, request a transcript and verify the chosen track, the returned
language and method, the cleanliness of the text, and that no audio download was attempted.

**Acceptance Scenarios**:

1. **Given** a video with manual subtitles in the job's language, **When** a transcript is
   requested, **Then** the transcript text comes from those subtitles, its language is the job's
   language, its method is "subtitles", and no audio download occurs.
2. **Given** a video with only automatically generated subtitles, **When** a transcript is
   requested, **Then** the automatic subtitles are used and no audio download occurs.
3. **Given** a video with manual subtitles only in English and automatic subtitles in the job's
   language, **When** a transcript is requested, **Then** the automatic subtitles in the job's
   language are used, because the job's language takes precedence over English and manual tracks
   win only within the same language (see FR-003 for the exact order).
4. **Given** subtitle content with timing lines, cue numbers, inline formatting tags and captions
   repeated across consecutive cues (typical of rolling automatic captions), **When** it is
   converted, **Then** the resulting text contains no timestamps, no cue identifiers, no markup,
   and no consecutive duplicate lines.
5. **Given** a job in German and a video with subtitles only in Spanish, **When** a transcript is
   requested, **Then** the Spanish subtitles are used, the transcript language is Spanish, and no
   audio download occurs.

---

### User Story 2 - Transcribe audio locally when no subtitles exist (Priority: P2)

When a video has no usable subtitles and the optional local transcription component is installed,
the system downloads only the video's audio to a temporary location, transcribes it locally with
the model size configured in settings, and returns the transcript with the detected language and
the method "transcription". The temporary audio is always removed afterwards, whether the
transcription succeeded or failed. Only one local transcription runs at a time, so a run with
many videos cannot exhaust the machine's CPU or memory.

**Why this priority**: Covers the minority of videos without subtitles; valuable but costly and
optional, so it comes after the subtitle path.

**Independent Test**: With a stubbed backend reporting no subtitles and a stubbed transcriber,
request a transcript and verify that audio is fetched, transcribed, returned with method
"transcription", that the temporary files are gone afterwards (also when the transcriber
raises), and that two concurrent requests never transcribe at the same time.

**Acceptance Scenarios**:

1. **Given** a video without subtitles and the transcription component installed, **When** a
   transcript is requested, **Then** audio only (not video) is downloaded, transcribed, and the
   transcript is returned with method "transcription" and the detected language.
2. **Given** a transcription that completes successfully, **When** the request finishes,
   **Then** no temporary audio files or directories remain.
3. **Given** a transcription or audio download that fails part-way, **When** the request
   finishes with an error, **Then** no temporary audio files or directories remain.
4. **Given** several videos needing transcription in the same run, **When** they are processed
   concurrently, **Then** at most one local transcription is in progress at any moment; the
   others wait their turn.
5. **Given** a configured transcription model size, **When** a transcription runs, **Then** that
   model size is used.
6. **Given** the default per-run cap of 3 and five subtitle-less videos in one run, **When** the
   run processes them, **Then** three are transcribed and the other two are deferred: no audio is
   downloaded for them, they are not marked failed, their attempts are unchanged, and the next run
   picks them up.
7. **Given** a subtitle-less 60-minute video and only 40 minutes left before the run's deadline,
   **When** its transcription would start, **Then** it is deferred to the next run like a video
   over the per-run cap, and the run finishes on time.

---

### User Story 3 - Bounded and optional transcription (Priority: P2)

The operator controls the cost of the fallback. Videos longer than the configured maximum
duration (default 90 minutes) are skipped before any audio is downloaded, with a clear error
naming the video, its duration and the limit. If the local transcription component is not
installed and a video has no subtitles, the system reports that no transcript is available
instead of failing the run.

**Why this priority**: Protects unattended runs from hours-long downloads and heavy installs;
required for safe production use of User Story 2.

**Independent Test**: Request transcripts for a stubbed subtitle-less video longer than the
limit, and for a subtitle-less video with the transcription component made unavailable; verify a
"transcript unavailable" outcome with a clear reason and no audio download in either case.

**Acceptance Scenarios**:

1. **Given** a subtitle-less video of 120 minutes and the default limit, **When** a transcript is
   requested, **Then** it is skipped with a "transcript unavailable" error that states the
   duration and the 90-minute limit, and no audio is downloaded.
2. **Given** a configured limit of 30 minutes, **When** a subtitle-less 45-minute video is
   processed, **Then** it is skipped with the same kind of error.
3. **Given** the transcription component is not installed and the video has no subtitles,
   **When** a transcript is requested, **Then** a "transcript unavailable" error is reported
   whose message explains that local transcription is not installed and how to enable it.
4. **Given** the transcription component is not installed but the video has subtitles,
   **When** a transcript is requested, **Then** the subtitles are returned normally.
5. **Given** a video longer than the limit that does have subtitles, **When** a transcript is
   requested, **Then** the subtitles are returned (the duration limit only bounds the audio
   fallback).

---

### User Story 4 - Video items flow through the pipeline (Priority: P1)

When the item processing pipeline routes a video item to its video path, the transcript becomes
the item's content, and the item continues to the existing text stage, relevance scoring and
summarization like any article. If no transcript can be obtained for a permanent reason, the
item is rated from its title and description (as with a too-short article page) and the reason
is logged; if the reason is transient, the item is marked failed so a later run retries it. In
both cases the rest of the run continues.

**Why this priority**: Without this integration the transcripts are never used; it is what
makes video sources deliver value end-to-end.

**Independent Test**: Run the item subgraph with a video item and a stubbed transcript provider
returning a transcript, then one raising "transcript unavailable"; verify the item content and
its next stage in the first case, and a recorded, non-fatal failure in the second.

**Acceptance Scenarios**:

1. **Given** a video item and an available transcript, **When** the item is processed, **Then**
   the transcript text is stored as the item's content and the item proceeds to the next stage
   with the same input/output contract as the current video-path placeholder.
2. **Given** a video item whose transcript is permanently unavailable (no subtitles and no
   transcription component, too long, private/deleted video, or empty transcription), **When**
   the item is processed, **Then** its content stays empty, the reason is logged with the item
   id, the item is rated from its title and description, and it is not marked failed.
3. **Given** a video item whose transcript retrieval fails transiently (network error, timeout,
   blocked server IP), **When** the item is processed, **Then** the item is marked failed with
   the reason, so a later run retries it (up to the existing maximum number of attempts), and
   other items of the run are unaffected.
4. **Given** a non-video item, **When** it is processed, **Then** its handling is unchanged.

---

### Edge Cases

- Subtitles exist only in a language other than the job's language and English: the best
  available track is used (manual before automatic, see FR-003) instead of transcribing the
  audio, and the track's actual language is reported as the transcript language.
- Subtitle track is listed but its content is empty or only contains markup/timing after
  cleaning: treated as "no usable subtitles" and the next option (next track or the fallback) is
  tried.
- Video duration is not reported by the backend: the audio fallback proceeds, but the downloaded
  audio is still checked against the limit before transcription and rejected if longer.
- Video is private, deleted or region-blocked: permanent "transcript unavailable"; the item is
  rated from its metadata. The network fails, times out or the server IP is blocked while reading
  subtitles or audio: transient "transcript unavailable"; the item is marked failed and retried in
  a later run. Neither case crashes the run.
- A network failure while checking for subtitles is not treated as "no subtitles": it is a
  transient failure and MUST NOT trigger the audio fallback.
- Blocked server IP: the existing optional cookies file and proxy settings from #28 also apply to
  subtitle and audio retrieval.
- Transcription produces empty text (e.g. a music-only video): treated as "transcript
  unavailable".
- Subtitle text with HTML entities, speaker/positioning tags or line breaks inside one cue: the
  entities are decoded, tags removed, and the cue's lines joined into readable text.
- The process is cancelled during download or transcription: a transcription that has not
  started yet is dropped; one already running finishes its current step, then its temporary
  audio is removed and the slot is released. A second transcription never starts in parallel.
- Configured maximum duration of zero or negative, a negative per-run transcription cap, or an
  unknown model size: rejected as invalid configuration with a clear message.
- The run's deadline is close when a subtitle-less video reaches the front of the transcription
  queue: the video is deferred (FR-009b) rather than risking a failed run.
- A deferred video that keeps getting deferred (always more subtitle-less videos than the cap)
  is processed in a later run as the backlog drains; deferral never turns it into a failure.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a single transcript retrieval operation that takes a video
  URL (and the job's preferred language) and returns a transcript consisting of text, language
  and the method used ("subtitles" or "transcription").
- **FR-002**: The system MUST try published subtitles before any other method, and MUST NOT
  download audio for a video that has at least one usable subtitle track.
- **FR-003**: Subtitle track selection MUST follow this order: manual in the job's language,
  automatic in the job's language, manual in English, automatic in English, then any other
  manual track, then any other automatic track. A track in another language is always preferred
  over audio transcription, and the returned language is the chosen track's language.
- **FR-004**: Subtitle content MUST be converted to plain text with all timing lines, cue
  identifiers, header/metadata blocks and inline markup removed, HTML entities decoded, and
  consecutive duplicate caption lines (including rolling-caption repeats) removed.
- **FR-005**: When no usable subtitles exist, the system MUST fall back to local transcription of
  the video's audio, downloading audio only (best available audio quality) into a temporary
  location unique to that request.
- **FR-006**: The fallback MUST refuse videos whose duration exceeds a configurable maximum
  (default 90 minutes) before downloading, and MUST also refuse downloaded audio exceeding it
  when the duration was not known beforehand; the error MUST name the video, its duration and
  the limit.
- **FR-007**: The transcription model size MUST be configurable in settings and default to
  `small`; unknown sizes are rejected as invalid configuration. The maximum duration MUST also be
  configurable in settings.
- **FR-008**: Temporary audio files and directories MUST be removed after every fallback attempt,
  on success, failure and cancellation alike.
- **FR-009**: At most one local transcription MUST run at a time within a process; additional
  requests MUST wait rather than fail.
- **FR-009a**: The number of local transcriptions per run MUST be capped by a setting (default
  3; zero disables the audio fallback for the run). A video that needs the fallback after the cap
  is reached MUST be deferred: no audio is downloaded, the item is left unchanged so a later run
  picks it up again, it is not marked failed, its attempt count is not increased, and the
  deferral is logged with the item id. Subtitle-based transcripts do not count toward the cap.
- **FR-009b**: A local transcription MUST only start (checked when it would actually start, after
  waiting for its turn) if the video's duration plus a fixed reserve of 10 minutes for the run's
  remaining stages fits in the run's remaining time before its deadline, assuming transcription
  is no slower than real time; a video of unknown duration counts as the configured maximum
  duration. Otherwise the video MUST be deferred
  exactly as in FR-009a (no audio downloaded, not failed, attempts unchanged, logged with the
  item id and the reason "not enough run time").
- **FR-010**: Local transcription MUST be an optional installable component; the base
  installation MUST work without it, and the absence of the component MUST only matter for
  videos without subtitles.
- **FR-011**: When no transcript can be obtained (component not installed, too long, video
  unavailable, retrieval failure, empty result), the operation MUST raise a dedicated "transcript
  unavailable" error carrying a clear, human-readable reason and whether the cause is permanent
  (too long, component missing, video private/deleted/region-blocked, empty result, no audio-only
  format offered, URL that is not a supported video URL) or transient
  (network error, timeout, blocked server IP); when the component is missing the message MUST
  say how to install it.
- **FR-012**: Subtitle and audio retrieval MUST honour the existing optional cookies file and
  proxy settings used by the YouTube source. The YouTube timeout setting bounds metadata and
  subtitle retrieval as a whole; the audio download is bounded per network request by the same
  timeout and in total by the duration cap (FR-006) and the run-time check (FR-009b).
- **FR-013**: The video path of the item processing pipeline MUST use this operation, store the
  transcript text as the item's content, and keep the existing input/output contract of the
  video-path step so subsequent stages work unchanged.
- **FR-014**: A permanent "transcript unavailable" outcome MUST NOT mark the item failed: the
  item's content stays empty, the reason is logged with the item id, and the item is rated from
  its title and description (same treatment as a too-short article page). A transient outcome
  MUST mark the item failed with its reason so the existing retry mechanism picks it up in a
  later run. A configuration problem that affects every video (the configured cookies file is
  missing or unreadable) is not a fact about the video: the item is marked failed like a
  transient outcome, so it is retried once the configuration is fixed. Neither outcome may abort
  the run or affect other items.
- **FR-015**: Retrieval steps MUST be logged in the project's structured log format, including
  the chosen method and language, without leaking credentials such as the proxy.
- **FR-016**: Automated tests MUST cover every acceptance criterion using stubbed subtitle,
  audio and transcription backends, with no network access and without the optional component
  installed in the default test environment.
- **FR-017**: Documentation MUST describe how to enable local transcription and the related
  settings (model size, maximum duration, transcriptions per run).
- **FR-018**: The standard deployment MUST offer a single opt-in switch, off by default, that
  installs the transcription component and raises the scheduled run service's memory limit to a
  documented value sufficient for the default model size. With the switch off, the deployed
  services, their sandbox and their limits MUST stay exactly as they are today. The deployment
  documentation MUST describe the switch, the memory figure, and where the downloaded model is
  stored (inside the service's existing writable state directory).

### Key Entities

- **Transcript**: The textual representation of a video's speech: text, language code, and
  method ("subtitles" or "transcription").
- **Subtitle Track**: A published caption track of a video, characterised by language and whether
  it is manual or automatically generated.
- **Transcription Settings**: Operator settings for the fallback: model size (default `small`),
  maximum video duration (default 90 minutes) and maximum transcriptions per run (default 3); reuses the YouTube access settings (cookies, proxy, timeout).
- **Transcript Unavailable**: The failure outcome stating why no transcript could be produced for
  a given video, and whether that cause is permanent or transient.
- **Video Item**: A pipeline item of kind "video" whose content is filled with the transcript
  text by the video path.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In 100% of tested cases where a video has at least one usable subtitle track, no
  audio download is attempted.
- **SC-002**: Converted subtitle text contains zero timestamps, zero markup tags and zero
  consecutive duplicate lines across all test fixtures.
- **SC-003**: 100% of videos longer than the configured limit and lacking subtitles are skipped
  with an error stating the duration and the limit, and with no audio downloaded.
- **SC-004**: After every tested fallback attempt (success, failure, cancellation), zero
  temporary audio files remain.
- **SC-005**: Without the optional transcription component, the installation and full test suite
  succeed, and subtitle-less videos yield a "transcript unavailable" outcome rather than a crash.
- **SC-006**: Under concurrent load, the number of simultaneous local transcriptions never
  exceeds one, and no run performs more transcriptions than its configured per-run cap.
- **SC-007**: For a video with subtitles, the transcript is available in under 10 seconds under
  normal network conditions (one metadata request and one subtitle download per video), making
  subtitle-backed videos about as fast to process as articles.
- **SC-008**: A video item with a transcript reaches relevance scoring with its transcript as
  content; a failed transcript affects only its own item.
- **SC-009**: A video whose transcript is permanently unavailable is rated in the run where it
  first appears and never triggers a second audio download in later runs.
- **SC-010**: With the deployment switch off, a redeploy changes none of the deployed services'
  limits or sandbox settings; with it on, a run that transcribes a 90-minute video with the
  default model size completes without hitting the service's memory limit.
- **SC-011**: No run fails or exceeds its deadline because of a local transcription; videos that
  do not fit in the remaining run time are deferred instead.

## Assumptions

- The YouTube source from #28 (video candidates, cookies/proxy/timeout settings, metadata
  backend) and the item processing pipeline from #21 (including the video-path placeholder and
  its ItemState in/out contract) are available and reused.
- The issue's module path `scout/sources/transcript.py` refers to the project's sources package;
  the transcript module lives alongside the other sources in `src/invio/sources/`.
- Videos with unknown duration are allowed into the fallback, but the downloaded audio is checked
  against the limit before transcription.
- The one-at-a-time limit applies per process. `invio run-due` runs all due jobs (possibly
  several in parallel) in one process with shared adapters, so the limit holds across those jobs;
  each run keeps its own per-run cap and deadline. A manual `invio job run` started at the same
  time is a separate process and is not coordinated with the scheduled one (operators avoid
  manual runs of transcription-heavy jobs while the scheduled service is busy).
- Unavailable transcripts reuse existing pipeline behaviour (see Clarifications): permanent causes
  follow the too-short-page path, and transient causes use the existing failed status and its
  retry limit. No new item status is introduced.
- On a default deployment (switch off) the transcription component is absent, so videos without
  subtitles follow the "transcription component missing" path (rated from title and
  description); enabling the switch is a deliberate operator decision (see Clarifications).
- Transcript caching across runs, translation into the job's language, and speaker labelling are
  out of scope.
- Operators are responsible for the legality and terms-of-service compliance of retrieving
  subtitles and audio from the platforms they configure.
