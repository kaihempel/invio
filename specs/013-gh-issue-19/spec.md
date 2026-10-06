# Feature Specification: Run Persistence with Token Budget Enforcement

**Feature Branch**: `gh-issue-19`

**Created**: 2026-10-06

**Status**: Draft

**Input**: User description: "GitHub issue #19: Add persistence of runs, digests and usage with token budget enforcement. Context: Each run must be traceable: status, stats, digest and LLM usage. The max_tokens_per_run budget protects against runaway costs. Implementation steps: (1) Create scout/graph/nodes/persist.py writing items, digest and usage in one transaction. (2) Implement BudgetTracker (in state or context) summing tokens from every LLM call; check() raises BudgetExceeded when above max_tokens_per_run. (3) On BudgetExceeded the pipeline skips remaining per-item calls and proceeds to synthesis with the best items so far; run status becomes partial. (4) Fill runs.stats with counts: found, new, after keyword filter, relevant, summarized, failed, tokens, estimated cost. (5) Set run status: success, partial (any item error or budget stop) or failed. Acceptance criteria: After a run, items, digest, usage rows and run stats exist consistently or not at all (transaction rollback tested); exceeding the budget stops further LLM item calls and marks the run partial; estimated cost uses prices from models.yaml. Depends on #4, #7. Branch: issue/19-add-persistence-of-runs-digests."

## Clarifications

### Session 2026-10-06

- Q: If the final save fails and the run's results are discarded, should the record of the LLM tokens it spent (and their cost) be discarded too? → A: No. Usage rows are saved separately, call by call, so they survive a failed save; the item changes, digest and run statistics stay all-or-nothing, and a failed run keeps its usage rows and is marked `failed`.
- Q: After the token budget is used up, may the run still make its one LLM call to write the digest? → A: Yes. The budget blocks only per-item calls (relevance, summaries); the single digest call always runs and its tokens are counted in the statistics.
- Q: Should an item skipped because the budget ran out get back the retry attempt it used up when the run picked it? → A: Yes. Skipped items are released: the attempt is undone and the link to the run is cleared, so the next run picks them up as if they had never been taken.
- Q: If a run ends with no item processed successfully (every item failed, or the budget ran out before any finished), should its status be `partial` or `failed`? → A: `failed` when at least one item was attempted and every attempted item failed; a budget stop with no successful items stays `partial`.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Consistent, all-or-nothing run results (Priority: P1)

As the operator of an unattended research job, after every run I want the run's outcome — the
updated items, the digest and the run statistics — saved together as one unit, so that I never
find a digest that references items whose state was not saved. The record of LLM usage is kept
separately and survives a failed save, because those tokens were spent and billed either way.

**Why this priority**: Without consistent persistence the run history cannot be trusted: a
half-written run would make the next run re-process or skip items incorrectly and would
misreport costs. Everything else in this feature (stats, budget) builds on a reliable save step.

**Independent Test**: Run the pipeline end to end against a local test database with a fake
LLM provider; verify the items, the digest and the usage rows of the run are all present. Then
repeat with a failure injected during the save step and verify none of that run's item updates,
or digest exists, while the usage rows of the calls already made remain and the run itself is
recorded as `failed`.

**Acceptance Scenarios**:

1. **Given** a run that processed items and produced a digest, **When** the save step
   completes, **Then** the item updates, the digest (linked to the run and listing its item
   ids) and the run statistics all exist, and one usage row per LLM call of the run exists and
   references the run.
2. **Given** a run whose save step fails part-way (e.g. a write is rejected), **When** the
   failure occurs, **Then** none of the run's item updates or digest remain stored, while the
   usage rows of the LLM calls already made remain stored and reference the run.
3. **Given** a run whose save step failed, **When** an operator inspects the run history,
   **Then** the run is shown with status `failed`, a finish time and an error description that
   contains no secrets or document text.

---

### User Story 2 - Token budget stops runaway costs (Priority: P1)

As the person paying for LLM usage, I want each run to stop making further per-item LLM calls
once the job's per-run token budget (`limits.max_llm_tokens_per_run`) is used up, so a
misconfigured job or an unexpectedly large batch of items cannot produce an unbounded bill.

**Why this priority**: Cost protection is the main motivation of the issue and a direct
financial risk for an unattended tool.

**Independent Test**: Configure a job with a small token budget and a fake provider that
reports a fixed token count per call; run the pipeline over more items than the budget allows
and count the provider calls made.

**Acceptance Scenarios**:

1. **Given** a job with a token budget and more pending items than the budget covers, **When**
   the tokens consumed by the run's LLM calls exceed the budget, **Then** no further per-item
   LLM calls (relevance scoring, summarization) are started for the run.
2. **Given** the budget was exceeded mid-run, **When** the pipeline continues, **Then** it
   proceeds to the digest step using the items already processed successfully so far, and the
   run finishes with status `partial`.
3. **Given** the budget was exceeded, **When** the run is saved, **Then** items that were not
   processed because of the budget stop are released — their attempt count is back to its
   value before the run and they are no longer linked to the run — and are not counted as
   failed.
4. **Given** a run whose total token usage stays within the budget, **When** it completes,
   **Then** the budget has no effect on its behaviour.

---

### User Story 3 - Traceable run statistics and estimated cost (Priority: P2)

As an operator reviewing past runs, I want each run to carry a statistics summary — how many
items were found, how many were new, how many passed the keyword filter, how many were
relevant, summarized or failed, the tokens used and the estimated cost — so I can understand
what a run did and what it cost without querying individual rows.

**Why this priority**: Observability of unattended runs is a project principle, but the run
is still correct and cost-capped without these figures, so it ranks after P1.

**Independent Test**: Run the pipeline with a fake provider over a known set of items with a
known outcome mix and verify every count and the cost figure in the stored run statistics.

**Acceptance Scenarios**:

1. **Given** a completed run, **When** its statistics are read, **Then** they contain the counts
   `found`, `new`, `after_keyword_filter`, `relevant`, `summarized`, `failed`, the total
   `tokens` (input and output) and the `estimated_cost_usd`.
2. **Given** the run's LLM calls used models with known prices in the model registry, **When**
   the estimated cost is computed, **Then** it equals the sum of each call's tokens multiplied
   by that model's registered input and output prices.
3. **Given** a run that used a model without a registered price, **When** the statistics are
   computed, **Then** the estimated cost includes only the calls with known prices and the
   statistics indicate that the cost is incomplete.
4. **Given** a run with no LLM calls (e.g. nothing new was found), **When** it completes,
   **Then** the statistics show zero tokens and zero cost.

---

### User Story 4 - Accurate run status (Priority: P2)

As an operator or monitoring script, I want every run to end with exactly one final status —
`succeeded`, `partial` or `failed` — that follows clear rules, so I can tell from the status
alone whether a run needs attention.

**Why this priority**: The status drives alerting and the exit code of unattended runs; it
depends on the outcomes defined in Stories 1–3.

**Independent Test**: Drive runs through each outcome (all items fine, one item fails, budget
stop, unrecoverable error) with a fake provider and check the final status of each.

**Acceptance Scenarios**:

1. **Given** a run in which every item was processed without error and the budget was not
   exceeded, **When** it finishes, **Then** its status is `succeeded`.
2. **Given** a run in which at least one item failed but at least one item was processed
   successfully, or the token budget stopped processing, **When** it finishes and the save
   step succeeds, **Then** its status is `partial`.
3. **Given** a run in which at least one item was attempted and every attempted item failed
   (and the budget did not stop it), **When** it finishes, **Then** its status is `failed`,
   and the item failures, digest (if any) and statistics are still saved atomically.
4. **Given** a run that hit an unrecoverable error (e.g. missing provider credentials, database
   failure, failed save step), **When** it ends, **Then** its status is `failed` with an error
   description.
5. **Given** any run, **When** it ends by any path, **Then** it never remains in status
   `running` and always has a finish time.

---

### Edge Cases

- A single LLM call pushes usage from below the budget to well above it: the call's tokens
  still count (they were spent), and the budget is checked before each subsequent per-item
  call, so overshoot is limited to calls already in flight.
- Several per-item calls run concurrently when the budget is reached: calls already started
  complete and their usage is recorded; no new call starts.
- The budget is exceeded before any item is processed successfully: the run still proceeds to
  the digest step with no items; no empty digest is stored, and the run is `partial` (a budget
  stop is a deliberate limit, not a malfunction).
- Every attempted item fails (e.g. the provider rejects every request): the run is `failed`
  so the exit code and monitoring catch the systemic problem; the item failures are still
  saved so retry counts stay correct.
- A call ends with an invalid structured answer: its tokens count against the budget and are
  recorded as usage, as they were spent.
- The digest step itself needs an LLM call after the budget is exceeded: the call always runs
  once so the run still yields a result; its usage is recorded and counted in the statistics,
  so the run's total may end above the budget by the size of that call.
- The run fails before the save step: the run is still marked `failed` with a finish time so
  it never stays `running`; usage rows of calls already made remain stored.
- The database is unreachable when marking the run `failed`: the error is logged (structured,
  with job and run id) and the command exits non-zero.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST save a run's item updates, its digest and its final status and
  statistics together as one atomic unit: either all are stored or none are.
- **FR-001a**: The system MUST record each LLM call's usage as soon as the call ends and MUST
  keep every usage row of the run when the atomic save fails or the run fails before it (the
  rows are written again in a separate step after the rollback). If the process itself is
  killed, the per-call structured log events remain the record of the spent tokens.
- **FR-002**: If the atomic save fails, the system MUST discard all of that run's pending item
  updates and digest, MUST keep the already stored usage rows, and MUST record the run as
  `failed` with a finish time, the token and cost totals of its stored usage rows, and a
  sanitized error description (no secrets, no document text).
- **FR-003**: The system MUST track the total tokens (input plus output) consumed by every LLM
  call of a run, including calls whose answer was invalid.
- **FR-004**: Before starting each per-item LLM call, the system MUST check the run's consumed
  tokens against the job's `limits.max_llm_tokens_per_run`; once consumption exceeds the
  budget, the system MUST signal a budget-exceeded condition and start no further per-item
  LLM calls in that run.
- **FR-005**: On a budget-exceeded condition the system MUST skip the remaining per-item work,
  continue to the digest step with the items processed successfully so far (ranked as the
  digest step normally ranks them), and mark the run `partial`. The budget MUST NOT block the
  digest step's single LLM call; that call's tokens MUST be recorded and counted in the run
  statistics like any other call.
- **FR-006**: Items not processed because of a budget stop MUST NOT be marked failed; they MUST
  be released as part of the run's atomic save: the retry attempt counted when the run took
  them is undone, their link to the run is cleared, and their status is set back to one a
  later run picks up (`new`, or kept `failed` for an item that was being retried and not yet
  touched), so the next run picks them up as if they had never been taken. This includes
  items already rated relevant but not yet summarized; they are rated again by the later run.
- **FR-007**: The system MUST store run statistics containing at least: `found`, `new`,
  `after_keyword_filter`, `relevant`, `summarized`, `failed`, `input_tokens`, `output_tokens`,
  `tokens` (total), `estimated_cost_usd`, and a `budget_exceeded` flag.
- **FR-008**: The estimated cost MUST be computed from the per-model input and output prices
  in the model registry (the provider model files), using the same rounding as stored usage
  costs; calls of models without a registered price MUST be excluded from the sum and the
  statistics MUST flag the cost as incomplete (`cost_complete: false`).
- **FR-009**: The token and cost totals in the run statistics MUST equal the sums over the
  run's stored usage rows.
- **FR-010**: The system MUST set exactly one final run status, checked in this order:
  `failed` on any unrecoverable error, including a failed save step; `failed` when the budget
  did not stop the run, at least one item was attempted and every attempted item failed;
  `partial` when the budget stopped processing (even with zero successful items) or when at
  least one item failed; otherwise `succeeded` (including runs with no items to process).
- **FR-011**: A run MUST NOT remain in status `running` after the pipeline ends by any path,
  and MUST always have a finish time.
- **FR-012**: The budget stop, the final status and the statistics MUST be logged as structured
  log events carrying the job and run id.
- **FR-013**: The behaviour MUST be testable without network access, a real LLM provider or a
  production database (fake provider, local test database), including the rollback path.

### Key Entities

- **Run**: One execution of a job. Holds the final status (`running` → `succeeded` /
  `partial` / `failed`), start and finish times, the statistics summary and an optional error
  description.
- **Run statistics**: The per-run summary — item counts per pipeline stage, token totals,
  estimated cost, whether the cost is complete and whether the budget stopped the run.
- **Digest**: The result of a run: title, body and the list of item ids it covers; linked to
  the job and the run.
- **Item**: A discovered research item whose status, relevance, summary or error is updated
  by the run.
- **LLM usage record**: One LLM call's provider, model, purpose, input/output tokens and
  estimated cost, linked to the job and the run; stored when the call ends, outside the run's
  atomic save.
- **Token budget tracker**: Per-run running total of consumed tokens compared against the
  job's `limits.max_llm_tokens_per_run`; reports when the budget is exceeded.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In 100% of tested failure injections during the save step, no item update or
  digest of the affected run remains stored, every usage row of the calls already made remains
  stored, and the run is recorded as `failed`.
- **SC-002**: In 100% of completed runs, the stored digest, item updates, usage rows and
  statistics are all present and mutually consistent (statistics totals equal the sum of the
  usage rows).
- **SC-003**: Once the budget is exceeded, zero additional per-item LLM calls are started;
  total overshoot is bounded by the calls already in flight at the moment the budget was
  reached plus the single digest call.
- **SC-004**: The estimated cost in run statistics matches the price-based calculation for the
  recorded token counts exactly (to the stored precision of 6 decimal places).
- **SC-005**: 100% of runs end in exactly one of `succeeded`, `partial` or `failed`; no run is
  left in `running` after the pipeline returns.
- **SC-006**: An operator can tell what a run found, processed, failed and cost from the run
  record alone, without inspecting individual items or usage rows.

## Assumptions

- The issue's status name `success` maps to the existing run status `succeeded`; the status
  vocabulary is not changed.
- The issue's `max_tokens_per_run` refers to the existing job setting
  `limits.max_llm_tokens_per_run` (default 200,000); no new setting is introduced.
- The issue's "prices from `models.yaml`" refers to the existing model registry, which merges
  prices from the per-provider model files; no new price file is introduced.
- The issue's `scout/` path predates the project rename; the persistence step lives with the
  other pipeline nodes of the `invio` package.
- The budget counts input plus output tokens of all LLM calls in the run (relevance,
  summarization, chunk combining and digest), but only blocks per-item calls; the single
  digest-step call is allowed after a budget stop so a run still produces a result.
- The "best items so far" are the items already processed successfully, ordered by the digest
  step's normal ranking (relevance); no separate ranking is introduced.
- `found` counts all candidates returned by sources in the run; `new` counts candidates not
  previously seen for the job; `after_keyword_filter` counts new items that passed the keyword
  filter; `relevant`, `summarized` and `failed` count items reaching those states in the run.
- The digest synthesis step is provided by another issue; this feature only requires that a
  digest result, when produced, is saved atomically with the rest of the run.
- Depends on #4 (data store: runs, digests, items, usage tables) and #7 (job configuration,
  including the limits section); both are merged.
- Marking a run `failed` after a rolled-back save happens in a separate, small write so that
  the failed status is visible even though the run's results were discarded.
