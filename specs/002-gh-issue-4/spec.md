# Feature Specification: Persistent Research Data Store with Versioned Schema

**Feature Branch**: `gh-issue-4`

**Created**: 2026-10-04

**Status**: Draft

**Input**: User description: "GitHub issue #4: [FEAT] Add SQLAlchemy models and Alembic migrations for MariaDB. Persistence runs on MariaDB/MySQL in production, SQLite in tests. The schema covers jobs, runs, items, digests, notifications and llm_usage. Long URLs cannot be indexed directly, so a SHA-256 url_hash is used. Items are unique per (job, url_hash) and carry a processing status, attempts, last error and a relevance score; jobs carry next run / lock times; deleting a job cascades; the schema is created and removed through versioned migrations, exposed via a CLI command; tests run against an in-memory database and optionally against a real MariaDB."

## Clarifications

### Session 2026-10-04

- Q: How should a stored job record relate to the job's YAML definition file? → A: Job identified by unique `name`; DB stores scheduling state (enabled, next run, lock) plus a JSON snapshot of the last-loaded config; the YAML file stays authoritative.
- Q: Which record should LLM usage entries belong to, and what happens to them when a job is deleted? → A: Required job reference (cascade delete with the job) plus optional run reference (set to empty when the run is deleted).
- Q: Which status values should runs and notifications be allowed to have? → A: Fixed lists — runs: `running`, `succeeded`, `partial`, `failed`; notifications: `pending`, `sent`, `failed`, `skipped`.
- Q: When a single run is deleted, what happens to items, digests and notifications linked to it, and to notifications linked to a deleted digest? → A: The link is cleared and the record is kept (items, digests, notifications lose their run link; notifications lose their digest link).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Create and upgrade the database schema from the CLI (Priority: P1)

An operator deploying invio points it at an empty production database (MariaDB/MySQL) and runs a single CLI command (`invio db upgrade`). The command brings the database to the latest schema version, creating every table the application needs: jobs, runs, items, digests, notifications and LLM usage. Running the command again on an up-to-date database changes nothing. A developer can also roll the schema back to an empty state.

**Why this priority**: Nothing in the pipeline (scheduling, fetching, filtering, notifying) can persist state until the schema exists. This is the foundation every later track builds on.

**Independent Test**: Run the upgrade against a fresh, empty database and verify all six tables exist; run the downgrade to base and verify they are gone; repeat on both supported database engines.

**Acceptance Scenarios**:

1. **Given** a fresh, empty MariaDB database, **When** the operator runs `invio db upgrade`, **Then** all tables (jobs, runs, items, digests, notifications, llm_usage) are created and the database is recorded as being at the latest schema version.
2. **Given** a fresh, empty file-based or in-memory SQLite database, **When** the schema is upgraded to the latest version, **Then** the same set of tables is created.
3. **Given** a database at the latest schema version, **When** the schema is downgraded to base, **Then** all application tables are removed again.
4. **Given** a database already at the latest version, **When** `invio db upgrade` is run again, **Then** it succeeds without changes and exits with code 0.
5. **Given** no database URL is configured, **When** the operator runs `invio db upgrade`, **Then** the command exits with configuration error code 2 and a message naming the missing setting, without revealing any credentials.
6. **Given** a database that cannot be reached, **When** the operator runs `invio db upgrade`, **Then** the command exits non-zero with a diagnostic on stderr that does not contain the database password.

---

### User Story 2 - Store discovered items without duplicates (Priority: P1)

The research pipeline records every item it discovers for a job, keyed by a fixed-length fingerprint of the item's URL (so arbitrarily long URLs can still be looked up efficiently). The same URL must never be stored twice for the same job, while the same URL may legitimately appear under different jobs. Each item tracks where it is in the processing lifecycle, how many processing attempts were made, the last error, and its relevance score.

**Why this priority**: Duplicate-free item storage is what prevents users from being notified about the same article repeatedly; it is the core data guarantee of the tool.

**Independent Test**: Insert an item for a job, then insert a second item with the same URL fingerprint for the same job and verify the store rejects it with an integrity error; insert it for a different job and verify it is accepted.

**Acceptance Scenarios**:

1. **Given** a job with a stored item, **When** a second item with the same URL fingerprint is stored for the same job, **Then** the store rejects it with an integrity error.
2. **Given** two different jobs, **When** an item with the same URL fingerprint is stored for each, **Then** both are accepted.
3. **Given** a newly stored item, **When** no status is supplied, **Then** its status is `new` and its attempt count is 0.
4. **Given** an item, **When** its status is set to any of `new`, `extracted`, `skipped_keyword`, `skipped_irrelevant`, `relevant`, `summarized`, `failed`, **Then** the value is stored and read back unchanged; any other status value is rejected.
5. **Given** an item, **When** a relevance score between 0.00 and 1.00 with two decimal places is stored, **Then** the same value is read back exactly.
6. **Given** an item whose URL is several thousand characters long and whose raw content is several megabytes, **When** it is stored, **Then** both are preserved in full.

---

### User Story 3 - Delete a job together with all its history (Priority: P2)

When an operator removes a research job, all data that belongs to it — its runs, items, digests, notifications and LLM usage records — disappears with it, so no orphaned records remain.

**Why this priority**: Keeps the database consistent and prevents stale data from leaking into later reports; important but only exercised when jobs are removed.

**Independent Test**: Create a job with at least one run, item, digest, notification and LLM usage record; delete the job; verify no dependent records remain.

**Acceptance Scenarios**:

1. **Given** a job with runs, items, digests and notifications, **When** the job is deleted, **Then** all of its runs, items, digests, notifications and LLM usage records are deleted as well.
2. **Given** two jobs each with dependent records, **When** one job is deleted, **Then** the other job's records are untouched.

---

### User Story 4 - Preserve international text and emoji (Priority: P2)

Item titles and summaries frequently contain German umlauts, other non-ASCII characters and emoji. These must be stored and read back exactly as written, on every supported database engine.

**Why this priority**: Corrupted or rejected text breaks digests for German-language sources, a primary use case, but it only matters once items are being stored.

**Independent Test**: Store an item with title and summary such as "Größenänderung — Übersicht 🚀🇩🇪" and verify both read back byte-for-byte identical on SQLite and MariaDB.

**Acceptance Scenarios**:

1. **Given** a title containing umlauts (ä, ö, ü, ß) and a summary containing 4-byte emoji, **When** the item is stored and read back, **Then** both values are identical to the input.

---

### User Story 5 - Find jobs that are due and coordinate their execution (Priority: P3)

The scheduler needs to efficiently find enabled jobs whose next run time has passed, and needs to mark a job as locked until a given time so that two scheduler instances do not run the same job concurrently.

**Why this priority**: Required by the later scheduling track; this issue only needs to provide the stored fields and lookup support, not the scheduling logic.

**Independent Test**: Store enabled and disabled jobs with various next-run times and verify a query for enabled jobs due before a given instant returns exactly the expected ones; verify `next_run_at` and `locked_until` round-trip.

**Acceptance Scenarios**:

1. **Given** enabled and disabled jobs with past and future next-run times, **When** querying for enabled jobs due now, **Then** only enabled jobs with a past or current next-run time are returned.
2. **Given** a job, **When** its lock-until time is set and read back, **Then** the same instant is returned.

---

### User Story 6 - Run the persistence tests against both engines (Priority: P2)

Developers run the test suite locally with no database server: tests get a fresh in-memory database per test. In CI, an additional optional job runs the same tests against a real MariaDB container, selected through the test database URL setting.

**Why this priority**: Behaviour differs between engines (character sets, constraint enforcement, column types); without dual-engine testing, production-only defects slip through.

**Independent Test**: Run the persistence tests with no test database configured (in-memory) and again with the test database URL pointing at MariaDB; both runs pass.

**Acceptance Scenarios**:

1. **Given** no test database URL is configured, **When** the test suite runs, **Then** persistence tests use a fresh, isolated in-memory database per test and pass without network access.
2. **Given** the test database URL points at a MariaDB server, **When** the persistence tests run, **Then** the same tests run against MariaDB and pass.
3. **Given** a CI pipeline, **When** the optional MariaDB job runs, **Then** it starts a MariaDB container and runs the persistence tests against it; failure of this job is reported separately from the required checks.

---

### Edge Cases

- Upgrading a database that already contains unrelated tables: only invio tables are created; unrelated tables are untouched.
- Upgrading a database that is partially migrated (older version): only the missing migration steps are applied (relies on the migration tool's version tracking; testable once a second migration exists).
- A database configured with a non-UTF-8 default character set: invio tables still store full Unicode (including 4-byte emoji) because the character set is set explicitly per table.
- A URL fingerprint that is not exactly 64 hexadecimal characters: treated as invalid input by the code producing it; the store reserves exactly 64 characters.
- Deleting a run, item or digest individually does not delete the parent job.
- Deleting a single run keeps the items, digests, notifications and LLM usage records linked to it (their run link is cleared), so the job's duplicate-detection and usage history survive run cleanup; deleting a digest keeps its notifications (their digest link is cleared). Deleting the job removes all of them.
- Relevance scores outside 0.00–1.00 or with more than two decimals: callers are expected to validate; the stored precision is two decimals.
- SQLite does not enforce foreign keys by default: the test setup must enable enforcement so cascade and integrity tests are meaningful.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST define persistent storage for six record types: jobs, runs, items, digests, notifications and LLM usage, defined once in the `db` package and imported by all consumers.
- **FR-002**: System MUST support MariaDB/MySQL as the production engine and SQLite as the test engine, using the same schema definition for both.
- **FR-003**: On MariaDB/MySQL, all tables MUST use the full 4-byte Unicode character set (`utf8mb4`) and a transactional storage engine that enforces foreign keys (InnoDB); the production connection MUST request `utf8mb4`.
- **FR-004**: Items MUST store a fixed-length 64-character URL fingerprint (SHA-256 hex of the URL exactly as stored, UTF-8 encoded) and MUST enforce uniqueness of (job, URL fingerprint). URL normalisation (e.g. trailing slash, letter case of the host) happens before storage and is not part of this feature.
- **FR-005**: Items MUST store the full URL without length-based truncation, and raw content of up to 16 MB (measured in UTF-8 bytes; larger content must be truncated by the caller).
- **FR-006**: Items MUST store a processing status restricted to `new`, `extracted`, `skipped_keyword`, `skipped_irrelevant`, `relevant`, `summarized`, `failed` (default `new`), an attempt counter (default 0), the last error text (optional) and a relevance score with two decimal places in the range 0.00–1.00 (optional).
- **FR-006a**: Runs MUST store a status restricted to `running` (default), `succeeded`, `partial`, `failed`; notifications MUST store a status restricted to `pending` (default), `sent`, `failed`, `skipped`. Any other value MUST be rejected.
- **FR-007**: Jobs MUST store an enabled flag, a next-run time and a lock-until time, and MUST support efficient lookup of enabled jobs by next-run time (a combined index on enabled flag and next-run time).
- **FR-007a**: Jobs MUST be uniquely identified by `name`; a second job with the same name MUST be rejected. The stored configuration snapshot is never used as the source of job settings — the job definition file is authoritative.
- **FR-008**: Runs, items, digests, notifications and LLM usage records MUST reference their job such that deleting the job deletes them (cascading delete enforced by the database).
- **FR-008a**: Items, digests, notifications and LLM usage records MAY reference the run they belong to, and notifications MAY reference their digest; deleting the referenced run or digest MUST clear the reference rather than delete the referencing record.
- **FR-009**: Structured data fields (e.g. job configuration snapshots, run statistics, notification payloads) MUST be stored as JSON values that round-trip on both engines.
- **FR-010**: The schema MUST be managed through versioned migrations with an initial migration that creates all tables and whose downgrade removes them all.
- **FR-011**: The CLI MUST provide `invio db upgrade` that applies all pending migrations up to the latest version using the configured database URL; it MUST be auto-discovered like other commands, exit 0 on success, exit 2 when the database URL is missing or cannot be parsed, and exit 1 on other failures.
- **FR-012**: Database credentials embedded in the URL MUST NOT appear in logs, error messages or command output.
- **FR-013**: The test suite MUST provide a reusable fixture that yields a fresh, isolated in-memory SQLite database with the full schema and foreign key enforcement enabled.
- **FR-014**: When the test database URL setting (`INVIO_TEST_DATABASE_URL`) is set, the persistence tests MUST run against that database instead.
- **FR-015**: CI MUST include an optional job that runs the persistence tests against a MariaDB container.
- **FR-016**: All timestamps MUST be stored and returned as UTC instants.
- **FR-017**: README/docs MUST document the `invio db upgrade` command and database configuration.

### Key Entities *(include if feature involves data)*

- **Job**: The persisted counterpart of a job definition file, identified by its unique `name` (by convention the job file's name without extension; how names are assigned is decided by the later job-sync work). Attributes: unique name, enabled flag, JSON snapshot of the last-loaded job configuration (informational/audit only — the YAML file remains the authoritative definition), next run time, lock-until time, created/updated timestamps. Parent of runs, items, digests and notifications.
- **Run**: One execution of a job. Attributes: job reference, start and finish times, status (`running`/`succeeded`/`partial`/`failed`), statistics (JSON), error text. Belongs to a job.
- **Item**: A discovered piece of content for a job. Attributes: job reference, optional run reference (cleared when the run is deleted), URL, URL fingerprint, item type, title, published time, raw content, summary, processing status, attempts, last error, relevance, timestamps. Unique per (job, URL fingerprint).
- **Digest**: A compiled summary of relevant items produced for a job/run. Attributes: job reference, optional run reference (cleared when the run is deleted), title, body content, list of included items (JSON), created time.
- **Notification**: A delivery of a digest to a recipient channel. Attributes: job reference, optional run reference and optional digest reference (each cleared when the referenced record is deleted), channel, recipient, status (`pending`/`sent`/`failed`/`skipped`), sent time, error text, payload (JSON).
- **LLM Usage**: Accounting record for one or more LLM calls. Attributes: job reference (required, cascades on job deletion), optional run reference (cleared when the run is deleted), provider, model, purpose, input tokens, output tokens, estimated cost, created time.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can bring an empty production database to a fully working schema with one command in under 1 minute.
- **SC-002**: 100% of the issue's acceptance criteria (upgrade, downgrade, duplicate rejection, cascade, Unicode round-trip, dual-engine tests) are covered by automated tests that pass on both supported engines.
- **SC-003**: 0 duplicate items exist for the same job and URL after any sequence of inserts.
- **SC-004**: 0 orphaned runs, items, digests, notifications or LLM usage records remain after a job is deleted.
- **SC-005**: 100% of tested non-ASCII strings (umlauts, accented letters, 4-byte emoji) read back identically to what was written.
- **SC-006**: The default local test suite still runs without any database server or network access, and the persistence tests add no more than 10 seconds to it.
- **SC-007**: Upgrade followed by downgrade to base leaves 0 application tables behind.

## Assumptions

- The issue text refers to the package as `scout` (`scout/db/models.py`, `scout db upgrade`, `SCOUT_TEST_DATABASE_URL`); in this repository the package and CLI are named `invio`, so these map to `src/invio/db/`, `invio db upgrade` and the existing `INVIO_DATABASE_URL` / `INVIO_TEST_DATABASE_URL` settings.
- The migration environment ships inside the `invio.db` package (so `invio db upgrade` works from any working directory and in installed builds); a root-level configuration lets developers run the migration tool from the repository root. The existing `alembic/` placeholder directory is replaced (see plan R2).
- Work happens on branch `gh-issue-4` per the project constitution (the issue's suggested `issue/04-...` branch name is superseded).
- This issue delivers the schema, migrations, CLI upgrade command and test infrastructure; repository/query classes beyond what the tests need (e.g. due-job lookup) belong to later issues. "Repository tests" in the acceptance criteria means the persistence tests added here.
- Digests and notifications reference the job directly (for cascade) and optionally the run/digest they belong to (see FR-008a for deletion behaviour).
- Only `invio db upgrade` is required as a CLI command; downgrade is exercised through the migration tool directly (developer use), not exposed in the CLI.
- The MariaDB CI job is marked optional so an outage of the service container cannot block unrelated work; for this feature's PR (and any later PR touching `src/invio/db/`) its result must nevertheless be green before merging (checked by the reviewer). The required CI checks remain ruff, mypy and pytest on SQLite.
- Database URL credentials are already held as secrets in settings (from issue #2) and are reused here.
- Depends on issue #2 (settings with database URLs), which is merged.
