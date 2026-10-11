# Feature Specification: End-to-End Integration Test Suite

**Feature Branch**: `gh-issue-26`

**Created**: 2026-10-10

**Status**: Draft

**Input**: User description: "GitHub issue #26 — [TEST] Add end-to-end integration test suite. Depends on #23, #25. Context: Unit tests cover single nodes. An end-to-end suite verifies that a job configured via YAML runs through the scheduler, produces a digest, mail and archive, and behaves correctly on repeated runs. Implementation steps: (1) Build fixtures: local HTTP server with an RSS feed and article pages, local SMTP sink (aiosmtpd), FakeProvider with deterministic scores/summaries. (2) Scenario A: create job → `run-due` → digest, mail, archive exist. (3) Scenario B: second `run-due` after bumping `next_run_at` produces no duplicates and sends no mail. (4) Scenario C: new article appears → only that item is processed. (5) Scenario D: provider failure for one item → run `partial`, mail still sent. (6) Run the suite in CI against SQLite and (nightly) MariaDB. Acceptance criteria: all four scenarios pass in CI; suite needs no internet access or real API keys; suite runs in under 60 seconds on SQLite; nightly MariaDB job is green."

## Clarifications

### Session 2026-10-10

- Q: How should the MariaDB run of the end-to-end suite be wired into CI? → A: A new nightly scheduled job (also manually triggerable) runs the end-to-end suite and the `db` tests on MariaDB and fails visibly; the existing push-time MariaDB job stays unchanged and does not run the end-to-end suite.
- Q: If the new suite uncovers a real product bug, should fixing it be part of this issue? → A: Small, local bugs are fixed here, with the scenario as the failing test; larger fixes go to a separate issue, and this issue stays open until that fix lands (no scenario is merged as skipped or expected-to-fail).
- Q: Should the scenarios start `invio` as a real separate process, or call the CLI inside the test process? → A: All scenarios call the CLI in-process; one additional smoke test runs Scenario A through a real separate `invio` process.

## User Scenarios & Testing *(mandatory)*

The "users" of this feature are the maintainers of invio and the CI system. Existing tests pin
down single pipeline stages and single commands in isolation. This suite proves that the pieces
work together the way an operator uses them: a job is defined in a YAML file, the scheduled
`invio run-due` invocation picks it up, and the operator receives a digest mail and finds the
digest in the static archive — and that repeated unattended invocations behave correctly.

Every scenario drives the system only through its public entry points (the `invio` CLI and the
job YAML file) and observes only externally visible outcomes (stored records, received mail,
archive files, exit codes). All external dependencies are replaced by local stand-ins: a local
web server serving a feed and article pages, a local mail sink capturing outgoing mail, and a
fake language-model provider returning deterministic scores and summaries.

### User Story 1 - First scheduled run delivers digest, mail and archive (Scenario A, Priority: P1)

A maintainer creates a job from a YAML file that points at a local feed with a few articles,
enables mail delivery to the local mail sink and enables the archive. When `invio run-due` is
invoked, the job is due, runs to completion, and the operator-visible results all exist: a
stored digest, exactly one mail per configured recipient, and an archive page plus updated
per-job and global indexes.

**Why this priority**: This is the core promise of the product end to end. If this path is
broken, nothing else matters, and no unit test currently covers the full chain from YAML to
mailbox and archive.

**Independent Test**: Can be fully tested by starting the local fixtures, creating the job via
the CLI, running `invio run-due` once, and inspecting the database, the mail sink and the archive
directory.

**Acceptance Scenarios**:

1. **Given** an empty database, the local fixtures running and a job YAML referencing the local
   feed, **When** the job is created via the CLI and `invio run-due` is invoked, **Then** the
   command exits with code 0 and exactly one run for the job is recorded with status `succeeded`.
2. **Given** the run from scenario 1, **When** the results are inspected, **Then** one digest is
   stored that contains every relevant article of the feed with the fake provider's
   deterministic summary, and articles scored below the job's threshold are not in it.
3. **Given** the run from scenario 1, **When** the mail sink is inspected, **Then** each
   configured recipient received exactly one digest mail whose content matches the stored
   digest, and (when an archive base URL is configured) the mail footer links to the archive
   page.
4. **Given** the run from scenario 1, **When** the archive directory is inspected, **Then** a
   digest page exists for the job, the per-job index and the global index both list it, and
   none of the archive files reference external resources.
5. **Given** the run from scenario 1, **When** the job record is inspected, **Then** its lock is
   released and its next run time lies in the future according to its schedule.

---

### User Story 2 - Repeated invocation does not duplicate work or mail (Scenario B, Priority: P1)

The scheduler invokes `invio run-due` many times a day. After a successful run, the job is made
due again (its next run time is moved into the past) while the feed is unchanged. The second
invocation runs the job, but finds nothing new: no item is stored or processed a second time,
no second digest is created, and no mail is sent.

**Why this priority**: Duplicate mails are the most visible failure an unattended research tool
can have; idempotency across runs must be guaranteed end to end, not just per component.

**Independent Test**: Can be tested by running Scenario A's setup, moving the job's next run time
into the past, invoking `invio run-due` again, and comparing item, digest, mail and archive
counts before and after.

**Acceptance Scenarios**:

1. **Given** a job that completed Scenario A and an unchanged feed, **When** the job's next run
   time is moved into the past and `invio run-due` is invoked again, **Then** the command exits
   with code 0 and a second run is recorded for the job.
2. **Given** the second run, **When** stored items are counted, **Then** the number of items is
   unchanged and no article was scored or summarized again (the fake provider received no new
   requests).
3. **Given** the second run, **When** the mail sink and the archive are inspected, **Then** no new
   mail was received and no new archive page was written.
4. **Given** the job had not been made due again, **When** `invio run-due` is invoked, **Then**
   the job does not run at all.

---

### User Story 3 - Only newly published articles are processed (Scenario C, Priority: P2)

After the first run, a new article is published in the feed. On the next due invocation, only
that new article is fetched, scored and summarized; the digest and mail contain only the new
article; and a new archive page is added alongside the first one.

**Why this priority**: Incremental processing is what makes repeated runs cheap and useful; it
depends on Scenario A's baseline and complements Scenario B's "nothing new" case.

**Independent Test**: Can be tested by running Scenario A's setup, adding one article to the
local feed, making the job due, invoking `invio run-due`, and checking which items the fake
provider was asked about and what was mailed.

**Acceptance Scenarios**:

1. **Given** a job that completed Scenario A, **When** one new article is added to the local feed,
   the job is made due and `invio run-due` is invoked, **Then** exactly one new item is stored
   and the fake provider was asked to score/summarize only that item.
2. **Given** the run from scenario 1 and a relevant new article, **When** the mail sink is
   inspected, **Then** exactly one new mail per recipient was received and its digest contains
   only the new article.
3. **Given** the run from scenario 1, **When** the archive is inspected, **Then** a second digest
   page exists for the job and both indexes list both pages, newest first.

---

### User Story 4 - One failing item yields a partial run that still mails (Scenario D, Priority: P2)

The fake provider is configured to fail for exactly one article of the feed. The run continues
with the remaining articles, is recorded as `partial`, records which item failed and in which
stage, and the digest of the remaining articles is still mailed and archived.

**Why this priority**: Unattended runs must degrade gracefully; a single bad item must never cost
the operator their digest. This verifies the failure path through the real scheduler entry point.

**Independent Test**: Can be tested from a fresh database by configuring the fake provider to
fail for one named article, creating the job, invoking `invio run-due`, and inspecting the run
status, item statuses, mail sink and archive.

**Acceptance Scenarios**:

1. **Given** a fresh database and a fake provider that fails for one specific article, **When**
   the job is created and `invio run-due` is invoked, **Then** the run is recorded with status
   `partial` and its error list names the failing item and stage.
2. **Given** the run from scenario 1, **When** items are inspected, **Then** the failing item is
   marked failed and all other relevant items are in the digest.
3. **Given** the run from scenario 1, **When** the mail sink and archive are inspected, **Then**
   each recipient received the digest mail and the archive page exists.
4. **Given** the run from scenario 1, **When** the job record is inspected, **Then** its lock is
   released and its next run time is set (a partial run is not treated as a failed run).

---

### User Story 5 - Suite runs in CI on SQLite and nightly on MariaDB (Priority: P3)

The end-to-end suite runs on every push and pull request as part of the regular test gate
against SQLite. A scheduled nightly CI job runs the same suite against a real MariaDB, so
database-specific behaviour (locking, transactions, encodings) is covered without slowing down
every push.

**Why this priority**: The scenarios deliver value as soon as they run locally; wiring them into
CI on both databases is what keeps them protecting `main` over time.

**Independent Test**: Can be tested by inspecting the CI configuration for a step that runs the
suite on push/PR and a scheduled nightly job against MariaDB, and by observing a green run of
each.

**Acceptance Scenarios**:

1. **Given** a push or pull request, **When** CI runs, **Then** all four scenarios run against
   SQLite as part of the blocking test gate and pass.
2. **Given** the nightly schedule fires (or the job is triggered manually), **When** the nightly
   MariaDB job runs, **Then** all four scenarios and the `db` persistence tests run against a
   MariaDB service and pass, and any failure turns the job red.
3. **Given** a push or pull request, **When** the existing push-time MariaDB job runs, **Then** it
   runs only the `db` persistence tests, as before.
4. **Given** a CI runner without internet access to third-party hosts and without any provider
   API keys or mail credentials, **When** the suite runs, **Then** it passes.

### Edge Cases

- A fixture port is already in use: fixtures must bind to free, dynamically assigned local ports
  so parallel or repeated runs never collide.
- Leftover state between scenarios: each scenario starts from its own fresh database, archive
  directory and mail sink (Scenarios B and C build on A's state inside the same scenario, never
  across scenarios).
- An accidental real network call (e.g. a misconfigured URL or a provider client reaching the
  internet): the suite must fail loudly rather than silently pass or hang.
- A real API key present in the developer's environment: the suite must not pick it up; the fake
  provider must be used regardless of ambient configuration.
- The mail sink receives mail asynchronously: assertions must wait for delivery deterministically
  (bounded, not by fixed sleeps) so the suite is not flaky.
- Clock-dependent behaviour (due times, archive file names in UTC, same-minute name collisions):
  the suite controls time or the next run time explicitly so results do not depend on wall-clock
  time.
- MariaDB unavailable in the nightly job: the job must fail visibly rather than skip the
  scenarios and report green.
- A run that is `failed` entirely (every item fails) is out of scope for Scenario D but must not
  be confused with `partial`; Scenario D asserts `partial` specifically.

## Requirements *(mandatory)*

### Functional Requirements

**Fixtures**

- **FR-001**: The suite MUST provide a local web server fixture that serves an RSS feed and the
  article pages it links to, and whose feed content can be changed during a test (to add an
  article for Scenario C).
- **FR-002**: The suite MUST provide a local mail sink fixture that accepts outgoing mail from
  invio and exposes the received messages (recipients, subject, body) for assertions.
- **FR-003**: The suite MUST provide a fake language-model provider that returns deterministic
  relevance scores and summaries per article, records which articles it was asked about, and can
  be configured to fail for specific articles.
- **FR-004**: Jobs in the suite MUST be defined as YAML job files and created through the `invio`
  CLI, exactly as an operator would, and runs MUST be triggered through `invio run-due`.
  Scenarios A–D MUST invoke the CLI inside the test process (same commands, arguments and exit
  codes as on the command line), so time, the fake provider and the network guard are under the
  test's control.
- **FR-019**: One additional smoke test MUST run Scenario A by starting the installed `invio`
  command as a separate process against the same local fixtures, with an environment stripped of
  provider API keys and mail credentials, and assert its exit codes, stored digest, received mail
  and archive page. Because the in-process fake provider is not available there, this test MUST
  use a provider configuration that resolves to a local stand-in only.
- **FR-005**: Each scenario MUST run against an isolated database, archive directory and mail
  sink; no state may leak between scenarios.

**Scenarios**

- **FR-006**: Scenario A MUST verify that a first `run-due` invocation produces a stored digest,
  one mail per recipient matching the digest, an archive page with updated per-job and global
  indexes, a `succeeded` run record, a released lock and a future next run time.
- **FR-007**: Scenario B MUST verify that a second `run-due` invocation after moving the job's
  next run time into the past, with an unchanged feed, creates no duplicate items, digests or
  archive pages, makes no new provider requests and sends no mail.
- **FR-008**: Scenario C MUST verify that after one article is added to the feed, the next due
  invocation stores and processes only that article, and the resulting mail and archive page
  contain only that article.
- **FR-009**: Scenario D MUST verify that a provider failure for one article results in a run with
  status `partial`, an error entry naming the failed item and stage, the remaining articles in the
  digest, and the digest mail still being sent and archived.
- **FR-010**: Every scenario MUST assert the exit code of each CLI invocation.

**Isolation and execution**

- **FR-011**: The suite MUST NOT require internet access, real provider API keys or real mail
  credentials, and the in-process scenarios MUST fail (not hang or skip) if any code path attempts
  a connection to a non-local host. The separate-process smoke test (FR-019) is configured to
  reach local fixtures only.
- **FR-012**: The suite MUST be deterministic: no dependence on wall-clock time, network timing
  or test order, and no fixed sleeps for synchronisation. Exception: the separate-process smoke
  test (FR-019) cannot be given a fake clock, so it uses real time. It asserts only counts (runs,
  digests, mails, archive pages), never time-derived values such as page names or exact
  schedule times, and waits for mail with a bounded poll.
- **FR-013**: The suite MUST be selectable on its own (e.g. via a dedicated test marker) so it can
  be run in isolation locally and in CI.
- **FR-014**: The suite MUST run against SQLite by default and against MariaDB when a MariaDB
  connection is configured for tests, using the same scenarios without modification.

**CI**

- **FR-015**: CI MUST run the suite on SQLite on every push and pull request as part of the
  blocking test gate.
- **FR-016**: CI MUST provide a nightly scheduled job, also triggerable manually, that runs the
  end-to-end suite and the existing `db` persistence tests against a MariaDB service. That job
  MUST NOT be marked as allowed to fail and MUST report failure (not success) when a scenario
  fails or MariaDB is unreachable.
- **FR-017**: The existing push-time MariaDB job MUST remain unchanged (non-blocking, `db` tests
  only); it MUST NOT run the end-to-end suite.

**Uncovered product bugs**

- **FR-018**: A product bug uncovered by a scenario that can be fixed locally and in a small change
  MUST be fixed within this feature, with that scenario serving as the failing test. A larger fix
  MUST be tracked in a separate issue, and this feature is not complete until that fix has landed.
  No scenario may be merged as skipped or expected-to-fail.

### Key Entities

- **Feed fixture**: A locally served feed with a mutable list of articles (title, link, published
  time, page content). Changing it simulates new publications.
- **Mail sink**: A local mail receiver holding the list of received messages for inspection.
- **Fake provider**: A stand-in language model with a per-article table of scores, summaries and
  forced failures, plus a log of the requests it received.
- **Scenario environment**: The per-scenario bundle of isolated database, archive directory,
  settings, job YAML file and the fixtures above.
- **Observed outcomes**: Run records (status, errors), items, digests, received mails, archive
  pages/indexes and job schedule state (lock, next run time) — the things each scenario asserts.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All four scenarios (A–D) pass in the regular CI test gate on every push and pull
  request.
- **SC-002**: The suite passes on a machine with outbound internet blocked and with no provider
  API keys or mail credentials configured.
- **SC-003**: The complete end-to-end suite finishes in under 60 seconds on SQLite on a standard
  CI runner.
- **SC-004**: The nightly MariaDB job runs all four scenarios and is green.
- **SC-005**: Running the suite 20 times in a row locally yields 20 identical, passing results
  (no flakiness).
- **SC-006**: Deliberately breaking any one of the end-to-end guarantees (e.g. re-sending mail on
  an unchanged feed, skipping the archive, aborting the run on one failed item) makes at least one
  scenario fail.

## Assumptions

- The features this suite exercises already exist: scheduled `invio run-due` with locking and
  next-run handling (#23), the static HTML archive with mail footer link (#25), mail delivery,
  item de-duplication and the `partial` run status for single-item failures. This issue mainly
  adds tests and CI wiring; see FR-018 for product bugs the suite uncovers.
- "Bumping `next_run_at`" means moving the job's next run time into the past (directly in the
  test database or via a controlled clock), so the job becomes due without waiting.
- In Scenario B, a run is still recorded (the job was due), but it finds no new items; "no
  duplicates" covers items, digests, archive pages and mails.
- In Scenario C, the new article is configured to be relevant so that a mail is produced; the
  digest contains only new items, matching the existing product behaviour.
- Scenario D's failure is injected at the provider (scoring or summarizing one article); other
  failure kinds (fetch errors, delivery errors) are already covered by existing tests.
- The existing test dependency on a local SMTP server library and the existing database test
  configuration (SQLite default, MariaDB via a test database URL) are reused.
- The existing MariaDB CI job runs on push and is non-blocking; it is left as is. The new nightly
  job is separate and fails visibly (see Clarifications).
- The 60-second budget applies to the end-to-end suite alone, not to the full test suite.
