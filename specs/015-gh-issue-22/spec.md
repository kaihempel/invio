# Feature Specification: Manual Job Runs with Dry-Run Output and Run History

**Feature Branch**: `gh-issue-22`

**Created**: 2026-10-07

**Status**: Draft

**Input**: User description: "GitHub issue #22: [FEAT] Add `scout job run` command with dry-run output. Depends on #9 (job config/CLI) and #21 (LangGraph research workflow). Context: Users must be able to test a job manually and see what would be sent, without touching the schedule or sending mail. Requirements: 1) Add `scout job run <name> [--dry-run] [--max-items N] [--verbose]`. 2) Show progress (found, new, relevant) using rich live output. 3) In dry-run, print the final digest Markdown and stats table; exit code 0 on success, 1 on `failed`, 2 on `partial`. 4) Print token usage and estimated cost at the end. 5) Add `scout run list [--job NAME]` and `scout run show <id>` for run history (status, stats, errors). Acceptance criteria: `--dry-run` prints digest and stats, sends no mail; exit codes follow the specification; `scout run show` displays errors of failed items; CLI tests use `FakeProvider` and fixtures."

## Clarifications

### Session 2026-10-07

- Q: Where should `run show` get the per-item errors it displays? → A: Every run persists its own error list (stage, item title/link, message) together with the run record when it finishes — for real and dry runs alike — so history stays accurate after items are retried.
- Q: Which exit code should `job run` use when it cannot start because the job's stored configuration is invalid, given that 2 means `partial`? → A: 1, like every other "could not start" case; for `job run`, exit code 2 is reserved for `partial` (other commands keep the CLI-wide 2 for configuration errors).
- Q: How should the total cost be shown when only some LLM calls of a run have a known cost? → A: Show the known sum as a lower bound with the number of unpriced calls (e.g. "≥ $0.0123 (2 calls without price)"); show "unknown" only when no call has a cost.
- Q: May `--max-items N` raise the cap above the job's configured per-run item limit? → A: No, lower only: the effective cap is min(N, job limit); if N exceeds the limit, a note says the job limit applies.

## User Scenarios & Testing *(mandatory)*

The "user" of this feature is the operator who configures research jobs from the command line
and wants to try a job by hand before trusting it to the scheduler, and who later wants to look
back at what earlier runs did. Scripts and cron wrappers are secondary users: they only read the
exit code.

### User Story 1 - Preview a job's digest without sending anything (Priority: P1)

The operator has just created or edited a job and wants to see what it would produce. They run
the job by name in dry-run mode. While it runs they see live progress counts (candidates found,
new candidates, relevant items). When it finishes they see the complete digest exactly as it
would have been sent, followed by a statistics table and the token usage and estimated cost of
the run. No notification is sent and the job's schedule is left untouched.

**Why this priority**: This is the core reason for the issue: a safe way to test a job end to end
and judge the result before it reaches anyone's inbox.

**Independent Test**: Run a fixture job in dry-run mode against a fake LLM provider and local
source fixtures; verify the digest and stats appear in the output, no notification was
delivered, and the job's next scheduled run time is unchanged.

**Acceptance Scenarios**:

1. **Given** an enabled job whose sources yield relevant items, **When** the operator runs it with
   dry-run, **Then** the output contains the full digest text, a statistics table (at least found,
   new, relevant, failed counts and duration), and a token usage and estimated cost summary.
2. **Given** the same job, **When** it is run with dry-run, **Then** no notification is delivered
   to any recipient and the job's next scheduled run time is the same before and after.
3. **Given** a dry run that finds no relevant items, **When** it finishes, **Then** the operator
   sees a clear "no digest — nothing relevant" message instead of an empty digest, together with
   the stats table.
4. **Given** a dry run is in progress on an interactive terminal, **When** stages complete,
   **Then** the progress display updates the found, new and relevant counts as they become known.

---

### User Story 2 - Scriptable outcome through exit codes (Priority: P1)

A script or the operator triggers a job run and needs to know, from the exit code alone, whether
the run succeeded fully, partially, or failed.

**Why this priority**: The tool is designed for unattended use; an exit code that reflects the
run outcome is required for any automation built on top of manual runs.

**Independent Test**: Run fixture jobs engineered to succeed, partially fail (one bad item) and
fail completely (every source fails); assert exit codes 0, 2 and 1 respectively.

**Acceptance Scenarios**:

1. **Given** a run that ends with status `succeeded`, **When** the command exits, **Then** the exit
   code is 0.
2. **Given** a run that ends with status `failed`, **When** the command exits, **Then** the exit
   code is 1.
3. **Given** a run that ends with status `partial`, **When** the command exits, **Then** the exit
   code is 2.
4. **Given** a job name that does not exist, is disabled, has an invalid stored configuration,
   or is currently being run by another process, **When** the operator tries to run it, **Then** a one-line error is shown on the error
   stream, no run is recorded, and the exit code is 1.

---

### User Story 3 - Real manual run (Priority: P2)

The operator wants to trigger a real run of a job now, outside its schedule — for example to get
today's digest early. Without the dry-run flag the run behaves exactly like a scheduled run:
results are saved and the notification is delivered. The output shows progress, a summary of the
outcome (status, stats, number of notifications sent/failed) and token usage and cost; the digest
text itself is shown only in dry-run or verbose mode.

**Why this priority**: Useful, but the issue's focus is safe testing; a real run reuses the same
command and pipeline.

**Independent Test**: Run a fixture job without dry-run against a fake notifier; verify one
notification was recorded as sent and the run summary is printed.

**Acceptance Scenarios**:

1. **Given** an enabled job, **When** the operator runs it without dry-run, **Then** the
   notification is delivered, the run is recorded with its final status, and the summary reports
   how many notifications were sent and how many failed.

---

### User Story 4 - Limit the size of a test run (Priority: P2)

To keep a test run quick and cheap, the operator passes a maximum number of items. The run then
processes at most that many items, regardless of the job's own configured per-run limit.

**Why this priority**: Makes trying jobs affordable, especially with paid LLM providers, but the
command is useful without it.

**Independent Test**: Run a fixture job whose sources yield 10 new items with a maximum of 3;
verify that at most 3 items were rated/summarized.

**Acceptance Scenarios**:

1. **Given** sources that yield more new items than N, **When** the job is run with a maximum of
   N items, **Then** at most N items are processed in this run.
2. **Given** a maximum of 0 or a negative number, **When** the command is invoked, **Then** it is
   rejected with a usage error before any run starts.
3. **Given** a maximum larger than the job's configured per-run limit, **When** the job is run,
   **Then** the job's limit applies, a note on the error stream says so, the run proceeds, and the
   stored job configuration is not changed.

---

### User Story 5 - Inspect run history (Priority: P2)

The operator wants to see past runs: a list of recent runs (optionally only for one job) with
their status and headline counts, and a detail view of one run showing its status, timing,
statistics, token usage and cost, and every recorded error — in particular which items failed and
why.

**Why this priority**: Needed to diagnose partial and failed runs (especially scheduled ones the
operator did not watch), but secondary to running jobs.

**Independent Test**: Seed the database with runs of different statuses (including one with
failed items) and verify the list and detail output.

**Acceptance Scenarios**:

1. **Given** several recorded runs across two jobs, **When** the operator lists runs, **Then** they
   see one row per run, newest first, with run id, job name, start time, duration, status, whether
   it was a dry run, and found/new/relevant counts.
2. **Given** the same runs, **When** the operator lists runs filtered by one job name, **Then** only
   that job's runs are shown.
3. **Given** a run in which some items failed, **When** the operator shows that run, **Then** each
   failed item is listed with its title or link, the stage where it failed and the error message.
4. **Given** a run id that does not exist, **When** the operator shows it, **Then** a one-line
   error is shown and the exit code is 1.
5. **Given** no runs recorded (or none for the filtered job), **When** the operator lists runs,
   **Then** a "no runs" message is shown and the exit code is 0.

---

### User Story 6 - Verbose diagnostics (Priority: P3)

When a run behaves unexpectedly, the operator adds a verbose flag to see more detail: per-stage
progress, per-item outcomes (relevant, irrelevant, failed with reason) and the digest even for a
real run.

**Why this priority**: A debugging aid; the command is complete without it.

**Independent Test**: Run a fixture job with verbose and verify per-item outcome lines appear on
the diagnostics stream.

**Acceptance Scenarios**:

1. **Given** a run with relevant, irrelevant and failed items, **When** run with verbose, **Then**
   each item's outcome is reported individually.

---

### Edge Cases

- Output is not an interactive terminal (piped, cron, CI): live progress animation is replaced by
  plain, line-based progress on the diagnostics stream so the results stream stays clean and
  parseable.
- The operator interrupts a running job (Ctrl-C): the run is closed as `failed` by the existing
  safety net, the job lock is released, a short "interrupted" message is shown and the exit code is
  non-zero (1).
- The LLM provider has no price information for the model used: token counts are still shown and
  the cost is displayed as "unknown" (no priced call) or as a marked lower bound (some priced
  calls), never as a silent 0 or an unmarked partial sum.
- A run used no LLM calls at all (nothing new): token usage is shown as 0 and cost as 0.
- The job's stored configuration became invalid: a configuration error is shown and the command
  exits with 1 without starting a run (2 is reserved for `partial` in `job run`).
- The database schema is out of date or unreachable: the same database error message as other
  commands is shown, exit code 1.
- `run show` on a run recorded before the error list existed: run-level error only, plus a note
  that item errors are unavailable for this run.
- `run show` on a run that is still in progress: status "running" is shown with the stats known so
  far (none), no error.
- Very long error messages or digests: the digest is printed in full. A stored error message is
  kept up to 500 characters and an item title up to 300; the detail view prints them in full as
  stored. The list view truncates to fit one row per run.

## Requirements *(mandatory)*

### Functional Requirements

**Running a job**

- **FR-001**: The CLI MUST provide a command to run one job by name immediately, with options for
  dry-run, a maximum number of items, and verbose output.
- **FR-002**: The run MUST use the same research workflow as scheduled runs; the manual command
  MUST NOT implement its own pipeline logic.
- **FR-003**: In dry-run mode the system MUST NOT deliver any notification and MUST NOT change the
  job's next scheduled run time; only a run record marked as a dry run remains.
- **FR-004**: In dry-run mode the system MUST print the complete final digest (in its Markdown
  form) to standard output, followed by a statistics table.
- **FR-005**: The statistics table MUST show at least: candidates found, new candidates, items
  after keyword filter, items processed, relevant items, failed items, and run duration, plus the
  run id and final status.
- **FR-006**: When a run produces no digest (nothing relevant), the system MUST say so explicitly
  instead of printing an empty digest.
- **FR-007**: While the run is in progress the system MUST show live progress with at least the
  found, new and relevant counts, updating as each becomes known.
- **FR-008**: Live progress MUST be written to the diagnostics (error) stream, and MUST degrade to
  plain line-based messages when that stream is not an interactive terminal.
- **FR-009**: At the end of every run (dry or real) the system MUST print the run's total input
  tokens, output tokens and estimated cost. When every call has a cost, the exact sum is shown;
  when only some calls have a cost, the known sum MUST be shown as a lower bound together with the
  number of unpriced calls (e.g. "≥ $0.0123 (2 calls without price)"); when no call has a cost,
  the cost MUST be shown as "unknown". A run with no LLM calls shows $0. The same rule applies to
  `run show`.
- **FR-010**: The maximum-items option MUST cap the number of items processed in this run only; the
  effective cap MUST be the smaller of N and the job's configured per-run item limit (it can only
  lower the cap). When N exceeds the job limit, a note MUST say the job limit applies. The option
  MUST accept positive integers only and MUST NOT modify the stored job configuration.
- **FR-011**: For a real (non-dry) run the summary MUST report the number of notifications sent
  and failed.
- **FR-012**: With verbose output the system MUST additionally report per-item outcomes and print
  the digest for real runs as well.

**Exit codes**

- **FR-013**: The run command MUST exit with 0 when the run status is `succeeded`, 1 when `failed`,
  and 2 when `partial`.
- **FR-014**: When the run cannot start — unknown job, disabled job, job busy (locked by another
  run), invalid stored job configuration, missing settings, database error — or is interrupted,
  the command MUST print a one-line error on the error stream and exit with 1. For `job run`, exit
  code 2 MUST mean only `partial`; this exception to the CLI-wide configuration-error code (2) MUST
  be stated in the command's help text. Invalid command-line arguments MUST be rejected as usage
  errors before any run starts.

**Run history**

- **FR-015**: The CLI MUST provide a command to list recorded runs, newest first, optionally
  filtered by job name, showing run id, job name, start time, duration, status, dry-run marker,
  and found/new/relevant counts.
- **FR-016**: The run list MUST be bounded to a sensible default number of most recent runs
  (default 20), with an option to change that number.
- **FR-017**: The CLI MUST provide a command to show one run by id, displaying job name, status,
  dry-run marker, start and end time, duration, full statistics, token usage and estimated cost,
  the run-level error (if any), and every recorded item-level error with item title or link, stage
  and message.
- **FR-018**: Filtering the run list by a job name that does not exist MUST produce an "unknown
  job" error with exit code 1; showing a run id that does not exist MUST produce a "run not found"
  error with exit code 1.
- **FR-018a**: When a run finishes (real or dry), the system MUST persist that run's complete
  error list — for each error: stage, item title and link (when item-related) and message —
  together with the run record. `run show` MUST read errors from this stored list, never from the
  items' current state, so a later retry of an item does not change an earlier run's history.
  Runs recorded before this feature (without a stored list) show only their run-level error.
- **FR-019**: History commands MUST be read-only and MUST exit 0 on success, including when there
  are no runs to show.

**Testing**

- **FR-020**: Automated CLI tests MUST cover every acceptance scenario above using the fake LLM
  provider and local source fixtures, with no network access and no real mail delivery.

### Key Entities *(include if feature involves data)*

- **Run**: one execution of a job — id, job, start/end time, status (running, succeeded, partial,
  failed), dry-run marker, statistics (found, new, after keyword filter, processed, relevant,
  failed), run-level error, and its stored error list. Already recorded by the research workflow
  (#21), except the error list, which this feature adds.
- **Run error**: an error recorded during a run — stage, optional item reference (title and
  link, captured at the time of the run), message. Stored with its run and immutable afterwards;
  item errors identify which item failed.
- **Item**: a candidate article/video handled in a run — title, link, status, last error.
- **LLM usage record**: token counts and estimated cost of one LLM call, attributable to a run;
  summed for the run's usage and cost display.
- **Digest**: the synthesized Markdown summary of relevant items; in a dry run it exists only in
  the run result and is printed, not stored.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An operator can preview what a job would send with a single command, and in 100% of
  dry runs no notification reaches any recipient and the job's next run time is unchanged.
- **SC-002**: For every final run status, the exit code matches the specification (0/1/2) in 100%
  of tested cases, so scripts can branch on the outcome without parsing output.
- **SC-003**: For a run with failed items, an operator can identify every failed item and its
  failure reason from one history command, without consulting logs or the database.
- **SC-004**: Every run's output ends with token usage and an estimated cost (or an explicit
  "unknown"), letting operators judge the cost of a job before enabling its schedule.
- **SC-005**: Using the maximum-items option, an operator can bound a test run to N processed
  items, and no test run processes more than N items.
- **SC-006**: Results on standard output contain only the digest and tables, so redirecting
  standard output to a file yields a clean digest/stats document with no progress noise.

## Assumptions

- The issue text uses the working name `scout`; the project's CLI is `invio`, so the commands are
  `invio job run <name> [--dry-run] [--max-items N] [--verbose]`, `invio run list [--job NAME]`
  and `invio run show <id>`. The `run` command group is a new auto-discovered command module; `job
  run` extends the existing `job` command group (#9).
- The research workflow from #21 (`run_job`) already provides dry-run semantics (no delivery, no
  `next_run_at` change, items released back to pending, only the run record kept, digest returned
  in the result), job locking, final status rules, stats and the sorted error list; this feature
  only adds the CLI on top and a way to pass a per-run item cap and receive progress updates.
- Progress counts are reported at stage granularity (after fetch: found; after deduplication: new;
  after relevance scoring: relevant), not per item, unless verbose is set.
- `--max-items N` can only lower the job's per-run item limit for this run (see Clarifications);
  the per-source limit stays as configured.
- Exit code 2 means only `partial` for `job run`; configuration errors there exit with 1 (see
  Clarifications). All other commands, including the history commands, keep the CLI-wide
  convention of 2 for configuration errors.
- Estimated cost comes from the existing per-call LLM usage records of the run; costs are shown
  in USD with a precision appropriate for small amounts.
- Real manual runs do not change the job's schedule beyond what a normal run does (the workflow
  computes `next_run_at` as for scheduled runs); this matches #21 behaviour.
- Times in history output are shown in the job's configured timezone, falling back to UTC.
- Results go to standard output; progress, logs and error messages go to the error stream
  (constitution principle II).
