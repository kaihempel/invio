# Feature Specification: Static Digest Archive (HTML)

**Feature Branch**: `gh-issue-25`

**Created**: 2026-10-08

**Status**: Draft

**Input**: User description: "GitHub issue #25 — [FEAT] Generate static digest archive (HTML). Context: besides the mail, digests should be browsable later without a web application. A static HTML archive written to a directory can be served by any existing web server and is easy to protect with basic auth. Depends on #19, #20. Requirements: (1) optional `archive` section in the job configuration: `enabled`, `base_url` (for links in mails). (2) `archive_dir` in settings (default `/var/lib/scout/archive`). (3) After the digest is persisted, render `archive_dir/<job-name>/<YYYY-MM-DD-HHMM>.html` using a shared layout (responsive, no external assets). (4) Regenerate a per-job `index.html` (digests newest first) and a global `index.html`. (5) Include the archive link in the mail footer when `base_url` is set. (6) Write files atomically (temp file + rename); sanitize Markdown as in #20; slugify job names. Acceptance criteria: each successful run with a non-empty digest creates an HTML file and updates both indexes; output contains no external requests (fonts, scripts); job names with special characters produce safe paths (no path traversal); mail footer contains the correct archive URL when `base_url` is set."

## Clarifications

### Session 2026-10-08

- Q: If the archive page could not be written for a run, should the digest mail still include the archive link? → A: No — omit the link when the page was not written.
- Q: Should a re-sent (retried) mail still get the archive link? → A: Yes — whenever the archive is enabled, `base_url` is set and the page exists at send time.
- Q: What should the archive page contain beyond the digest text? → A: Same content as the mail — digest text plus the run stats footer, without the archive link itself.
- Q: How are two digests of the same job in the same minute named? → A: The later page gets a numeric suffix (`-2`, `-3`, …); nothing is overwritten.
- Q: Which time zone do file names and displayed run times use? → A: UTC, labelled "UTC" on pages and indexes.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Browse a past digest as a web page (Priority: P1)

A job owner has enabled the archive for a job. After each successful run that produces a non-empty digest, a self-contained HTML page for that digest is written to the archive directory, so the owner (or anyone they share the site with) can open it later in a browser, from a server that only serves static files.

**Why this priority**: This is the core value — digests remain readable after the mail is gone, with no application running.

**Independent Test**: Enable the archive for one job, run it with a non-empty digest, and open the generated page directly from disk or from a static file server; the digest is readable.

**Acceptance Scenarios**:

1. **Given** a job with the archive enabled, **When** a run completes successfully with a non-empty digest, **Then** a page for that digest exists at `<archive_dir>/<job-slug>/<YYYY-MM-DD-HHMM>.html` containing the job name, the run date and the digest content.
2. **Given** a job with the archive disabled or not configured, **When** a run completes, **Then** no archive files are created or changed for that job.
3. **Given** a run whose digest is empty, **When** the run completes, **Then** no archive page is created and the indexes are not changed.
4. **Given** a failed run, **When** it finishes, **Then** no archive page is created.
5. **Given** a digest containing script tags or unsafe attributes in its Markdown, **When** the page is rendered, **Then** those are stripped using the same rules as the mail HTML part (#20).

---

### User Story 2 - Navigate digests through indexes (Priority: P2)

A reader opens the archive's top-level page, sees all archived jobs, opens one, and sees that job's digests listed newest first, each linking to its page.

**Why this priority**: Without indexes, pages are only reachable by guessing file names; indexes make the archive browsable.

**Independent Test**: Archive digests for two jobs across several runs, then open the global index and each job index and verify listing, ordering and links.

**Acceptance Scenarios**:

1. **Given** a new digest is archived, **When** the run completes, **Then** both the job's `index.html` and the global `index.html` are regenerated and include the new entry.
2. **Given** a job with several archived digests, **When** its index is opened, **Then** entries appear newest first and each links to the matching page.
3. **Given** several archived jobs, **When** the global index is opened, **Then** each job appears once with a link to its job index.
4. **Given** the archive directory already holds pages from earlier runs (including after a restart), **When** an index is regenerated, **Then** it lists all existing pages, not only the latest run.

---

### User Story 3 - Jump from the mail to the archived digest (Priority: P2)

A recipient reading the digest mail sees a footer link to the archived version of the same digest and can open it in a browser.

**Why this priority**: Connects the existing delivery channel to the archive; valuable but depends on the archive existing.

**Independent Test**: Configure a `base_url`, send a digest mail, and check that the footer link equals the base URL plus the job slug and page name of the archived page.

**Acceptance Scenarios**:

1. **Given** the archive is enabled with a `base_url`, **When** the digest mail is built, **Then** the footer contains the full URL of that digest's archive page.
2. **Given** the archive is enabled without a `base_url`, or disabled, **When** the mail is built, **Then** the footer contains no archive link.
3. **Given** a `base_url` with or without a trailing slash, **When** the link is built, **Then** the resulting URL has exactly one slash between segments.
4. **Given** the archive is enabled with a `base_url` but the page could not be written, **When** the digest mail is built, **Then** the mail is still sent and its footer contains no archive link.
5. **Given** a failed notification is re-sent later and the page exists, the archive is enabled and `base_url` is set, **When** the retried mail is built, **Then** its footer contains the archive link.

---

### User Story 4 - Safe, self-contained output (Priority: P1)

An operator serves the archive behind basic auth on an ordinary web server. Pages must not load anything from third parties, and no job name may cause files to be written outside the archive directory or be left half-written.

**Why this priority**: Privacy (no third-party requests from private digests) and filesystem safety are hard constraints for unattended operation.

**Independent Test**: Archive a job named with path separators, dots and unicode; verify all files are inside the archive directory. Scan all generated pages for external references.

**Acceptance Scenarios**:

1. **Given** a job name such as `../../etc/x`, `a/b`, or one with spaces, unicode or control characters, **When** it is archived, **Then** the directory name is a safe slug and every written file lies inside the archive directory.
2. **Given** any generated page or index, **When** inspected, **Then** it contains no references to external resources (fonts, scripts, stylesheets, images, trackers); styling is inlined.
3. **Given** a page is being written, **When** a reader reads or a crash interrupts, **Then** readers see either the previous complete file or the new complete file, never a partial one.
4. **Given** a page is viewed on a narrow (phone) screen, **When** rendered, **Then** content fits without horizontal scrolling.

---

### Edge Cases

- Two different job names that slugify to the same value: they must not overwrite each other's archives (disambiguate deterministically).
- A job name that slugifies to an empty string (e.g. only symbols): a safe fallback name is used.
- Two runs of the same job within the same minute: the second page gets a numeric suffix and the first is preserved.
- Archive directory missing or not writable: the archive failure is reported but does not fail the run or block mail delivery; the mail is sent without an archive link.
- Archive directory does not yet exist on first run: it is created.
- A digest page file that exists but is not listed in an index (manual copy, interrupted regeneration): indexes are rebuilt from directory content.
- `base_url` is set but `enabled` is false: no archive is written and no link is added to mail.
- Very long job names: slug length is bounded so the path remains valid.
- Invalid `base_url` (not an http/https URL): configuration is rejected with a message naming the field.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: A job definition MUST accept an optional archive section with an `enabled` flag and an optional `base_url`; unknown keys MUST be rejected and `base_url` MUST be validated as an http/https URL.
- **FR-002**: The archive is disabled by default; a job without the section behaves exactly as before.
- **FR-003**: Application settings MUST provide an archive directory, defaulting to `/var/lib/invio/archive` (the issue's `/var/lib/scout/archive` uses the issue's working name `scout`; the project and its deployment use `invio`), overridable via the usual settings mechanism.
- **FR-004**: After a run's digest has been persisted and delivery has started (runs that end `succeeded` or `partial`; never dry runs or `failed` runs), and only if the digest is non-empty and the job's archive is enabled, the system MUST write a page at `<archive_dir>/<job-slug>/<YYYY-MM-DD-HHMM>.html`.
- **FR-005**: All pages and indexes MUST use one shared layout that is responsive and fully self-contained (inline styles, no external fonts, scripts, stylesheets, images or other resources).
- **FR-006**: Each page MUST show the job name, run date/time, the run statistics footer shown in the mail (but not the archive link itself) and the digest content rendered from Markdown with the same allowlist sanitization as the mail HTML (#20); all other inserted values MUST be escaped.
- **FR-007**: After each archived digest, the system MUST regenerate the job's `index.html` (digests newest first, each linking to its page) and the global `index.html` (all archived jobs, each linking to its job index), built from the archive directory's actual content.
- **FR-008**: Job names MUST be converted to a filesystem-safe slug before use in any path; the result MUST never contain path separators, parent-directory references or control characters, and MUST be non-empty and length-bounded.
- **FR-009**: Distinct job names MUST map to distinct archive directories even if their plain slugs collide.
- **FR-010**: Every file MUST be written atomically (complete content to a temporary file in the same directory, then renamed into place); no partial file may ever be visible, and temporary files MUST NOT be left behind on failure.
- **FR-011**: When the archive is enabled, `base_url` is set and the digest's page was successfully written, the mail footer MUST contain the absolute URL of that digest's page (base URL + job slug + page name, normalised slashes); otherwise the footer MUST NOT contain an archive link. The archive write therefore completes before the mail is built.
- **FR-012**: An archive failure (unwritable directory, disk full, rendering error) MUST be logged with the cause, MUST NOT fail the run, and MUST NOT prevent mail delivery (the mail is sent without an archive link).
- **FR-013**: A second digest for the same job in the same minute MUST NOT overwrite the first page; it MUST be written as `<YYYY-MM-DD-HHMM>-<n>.html` with the next free number `n` starting at 2, and the mail link MUST use that actual name.
- **FR-014**: Empty digests, failed runs and dry runs MUST NOT create pages or alter indexes.

### Key Entities *(include if feature involves data)*

- **Archive Settings (per job)**: whether the archive is on and the public base URL used for links in mail.
- **Archive Directory**: the configured root that holds one folder per job plus the global index.
- **Job Archive**: a folder named by the job's safe slug, containing digest pages and the job index.
- **Digest Page**: a self-contained HTML rendering of one digest, named by run date and time.
- **Index (job / global)**: a generated listing page; the job index lists digests newest first, the global index lists jobs.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of runs that end `succeeded` or `partial` with a non-empty digest on an archive-enabled job produce a readable page and updated job and global indexes; 0% of empty digests, failed runs or dry runs do.
- **SC-002**: A scan of all generated files finds 0 references to external resources, and pages open fully styled with no network access.
- **SC-003**: For a test set of at least 20 hostile job names (path traversal, separators, unicode, control characters, very long, symbols only), 100% of written files lie inside the archive directory.
- **SC-004**: When the archive is configured with a base URL, 100% of digest mails carry a footer link that resolves to the matching archived page; 0% carry one otherwise.
- **SC-005**: Interrupting a write at any point never leaves a partially written page or index visible to readers.
- **SC-006**: A reader can reach any archived digest from the global index in at most two clicks.
- **SC-007**: Pages remain usable on a 320px-wide screen without horizontal scrolling.

## Assumptions

- Depends on #19 (digest persistence and run pipeline) and #20 (mail notifier and its Markdown sanitization); this feature reuses the sanitizer and the mail footer rather than redefining them.
- Serving, TLS and basic auth are the operator's responsibility; this feature only writes files.
- The archive keeps all pages indefinitely; retention and pruning are out of scope.
- The date/time in the file name and on pages/indexes is the run's timestamp in UTC, labelled "UTC" wherever displayed.
- Search, tagging, RSS feeds and full-text features are out of scope.
- The archive link in mail is only added when the archive is enabled and a `base_url` is set, since a link without a published page would be dead.
- Slug collisions are resolved by a short deterministic suffix derived from the original name.
- Technology choices named in the issue (template engine, Markdown renderer) are planning constraints decided in `/speckit-plan`, not in this spec.
