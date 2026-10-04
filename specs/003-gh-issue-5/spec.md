# Feature Specification: Job Management Service and Record Access Layer

**Feature Branch**: `gh-issue-5`

**Created**: 2026-10-04

**Status**: Draft

**Input**: User description: "GitHub issue #5: [FEAT] Add repositories and JobService for job CRUD. Depends on #3 (job configuration schema) and #4 (persistent data store). CLI, scheduler and pipeline must not touch the database directly. A service layer centralizes job management and enables a later API or UI without duplicating logic. Requirements: a record access layer for jobs, runs, items, digests, notifications and LLM usage; a job service offering create, get by name, list, update, enable/disable, delete, export to YAML and import from YAML; every write validates through the job configuration model and stores its normalized form; the next run time is computed and stored on create, update and re-enable (uses the scheduling feature from #6, stubbed with 'now' until available); short-lived units of work. Acceptance criteria: creating a job with a duplicate name fails with a dedicated 'job exists' error; updating a job re-validates the config and recalculates the next run time; disabling a job keeps its history while deleting removes it; import followed by export returns an equal config; the record access layer is covered by tests on the in-memory test database."

## Clarifications

### Session 2026-10-04

- Q: When a job is updated, should the caller provide the complete new configuration or only changed fields, and can an update rename the job? → A: Full replacement of the configuration; the name cannot be changed.
- Q: When a job is disabled, should its stored next run time be cleared or kept? → A: Cleared on disable; re-enable calculates a new one.
- Q: When a job's stored configuration no longer validates, what should listing jobs do? → A: Fetching that job by name raises a configuration error naming it; listing skips it and logs a structured warning.
- Q: Should the job service write a structured log line for every job change? → A: Yes — one structured info line per successful change (event + job name only, no configuration contents).

## User Scenarios & Testing *(mandatory)*

The "users" of this feature are the other parts of invio — the CLI, the scheduler and the
research pipeline — and, through them, the operator who manages research jobs. The feature
gives them one place to manage jobs and their history, so none of them has to deal with
storage details and a later API or UI can reuse the same behaviour.

### User Story 1 - Register and look up research jobs (Priority: P1)

An operator (through the CLI, later an API or UI) registers a new research job under a unique
name together with its configuration. The job service checks the configuration against the job
schema, stores it in its normalized form, marks the job as enabled and records when it should
run next. The operator can then fetch the job by name or list all jobs, optionally only the
enabled ones.

**Why this priority**: Every other capability (scheduling, running, reporting) needs jobs to
exist in the data store. Create, look-up and list form the smallest useful slice.

**Independent Test**: Create a job with a valid configuration, fetch it by name and list all
jobs; the job appears with its configuration, enabled state and next run time. Creating a
second job with the same name is rejected with a "job exists" error.

**Acceptance Scenarios**:

1. **Given** no job named `ai-news` exists, **When** a job `ai-news` is created with a valid
   configuration, **Then** it is stored as enabled, its stored configuration equals the
   normalized configuration, and it has a next run time.
2. **Given** a job `ai-news` exists, **When** another job named `ai-news` is created,
   **Then** creation fails with a dedicated "job exists" error and the existing job is unchanged.
3. **Given** a configuration that violates the job schema (e.g. an unknown key or an invalid
   timezone), **When** a job is created with it, **Then** creation fails with the same
   field-level validation messages as loading a job file, and nothing is stored.
4. **Given** several jobs, some disabled, **When** jobs are listed, **Then** all jobs are
   returned in name order; **When** only enabled jobs are requested, **Then** disabled jobs are
   left out.
5. **Given** no job named `missing` exists, **When** it is fetched by name, **Then** a dedicated
   "job not found" error is raised.

---

### User Story 2 - Change, pause and remove jobs (Priority: P1)

An operator edits an existing job's configuration, temporarily pauses (disables) a job and
later resumes (re-enables) it, or removes a job for good. Edits are validated just like new
jobs, and the next run time follows the new schedule. Pausing keeps the job's full history
(runs, items, digests, notifications, LLM usage); removing a job removes the job and all of its
history.

**Why this priority**: Without update, pause and delete the operator cannot correct mistakes or
stop unwanted runs; the acceptance criteria of the issue centre on these operations.

**Independent Test**: Create a job with history records attached, update its schedule, disable
it, re-enable it and finally delete it, checking the stored state after each step.

**Acceptance Scenarios**:

1. **Given** an existing job, **When** its configuration is updated with a complete, valid new
   configuration, **Then** the new configuration fully replaces the old one, the name is
   unchanged, and the next run time is recalculated.
2. **Given** an existing job, **When** it is updated with an invalid configuration, **Then** the
   update fails with field-level validation messages and the stored job is unchanged.
3. **Given** an enabled job with runs, items, digests, notifications and LLM usage, **When** it
   is disabled, **Then** it is marked disabled, has no next run time, and all history records
   still exist.
4. **Given** a disabled job, **When** it is re-enabled, **Then** it is marked enabled and a fresh
   next run time is calculated.
5. **Given** a job with history, **When** it is deleted, **Then** the job and all of its history
   records are gone, and other jobs' records are untouched.
6. **Given** no job with the given name, **When** update, enable/disable or delete is requested,
   **Then** a "job not found" error is raised.

---

### User Story 3 - Move jobs between YAML files and the data store (Priority: P2)

An operator imports a job from a YAML job file into the data store, and exports a stored job
back to YAML (to edit it, back it up or move it to another installation). Import followed by
export yields an equal configuration.

**Why this priority**: Job files are the operator's main authoring format (#3); import/export
connects them with the data store. It builds on Story 1 and 2 and is not needed for the basic
CRUD flow.

**Independent Test**: Import the example job file, export it, and compare the exported
configuration with the imported one.

**Acceptance Scenarios**:

1. **Given** a valid job file, **When** it is imported under a name, **Then** a job with that
   name exists and its configuration equals the file's validated configuration.
2. **Given** an imported job, **When** it is exported, **Then** the exported YAML, loaded again,
   equals the imported configuration.
3. **Given** an invalid job file, **When** it is imported, **Then** import fails with the same
   error messages as loading the file, and nothing is stored.
4. **Given** a job with the target name already exists, **When** a file is imported without
   asking to replace, **Then** import fails with the "job exists" error; **When** replacement is
   explicitly requested, **Then** the existing job is updated (as in Story 2) and its history is
   kept.

---

### User Story 4 - Record and query run history through one access layer (Priority: P2)

The scheduler and pipeline record runs, discovered items, digests, notifications and LLM usage,
and query them (e.g. the runs of a job, items of a job by processing status, whether an item URL
was already seen, usage totals of a run). They do this through dedicated record access
components, one per kind of record, instead of working with storage directly.

**Why this priority**: Later tracks (scheduler, pipeline, notification) need these, but this
issue only has to provide the basic operations and tests; the job service is the main consumer
now.

**Independent Test**: Against the in-memory test database, add and read back each record kind
through its access component and check filters, ordering and the uniqueness of items per job
and URL.

**Acceptance Scenarios**:

1. **Given** a job, **When** a run is started and later finished with a status and statistics,
   **Then** the run can be read back with those values, and the job's runs are listed newest
   first.
2. **Given** a job, **When** an item with an already recorded URL for that job is added again,
   **Then** no duplicate is created and the caller can tell the item already existed.
3. **Given** items in different processing states, **When** items of a job are requested by
   status, **Then** only items in that status are returned.
4. **Given** digests, notifications and LLM usage entries for a run, **When** they are queried by
   job or run, **Then** the matching records are returned, and LLM usage can be summed per run
   and per job.

---

### Edge Cases

- Job name empty, only whitespace, or longer than the stored maximum (200 characters): rejected
  with a validation error before anything is stored.
- Two callers create the same job name at the same time: exactly one succeeds; the other gets
  the "job exists" error (the data store's uniqueness rule is the final arbiter).
- A stored configuration no longer validates (e.g. written by an older version): fetching that
  job by name raises a configuration error naming the job; listing skips it, logs a structured
  warning naming the job, and still returns all valid jobs.
- Disabling an already disabled job, or enabling an already enabled job: succeeds without
  changing anything (enabling an enabled job does not move its next run time).
- Updating a disabled job: the configuration is stored, but the job stays disabled without a
  next run time.
- Deleting a job while one of its runs is in progress: the job and its history are removed; the
  service does not wait for or protect running work (locking is out of scope).
- Export target path not writable: export fails with a file error; the stored job is unchanged.
- Any failure during a write operation leaves the data store exactly as it was before (no
  partial writes).

## Requirements *(mandatory)*

### Functional Requirements

**Job service**

- **FR-001**: The system MUST provide a job service that is the only component the CLI,
  scheduler and pipeline use to create, read, list, update, enable/disable, delete, import and
  export jobs.
- **FR-002**: Creating a job MUST require a unique name and a configuration; a name that already
  exists MUST fail with a dedicated "job exists" error that names the job.
- **FR-003**: Every write that stores a configuration (create, update, import) MUST validate it
  against the job configuration schema from #3 and store the validated, normalized form (all
  fields, defaults included, in a plain-data representation).
- **FR-003a**: Updating a job MUST replace its whole configuration with the newly supplied
  configuration (no partial merge); the job's name MUST NOT change through an update.
- **FR-004**: Validation failures MUST raise the same configuration error type and field-level
  messages as loading a job file, and MUST NOT store anything.
- **FR-005**: The service MUST return jobs as plain, storage-independent job records (name,
  enabled flag, validated configuration, next run time, created/updated times) that remain
  usable after the operation has finished.
- **FR-006**: Looking up, updating, enabling/disabling or deleting a job that does not exist
  MUST raise a dedicated "job not found" error that names the job.
- **FR-007**: Listing jobs MUST return them ordered by name and MUST support restricting the
  result to enabled jobs.
- **FR-007a**: If a stored job configuration fails validation when read, fetching that job by
  name MUST raise a configuration error naming the job; listing MUST skip that job, log a
  structured warning naming it, and return the remaining valid jobs.
- **FR-008**: The next run time MUST be calculated from the job's schedule and stored on create,
  on update of an enabled job, and on re-enable; until the schedule calculation from #6 exists,
  the calculation MUST be a single replaceable step that returns the current time.
- **FR-009**: Disabling a job MUST mark it disabled and clear its next run time (so a disabled
  job can never appear due), and MUST keep all of its history records; re-enabling MUST
  calculate a new next run time (FR-008).
- **FR-010**: Deleting a job MUST remove the job and all of its history (runs, items, digests,
  notifications, LLM usage).
- **FR-011**: Exporting a job MUST produce YAML in the same format as saving a job file (#3),
  either as text or written to a file path.
- **FR-012**: Importing MUST read a job file with the same rules as loading a job file (#3) and
  store it under a given name, defaulting to the file name without extension; if the name exists,
  import MUST fail with the "job exists" error unless replacement is explicitly requested, in
  which case it behaves like an update.
- **FR-013**: Importing a job and then exporting it MUST yield a configuration equal to the
  imported one.
- **FR-014**: Job names MUST be non-empty, without leading/trailing whitespace, and at most 200
  characters; invalid names MUST be rejected before anything is stored.

- **FR-014a**: Every successful job change (create, update, enable, disable, delete, import)
  MUST emit exactly one structured info log line containing the event and the job name, and
  MUST NOT include configuration contents (e.g. recipients, source URLs); failed operations
  MUST NOT emit a change line.

**Record access layer**

- **FR-015**: The system MUST provide one record access component per record kind — jobs, runs,
  items, digests, notifications and LLM usage — offering the add, look-up, filter and update
  operations listed in User Story 4.
- **FR-016**: Record access components MUST NOT commit or manage the unit of work themselves; the
  caller decides where a unit of work begins and ends.
- **FR-017**: Adding an item whose URL is already recorded for the same job MUST NOT create a
  duplicate and MUST tell the caller the item already existed.

**Units of work**

- **FR-018**: The system MUST provide a short-lived unit-of-work helper that commits when the
  block succeeds, rolls back on any error, and always releases its resources.
- **FR-019**: Each job service operation MUST run in its own unit of work, so that a failure
  leaves the data store unchanged.

**Boundaries and quality**

- **FR-020**: Callers of the job service MUST NOT need to import or handle storage-specific types
  or errors; storage errors that represent a known condition (duplicate name) MUST be translated
  into the service's own errors.
- **FR-021**: The record access layer and job service MUST be covered by automated tests that run
  against the in-memory test database, including every acceptance criterion and the rejection
  paths.

### Key Entities

- **Job record**: A named research job as seen by callers — unique name, enabled flag, validated
  configuration, next run time, creation and last-change times.
- **Job configuration**: The validated job definition from #3 (schedule, notification, sources,
  search, LLM, limits); stored in normalized plain-data form.
- **Run, Item, Digest, Notification, LLM usage**: The history records of a job defined in #4;
  all belong to a job and are kept on disable, removed on delete.
- **Unit of work**: A short-lived, all-or-nothing scope in which records are read and written.
- **Job exists / Job not found errors**: Dedicated, storage-independent errors that name the job.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: 100% of the issue's acceptance criteria (duplicate name rejection, re-validation and
  next-run recalculation on update, history kept on disable / removed on delete, import→export
  equality, record access tests) are demonstrated by automated tests that pass.
- **SC-002**: Import followed by export yields an equal configuration in 100% of cases for the
  example job file and for a set of valid job files that together cover every source type
  (rss, web, sitemap, YouTube channel, YouTube playlist), every schedule frequency (daily,
  weekly, monthly) and non-ASCII text.
- **SC-003**: After any failed job operation (validation error, duplicate name, missing job,
  storage failure), the data store contents are identical to before the operation in 100% of
  tested cases.
- **SC-004**: No module of the CLI, scheduler or pipeline needs to reference storage internals to
  manage jobs; job management is reachable through one service interface.
- **SC-005**: The full test suite for this feature runs without network access or an external
  database server and completes in under 10 seconds on a developer machine.

## Assumptions

- The issue's `scout/...` paths refer to this repository's `src/invio/...` package; the record
  access layer belongs to the database adapter package and the job service to a new services
  package, consistent with the constitution's dependency direction.
- Job names are not part of the job configuration; they are given when creating or importing a
  job (import defaults to the file name without extension).
- Following the clarification of #4, the stored configuration is a snapshot of the validated
  configuration; when a job comes from a YAML file, the file stays the operator's authoring
  source and is brought into the data store by import.
- The next run time is a placeholder ("now") until #6 provides schedule calculation; tests check
  that it is set/recalculated, not its exact schedule-based value.
- Job locking (`locked_until`) and run claiming are out of scope; they belong to the scheduler.
- No new CLI commands are added in this issue; wiring the service into the CLI is a separate
  issue. Therefore no README/CLI documentation change is required beyond describing the service
  for developers if useful.
- The record access operations beyond those needed by the job service are kept to the basic set
  listed in User Story 4 (simplicity first); later tracks may extend them.
