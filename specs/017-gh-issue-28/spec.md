# Feature Specification: YouTube Channel and Playlist Source

**Feature Branch**: `gh-issue-28`

**Created**: 2026-10-08

**Status**: Draft

**Input**: User description: "GitHub issue #28: [FEAT] Add YouTube channel and playlist source. Depends on #10. Context: Videos from curated channels and playlists are a key input. Listing must be metadata-only and fast; transcription happens later per relevant item. Requirements: 1) Implement a YouTube source that lists videos (id, title, upload date, URL) without downloading media. 2) Support `youtube_channel` and `youtube_playlist` configs; resolve `@handle` URLs. 3) Respect `max_age_days` and a per-source maximum (default 20). 4) Listing runs off the main flow with a timeout; failures map to `FetchError`. 5) Allow optional `cookies_file` and `proxy` in settings for blocked server IPs. 6) Candidates carry `type=\"video\"`. Acceptance criteria: channel and playlist URLs yield candidates with `type=\"video\"` (tested with a mocked listing backend); `max_age_days` and per-source limits are applied; network blocks surface as `FetchError` without crashing the run."

## Clarifications

### Session 2026-10-08

- Q: When a listed video has no upload date, should it be kept or dropped? → A: Kept, ranked after dated videos, and only within the per-source maximum; no extra requests to fetch dates.

## User Scenarios & Testing *(mandatory)*

The primary user is the operator who curates a research job's sources and wants new videos from
trusted YouTube channels and playlists to show up as candidates alongside articles. The secondary
user is the downstream pipeline, which receives these candidates, scores their relevance, and
only later (per relevant item) fetches transcripts.

### User Story 1 - Collect recent videos from a channel (Priority: P1)

The operator adds a YouTube channel (by channel URL or `@handle` URL) as a source of a job. When
the job runs, the source lists the channel's most recent videos and hands each one to the
pipeline as a candidate of type "video", carrying its video id, title, upload date, and link.
No video or audio is downloaded; only metadata is read, so listing finishes quickly.

**Why this priority**: Channels are the main way curated video input is expressed; this alone is
a viable MVP.

**Independent Test**: Configure a channel source against a stubbed listing result containing
several videos; run the source and verify one video-type candidate per listed video with the
expected id, title, date, and URL.

**Acceptance Scenarios**:

1. **Given** a channel source configured with a channel URL, **When** the source runs, **Then**
   each recent video becomes a candidate of type "video" with id, title, upload date, and URL.
2. **Given** a channel source configured with an `@handle` URL, **When** the source runs, **Then**
   the handle is resolved to the channel's videos and candidates are produced as for a channel URL.
3. **Given** a channel with no videos, **When** the source runs, **Then** it yields zero
   candidates and no error.

---

### User Story 2 - Collect videos from a playlist (Priority: P1)

The operator adds a YouTube playlist URL as a source. When the job runs, the playlist's videos
are listed and turned into video candidates exactly as for a channel.

**Why this priority**: Playlists are the second curated-input form named in the requirements and
share the same output contract.

**Independent Test**: Configure a playlist source against a stubbed listing and verify
video-type candidates are produced.

**Acceptance Scenarios**:

1. **Given** a playlist source with a playlist URL, **When** the source runs, **Then** each
   playlist video becomes a video-type candidate with id, title, upload date, and URL.
2. **Given** a playlist containing an unavailable or private entry with no usable metadata,
   **When** the source runs, **Then** that entry is skipped and the remaining videos are still returned.

---

### User Story 3 - Limit by age and count (Priority: P2)

The operator controls volume: videos older than the source's `max_age_days` are excluded, and at
most a per-source maximum number of candidates (default 20) is returned, newest first.

**Why this priority**: Prevents old backlog and huge channels from flooding the pipeline; needed
for sensible production use but not for the basic listing.

**Independent Test**: Provide a stubbed listing with videos of varying ages and more entries than
the limit; verify old videos are dropped and the count never exceeds the limit.

**Acceptance Scenarios**:

1. **Given** `max_age_days` of 7 and videos aged 2 and 30 days, **When** the source runs,
   **Then** only the 2-day-old video is returned.
2. **Given** no explicit maximum and 50 eligible videos, **When** the source runs, **Then**
   exactly 20 candidates are returned, the newest ones.
3. **Given** an explicit maximum of 5, **When** the source runs, **Then** at most 5 candidates are returned.
4. **Given** a video whose upload date is not provided by the listing, **When** age filtering
   applies, **Then** the video is kept (it cannot be age-filtered) but ranked after all dated
   videos, and returned only if it still fits within the per-source maximum.

---

### User Story 4 - Survive blocks and failures without crashing the run (Priority: P2)

When YouTube blocks the server's IP, the network is down, the channel does not exist, or the
listing hangs, the source reports a fetch failure for that source only. The job run continues
with its other sources and records the error. If the operator has configured a cookies file or a
proxy, those are used for the listing to get around blocked server IPs.

**Why this priority**: Unattended runs from a server IP are the common failure scenario; the
run must degrade gracefully.

**Independent Test**: Make the stubbed listing raise a network or block error, hang past the
timeout, and return a not-found error; verify each surfaces as a fetch error for that source
and other sources still run. Verify configured cookies/proxy are passed to the listing.

**Acceptance Scenarios**:

1. **Given** the listing fails with a network/block error, **When** the source runs, **Then** a
   fetch error identifying the source is raised and the overall run is not aborted.
2. **Given** the listing takes longer than the timeout, **When** the source runs, **Then** it is
   abandoned and reported as a fetch error, and the run is not blocked.
3. **Given** a cookies file and/or proxy are configured, **When** the source runs, **Then** the
   listing uses them; when absent, none are used.
4. **Given** a configured cookies file path that does not exist, **When** the source runs (or is
   validated), **Then** a clear error is reported rather than silently ignoring it.

---

### Edge Cases

- Malformed or non-YouTube URL in a channel/playlist config: rejected with a clear configuration error.
- Duplicate videos appearing in both a channel and a playlist: de-duplication is left to the existing pipeline stage; each source reports what it sees.
- Channel pages that list several tabs (videos, shorts, live): only regular uploads are listed.
- Listing returns entries lacking title or URL: such entries are skipped, not fatal.
- Per-source maximum of zero or negative: rejected as invalid configuration.
- Upload dates given only as a date (no time of day): treated as midnight UTC.
- Cancellation of a run while a listing is in progress: the listing does not keep the run waiting.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a YouTube source type for channels and one for playlists, selectable by the source configuration kinds `youtube_channel` and `youtube_playlist`.
- **FR-002**: The source MUST list videos using metadata only; it MUST NOT download video or audio content, and transcription MUST NOT occur at listing time.
- **FR-003**: Each listed video MUST become a candidate with type "video" carrying its video id, title, upload date, and canonical URL.
- **FR-004**: Channel sources MUST accept channel URLs and `@handle` URLs and resolve them to the channel's uploads.
- **FR-005**: Playlist sources MUST accept playlist URLs and list the playlist's videos.
- **FR-006**: The source MUST exclude videos older than the source's `max_age_days`. Videos without an upload date MUST NOT be excluded by age; they are ranked after dated videos and count toward the per-source maximum (FR-007). The listing MUST NOT make extra per-video requests to obtain missing dates.
- **FR-007**: The source MUST return at most a per-source maximum number of candidates, defaulting to 20 and configurable per source, preferring the newest videos. The job-wide per-source item limit still applies afterwards, so the effective cap is the smaller of the two.
- **FR-008**: The listing MUST be bounded by a timeout so a stalled request cannot block the run indefinitely, and MUST NOT block other concurrent work in the run while in progress.
- **FR-009**: Any listing failure (network error, IP block, unknown channel/playlist, timeout, unexpected response) MUST be reported as a fetch error that names the source, without raising anything that aborts the whole run.
- **FR-010**: Operators MUST be able to optionally configure a cookies file and a proxy in settings; when set, the listing MUST use them, and when unset it MUST work without them.
- **FR-011**: Invalid configuration (missing or malformed URL, non-positive maximum, non-existent cookies file) MUST be rejected with a clear message before or at the start of fetching.
- **FR-012**: Entries without usable metadata (private, deleted, missing id or URL) MUST be skipped while the remaining videos are still returned.
- **FR-013**: The new source MUST behave consistently with the existing source types in how it is configured, registered, and reported (same candidate contract, same error contract, same age-filter semantics).
- **FR-014**: Automated tests MUST cover channel, `@handle`, and playlist listing, age and count limits, and each failure path using a stubbed listing backend, with no real network access.

### Key Entities

- **YouTube Source Configuration**: Describes one channel or playlist to monitor: kind (channel or playlist), URL, maximum age in days, per-source maximum (default 20).
- **YouTube Access Settings**: Optional global settings: cookies file path and proxy address used to reach YouTube from a blocked server IP.
- **Video Candidate**: A candidate of type "video" with id, title, upload date, and URL; later scored and, if relevant, transcribed.
- **Fetch Error**: The existing failure report identifying which source failed and why, recorded without aborting the run.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a channel or playlist with at least one eligible video, 100% of eligible listed videos appear as video-type candidates with all four fields (id, title, date, URL) populated.
- **SC-002**: Listing a source of up to 20 videos completes in under 10 seconds under normal network conditions, as no media is downloaded.
- **SC-003**: No source ever returns more than its configured maximum (default 20) or any video older than its configured age limit, across all tested combinations.
- **SC-004**: In every tested failure scenario (network error, block, not found, timeout), the affected source reports a fetch error and 100% of the other sources in the same run still complete.
- **SC-005**: A stalled listing is abandoned within the configured timeout, so a job run is never delayed beyond that timeout by one YouTube source.
- **SC-006**: The full behavior is verifiable by the automated test suite without any real network access.

## Assumptions

- The source-framework from issue #10 (candidate model, fetch error type, source registry, age-filter helper) is already available and is reused as is.
- Videos with no upload date in the listing are kept and ranked after dated videos, subject to the count limit (see Clarifications).
- Default timeout for a listing is a generous fixed value (about 60 seconds), adjustable in settings.
- Cookies and proxy are global settings, not per-source, since they relate to the server's network identity.
- YouTube Shorts and live/upcoming streams are out of scope for v1; only regular uploads are listed.
- Transcript retrieval, relevance scoring, and cross-source de-duplication belong to other issues and are out of scope.
- Operators are responsible for the legality and terms-of-service compliance of any cookies they supply.
