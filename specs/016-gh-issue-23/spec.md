# Feature Specification: Scheduled Execution of Due Jobs with Locking and Missed-Run Handling

**Feature Branch**: `gh-issue-23`

**Created**: 2026-10-07

**Status**: Draft

**Input**: User description: "GitHub issue #23: [FEAT] Add `scout run-due` with locking and missed-run handling (the CLI in this repository is `invio`, so the command is `invio run-due`). Depends on #6 and #21. Context: A systemd timer invokes `run-due` periodically. Due jobs are determined from the database, and overlapping invocations must never run the same job twice. Requirements: 1) Add `run-due [--limit N]` selecting enabled jobs whose next run time has passed, ordered by next run time. 2) Claim a job atomically (lock until now + 2 hours, only if unlocked or the lock has expired); only proceed if the claim succeeded. 3) Run jobs sequentially (provider rate limits) or with `--parallel N`. 4) Always release the lock and set the next run time when a run finishes, also on failure; use an exponential retry delay (e.g. +1h) after a failed run instead of waiting for the next regular slot. 5) Missed runs (server was down) execute once. 6) Ping `healthcheck_url` (if configured) after a completed invocation; ping `/fail` on exceptions. Acceptance criteria: two concurrent `run-due` processes never execute the same job (tested with threads on MariaDB); a stale lock older than its timeout is reclaimed; an overdue job runs exactly once, not once per missed slot; failed runs are retried later, not immediately in a loop; healthcheck URL is called on success and failure paths."

## Clarifications

### Session 2026-10-07

- Q: When one or more jobs fail in an invocation that otherwise finished normally, should the health-check service get the failure or the success signal? → A: The failure signal: `/fail` is sent whenever the invocation exits non-zero (at least one run failed, or the invocation aborted); the success signal otherwise.
- Q: Should a failed manual (non-dry-run) `invio job run` also set the job's next run time to the retry time, like a failed run started by `run-due`? → A: Yes: every real run that fails, manual or scheduled, sets the retry time, and all real runs count toward the consecutive-failure streak; dry runs never change the schedule or the streak.

## User Scenarios & Testing *(mandatory)*

The primary "user" of this feature is the scheduler: a timer on the server that starts
`invio run-due` every few minutes without any human present. The secondary user is the operator
who installs that timer, watches an external health-check service, and later reads the run
history to see what happened unattended.

### User Story 1 - Run every job that is due, unattended (Priority: P1)

The timer starts `invio run-due`. The command finds every enabled job whose next scheduled run
time has been reached, oldest due first, runs each one, and afterwards moves each job's next run
time to its next regular slot. Jobs that are not yet due, and disabled jobs, are left alone.
When nothing is due the command finishes quickly and successfully without doing anything.

**Why this priority**: This is the core of autonomous operation: without it, jobs only run when
someone triggers them by hand.

**Independent Test**: Seed several jobs (one due long ago, one due recently, one due in the
future, one disabled but due), invoke the command with a fake LLM provider and local source
fixtures, and verify that exactly the two due enabled jobs ran, in order of their due time, and
that both now have a next run time in the future.

**Acceptance Scenarios**:

1. **Given** two enabled jobs whose next run times are in the past and one whose next run time is
   in the future, **When** `run-due` is invoked, **Then** exactly the two due jobs run, the one
   that has been due longer runs first, and the future job is not touched.
2. **Given** a job that is due but disabled, **When** `run-due` is invoked, **Then** the job does
   not run and its next run time is unchanged.
3. **Given** a job that ran successfully, **When** its run finishes, **Then** its lock is released
   and its next run time is set to the next regular slot of its schedule after the finish time.
4. **Given** no job is due, **When** `run-due` is invoked, **Then** it exits successfully, starts
   no run, and reports that nothing was due.
5. **Given** five due jobs, **When** `run-due --limit 2` is invoked, **Then** only the two jobs
   that have been due longest run; the other three stay due for the next invocation.

---

### User Story 2 - Overlapping invocations never run the same job twice (Priority: P1)

A long run is still in progress when the timer fires again, or the operator starts `run-due` by
hand while the timer is active. Every job may be picked up by only one invocation at a time: an
invocation first claims the job, and if another invocation (or a manual `invio job run`) already
holds the claim, it skips that job and moves on.

**Why this priority**: Running a job twice sends duplicate digests to recipients and doubles
provider cost. Overlapping invocations are normal under a timer, so this guarantee is mandatory.

**Independent Test**: Start two `run-due` invocations concurrently in threads against the real
database type used in production (MariaDB), with the same set of due jobs; verify that every
job has exactly one run record from this round and that each invocation reports the jobs it
skipped as already claimed.

**Acceptance Scenarios**:

1. **Given** several due jobs and two invocations started at the same moment, **When** both
   finish, **Then** every due job was run exactly once in total, and no job was run by both.
2. **Given** a job that is currently being run (its claim is held and not expired), **When**
   `run-due` is invoked, **Then** that job is skipped, is reported as busy, and the remaining due
   jobs still run.
3. **Given** a job claimed by an invocation, **When** another invocation tries to claim it, **Then**
   the second claim fails without changing the existing claim.

---

### User Story 3 - A crashed run does not block a job forever (Priority: P1)

The server or the process died in the middle of a run, so a job's claim was never released. Once
the claim is older than its timeout, the next invocation takes it over and runs the job, instead
of the job being stuck forever.

**Why this priority**: Without reclaiming, a single crash silently stops a job from ever running
again, which in unattended operation can go unnoticed for weeks.

**Independent Test**: Seed a due job with a claim that expired in the past, invoke `run-due`, and
verify the job runs and ends with a released claim; seed another due job with a claim that is
still valid and verify it is skipped.

**Acceptance Scenarios**:

1. **Given** a due job whose claim expired before now, **When** `run-due` is invoked, **Then** the
   job is claimed anew and runs.
2. **Given** a due job whose claim is still valid, **When** `run-due` is invoked, **Then** the job
   is skipped and its claim is unchanged.
3. **Given** a run that outlives its own claim, **When** another invocation reclaims the job,
   **Then** the original run stops at its next checkpoint and does not overwrite the new claim or
   the job's next run time.

---

### User Story 4 - Missed slots after downtime run once (Priority: P2)

The server was off for three days while a daily job's slots passed. When the timer starts again,
the job runs once — not three times — and then continues with its next regular slot in the
future.

**Why this priority**: Catching up every missed slot would flood recipients with near-identical
digests and burn provider budget; skipping the job entirely would lose a run the operator expects.

**Independent Test**: Seed a daily job whose next run time is three days in the past, invoke
`run-due` twice in a row, and verify there is exactly one new run and the job's next run time is
in the future after the first invocation.

**Acceptance Scenarios**:

1. **Given** a job whose next run time is several slots in the past, **When** `run-due` is invoked,
   **Then** the job runs exactly once and its next run time is the first regular slot after the
   finish time.
2. **Given** the same job right after that run, **When** `run-due` is invoked again, **Then** the
   job does not run again.

---

### User Story 5 - Failed runs are retried later with growing delay (Priority: P2)

A job's run fails, for example because a provider is temporarily unavailable. Rather than waiting
a whole day (or a week) for its next regular slot, the job is retried after a short delay; if it
keeps failing, each retry waits longer than the previous one. A failed job is never re-run in a
tight loop.

**Why this priority**: Transient failures are common for unattended network-bound jobs; early
retries keep digests timely, while growing delays protect provider rate limits and cost.

**Independent Test**: With a fake provider that fails, invoke `run-due` for a due job and verify
the job's next run time is about one hour later; advance the clock, let it fail again, and verify
the next delay is longer; let it succeed and verify the next run time returns to the regular
schedule.

**Acceptance Scenarios**:

1. **Given** a due job whose run fails, **When** the run finishes, **Then** its claim is released
   and its next run time is set to the first retry delay (1 hour) after the finish time, unless the
   next regular slot is earlier, in which case the regular slot is used.
2. **Given** a job that has failed several times in a row, **When** it fails again, **Then** the
   retry delay is double the previous one, up to a maximum of 24 hours, and never later than the
   next regular slot.
3. **Given** a job whose previous runs failed, **When** a run succeeds or ends partially
   successful, **Then** the next run time returns to the regular schedule and the retry delay
   resets.
4. **Given** a job that just failed, **When** `run-due` is invoked again immediately, **Then** the
   job does not run again.
5. **Given** a run that ends with an unexpected error before it could record its result, **When**
   the invocation handles that error, **Then** the job's claim is still released and its next run
   time is still set according to the failure rule.
6. **Given** an operator runs a job manually with `invio job run` (not a dry run) and the run
   fails, **When** it finishes, **Then** the job's next run time follows the same retry rule and
   the failure counts toward the job's failure streak; **Given** a manual dry run, **Then** the
   next run time and the failure streak are unchanged whatever its outcome.

---

### User Story 6 - External health monitoring (Priority: P2)

The operator has configured a health-check URL with an external monitoring service. After each
invocation in which every run succeeded (or nothing ran), the service receives a success signal;
when at least one run failed or the invocation aborted with an unexpected error, the service
receives a failure signal — always matching the invocation's exit code. If the timer stops
firing, the missing signals let the monitoring service alert the operator.

**Why this priority**: Unattended operation is only trustworthy if a silent stop is noticed.

**Independent Test**: Configure a health-check URL pointing to a local recorder, run one
invocation whose runs all succeed, one in which a run fails, and one that aborts (e.g. the
database is unreachable), and verify the recorder received the success signal, the failure
signal and the failure signal respectively; with no URL configured, verify nothing is called.

**Acceptance Scenarios**:

1. **Given** a configured health-check URL, **When** an invocation finishes with exit code 0
   (every run succeeded or was partial, nothing was due, or all due jobs were busy), **Then** the
   URL is called once after all runs of that invocation finished.
2. **Given** a configured health-check URL, **When** at least one run of the invocation failed,
   **Then** the failure address (the URL followed by `/fail`) is called once after all runs
   finished, and the invocation exits 1.
3. **Given** a configured health-check URL, **When** an invocation aborts with an unexpected
   error, **Then** the failure address is called once, and the invocation exits non-zero.
4. **Given** the health-check service is unreachable or slow, **When** the signal is sent,
   **Then** the invocation's exit code and the job results are unaffected, the problem is logged
   as a warning, and the call gives up after a bounded time.
5. **Given** no health-check URL is configured, **When** an invocation finishes either way,
   **Then** no health-check call is made.

---

### User Story 7 - Run several due jobs in parallel when allowed (Priority: P3)

The operator's providers permit more throughput, so they start `run-due --parallel 3`. Up to
three due jobs run at the same time; by default jobs run one after another to respect provider
rate limits.

**Why this priority**: Useful once many jobs exist, but sequential execution is the safe default
and fully satisfies the core need.

**Independent Test**: Seed four due jobs whose fake runs record their start and end times; verify
that without `--parallel` no two runs overlap, and with `--parallel 2` at most two runs overlap
at any moment and all four jobs run exactly once.

**Acceptance Scenarios**:

1. **Given** several due jobs and no parallel option, **When** `run-due` runs, **Then** the jobs
   run strictly one after another in order of due time.
2. **Given** several due jobs and `--parallel N`, **When** `run-due` runs, **Then** at most N jobs
   run at the same time and each due job runs exactly once.
3. **Given** `--parallel` or `--limit` with a value below 1 or not a whole number, **When** the
   command is invoked, **Then** it is rejected with a message naming the option and exits with the
   configuration-error exit code, without running anything.

---

### Edge Cases

- A job becomes due while an invocation is already running: it is not picked up by that
  invocation (the due set is fixed at the start) and runs on the next invocation.
- A job is claimed in the selection but has been disabled or deleted by the time the invocation
  reaches it: it is skipped without a run and reported as skipped.
- A due job's stored configuration or schedule cannot be read: the run is recorded as failed and
  the lock is released; since no regular slot can be computed, the retry delay alone determines
  the next run time, so the job is not retried in a loop.
- One job fails (or raises unexpectedly) in the middle of an invocation: the remaining due jobs
  still run; the failure does not abort the invocation.
- The database is unreachable at the start of the invocation: the invocation aborts, pings the
  failure address, and exits non-zero; no claims are taken.
- The process is terminated (e.g. the timer's stop timeout) during a run: the claim is left in
  place and is reclaimed after its timeout by a later invocation (User Story 3).
- A manual `invio job run` holds the claim when `run-due` reaches the job: the job is skipped as
  busy, exactly like a claim held by another `run-due` invocation.
- A job's next run time is not set: the job is not considered due.
- A failed job's retry time would land after its next regular slot: the regular slot is used.
- Clocks: due checks, claims and next run times all use UTC, so daylight-saving changes in the
  job's schedule time zone cannot make a job due twice or skip it.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The CLI MUST provide `invio run-due` with an optional `--limit N` (maximum number of
  jobs started in this invocation) and an optional `--parallel N` (maximum number of jobs running
  at the same time; default 1).
- **FR-002**: The command MUST determine the due jobs once at its start as all enabled jobs whose
  next run time is at or before the current UTC time, ordered by next run time ascending (ties
  broken by job id), and MUST consider at most `--limit` of them when the option is given.
- **FR-003**: Before running a job the command MUST claim it in a single atomic step that succeeds
  only if the job has no claim or its claim has expired; a successful claim MUST set the claim's
  expiry to the current time plus the configured run lock duration (default 2 hours).
- **FR-004**: The command MUST run a job only when its own claim succeeded; a job whose claim fails
  MUST be skipped and reported as busy, and MUST NOT count as a failure.
- **FR-005**: Two or more invocations running at the same time (and any manual job run) MUST never
  execute the same job concurrently, and a job MUST be executed at most once per due occurrence
  across all invocations.
- **FR-006**: A claim whose expiry has passed MUST be reclaimable by the next invocation; a run
  that has lost its claim MUST stop and MUST NOT release or modify the new claim or the job's next
  run time.
- **FR-007**: Without `--parallel`, jobs MUST run one after another in due order; with
  `--parallel N`, at most N jobs MUST run at the same time.
- **FR-008**: Whenever a run of a claimed job ends — successfully, partially, failed, or with an
  unexpected error — the command MUST release the job's claim and set its next run time,
  provided the run still owns the claim.
- **FR-009**: After a successful or partial run, the next run time MUST be the first regular slot
  of the job's schedule after the finish time, so that any number of missed slots results in
  exactly one run.
- **FR-010**: After a failed run, the next run time MUST be the earlier of (a) the finish time
  plus a retry delay and (b) the next regular slot. The retry delay MUST start at 1 hour for the
  first consecutive failure and double for each further consecutive failure, capped at 24 hours;
  a successful or partial run resets the sequence. This rule MUST apply to every real run of a
  job — started by `run-due` or manually by `invio job run` — and every real run MUST count
  toward the consecutive-failure streak; dry runs MUST NOT change the next run time or the
  streak.
- **FR-011**: A failure of one job MUST NOT prevent the other due jobs of the same invocation from
  running.
- **FR-012**: The command MUST report on stdout, per job, whether it ran (with its run id and final
  status), was skipped as busy, or was skipped for another reason, plus a summary of the counts;
  diagnostics MUST go to stderr.
- **FR-013**: The command MUST exit 0 when every job it ran succeeded or was partial (including
  when nothing was due or all due jobs were busy), 1 when at least one run failed or the
  invocation aborted with an unexpected error, and 2 for invalid options or configuration.
- **FR-014**: When a health-check URL is configured, the command MUST send exactly one signal per
  invocation, after all its runs have finished, matching its exit code (FR-013): the URL itself
  for exit code 0, and the URL with `/fail` appended for a non-zero exit (at least one run
  failed, or the invocation aborted with an unexpected error); it MUST NOT call either when no
  URL is configured. Invalid options (exit code 2) are rejected before any signal is sent.
- **FR-015**: A health-check call MUST be bounded in time, MUST NOT change the exit code or any job
  result when it fails, and MUST be logged as a warning on failure without revealing secrets
  contained in the URL.
- **FR-016**: Each run started by `run-due` MUST be recorded in the run history exactly like a
  manual real run (status, stats, errors), so `invio run list` and `invio run show` cover
  scheduled runs too.

### Key Entities *(include if feature involves data)*

- **Job**: A configured research job. Relevant attributes: enabled flag, schedule, next run time
  (UTC), claim expiry (empty when unclaimed), and the count of consecutive failed real runs —
  scheduled and manual, excluding dry runs — (or enough run history to derive it) for the retry
  delay.
- **Claim (job lock)**: The exclusive right of one run to execute a job until its expiry; acquired
  atomically, released when the run ends, reclaimable after expiry.
- **Run**: One execution of a job, with final status (succeeded, partial, failed), stats and
  errors; already exists from #19/#22 and is reused unchanged.
- **Invocation**: One execution of `run-due`; has a fixed set of due jobs, an outcome
  (completed or aborted) and a per-job report. Not persisted.
- **Health-check signal**: A success or failure notification sent to the configured monitoring
  address at the end of an invocation.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In a concurrency test with two simultaneous invocations over at least 10 due jobs,
  repeated at least 20 times, every job is executed exactly once per round, with zero duplicates.
- **SC-002**: A job whose claim expired is run by the first invocation after the expiry, in 100%
  of test cases; a job with a valid claim is never run by another invocation.
- **SC-003**: A job with any number of missed slots produces exactly one run and is then not due
  again until its next regular slot.
- **SC-004**: A continuously failing job is run at most once per retry delay (1 h, 2 h, 4 h, …,
  at most every 24 h) and never more often than its regular schedule would allow; it never runs
  twice in the same invocation or in back-to-back invocations without the delay having passed.
- **SC-005**: Every claimed job ends with its claim released and a next run time in the future,
  whatever the run's outcome, except when the process itself was killed.
- **SC-006**: The health-check address receives exactly one signal per invocation — success when
  the invocation exits 0, failure when any run failed or the invocation aborted — and an
  unreachable monitoring service never changes the invocation's outcome.
- **SC-007**: An invocation with nothing due finishes in under 5 seconds on a typical server.
- **SC-008**: Every acceptance scenario above is covered by an automated test; the concurrency
  test runs against the production database type.

## Assumptions

- The command is named `invio run-due`, following this repository's CLI name; the issue's
  `scout` is the project's former name.
- Building blocks from earlier issues are reused: the job table with next run time and claim
  expiry (#6), the research workflow (#21), and the claim/run/release cycle, run history and
  dry-run machinery of `invio job run` (#22). `run-due` always performs real runs; it has no
  dry-run mode. The retry rule (FR-010) changes #22's behaviour for failed manual real runs, which
  until now moved the job to its next regular slot.
- The claim duration is the existing configurable run lock duration, whose default is 2 hours as
  stated in the issue.
- "Partial" runs count as completed for scheduling: they are not retried early and they reset
  the retry sequence, because their deliverable was produced.
- The issue's "ping `/fail` on exceptions" is widened (see Clarifications): the health-check
  signal follows the exit code, so failed runs also trigger `/fail` and a job that keeps failing
  is noticed even though it is retried.
- The retry delay sequence (1 h doubling, capped at 24 h) is the reasonable reading of "exponential
  retry delay (e.g. +1h)"; it can be made configurable later without changing this behaviour.
- The systemd timer and service unit files themselves are out of scope; only the command they
  invoke is specified here.
- Unit tests use fake providers and local fixtures; the concurrency acceptance test uses a real
  MariaDB instance as stated in the issue and may be marked as an integration test.
