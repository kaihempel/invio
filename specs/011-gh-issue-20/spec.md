# Feature Specification: E-mail Notifier with HTML and Plaintext Templates

**Feature Branch**: `gh-issue-20`

**Created**: 2026-10-05

**Status**: Draft

**Input**: User description: "GitHub issue #20 (short name: gh-issue-20): Add e-mail notifier with HTML and plaintext templates. Context: the digest is delivered by e-mail to the addresses in the job config; delivery status must be tracked so failures can be retried without regenerating the digest. Depends on #3, #4. Requirements: (1) scout/notify/email.py sends mail via aiosmtplib using STARTTLS or SSL per settings. (2) Digest Markdown is rendered to HTML with an allowlist sanitizer (markdown-it-py + nh3); message is multipart with an HTML part and a plaintext alternative. (3) Jinja2 templates in scout/notify/templates/ (digest.html.j2, digest.txt.j2) include job name, date and a run stats footer. (4) Subject comes from notification.subject with placeholders {job_name} and {date}. (5) With send_if_empty=false, an empty digest sends no mail and creates no notification rows. (6) One notifications row per recipient; status updated to sent/failed with error text. (7) New CLI command `scout notify retry` re-sends all failed notifications. Acceptance criteria: mail is sent with HTML and plaintext parts (verified against a local test SMTP server); script tags and unsafe attributes in digest Markdown are stripped from HTML; empty digest with send_if_empty=false sends nothing; SMTP failure leaves a failed notification and does not fail the run; `scout notify retry` re-sends failed notifications and updates their status."

## Clarifications

### Session 2026-10-05

- Q: When some or all e-mails for a digest fail to send, what status should the run end with? → A: If any e-mail fails, a run that would have ended `succeeded` ends `partial` instead; a run that is already `partial` or `failed` keeps its status.
- Q: Should `invio notify retry` stop retrying a notification after it has failed a set number of times? → A: Yes. Attempts are counted (first send plus retries); after 5 failed attempts the notification is moved to `skipped` with its last error kept, and retry ignores it from then on.
- Q: How should the settings choose between STARTTLS and implicit TLS (SSL) for the mail server connection? → A: One `smtp_security` setting replaces `smtp_starttls`; it accepts `starttls` (default), `ssl` or `none` and rejects any other value.
- Q: What should happen to a notification left in `pending` because invio crashed or was killed while sending? → A: Retry also picks up `pending` notifications older than 1 hour, counts the interrupted send as one failed attempt, and re-sends; a duplicate mail is accepted if the original send had actually succeeded.

## User Scenarios & Testing *(mandatory)*

The "users" of this feature are (a) the **recipients** listed in a job's notification settings, who read the digest in their mail client, and (b) the **operator**, who runs invio unattended and needs to know whether each digest reached each recipient and to re-deliver it when it did not.

### User Story 1 - Receive the digest by e-mail (Priority: P1)

After a job run produces a digest, every address in the job's notification recipient list receives one e-mail. The mail has a formatted (HTML) version and a plain-text version of the same content, so it reads well in modern mail clients and in text-only clients. The body shows the job name, the digest date, the digest content and a short footer with the run's statistics. The subject is taken from the job's configured subject, with `{job_name}` and `{date}` replaced by the job name and the digest date.

**Why this priority**: Delivering the digest is the whole point of the pipeline; without it the research results never reach anyone.

**Independent Test**: Configure a job with two recipients against a local test mail server, deliver a stored digest, and inspect the two received messages.

**Acceptance Scenarios**:

1. **Given** a job with recipients `a@example.org` and `b@example.org` and a non-empty digest, **When** the digest is delivered, **Then** the local test mail server receives exactly one message per recipient, each addressed to only that recipient.
2. **Given** a delivered message, **When** it is inspected, **Then** it contains both an HTML part and a plain-text part as alternatives of the same content, and both include the job name, the digest date, the digest content and the run statistics footer.
3. **Given** the configured subject `invio: {job_name} – {date}` for job `ai-news` and a digest dated 2026-10-05, **When** the digest is delivered, **Then** the subject is `invio: ai-news – 2026-10-05`.
4. **Given** a subject without placeholders, **When** the digest is delivered, **Then** the subject is used verbatim.
5. **Given** `smtp_security` is `starttls` (upgrade a plain connection to encryption), `ssl` (connection encrypted from the start) or `none` (unencrypted), **When** the digest is delivered, **Then** the connection uses exactly that mode and authenticates with the configured credentials when they are set.
6. **Given** `smtp_security` has any other value (e.g. `tls`), **When** the settings are loaded, **Then** loading fails with an error that names `smtp_security` and lists the allowed values.

---

### User Story 2 - Digest content cannot inject active or unsafe content (Priority: P1)

The digest text is partly generated from external web content and LLM output. When it is turned into the HTML version of the mail, anything outside a fixed allowlist of harmless formatting (headings, paragraphs, emphasis, lists, links, quotes, code, tables) is removed: script elements, event-handler attributes, inline styles, `javascript:` and other unsafe link targets, embedded frames and forms.

**Why this priority**: Without this, a hostile source page could place active content into a mail sent to every recipient — a security issue that blocks shipping the notifier at all.

**Independent Test**: Render a digest whose text contains `<script>`, `onclick=`/`onerror=` attributes, `javascript:` links and an `<iframe>`, and inspect the resulting HTML part.

**Acceptance Scenarios**:

1. **Given** digest text containing `<script>alert(1)</script>`, **When** the HTML part is produced, **Then** it contains no `<script>` element (and its content is not executed or shown as markup).
2. **Given** digest text containing an element with `onclick`, `onerror` or `style` attributes, **When** the HTML part is produced, **Then** those attributes are absent.
3. **Given** a link with target `javascript:…` or `data:…`, **When** the HTML part is produced, **Then** the unsafe target is removed; links with `http`, `https` or `mailto` targets are kept.
4. **Given** ordinary digest formatting (headings, bullet lists, bold text, links), **When** the HTML part is produced, **Then** that formatting is preserved.
5. **Given** a job name or subject containing HTML special characters (e.g. `<b>&`), **When** the mail is built, **Then** they appear as literal text, not as markup.

---

### User Story 3 - Delivery failures are recorded without failing the run (Priority: P1)

For each digest, invio records one notification per recipient. Each record starts as pending and ends as `sent` (with the time it was sent) or `failed` (with the error text). If the mail server is unreachable, rejects the login or rejects a recipient, the affected notifications are marked `failed` and the run still completes: a run that would have ended `succeeded` ends `partial`, while a run that is already `partial` or `failed` keeps its status. The digest and its items stay stored.

**Why this priority**: The operator must be able to see what was and was not delivered, and a temporary mail outage must not throw away a whole research run.

**Independent Test**: Deliver a digest while the mail server is unreachable (or rejects one recipient), then inspect the run status and the notification records.

**Acceptance Scenarios**:

1. **Given** a successful delivery to two recipients, **When** the notification records are inspected, **Then** there are exactly two records for that digest, both `sent`, each with a send time and no error text.
2. **Given** the mail server is unreachable, **When** the digest is delivered, **Then** every notification for that digest is `failed` with an error text describing the problem, and a run that would otherwise have `succeeded` ends `partial` (never `failed` because of delivery).
3. **Given** the mail server accepts recipient A but rejects recipient B, **When** the digest is delivered, **Then** A's notification is `sent`, B's is `failed` with the rejection reason, and the run ends `partial`.
4. **Given** mail server settings are missing (no server host or sender address configured), **When** a digest is to be delivered, **Then** each notification is `failed` with an error text naming the missing setting, and the run ends `partial` (not `failed`).
5. **Given** any failure, **When** the error text is stored or logged, **Then** it never contains the mail server password.

---

### User Story 4 - No mail for empty digests unless requested (Priority: P2)

If a run finds nothing new, the digest is empty. When the job's `send_if_empty` setting is `false` (the default), no mail is sent and no notification records are created. When it is `true`, the recipients receive a mail stating that nothing new was found, together with the run statistics footer.

**Why this priority**: Prevents inbox noise from daily jobs on quiet days; important for acceptance but secondary to delivering real content.

**Independent Test**: Deliver an empty digest once with `send_if_empty=false` and once with `send_if_empty=true` against the local test mail server.

**Acceptance Scenarios**:

1. **Given** an empty digest and `send_if_empty=false`, **When** delivery is invoked, **Then** the test mail server receives no message and no notification records exist for that digest.
2. **Given** an empty digest and `send_if_empty=true`, **When** delivery is invoked, **Then** each recipient receives a mail (HTML and plain text) stating that no new items were found, and one `sent` record exists per recipient.

---

### User Story 5 - Retry failed notifications from the CLI (Priority: P2)

After fixing a mail problem, the operator runs `invio notify retry`. invio re-sends every notification currently in status `failed` — plus any notification stuck in `pending` for more than 1 hour (left behind by a crash) — using the digest that was already stored — no research or LLM work is repeated. Each retried notification ends as `sent` or again `failed` with the new error text. Each notification gets at most 5 send attempts in total (first send plus retries); after the 5th failed attempt it is moved to `skipped` with its last error kept and is no longer retried. The command prints a short summary (retried / sent / still failed / given up) and exits non-zero if any retried notification did not end `sent`.

**Why this priority**: Turns transient delivery failures into a recoverable situation; depends on Story 3's records existing.

**Independent Test**: Create failed notifications (mail server down), bring a local test mail server up, run the retry command, and inspect received messages, records and exit code.

**Acceptance Scenarios**:

1. **Given** two `failed` notifications and a working mail server, **When** `invio notify retry` runs, **Then** both recipients receive the mail, both records become `sent` with a send time and cleared error text, the summary reports 2 retried / 2 sent / 0 failed, and the exit code is 0.
2. **Given** a `failed` notification and a still-unreachable mail server, **When** the command runs, **Then** the record stays `failed` with the new error text, the summary reports it, and the exit code is non-zero.
3. **Given** notifications in status `sent` or `skipped`, or in `pending` for less than 1 hour, **When** the command runs, **Then** they are not re-sent.
4. **Given** no failed notifications, **When** the command runs, **Then** it reports that there is nothing to retry and exits 0.
5. **Given** a retried notification, **When** the received mail is compared with the original attempt, **Then** subject and content are the same as for the original delivery (same digest, same date), and no new digest or run is created.
6. **Given** a failed notification whose digest has since been deleted, **When** the command runs, **Then** that notification is reported and fails again with the error text `digest no longer available` (the attempt counts toward the limit, so it ends `skipped` after the 5th attempt), and the other notifications are still retried.
7. **Given** a `failed` notification that has already had 4 failed attempts and a still-unreachable mail server, **When** the command runs, **Then** the 5th attempt fails, the record moves to `skipped` with the last error text kept, the summary reports it as given up, and the exit code is non-zero; **When** the command runs again, **Then** that notification is not attempted and (with no other failures) the command exits 0.
8. **Given** a notification left in `pending` for more than 1 hour after an interrupted run, **When** the command runs, **Then** the interrupted send counts as one failed attempt, the notification is re-sent and ends `sent` (or `failed`/`skipped` per the attempt limit), and it appears in the summary as retried.
9. **Given** failed notifications and no mail server host (or sender) configured, **When** the command runs, **Then** it reports a configuration error naming the missing setting, exits with code 2, and every notification keeps its status and attempt count.

---

### Edge Cases

- A recipient address appears twice in the job's list → one notification and one mail per distinct address.
- The mail server accepts the connection but times out mid-transfer → notification `failed` with a timeout error; other recipients are still attempted.
- Very long error texts from the mail server are truncated to 2,000 characters with the marker `… [truncated]`, never causing the record update itself to fail.
- The subject contains braces other than the two supported placeholders (e.g. `{unknown}` or a literal `{`) → those are left verbatim; the subject is never rejected at send time.
- The subject contains line breaks after substitution (e.g. via a job name) → line breaks are removed so no extra mail headers can be injected.
- Digest text contains raw HTML that is allowed by the allowlist (e.g. `<em>`) → kept; anything else is stripped while its harmless text is preserved.
- Non-ASCII content (umlauts, emoji) in subject, job name or digest → delivered correctly encoded in both parts.
- `invio notify retry` while a job run is concurrently delivering → only notifications already `failed` (or `pending` for more than 1 hour) at query time are retried, so an in-progress delivery is not touched; a notification is never sent twice by one retry invocation.
- invio is killed between sending a mail and saving `sent` → the record stays `pending`; after 1 hour retry re-sends it, so the recipient may receive the digest twice (accepted trade-off over silent loss).
- A recipient that the mail server rejects permanently → retried until the 5-attempt limit, then `skipped`, so a cron-driven retry stops reporting it.
- The job that owned a failed notification has been changed since (e.g. different recipient list) → retry re-sends to the recipient stored on the notification record, not to the current list.

## Requirements *(mandatory)*

### Functional Requirements

**Delivery**

- **FR-001**: System MUST deliver each non-empty digest by e-mail to every distinct address in the job's notification recipient list, as one separate message per recipient.
- **FR-002**: System MUST connect to the mail server configured in the application settings, using the connection security selected by the `smtp_security` setting — `starttls` (default), `ssl` (implicit TLS) or `none` — and authenticate with the configured user and password when both are set.
- **FR-002a**: The `smtp_security` setting MUST replace the existing `smtp_starttls` flag and MUST reject any value other than `starttls`, `ssl` or `none` with an error naming the setting and the allowed values.
- **FR-003**: Each message MUST contain an HTML part and a plain-text part as alternative representations of the same content, with the sender taken from the configured sender address.
- **FR-004**: Both parts MUST be produced from templates and MUST include the job name, the digest date, the digest content and a footer with run statistics (at least: items found, items included in the digest, and run duration when available).
- **FR-005**: The subject MUST be the job's configured subject with `{job_name}` replaced by the job name and `{date}` by the digest date (ISO format `YYYY-MM-DD`, in the job's schedule time zone); other text MUST be kept verbatim and line breaks MUST be removed.

**Safe rendering**

- **FR-006**: The HTML part MUST be generated from the digest's Markdown text and then sanitized against an allowlist of harmless elements and attributes; script/style elements, frames, forms, event-handler and style attributes, and link/image targets with schemes other than `http`, `https` and `mailto` MUST be removed.
- **FR-007**: Values inserted into the templates outside the sanitized digest body (job name, subject, statistics) MUST be escaped so they render as text.

**Empty digests**

- **FR-008**: When the digest is empty and the job's `send_if_empty` is `false`, System MUST send no mail and create no notification records.
- **FR-009**: When the digest is empty and `send_if_empty` is `true`, System MUST send a mail stating that no new items were found, including the run statistics footer.

**Tracking**

- **FR-010**: System MUST create exactly one notification record per recipient per delivered digest, linked to the job, the run and the digest, with channel `email` and the recipient address, before attempting to send.
- **FR-011**: After each send attempt, System MUST set the record to `sent` (with send time, error cleared) or `failed` (with the error text).
- **FR-012**: A delivery failure for one recipient MUST NOT prevent attempts for the other recipients.
- **FR-013**: Delivery failures (including missing mail settings) MUST NOT mark the run as `failed` or raise an error out of the run; they MUST be logged with job, run and recipient. If at least one notification of the run ends `failed`, a run that would otherwise end `succeeded` MUST end `partial`; a run already `partial` or `failed` keeps its status.
- **FR-014**: Error texts and logs MUST NOT contain the mail server password or other secret settings.
- **FR-015**: The subject and date used for a notification MUST be stored with the record so that a later retry reproduces the same mail.

**Retry**

- **FR-016**: The CLI MUST provide `invio notify retry`, which re-sends every notification in status `failed` and every notification that has been `pending` for more than 1 hour (measured from its last send attempt, or its creation time if it was never attempted), rebuilding the message from the stored digest and the data stored with the notification, without starting a run or regenerating the digest. Before touching any notification, retry MUST check that the mail server host and sender are configured; if not, it MUST exit with the configuration-error code (2) and leave all notifications unchanged (no attempt is counted).
- **FR-017**: Retry MUST send to the recipient stored on the record and update the record's status, send time and error text exactly as for the first attempt.
- **FR-018**: Retry MUST NOT re-send notifications in status `sent` or `skipped`, nor `pending` notifications younger than 1 hour. A stale `pending` notification picked up by retry MUST have its interrupted send counted as one failed attempt toward the 5-attempt limit before it is re-sent; a resulting duplicate mail (if the interrupted send had in fact succeeded) is accepted.
- **FR-019**: Retry MUST print a summary (retried, sent, still failed, given up — i.e. moved to status `skipped`) to stdout and exit with code 0 when every retried notification ended `sent` (or there was nothing to retry), and non-zero otherwise.
- **FR-020**: If a failed notification's digest no longer exists, retry MUST count the attempt as failed with the explanatory error text `digest no longer available` (so it becomes `failed`, or `skipped` once the attempt limit of FR-021 is reached) and continue with the others.
- **FR-021**: System MUST count send attempts per notification (the first send counts as attempt 1). When an attempt fails and the notification has reached 5 attempts, System MUST set it to `skipped` instead of `failed`, keeping the last error text; `skipped` notifications are never retried.

### Key Entities

- **Digest**: The stored result of a run for a job — title, Markdown body and the included items. Read-only for this feature; it is the single source of mail content for first delivery and retries.
- **Notification**: One delivery of one digest to one recipient over one channel (`email`). Holds job, run, digest, recipient, status (`pending` → `sent` | `failed`; `failed` → `sent` | `failed` | `skipped` after 5 failed attempts; `pending` older than 1 hour is treated like `failed` by retry), attempt count, send time, error text, and the data needed to reproduce the mail (rendered subject, digest date).
- **Notification settings (job)**: Recipient list, subject template with `{job_name}`/`{date}` placeholders, and `send_if_empty` flag (defined by #3).
- **Mail server settings (application)**: Host, port, user, password (secret), sender address, and connection security `smtp_security` (`starttls` | `ssl` | `none`, default `starttls`).
- **Run statistics**: Counts and timings of the run shown in the mail footer.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For a digest with N distinct recipients and a working mail server, exactly N messages are received and exactly N notification records exist, all `sent` (verified against a local test mail server).
- **SC-002**: 100% of delivered messages contain both a formatted and a plain-text version with job name, date, content and statistics footer.
- **SC-003**: 0 script elements, event-handler attributes or unsafe link targets survive into the formatted version across a test corpus of known injection patterns, while 100% of allowed formatting in that corpus is preserved.
- **SC-004**: An empty digest with `send_if_empty=false` results in 0 messages and 0 notification records.
- **SC-005**: When the mail server is unavailable, 100% of affected notifications end in `failed` with a non-empty error text, and the run ends `partial` instead of `succeeded` (never `failed` because of delivery).
- **SC-006**: After the mail server is restored, a single `invio notify retry` delivers 100% of previously failed notifications, with no new run or digest created.

## Assumptions

- The issue refers to the package as `scout`; this project's package and CLI are named `invio`, so the code lives under the `invio` notify package (with templates in its `templates/` folder) and the command is `invio notify retry`.
- Technology choices named in the issue (async SMTP client, Markdown renderer, allowlist sanitizer, template engine) are planning constraints and are decided in `/speckit-plan`, not in this spec.
- Job configuration (#3) already provides `notification.to`, `notification.subject` and `notification.send_if_empty`; the database layer (#4) already provides the `notifications` table, its statuses (`pending`, `sent`, `failed`, `skipped`) and a repository to add and mark records. The rendered subject, date and stats snapshot go into the existing notification payload; attempt counting and safe claiming for retry need two new columns (`attempts`, `last_attempt_at`), added by a migration (see plan.md, Complexity Tracking).
- Application settings already define mail host, port, user, password and sender address; this feature replaces the `smtp_starttls` flag with `smtp_security`. The port stays an explicit setting (default 587); it is not derived from the security mode. `.env.example` (`INVIO_SMTP_STARTTLS`) and the existing settings tests are updated to the new setting.
- "Empty digest" means a digest that includes zero items.
- The digest date is the date of the run that produced the digest, in the job's schedule time zone.
- Retry has no automatic scheduling in this issue; it is triggered manually by the operator (or by an external cron). The attempt limit is fixed at 5 and not configurable in this issue.
- Retry covers failed notifications of all jobs; filtering by job is optional and not required for acceptance.
- Wiring the notifier into the end-to-end pipeline graph is done where the digest is produced; this feature provides the delivery step and its CLI, callable with a stored digest.
- Tests use a local in-process test mail server; no external mail service is contacted (constitution principle III).
