# Feature Specification: Per-Job Deduplication, Baseline Mode and Run Limits

**Feature Branch**: `gh-issue-14`

**Created**: 2026-10-05

**Status**: Draft

**Input**: User description: "GitHub issue #14: Implement deduplication, baseline mode and run limits. Context: The same article may be relevant for several jobs, so deduplication is per job. Failed items are retried a limited number of times. A job's very first run must not process an entire archive. Implementation: Create scout/graph/nodes/deduplicate.py working on ItemRepository. For each candidate: insert if (job_id, url_hash) is unknown; if known and content_hash changed (web sources), treat as new version; if known with status failed and attempts < 3, include for retry; otherwise drop. Baseline mode: if the job has no previous successful run, keep only the newest baseline_items (default 10) per source and mark the rest skipped_irrelevant with reason baseline. Apply max_items_per_run (newest first). Increment attempts when an item is taken into a run. Acceptance criteria: Re-running with identical candidates returns an empty list; A web page with a changed content_hash is returned again; A failed item is retried at most 3 times; First run keeps only baseline_items per source; max_items_per_run is respected and newest items win. Depends on #4, #5. Branch: issue/14-implement-deduplication-baseline-mode-and"

## Clarifications

### Session 2026-10-05

- Q: When a run hits `max_items_per_run`, what should happen to the eligible items that didn't fit? → A: Save them as new with 0 attempts; later runs take them from storage (newest first) together with fresh candidates, even if the source no longer lists them.
- Q: Which earlier runs should count as "successful", so that a job leaves baseline mode? → A: Runs that ended as `succeeded` or `partial`; `failed` and still-running runs do not count.
- Q: An item can be taken into a run that then crashes or gets interrupted before the item is finished, leaving it with status new and 1 or more attempts. What should later runs do with it? → A: Retry it like a failed item while it has fewer than 3 attempts.
- Q: Where should the reason "baseline" be stored for items that baseline mode skips? → A: ~~In the item's existing error/reason text field, as the fixed value `baseline`; no schema change.~~ Revised after PR review (#53): as its own item status `skipped_baseline` (migration 0003), so the error field only ever holds errors.
- Q (PR review #53): Should baseline mode also skip known work (retries, changed pages, waiting items) of a job whose runs all failed so far? → A: No. Baseline only limits new items; known work is never discarded by it.
- Q (PR review #53): What happens to an interrupted or failed item that no source reports anymore? → A: It is loaded from storage like a waiting item and retried while it has fewer than 3 attempts.

## User Scenarios & Testing *(mandatory)*

The direct "user" of this feature is the research pipeline: after the sources of a job have
reported their candidates, the deduplication step decides which of them the current run
should actually work on. Through it, the operator benefits: each run only spends time and LLM
budget on items that are new (or worth retrying), a new job does not flood the first digest
with an entire archive, and no run grows beyond the configured size.

### User Story 1 - Process each item only once per job (Priority: P1)

When a job runs, its sources report candidate items. The deduplication step compares each
candidate with what the job has already stored. Candidates the job has never seen are stored
and passed on to the run; candidates the job already knows are dropped. Deduplication is per
job: an article already stored for job A is still new for job B.

**Why this priority**: Without deduplication every run would reprocess, re-summarize and
re-notify the same items. It is the smallest slice that makes repeated runs useful.

**Independent Test**: Run the step twice for the same job with an identical list of
candidates. The first call returns all candidates, the second returns an empty list. Running
the same candidates for a second job returns all of them again.

**Acceptance Scenarios**:

1. **Given** a job with no stored items, **When** the step receives candidates, **Then** all
   of them are stored for the job and returned for processing.
2. **Given** the step already processed a list of candidates for a job, **When** it receives
   the identical list again, **Then** it returns an empty list and stores nothing new.
3. **Given** an item stored for job A, **When** job B receives the same item as a candidate,
   **Then** it is stored for job B and returned for processing.
4. **Given** the same item appears twice within one candidate list, **When** the step runs,
   **Then** it is stored and returned only once.

---

### User Story 2 - Pick up changed web pages again (Priority: P2)

Web sources watch pages whose content can change while the address stays the same. When a
known page reports a different content fingerprint than the one stored, the step treats it
as a new version: it records the new fingerprint, resets the item for processing and returns
it again, so the operator learns about the update.

**Why this priority**: Monitoring pages for changes is a core use case of web sources;
without it, an updated page would be silently ignored forever.

**Independent Test**: Store a web item with fingerprint X, then run the step with the same
address and fingerprint Y; the item is returned. Run it again with Y; it is dropped.

**Acceptance Scenarios**:

1. **Given** a stored item with content fingerprint X, **When** a candidate with the same
   address and fingerprint Y arrives, **Then** the item is returned as a new version and the
   stored fingerprint becomes Y.
2. **Given** a stored item with content fingerprint X, **When** a candidate with the same
   address and fingerprint X arrives, **Then** it is dropped.
3. **Given** a stored item or a candidate without a content fingerprint (e.g. feed entries),
   **When** a candidate with the same address arrives, **Then** it is treated as unchanged
   and dropped.

---

### User Story 3 - Retry failed items a limited number of times (Priority: P2)

Fetching or summarizing an item can fail for transient reasons. When a job's sources report
a known item that previously failed, the step includes it in the run again — but only while
the item has been taken into fewer than 3 runs. The same applies to an item whose run crashed
or was interrupted before finishing it. Each time an item is taken into a run, its
attempt counter goes up by one.

**Why this priority**: Transient failures should not lose items, but permanently broken items
must not be retried forever and consume budget on every run.

**Independent Test**: Store an item, mark it failed after each run and re-run the step with
the same candidate. It is returned in the first three runs in total and dropped from the
fourth on.

**Acceptance Scenarios**:

1. **Given** a known item with status failed that was taken into fewer than 3 runs, **When**
   it appears as a candidate, **Then** it is returned for retry and its attempt count rises
   by one.
2. **Given** a known item with status failed that was taken into 3 runs, **When** it appears
   as a candidate, **Then** it is dropped.
3. **Given** a new item, **When** it is taken into a run, **Then** its attempt count is 1.
4. **Given** a known item with status new and 1 or 2 attempts (its run was interrupted),
   **When** it appears as a candidate, **Then** it is returned for retry like a failed item;
   with 3 attempts it is dropped.
5. **Given** a known item with any other status (e.g. summarized, skipped),
   **When** it appears as a candidate with an unchanged fingerprint, **Then** it is dropped.

---

### User Story 4 - Start a new job with a small baseline (Priority: P1)

A newly created job may point at feeds or pages with hundreds of older entries. If the job
has never completed a successful run, the step keeps only the newest `baseline_items`
(default 10) candidates of each source for processing. The older candidates are stored
with status `skipped_baseline`, so later runs treat them as known and never process them.

**Why this priority**: Without a baseline, the first run would fetch, rate and summarize the
entire archive of every source — slow, expensive and useless to the operator.

**Independent Test**: For a job without a successful run, pass 25 candidates from source A
and 5 from source B with baseline 10. The step returns the 10 newest from A and all 5 from B;
the 15 older items of A are stored with status `skipped_baseline`. A second run with the
same candidates returns nothing.

**Acceptance Scenarios**:

1. **Given** a job without a successful run and a source with more than `baseline_items`
   candidates, **When** the step runs, **Then** only the newest `baseline_items` of that
   source are returned and the rest are stored with status `skipped_baseline`.
2. **Given** a job without a successful run and a source with at most `baseline_items`
   candidates, **When** the step runs, **Then** all of that source's new candidates are
   returned.
3. **Given** a job with at least one successful run, **When** the step runs, **Then** no
   baseline limit is applied.
4. **Given** a job whose earlier runs all failed, **When** the step runs, **Then** baseline
   mode still applies.

---

### User Story 5 - Cap the size of a run (Priority: P2)

After deduplication and baseline mode, the step applies the job's `max_items_per_run` limit:
if more items remain, only the newest ones up to the limit are taken into the run. The items
that did not fit are saved as waiting work (status new, 0 attempts), and later runs take them
from storage together with fresh candidates, so a busy job catches up over several runs.

**Why this priority**: The run cap protects time and LLM budget for jobs with many busy
sources, independent of whether the job is new.

**Independent Test**: With `max_items_per_run` = 5 and 8 new candidates with distinct
publish dates, the step returns exactly the 5 newest.

**Acceptance Scenarios**:

1. **Given** more eligible items than `max_items_per_run`, **When** the step runs, **Then**
   exactly `max_items_per_run` items are returned, and they are the newest by publish date.
2. **Given** at most `max_items_per_run` eligible items, **When** the step runs, **Then** all
   of them are returned.
3. **Given** new or changed items cut by the run limit, **When** the step runs, **Then** they
   are stored with status new and 0 attempts and are not linked to the run; a cut retry item
   keeps its stored status and attempt count and is retried when reported again.
4. **Given** stored items waiting with status new and 0 attempts, **When** a later run starts,
   **Then** they compete with the fresh candidates for the run's slots by publish date, even
   if no source reports them anymore.

---

### Edge Cases

- Candidates without a publish date are ordered as the oldest when choosing the newest items
  (baseline and run limit); ties are broken deterministically (order reported by the source,
  waiting items from storage in the order they were stored).
- A candidate list that is empty still returns waiting items from storage (up to the run
  limit); with no waiting items it yields an empty result and changes nothing.
- A waiting item that a source reports again is counted once (no duplicate from storage and
  candidates).
- Two concurrent runs of the same job report the same new item: it is stored once; the
  existing storage-level duplicate protection applies.
- A retried failed item, a changed web page or a reported waiting item during the first run
  does not count toward the baseline of its source and is never skipped by it.
- An item changed (new fingerprint) whose stored status is failed with 3 attempts: the new
  version is a fresh start, so it is returned and its attempt count restarts.
- A job with a disabled source: candidates are only those reported by enabled sources;
  baseline counting is per reporting source.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The pipeline MUST provide a deduplication step that receives the current job,
  the current run and the candidates reported by each source, and returns the items the run
  should process.
- **FR-002**: Item identity MUST be per job, based on the job and the item's address
  fingerprint; the same address in different jobs MUST be handled independently.
- **FR-003**: A candidate unknown to the job MUST be stored for the job and returned.
- **FR-004**: A known candidate whose content fingerprint is present on both sides and
  differs from the stored one MUST be treated as a new version: the stored fingerprint is
  updated, the item is reset for processing (status new, attempt count restarted, previous
  error cleared) and it is returned (subject to baseline mode, FR-009, and the run limit,
  FR-011).
- **FR-005**: A known candidate with fewer than 3 attempts MUST be returned for retry when its
  stored status is failed, or when it is still new but already has 1 or more attempts (it
  was taken into a run that ended before finishing it).
- **FR-006**: A known candidate whose stored item is still waiting (status new, 0 attempts)
  MUST be treated as eligible, not dropped. Every other known candidate MUST be dropped
  without changing the stored item.
- **FR-007**: The maximum number of attempts MUST be 3.
- **FR-008**: Duplicate candidates within the same input MUST be returned at most once.
- **FR-009**: If the job has no previous successful run (an earlier run that ended as
  `succeeded` or `partial`), the step MUST keep only the newest
  `baseline_items` new candidates per source and store the other new candidates with status
  `skipped_baseline` (migration 0003). Changed, retried and waiting items are exempt.
- **FR-010**: `baseline_items` MUST be configurable per job with a default of 10 and accept
  only positive whole numbers.
- **FR-011**: After baseline mode, the step MUST keep at most `max_items_per_run` items,
  choosing the newest by publish date.
- **FR-012**: New and changed items cut by the run limit MUST be stored with status new and
  0 attempts (waiting), not linked to the run; cut retry items MUST keep their stored status
  and attempt count. At the start of each step, the job's stored pending items (waiting: status new, 0
  attempts; retryable: failed, or new after an interrupted run, with fewer than 3 attempts)
  MUST be considered together with the fresh candidates for the run limit, newest first,
  whether or not a source still reports them.
- **FR-013**: Each item taken into the run MUST have its attempt count incremented by one and
  be linked to the current run.
- **FR-014**: Ordering by "newest" MUST use the publish date, with undated items treated as
  oldest and ties broken by the order the source reported them.
- **FR-015**: The step MUST be deterministic: the same stored state and input always yield
  the same result.
- **FR-016**: The step MUST log one structured summary line per run with the counts of new,
  changed, retried, dropped, baseline-skipped and limit-cut items.
- **FR-017**: All storage access MUST go through the existing record access layer; the step
  MUST NOT access the data store directly.

### Key Entities

- **Candidate**: An item reported by a source (address, address fingerprint, title, publish
  date, type, teaser, optional content fingerprint), tagged with the source it came from.
- **Item**: A stored record per job and address fingerprint, with status, content
  fingerprint, attempt count, last error/reason and the run it was last taken into.
- **Run**: One execution of a job; "successful" means it completed with status succeeded or
  partial.
- **Job limits**: Per-job caps including `max_items_per_run` (existing) and `baseline_items`
  (new, default 10).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Re-running a job with identical candidates processes 0 items.
- **SC-002**: 100% of web pages whose content fingerprint changed are processed again in the
  next run.
- **SC-003**: No item is taken into more than 3 runs while its content is unchanged.
- **SC-004**: A job's first run processes at most `baseline_items` items per source (10 by
  default), regardless of how many entries each source offers.
- **SC-005**: No run processes more than `max_items_per_run` items, and every item it
  processes is at least as new as every eligible item it left out.
- **SC-006**: Every acceptance scenario above is covered by an automated test that runs
  without network access or a production data store.

## Assumptions

- The issue's `scout/graph/nodes/deduplicate.py` maps to `src/invio/graph/nodes/deduplicate.py`
  in this repository (same naming translation as previous issues).
- `baseline_items` is added to the job's existing limits section of the job configuration,
  default 10, minimum 1; existing job files stay valid.
- Candidates are grouped by source when they reach the step (the pipeline state carries the
  source of each candidate), so baseline can be counted per source.
- Baseline mode filters only the new candidates reported by the sources of a job without a
  successful run; changed, retried and waiting items (reported or loaded from storage) are
  exempt from baseline.
- Only the content fingerprint decides whether a known page changed; title or teaser changes
  alone do not create a new version.
- Item status transitions after the run (e.g. setting failed, summarized) are handled by
  later pipeline steps, not by this feature.
- Depends on #4 (data store, item table with attempts and content fingerprint) and #5 (record
  access layer); the record access layer may be extended with the lookups and updates this
  step needs.
