# Feature Specification: Assembled Research Workflow with Parallel Item Processing and Retries

**Feature Branch**: `gh-issue-21`

**Created**: 2026-10-06

**Status**: Draft

**Input**: User description: "GitHub issue #21: Assemble LangGraph research workflow with fan-out and retries. Context: All nodes are wired into one graph per job run: load, fetch, deduplicate, prefilter, per-item processing in parallel, synthesis, persistence, notification and finalization. Implementation steps: (1) Create scout/graph/state.py with RunState (job_id, run_id, config, candidates, items and errors with operator.add reducers, digest_md). (2) Create scout/graph/build.py building the StateGraph: load_job → fetch_sources → deduplicate → keyword_prefilter → (Send per item) process_item → join → synthesize_digest → persist → notify → finalize. (3) process_item subgraph: extract_text or video path (placeholder until #29) → score_relevance → conditional edge → summarize_item. (4) Limit parallelism with a semaphore (default 4) to respect provider rate limits. (5) Use LangGraph RetryPolicy on network/LLM nodes (exponential backoff, retry on LLMRateLimitError, LLMUnavailableError, FetchError). (6) Each fan-out node catches its own exceptions and returns an item with error, so one bad item never aborts the run. (7) finalize always runs (also after failures): closes the run, sets status, computes next_run_at, releases the lock. (8) Expose run_job(job_id, *, dry_run=False) -> RunResult; in dry-run no mail is sent and no next_run_at change is made. Acceptance criteria: End-to-end run with FakeProvider and local fixtures produces a digest; one failing item yields run status partial, other items are processed; finalize runs after an exception in fetch_sources; concurrency never exceeds the configured limit; dry-run sends no mail and does not alter next_run_at. Depends on #11–#20. Branch: issue/21-assemble-langgraph-research-workflow-with."

## Clarifications

### Session 2026-10-06

- Q: In a dry run, should the run still save its results (items marked as processed, the digest, LLM usage) to the database, or leave the job's stored data untouched? → A: Save only a run record (status + stats, marked as a dry run); items are released back to pending, no digest is stored, the digest is returned in the run result only.
- Q: Should `run_job` take the job's run lock itself, or rely on the caller (scheduler, #23) to have taken it? → A: `run_job` takes the lock as its first step (fails with "job busy" if another run holds an unexpired lock); `finalize` releases it.
- Q: Until video support arrives (#29), what should happen to an item that is a video? → A: Process it like a text item using the page's available text (title, description, teaser); the video path is a pass-through hook for #29 — no new status, no migration.
- Q: If one or more of a job's sources can't be fetched at all (even after retries), how should that affect the run's final status? → A: Any failed source → run is `partial`; every source fails → run is `failed`; failures are recorded as run errors.

## User Scenarios & Testing *(mandatory)*

The "user" of this feature is the operator who configures research jobs and relies on unattended
runs (scheduler, CLI) to produce a digest, and the scheduler/CLI that triggers a run by job id.

### User Story 1 - One job run produces a digest end to end (Priority: P1)

An operator triggers a run of a configured job. The run loads the job, collects candidates from
all of its sources, drops already-known and keyword-excluded candidates, rates and summarizes the
remaining items, synthesizes a digest, saves everything, delivers the notification and closes the
run with a final status and the next scheduled run time.

**Why this priority**: This is the core value of the tool: without an assembled run, the
individual stages (#11–#20) deliver nothing to the operator.

**Independent Test**: Run a job against local source fixtures with the fake LLM provider and a
test mail sink; verify that a digest is stored, the run is closed with status `succeeded`, the
statistics reflect every stage, and one notification was delivered.

**Acceptance Scenarios**:

1. **Given** a job with sources served from local fixtures and a fake LLM provider scripted to
   rate items relevant and summarize them, **When** the job is run, **Then** a digest covering the
   summarized items is stored, the run ends `succeeded`, and the run result reports the run id,
   final status, digest and statistics.
2. **Given** the same job, **When** the run completes, **Then** the stages ran in the order load →
   fetch → deduplicate → keyword filter → per-item processing → synthesis → save → notify →
   finalize, and every item that passed the keyword filter was processed exactly once.
3. **Given** a job whose sources yield no new items, **When** the job is run, **Then** no LLM item
   calls are made, the run ends `succeeded` with zero counts, and finalization still sets the next
   run time. An empty digest is stored and sent only when the job's notification settings ask for
   it (`send_if_empty`); otherwise no digest and no notification are created.
4. **Given** an item that is rated not relevant, **When** it is processed, **Then** it is not
   summarized and is not part of the digest.

---

### User Story 2 - One bad item never aborts the run (Priority: P1)

When extracting, rating or summarizing a single item fails (after retries), that item is marked
failed with an error and the remaining items are still processed; the run ends `partial` instead
of failing as a whole.

**Why this priority**: Feeds and web pages are unreliable; an unattended run that dies on the
first broken page would deliver nothing most days.

**Independent Test**: Run a job with three fixture items where one item's processing always
raises; verify the other two are summarized and in the digest, the failing item is recorded as
failed with an error, and the run status is `partial`.

**Acceptance Scenarios**:

1. **Given** three items where processing one item raises a non-retryable error, **When** the job
   runs, **Then** the other two items are summarized, the digest contains them, the failing item
   is stored as failed with its error recorded, and the run status is `partial`.
2. **Given** every processed item fails, **When** the job runs, **Then** the run status is
   `failed` (per the existing status rules) and finalization still runs.
3. **Given** an item failure, **When** the run result is inspected, **Then** the error list names
   the item and the error class, never provider or database message text containing secrets or
   document content.

---

### User Story 3 - Transient failures are retried with backoff (Priority: P2)

Network fetches and LLM calls that fail with a transient error (rate limit, provider unavailable,
fetch failure) are retried automatically with exponentially growing waits before the item or
source is given up.

**Why this priority**: Rate limits and short outages are routine; retrying them avoids needless
`partial` runs and repeated work on the next run.

**Independent Test**: Script the fake provider to raise a rate-limit error twice and then
succeed; verify the item is summarized, the run is `succeeded`, and the waits between attempts
grew.

**Acceptance Scenarios**:

1. **Given** an LLM call that raises a rate-limit or unavailable error and then succeeds within
   the retry limit, **When** the item is processed, **Then** the item succeeds and the run is not
   marked partial because of it.
2. **Given** a source fetch that raises a fetch error and then succeeds within the retry limit,
   **When** the run fetches sources, **Then** the source's candidates are included.
3. **Given** a transient error that persists past the retry limit, **When** the item is processed,
   **Then** the item is marked failed and the run continues (User Story 2).
4. **Given** a non-transient error (authentication, invalid request, invalid output), **When** it
   occurs, **Then** it is not retried.

---

### User Story 4 - Parallelism respects provider rate limits (Priority: P2)

Items are processed in parallel to keep runs short, but never more than a configured number at
once (default 4), so provider rate limits are respected.

**Why this priority**: Sequential processing makes large runs slow; unbounded parallelism
triggers rate limits and cost spikes.

**Independent Test**: Run a job with more items than the limit, using a fake processing step that
records how many items are in flight; verify the recorded maximum never exceeds the limit and
reaches it when enough items exist.

**Acceptance Scenarios**:

1. **Given** 10 items and a concurrency limit of 4, **When** the job runs, **Then** at no moment are
   more than 4 items being processed, and all 10 are processed.
2. **Given** a configured limit of 1, **When** the job runs, **Then** items are processed one at a
   time.
3. **Given** no limit configured, **When** the job runs, **Then** the default limit of 4 applies.

---

### User Story 5 - Finalization always happens (Priority: P1)

Whatever goes wrong during a run — including a crash in loading sources — the run is closed with a
final status, the error is recorded, the next run time is computed and the job's run lock is
released, so the job is not stuck and runs again on schedule.

**Why this priority**: A run left `running` with a held lock blocks the job forever in unattended
operation.

**Independent Test**: Make the source fetch stage raise an unexpected exception; verify the run is
stored `failed` with an error, `next_run_at` is advanced, the lock is released, and `run_job`
returns a result instead of hanging.

**Acceptance Scenarios**:

1. **Given** the fetch stage raises an unexpected exception, **When** the job runs, **Then**
   finalization runs, the run is stored `failed` with a sanitized error, the job's next run time is
   recomputed, and the job's lock is released.
2. **Given** the save stage fails, **When** the job runs, **Then** the run is recorded `failed` via
   the existing recovery path and finalization still releases the lock and sets the next run time.
3. **Given** notification delivery fails, **When** the job runs, **Then** the run status reflects
   the delivery outcome per the existing notification rules and finalization still runs.
4. **Given** the run is interrupted (e.g. cancellation), **When** the interruption propagates,
   **Then** finalization still releases the lock before the interruption is re-raised.
5. **Given** a run of a job is in progress, **When** a second run of the same job is started (e.g.
   from the CLI), **Then** the second run is refused as "job busy" without creating a run or
   taking items, and the first run completes unaffected.

---

### User Story 6 - Dry run for safe testing (Priority: P3)

An operator can run a job as a dry run to see what it would produce without sending mail and
without changing when the job runs next.

**Why this priority**: Operators need to try job configurations safely; it is not needed for
scheduled operation.

**Independent Test**: Run a job with `dry_run` enabled; verify no mail was handed to the mailer,
the job's next run time is unchanged, and the run result still contains the digest.

**Acceptance Scenarios**:

1. **Given** a job with a configured recipient, **When** it runs as a dry run, **Then** no mail is
   sent and the run result contains the digest.
2. **Given** a job with a stored next run time, **When** it runs as a dry run, **Then** the next
   run time is unchanged afterwards.
3. **Given** a dry run, **When** it finishes or fails, **Then** the run is still closed with a
   final status and the lock is released.
4. **Given** a dry run that processed and summarized items, **When** it finishes, **Then** a run
   record marked as a dry run exists with status and statistics, no digest and no LLM usage rows
   are stored, and every item it took is pending again with its attempt count unchanged, so the
   next real run processes the same items.

---

### Edge Cases

- The job id does not exist or the job is disabled: the run fails fast with a clear error; no run
  row is left `running` and no lock is held.
- The job's stored configuration is invalid: the run is closed `failed` with a configuration error
  that names the offending field paths (never their values); no LLM calls are made.
- The job is already locked by another run (an unexpired lock): the run does not start; the
  caller gets a distinct "job busy" error, no run row is created, no items are taken and the
  other run's lock is left untouched.
- The job holds an expired lock (a crashed earlier run): the lock is taken over and the run
  proceeds.
- A source type that is not supported yet (no adapter) is skipped with a warning. It counts
  neither as a fetched source nor as a failed one, so it never makes a run `partial` or
  `failed`; a job whose enabled sources are all unsupported runs with zero candidates.
- One source fails completely (after retries) while others succeed: candidates from the healthy
  sources are processed, the source failure is recorded as a run error, and the run is `partial`
  (or `failed` if the item rules already make it `failed`).
- Every source of the job fails (after retries): the run still proceeds through the remaining
  stages (pending items left from earlier runs are still processed), finalization runs as usual,
  and the run is `failed`, whatever the item outcomes.
- The token budget is exceeded mid fan-out: remaining per-item LLM calls are skipped, items
  already in flight finish or stop at their next call, synthesis uses the items summarized so far,
  and the run is `partial` (per #19).
- An item is a video: it takes the video path, which until #29 passes the item on to text
  extraction, so it is rated and summarized from the page's available text (title, description,
  teaser) like any other item; it is neither failed nor skipped for being a video. A video page
  with no usable text is handled exactly like a text page with no usable text.
- The digest synthesis call fails or returns an unusable answer: the existing fallback digest
  (#18) listing every summarized item is used, it is saved and sent like a normal digest, and the
  run is at most `partial`. A credential or configuration error during synthesis is a stage
  failure: the run is `failed`, no notification is sent, and finalization runs.
- Errors from parallel items arrive in any order: all are kept; none overwrites another.
- The process is cancelled while items are in flight: in-flight items are cancelled, finalization
  runs, and the cancellation is re-raised.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide one entry point that runs a job by id, with an option for a
  dry run, and returns a run result containing at least the run id, the final run status, the
  digest (if any), the run statistics, and the list of recorded errors.
- **FR-002**: A run MUST execute the stages in this order: load job, fetch sources, deduplicate,
  keyword prefilter, per-item processing, join, synthesize digest, save, notify, finalize.
- **FR-003**: Per-item processing MUST run once for each item that passed the keyword prefilter,
  and MUST consist of: text extraction (or the video path for video items), relevance rating, and
  — only if the item is rated relevant — summarization.
- **FR-004**: The video path MUST exist as a pass-through placeholder: until video support (#29)
  is delivered, it hands video items to text extraction so they are processed from the page's
  available text like text items, with no dedicated status and no schema change. #29 MUST be able
  to replace the placeholder without changing the rest of the per-item flow.
- **FR-005**: The run state MUST collect items and errors produced by parallel item processing
  without losing or overwriting any entry, regardless of completion order.
- **FR-006**: The number of items processed at the same time MUST NOT exceed a configurable limit,
  defaulting to 4.
- **FR-007**: Source fetching and LLM-calling stages MUST retry transient failures — rate-limit,
  provider-unavailable and fetch errors — with exponential backoff and a bounded number of
  attempts; other errors MUST NOT be retried.
- **FR-008**: A failure while processing one item (after retries) MUST be caught within that
  item's processing, recorded as an item error, and MUST NOT stop the processing of other items
  or the rest of the run.
- **FR-009**: The run status MUST be decided by the existing status rules (#19, #20), extended
  by source outcomes, in this order: `failed` when a stage before the save failed, when every
  source of the job failed to fetch (after retries), or when every attempted item failed;
  `partial` when at least one source failed to fetch, any item failed, or the budget stopped the
  run; otherwise `succeeded`. Each failed source MUST be recorded as a run error and counted in
  the run statistics.
- **FR-010**: Finalization MUST run on every exit path — success, item failures, a stage
  exception (including in fetch sources), a save failure, a notification failure and
  cancellation — and MUST close the run with its final status, record a sanitized error when
  there is one, compute and store the job's next run time, and release the job's run lock.
- **FR-011**: Recorded errors MUST carry the stage, the item (where applicable) and the error
  class, and MUST NOT contain secrets, provider/database message text or document content
  (Constitution V; #19 R8).
- **FR-012**: In a dry run, the system MUST NOT send any mail, MUST NOT change the job's next
  run time, MUST NOT store a digest or LLM usage rows, and MUST NOT leave any item change behind:
  every item the run took is released back to pending with its attempt count unchanged. The only
  persisted outcome is a run record marked as a dry run, carrying its final status and
  statistics; the digest is returned in the run result only. Fetching, deduplication, filtering,
  item processing, synthesis, finalization of the run record and lock release MUST otherwise
  behave as in a normal run.
- **FR-013**: A normal (non-dry) run MUST honour the existing persistence preconditions (#19): the run row and
  the deduplication take are committed before the LLM stages begin, no usage row is committed
  before the save, and a stage that raises before the save is recorded through the existing
  failed-run recovery.
- **FR-014**: Every log line emitted during a run MUST carry the job and run id (run context,
  Constitution V).
- **FR-015**: The whole run MUST be executable in tests without network access, a real LLM
  provider or a real mail server, using the fake provider, local source fixtures and a test
  mailer.
- **FR-016**: Before any other stage, a run MUST acquire the job's run lock atomically (only
  one of two concurrent attempts can succeed). If another run holds an unexpired lock, the run
  MUST be refused with a distinct "job busy" error, without creating a run record, taking items
  or changing the lock. An expired lock MUST be taken over. The lock acquired by a run MUST be
  released by its finalization (FR-010), and only a lock the run itself acquired.

### Key Entities

- **Run State**: The shared state of one run as it moves through the stages: job id, run id, the
  validated job configuration, fetched candidates, processed items (accumulated from parallel
  processing), errors (accumulated from all stages), and the rendered digest text.
- **Processed Item**: The outcome of one item's processing: the item, its relevance outcome, its
  summary outcome (if relevant), and an error if processing failed.
- **Run Error**: A sanitized record of one failure: stage, item id or source (optional), error
  class.
- **Run Result**: What a caller receives after a run: run id, final status, digest (if any —
  for a dry run this is the only place the digest exists), statistics, errors, and whether it
  was a dry run.
- **Concurrency Limit**: The maximum number of items processed at once (default 4).
- **Retry Policy**: Bounded attempts with exponentially growing waits, applied to fetch and LLM
  stages, for transient error kinds only.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: An end-to-end run against local fixtures with the fake provider produces a stored
  digest and a `succeeded` run in 100% of test executions, with no network access.
- **SC-002**: With one of N items failing, N−1 items are summarized and the run is `partial` in
  100% of test executions.
- **SC-003**: In 100% of runs where any stage raises, including fetching sources, the run leaves
  the `running` state, the lock is released and the next run time is set (except in dry runs).
- **SC-004**: Across a run of at least 10 items with a limit of 4, the observed number of items in
  flight never exceeds 4, and reaches 4 at least once.
- **SC-005**: A transient error that clears within the retry limit causes no item failure; one
  that persists causes exactly one item failure and no run abort.
- **SC-006**: A dry run sends zero mails, leaves the job's next run time byte-for-byte
  unchanged, stores zero digests and zero usage rows, and leaves every item's status and attempt
  count as it was before the run.
- **SC-007**: No recorded error or log line from a failing run contains secret values, provider
  message text or document content.
- **SC-008**: With one of several sources unreachable, the run is `partial` and the other sources'
  items appear in the digest; with every source unreachable, the run is `failed`, in 100% of test
  executions.

## Assumptions

- The issue refers to a `scout/` package; this repository's package is `src/invio/`, so the run
  state and graph assembly live under `src/invio/graph/` next to the existing nodes.
- The existing stage implementations from #11–#20 (deduplication, keyword filter, relevance,
  summarization, synthesis, persistence with budget, e-mail notification, next-run computation)
  are reused as-is; this feature wires them together and adds only glue, state, retries,
  concurrency control and finalization.
- `RunResult` already names the persistence input in `graph/nodes/persist.py`; the public result
  of the run entry point is a distinct type, and its final name is decided in planning.
- The run entry point acquires and releases the per-job run lock (`locked_until`) itself; the
  scheduler (#23) selects due jobs and calls the entry point, treating "job busy" as a skip. The
  lock lifetime (how far `locked_until` is set ahead) is a configurable value chosen in planning,
  long enough to cover a normal run.
- Retry defaults: up to 3 attempts with exponential backoff starting around 1 second and capped
  at a modest maximum, with jitter; exact values are set in planning and are configurable.
- The concurrency limit is configurable per run (and optionally via settings); the default is 4.
- A dry run still creates and closes a run record (marked as a dry run) so the outcome can be
  inspected; whether the mark is a new column or a statistics field is decided in planning.
- Video processing (#29) is out of scope; only its pass-through placeholder path is delivered
  here. How an item is recognised as a video (e.g. by source type or URL) is decided in planning;
  until #29 a misclassification has no effect, because both paths process the item the same way.
- CLI wiring of the run command (e.g. `invio job run`) is out of scope unless trivially exposed;
  this feature delivers the callable entry point.
