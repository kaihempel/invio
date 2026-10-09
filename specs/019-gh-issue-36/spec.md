# Feature Specification: Source Discovery Command

**Feature Branch**: `gh-issue-36`

**Created**: 2026-10-09

**Status**: Draft

**Input**: User description: "GitHub issue #36: Add `scout source discover <url>` CLI command. Users often only know a website URL; discovery finds the feed, sitemap or YouTube channel and offers it as a ready-to-use source entry. Requirements: (1) Add `scout source discover <url>`. (2) Fetch the page and parse `<link rel=\"alternate\" type=\"application/rss+xml|atom+xml\">`; probe common paths (`/feed`, `/rss`, `/atom.xml`, `/sitemap.xml`). (3) Detect YouTube channel links and resolve the channel/playlist URL. (4) Validate each finding by fetching and parsing it; show title and latest entry date. (5) Print YAML snippets and offer to append the selection to an existing job (`--add-to JOB`). Acceptance criteria: a fixture page with an RSS `<link>` tag yields a validated RSS source; probing common paths finds `/sitemap.xml` on a fixture site; `--add-to` appends the chosen source to the job and re-validates its config; unreachable or invalid candidates are not offered. Depends on #9 and #10. Labels: feature, m6-comfort, cli, sources, track:cli-sched, round-3. Effort M. Branch: issue/36-add-scout-source-discover-command."

## Clarifications

### Session 2026-10-09

- Q: When discovery runs on a page inside a sub-section of a site, where are the common locations tried? → A: At the site root and, if different, also in the folder the entered page is in (e.g. `https://example.com/blog/feed` for `https://example.com/blog/post-1`).
- Q: When `--add-to` is used without a terminal, how is the source to add chosen? → A: By its list number via `--pick N`; if exactly one validated candidate was found, it is added without `--pick`; with several candidates and no `--pick`, nothing changes and the command exits non-zero.
- Q: Should sitemaps listed in the site's `robots.txt` (`Sitemap:` lines) also be offered? → A: Yes — up to 5 `Sitemap:` entries are taken as candidates and validated like any other candidate.
- Q: How are comment feeds (e.g. WordPress "Comments Feed") treated? → A: They are kept, but listed after all other candidates and marked "comments".

## User Scenarios & Testing *(mandatory)*

The user of this feature is the operator who maintains research jobs from the terminal. They often know only the address of a website ("I want to follow example.com") and not the address of its feed, its sitemap or the YouTube channel behind it. Following the project decision taken for #9, the command lives under the project's `invio` CLI (`invio source discover <url>`); "scout" is a legacy working name.

### User Story 1 - Discover the feed announced by a website (Priority: P1)

The operator runs `invio source discover https://example.com`. The command loads the page, finds the RSS or Atom feeds the page announces in its header, checks each one by loading and reading it, and prints a list of working candidates. For each candidate it shows the source type, its address, the feed title and the date of its newest entry, followed by a ready-to-paste YAML snippet in the job file format.

**Why this priority**: Announced feeds are the most common and most reliable way a site exposes its content; this alone answers the operator's main question ("what do I put in my job?") and is the first acceptance criterion.

**Independent Test**: Serve a local fixture page whose header announces an RSS feed, plus that feed; run the command against the page and verify that exactly one validated `rss` candidate with the feed's title and latest entry date and a valid YAML snippet is printed.

**Acceptance Scenarios**:

1. **Given** a page whose header announces an RSS feed that is reachable and well-formed, **When** the operator runs discovery on the page address, **Then** one `rss` candidate is listed with the feed's absolute address, its title and the date of its newest entry, together with a YAML snippet that is accepted as a source by the job configuration.
2. **Given** a page announcing both an RSS and an Atom feed, **When** discovery runs, **Then** both are listed as separate `rss` candidates (the job format uses `rss` for both feed flavours).
3. **Given** a page that announces a feed with a relative address (e.g. `/blog/feed.xml`), **When** discovery runs, **Then** the address is resolved against the page address before it is checked and shown.
4. **Given** a page announcing a feed that answers with an error status, cannot be reached or does not contain a readable feed, **When** discovery runs, **Then** that feed is not listed as a candidate.
5. **Given** the address entered by the operator is itself a feed (not an HTML page), **When** discovery runs, **Then** the feed itself is offered as a validated `rss` candidate.
6. **Given** a page announcing a main feed and a comments feed (address ending in `/comments/feed` or title containing "Comments"), **When** discovery runs, **Then** both are offered, the main feed is listed first and the comments feed is listed last, marked "comments".

---

### User Story 2 - Find feeds and sitemaps at common locations (Priority: P1)

Many sites do not announce their feed or sitemap in the page header but serve them at well-known locations. After inspecting the page, discovery also tries the common locations `/feed`, `/rss`, `/atom.xml` and `/sitemap.xml` at the site root and in the folder of the entered page, and takes the sitemaps listed in the site's `robots.txt`; it checks whatever it finds there, and lists the working ones: feeds as `rss` candidates and sitemaps as `sitemap` candidates. For a sitemap the listing shows the number of contained addresses and the newest modification date instead of a feed title.

**Why this priority**: Probing finds sources on sites that do not announce them, and finding `/sitemap.xml` is an explicit acceptance criterion.

**Independent Test**: Serve a local fixture site whose home page announces nothing and which serves a valid sitemap at `/sitemap.xml` and nothing at the other common locations; run discovery and verify that exactly one validated `sitemap` candidate is listed.

**Acceptance Scenarios**:

1. **Given** a site that serves a valid sitemap at `/sitemap.xml`, **When** discovery runs on its home page, **Then** a `sitemap` candidate for that address is listed with its entry count and newest modification date (if the sitemap provides dates) and a valid YAML snippet.
2. **Given** a site that serves a sitemap index (a sitemap listing further sitemaps) at `/sitemap.xml`, **When** discovery runs, **Then** it is offered as a `sitemap` candidate.
3. **Given** a common location that answers with "not found", an error, an HTML page or unreadable content, **When** discovery runs, **Then** no candidate is listed for that location.
4. **Given** a feed that is both announced in the page header and served at a common location (same final address), **When** discovery runs, **Then** it is listed only once.
5. **Given** discovery is run on a deep page such as `https://example.com/blog/post-1`, **When** common locations are probed, **Then** they are tried at the root of the site (`https://example.com/feed`, …) and in the page's folder (`https://example.com/blog/feed`, …).
6. **Given** discovery is run on a page directly at the site root (e.g. `https://example.com/` or `https://example.com/about`), **When** common locations are probed, **Then** each location is tried only once, at the root.
7. **Given** a site whose `robots.txt` lists `Sitemap: https://example.com/sitemap_index.xml` and which serves nothing at `/sitemap.xml`, **When** discovery runs, **Then** the listed sitemap is validated and offered as a `sitemap` candidate.
8. **Given** a `robots.txt` listing more than 5 sitemaps, **When** discovery runs, **Then** only the first 5 are checked and the rest are reported as skipped.

---

### User Story 3 - Add a discovered source to an existing job (Priority: P2)

Having found the right source, the operator wants it in a job without copying YAML by hand. With `--add-to JOB`, the command lets the operator choose one of the validated candidates, appends it to the sources of the named job, validates the complete job configuration again and saves it only if it is still valid.

**Why this priority**: It turns discovery from an information command into a one-step workflow, and is an explicit acceptance criterion; the printed snippets (Stories 1 and 2) already deliver value without it.

**Independent Test**: With a stored valid job and a fixture site offering one feed, run discovery with `--add-to` and choose the candidate; verify the stored job now contains the new source as its last source, still validates, and that all other job settings are unchanged.

**Acceptance Scenarios**:

1. **Given** an existing valid job and at least one validated candidate, **When** the operator runs discovery with `--add-to <job>` and selects a candidate, **Then** the candidate is appended to the job's sources, the full job configuration is validated again, the job is saved, and a confirmation names the job and the added source.
2. **Given** several validated candidates in an interactive terminal, **When** `--add-to` is used, **Then** the operator is asked to pick exactly one candidate from the list (or cancel); cancelling (or Ctrl+C) leaves the job unchanged, the command reports that nothing was added and exits 0.
3. **Given** a non-interactive run (no terminal) with several validated candidates, **When** `--add-to` is used without `--pick`, **Then** the command does not guess: it lists the candidates, changes nothing and exits with a non-zero code explaining how to choose one with `--pick N`.
4. **Given** a non-interactive run with exactly one validated candidate, **When** `--add-to` is used without `--pick`, **Then** that candidate is added (subject to the same duplicate and re-validation rules).
5. **Given** any run, **When** `--add-to` is used with `--pick N`, **Then** candidate number N of the printed list is added without a prompt; a number outside the list changes nothing and exits non-zero naming the valid range.
6. **Given** `--add-to` names a job that does not exist, **When** the command runs, **Then** it fails with a message naming the missing job before any network access, and exits non-zero.
7. **Given** the job already contains a source with the same type and address as the selected candidate, **When** the operator selects it, **Then** the job is not changed and the operator is told the source is already present.
8. **Given** appending the candidate would make the job configuration invalid (or the stored job is already invalid), **When** re-validation runs, **Then** the job is not saved, the validation errors are shown naming the offending field, and the command exits with the configuration-error code.
9. **Given** no validated candidate was found, **When** `--add-to` is used, **Then** the job is not changed.

---

### User Story 4 - Discover YouTube channels and playlists linked from a site (Priority: P3)

Many sites (podcasts, creators, conferences) link to their YouTube channel or playlists. Discovery recognises links to YouTube channels (`/channel/<id>`, `/@handle`, including links with a tab like `/videos`) and playlists (`list=<id>`) on the page, resolves them to a channel or playlist the job format accepts, checks that the channel or playlist exists and lists videos, and offers them as `youtube_channel` or `youtube_playlist` candidates with the channel/playlist title and the date of the newest video (if known).

**Why this priority**: Valuable for video-heavy sites but less common than feeds and sitemaps; it builds on the same listing, validation and add-to flow.

**Independent Test**: Serve a fixture page that links to a YouTube channel by handle and to a playlist, with YouTube access replaced by a fake; run discovery and verify one `youtube_channel` and one `youtube_playlist` candidate with titles are listed and their snippets validate.

**Acceptance Scenarios**:

1. **Given** a page linking to `https://www.youtube.com/@somecreator`, **When** discovery runs and the channel can be resolved, **Then** a `youtube_channel` candidate is listed with the channel's title and newest video date (if available).
2. **Given** a page linking to a playlist (`https://www.youtube.com/playlist?list=PL…` or a video link carrying `list=`), **When** discovery runs, **Then** a `youtube_playlist` candidate is listed for that playlist.
3. **Given** a page linking to the same channel several times (different tabs or URL forms), **When** discovery runs, **Then** the channel is listed only once.
4. **Given** a link to a single video without a playlist, or a link to a channel/playlist that cannot be resolved or has no videos, **When** discovery runs, **Then** no candidate is listed for it.
5. **Given** the operator enters a YouTube channel or playlist address directly, **When** discovery runs, **Then** it is offered as the corresponding YouTube candidate.

---

### Edge Cases

- **Nothing found**: No validated candidate exists → the command says that no source was found, lists which kinds of checks were tried, and exits with a non-zero (non-configuration) code so scripts can tell.
- **Unreachable start page**: The entered page cannot be loaded (network error, error status) → the command still probes the common locations on the site; if those fail too, it reports the page error and exits non-zero.
- **Invalid input address**: Not an `http`/`https` URL (e.g. `ftp://…`, `example` without scheme) → rejected before any network access with a usage error. A bare host name such as `example.com` is treated as `https://example.com`.
- **Unsafe address**: The page, a probed location, an announced feed or any redirect points to a non-public address, or robots.txt disallows it → the shared safe HTTP layer (#10) refuses it and that candidate is not offered; a refused start page is reported with the reason.
- **Redirects**: A candidate that redirects is checked at its final address, and the final address is the one shown and offered. When the start page redirects (e.g. `example.com` → `www.example.com/home/`), the site root and folder used for probing and `robots.txt` are taken from its final address; if the start page could not be loaded, from the entered address.
- **Oversized or slow responses**: Bounded by the limits of the shared HTTP layer (#10); such a candidate is not offered.
- **Many announced feeds / links**: At most 20 announced feeds and 20 YouTube links are checked per run; anything beyond is reported as skipped so the run stays bounded.
- **Feed without entries or without dates**: A readable feed with no entries is still offered (it is valid), with "no entries" shown; a missing newest-entry date is shown as "unknown".
- **Wrong content type**: An announced "feed" that turns out to be HTML, or a sitemap location that serves a feed, is classified by its actual content, not by its announced type or location; content that is neither is not offered.
- **Job names**: `--add-to` with a job name that violates naming rules is rejected like an unknown job.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The CLI MUST provide a `source discover <url>` command under the `invio` CLI that takes one website address.
- **FR-002**: The command MUST accept `http` and `https` addresses, treat a scheme-less host name as `https`, and reject any other input with a usage error before any network access.
- **FR-003**: The command MUST load the given page and collect every feed it announces as an alternate representation of type RSS or Atom, resolving relative addresses against the page address.
- **FR-004**: The command MUST probe the common locations `feed`, `rss`, `atom.xml` and `sitemap.xml` at the root of the site and, when the entered page lies in a sub-folder, also in that folder (the page path up to its last `/`), regardless of whether the page itself could be loaded; each resulting address is tried at most once.
- **FR-004a**: The command MUST take the `Sitemap:` entries of the site's `robots.txt` (at most 5, in file order) as `sitemap` candidates; a missing or unreadable `robots.txt` simply yields none.
- **FR-005**: The command MUST collect links on the page to YouTube channels (channel id, `@handle`, optionally with a tab) and playlists (`list=` parameter) and resolve each to a channel or playlist locator accepted by the job configuration.
- **FR-006**: If the entered address is itself a feed, a sitemap or a YouTube channel/playlist address, the command MUST offer it as the corresponding candidate.
- **FR-007**: Every candidate MUST be validated by actually loading and reading it before it is offered; candidates that are unreachable, refused by the safety rules, return error status, exceed limits or cannot be read as their type MUST NOT be offered.
- **FR-008**: The type of a candidate (`rss` or `sitemap`) MUST be determined from its actual content, not from its announcement or location.
- **FR-009**: Candidates MUST be de-duplicated by type and final address (for YouTube: by channel or playlist identity).
- **FR-010**: Validated candidates MUST be listed in a stable order — the entered address itself, then announced feeds (page order), probed locations (root before folder, in the order `feed`, `rss`, `atom.xml`, `sitemap.xml`), `robots.txt` sitemaps, YouTube links (page order) — with comment feeds (address path ending in `/comments/feed` or `/comments/`, or a title containing "comments", case-insensitive) moved to the end and marked "comments". For each validated candidate the command MUST print a numbered entry with type, address, title (feeds, channels, playlists) or entry count (sitemaps), and the date of the newest entry (or "unknown"/"no entries").
- **FR-011**: For each validated candidate the command MUST print a YAML snippet in the job file's source format that the job configuration accepts without changes.
- **FR-012**: All HTTP access (start page, announced feeds, probes, `robots.txt`) MUST go through the shared safe HTTP layer (#10), inheriting its address-safety checks, robots.txt rules, rate limiting, timeouts and size limits; YouTube validation MUST use the same mechanism the YouTube sources use when a job runs.
- **FR-013**: The number of announced feeds and YouTube links checked per run MUST be capped (20 each), and `robots.txt` sitemap entries at 5; skipped items MUST be reported.
- **FR-014**: With `--add-to JOB`, the command MUST verify the job exists before any network access and fail with a message naming the job if it does not.
- **FR-015**: With `--add-to JOB` in an interactive terminal and without `--pick`, the command MUST let the operator choose exactly one validated candidate or cancel.
- **FR-016**: The command MUST accept `--pick N` to choose candidate N of the printed list without a prompt (in any run). Without a terminal and without `--pick`, it MUST add the candidate if exactly one was validated, and otherwise change nothing and exit non-zero with guidance; a `--pick` outside the list MUST change nothing and exit non-zero naming the valid range.
- **FR-017**: Before saving, the command MUST append the chosen candidate as the last source of the job and validate the complete job configuration again; the job MUST be saved only if validation succeeds, and all other job settings MUST remain unchanged.
- **FR-018**: If the job already contains a source of the same type with the same address/locator, the command MUST NOT add a duplicate and MUST tell the operator.
- **FR-019**: Results (candidate list, snippets, confirmation) MUST go to standard output and diagnostics to standard error; the command MUST exit 0 when at least one candidate was found (and, with `--add-to`, the source was added, was already present, or the operator cancelled the selection), non-zero when nothing was found or the addition failed, and with the configuration-error code (2) when re-validation of the job fails.
- **FR-020**: The command and its options MUST be documented in the README or `docs/` alongside the other CLI commands.

### Key Entities

- **Discovery target**: The address the operator entered, normalised to an absolute `http`/`https` address; also defines the site root used for probing.
- **Candidate**: A possible source found by announcement, probing, YouTube link or the target itself. Attributes: source type (`rss`, `sitemap`, `youtube_channel`, `youtube_playlist`), final address or locator, how it was found (announced / probed / robots.txt / linked / direct), and whether it is a comment feed.
- **Validated candidate**: A candidate that was loaded and read successfully. Adds: title or entry count, newest entry date (optional), and the ready-to-use source entry for the job file.
- **Job**: An existing stored research job (from #9) whose source list can receive one validated candidate; must stay valid after the addition.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a site that announces its feed or serves a feed/sitemap at a common location, an operator gets a working, ready-to-paste source entry with a single command, without opening the site's HTML.
- **SC-002**: 100% of offered candidates are readable at the time of discovery; none of the fixtures for unreachable, error-status, unsafe or unreadable candidates appears in the output.
- **SC-003**: 100% of printed snippets are accepted unchanged as a source by the job configuration validation.
- **SC-004**: Adding a discovered source to an existing job takes one command (plus one selection when interactive), and the job is never left in an invalid state: every failed addition leaves the stored job byte-for-byte unchanged.
- **SC-005**: Discovery on a typical site (one page, up to eight probe locations, a handful of announced feeds) completes within 30 seconds under the default timeouts and rate limits.
- **SC-006**: Every acceptance criterion of issue #36 is covered by at least one automated test that runs without internet access.

## Assumptions

- The command is `invio source discover`, not `scout source discover`, consistent with the clarification recorded for #9; no `scout` entry point is added.
- Probing is limited to the four locations named in the issue, at the site root and in the entered page's folder (not further parent folders); `/index.xml`, `/feed.xml`, pagination and crawling of further pages are out of scope.
- Only the given page is inspected for announcements and YouTube links; no additional pages are crawled.
- Only one candidate is added per `--add-to` run; adding several sources means running the command again.
- The added source gets only its required fields plus the defaults of the job format; optional settings (name, `max_age_days`, `url_pattern`, …) are left to `invio job edit`.
- `web` sources (change tracking of a page) are not offered by discovery; the operator can still add the page itself as a `web` source manually.
- YouTube resolution and validation reuse the existing YouTube source mechanism; in tests it is replaced by a fake, so no real YouTube access is needed.
- Depends on #9 (job storage, job service and CLI conventions) and #10 (safe HTTP client); both are merged.
