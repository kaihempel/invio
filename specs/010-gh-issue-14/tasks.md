---

description: "Task list for issue #14 — deduplication, baseline mode and run limits"
---

# Tasks: Per-Job Deduplication, Baseline Mode and Run Limits

**Input**: Design documents from `specs/010-gh-issue-14/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/python-api.md,
quickstart.md

**Tests**: Included — required by constitution principle III and SC-006 (every acceptance
scenario covered, rejection paths included, no network). Write each story's tests first and
confirm they fail before implementing.

**Organization**: Tasks are grouped by user story (spec.md) in priority order:
US1 (P1) → US4 (P1) → US2 (P2) → US3 (P2) → US5 (P2).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1…US5)

## Path Conventions

Single package: source in `src/invio/`, tests in `tests/`, docs in `docs/`. Run all commands
with `uv run` from the repository root.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Work on the issue branch and create the new package so the node module has a home

- [X] T000 Work on branch `gh-issue-14` (constitution "Development Workflow"): `git switch gh-issue-14` (create with `git switch -c gh-issue-14` if missing); never commit to `main` or `worktree-track-db`
- [X] T001 Create package `src/invio/graph/nodes/__init__.py` with a one-line module docstring (`"""Pipeline nodes of the research graph."""`), mirroring `src/invio/graph/__init__.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Config field, repository queries and the node skeleton every story builds on

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Tests for Foundational

- [X] T002 [P] Add tests for `LimitsConfig.baseline_items` in `tests/test_job_sections.py`: add `"baseline_items"` after `"max_items_per_run"` to the `field` parametrize list of `test_limits_rejected` (~line 47, covers `0`, `-1`, `1.5`); add a test for default `10`, acceptance of `1`, rejection of the string `"10"` with an error naming `limits.baseline_items`; existing job files without the key stay valid
- [X] T003 [P] Add `db`-marked repository tests in `tests/test_repositories.py` (item section): `ItemRepository.find_many(job_id, url_hashes)` returns only that job's items keyed by `url_hash`, returns `{}` for an empty collection, works across more than 500 hashes (chunking); `list_waiting(job_id)` returns only items with status `new` and `attempts == 0`, ordered by id; `mark_taken(items, run_id)` sets `run_id` and increments `attempts` by exactly one per item; `mark_skipped(item, ItemStatus.SKIPPED_IRRELEVANT, reason="baseline")` sets status and `last_error="baseline"`; `reset_version(item, candidate)` stores the candidate's `content_hash`, `title`, `teaser`, `published_at` and sets status `new`, `attempts=0`, `last_error=None`, `run_id=None`
- [X] T004 [P] Add `db`-marked tests for `RunRepository.has_successful_run(job_id)` in `tests/test_repositories.py` (run section): `False` without runs, with only `running`/`failed` runs and for another job's `succeeded` run; `True` with a `succeeded` run and with a `partial` run

### Implementation for Foundational

- [X] T005 [P] Add `baseline_items: StrictInt = Field(default=10, ge=1)` to `LimitsConfig` directly after `max_items_per_run` in `src/invio/config/job.py` (docstring "Per-run caps" stays; per source, first runs only)
- [X] T006 [P] Implement `find_many` (one `SELECT … WHERE job_id = :job AND url_hash IN (…)` per chunk of 500 hashes, returns `dict[str, Item]`), `list_waiting`, `reset_version`, `mark_taken` and `mark_skipped` on `ItemRepository` in `src/invio/db/repositories.py`; signatures exactly as in `specs/010-gh-issue-14/contracts/python-api.md`; every write flushes and never commits
- [X] T007 Implement `RunRepository.has_successful_run(job_id) -> bool` as an `EXISTS` query over `status IN (RunStatus.SUCCEEDED, RunStatus.PARTIAL)` in `src/invio/db/repositories.py` (after T006 — same file)
- [X] T008 Create `src/invio/graph/nodes/deduplicate.py` with module docstring, `__all__`, `MAX_ATTEMPTS: Final = 3`, `BASELINE_REASON: Final = "baseline"`, `SelectReason = Literal["new", "changed", "retry", "waiting"]`, frozen kw-only slotted dataclasses `SelectedItem`, `DedupStats`, `DedupResult` (fields exactly per contracts/python-api.md) and the `deduplicate(session, *, job_id, run_id, candidates: Mapping[str, Sequence[Candidate]], limits: LimitsConfig) -> DedupResult` signature with docstring; body: flatten candidates in mapping order then position, keep the first occurrence per `url_hash`, look up known items with `ItemRepository.find_many`, and build a private `_Entry` record (candidate, stored item or `None`, source index, position) per unique candidate; also add a private `_newest_key` sort helper implementing research R6: `(published_at is None, -published_at timestamp, origin 0=reported/1=stored, source_index, position or item id)`
- [X] T009 Propagate `baseline_items` (after T005): (a) regenerate `docs/job.schema.json` with `uv run python -m invio.config.job`; (b) add `baseline_items: 10` after `max_items_per_run` in `docs/job.example.yaml`; (c) add `("baseline_items", "Max baseline items per source (first run)")` after the `max_items_per_run` entry of `_LIMIT_QUESTIONS` in `src/invio/cli/wizard.py` — the label MUST start with `"Max "` because tests filter prompts by that prefix; (d) in `tests/test_job_yaml.py` add `"baseline_items": 10` after `"max_items_per_run": 100` in both full limits dicts (~lines 265 and 563) and `"baseline_items"` after `"max_items_per_run"` in the key-order list of `test_dump_nested_key_order` (~line 590); (e) in `tests/test_wizard.py` `test_limits_no_asks_four_values_prefilled` (~line 233): rename to `…_five_values_…`, extend the script to `limits=[False, "", "", "", "", "7"]`, expect 5 limit questions and defaults `["20", "100", "10", "20", "200000"]`; also add one `""` to the retry script at ~line 219 (`limits=[False, bad, "5", "", "", "", ""]`) so all five prompts are answered

**Checkpoint**: `uv run pytest tests/test_job_sections.py tests/test_job_schema.py tests/test_repositories.py` passes; the node imports and type-checks

---

## Phase 3: User Story 1 - Process each item only once per job (Priority: P1) 🎯 MVP

**Goal**: Unknown candidates are stored and selected; known ones are dropped; dedupe is per job

**Independent Test**: Call `deduplicate` twice for a job that has a `succeeded` run with identical candidates — first call returns all, second returns `[]`; the same candidates for a second job are returned again

### Tests for User Story 1

- [X] T010 [P] [US1] Create `tests/test_graph_deduplicate.py` (`pytestmark = pytest.mark.db`) with helpers `_job_with_success(session, name)` (job + one `succeeded` run so baseline is off), `_current_run(session, job)` (run in status `running`) and `_limits(**kw) -> LimitsConfig`, using `make_job`/`make_run`/`make_candidate` from `tests/db_helpers.py`; add tests for US1 AS1 (all unknown candidates stored and returned, `reason="new"`, `attempts == 1`, `run_id` = current run), AS2/SC-001 (second identical call returns `[]`, no new rows, stats `dropped == found`), AS3 (same URL for job A and job B selected for both), AS4 (same URL twice in one source and across two sources → one row, selected once, `found` counts it once), empty input with no waiting items returns `[]` and changes nothing, and a concurrent-insert case where `ItemRepository.add` returns `created=False` for a candidate classified new (monkeypatch) → not selected, counted as dropped

### Implementation for User Story 1

- [X] T011 [US1] In `src/invio/graph/nodes/deduplicate.py` classify each `_Entry`: no stored item → `new`; stored item → `drop` (later stories add more branches before the drop fallback); insert `new` entries via `ItemRepository.add(job_id, candidate)` and treat `created=False` as dropped (research R8); select all eligible entries sorted by `_newest_key`; call `ItemRepository.mark_taken(selected, run_id)`; build `SelectedItem`s from the stored items after the update and `DedupStats` (`found`, `new`, `dropped`, `selected`, others 0, `baseline=False`)
- [X] T012 [US1] Emit one structured log line in `src/invio/graph/nodes/deduplicate.py`: `logger = logging.getLogger(__name__)`; `logger.info("deduplicated", extra={"event": "deduplicate", **counts, "baseline": stats.baseline})` with every `DedupStats` count and no URLs/titles; add a test with `caplog` in `tests/test_graph_deduplicate.py` asserting exactly one record with the counts (FR-016)

**Checkpoint**: US1 tests pass — repeated runs no longer reprocess items

---

## Phase 4: User Story 4 - Start a new job with a small baseline (Priority: P1)

**Goal**: A job without a `succeeded`/`partial` run keeps only the newest `baseline_items` per source; the rest are stored as `skipped_irrelevant` with `last_error="baseline"`

**Independent Test**: No successful run, source A with 25 dated candidates, source B with 5, `baseline_items=10` → 10 newest of A + 5 of B returned; 15 of A stored as skipped with reason `baseline`; a second call returns `[]`

### Tests for User Story 4

- [X] T013 [P] [US4] Add tests to `tests/test_graph_deduplicate.py`: US4 AS1 + SC-004 (25/5 split above; check `stats.baseline is True`, `baseline_skipped == 15`, skipped rows have status `skipped_irrelevant`, `last_error == "baseline"`, `attempts == 0`, `run_id is None`; rerun returns `[]`), AS2 (source with ≤ `baseline_items` keeps all), AS3 (job with a `succeeded` run: no baseline), a `partial` run also ends baseline, AS4 (only `failed` earlier runs and the current `running` run → baseline still applies), undated candidates lose against dated ones within a source, and ties keep source order (FR-014)

### Implementation for User Story 4

- [X] T014 [US4] In `src/invio/graph/nodes/deduplicate.py` call `RunRepository.has_successful_run(job_id)` once; baseline runs AFTER changed versions have been reset (T016) so a baseline skip is never overwritten by a reset; when `False`, group eligible reported entries by source index, sort each group by `_newest_key`, keep the first `limits.baseline_items` and mark the rest: unknown ones are inserted via `ItemRepository.add` then `mark_skipped(item, ItemStatus.SKIPPED_IRRELEVANT, reason=BASELINE_REASON)`, known eligible ones get `mark_skipped` directly (attempts unchanged); count them in `baseline_skipped` and set `stats.baseline=True`

**Checkpoint**: US1 + US4 tests pass — the MVP (no reprocessing, no archive flood) is complete

---

## Phase 5: User Story 2 - Pick up changed web pages again (Priority: P2)

**Goal**: A known item whose `content_hash` differs (both present) is reset and selected as a new version

**Independent Test**: Stored web item with hash X; candidate with hash Y → returned with `reason="changed"`, stored hash Y; same candidate again → dropped

### Tests for User Story 2

- [X] T015 [P] [US2] Add tests to `tests/test_graph_deduplicate.py`: US2 AS1/SC-002 (X→Y returned, `reason="changed"`, stored `content_hash == Y`, `attempts == 1`, `last_error is None`), AS2 (same hash → dropped), AS3 (stored hash `None` or candidate hash `None` → dropped), a stored `failed` item with `attempts == 3` and a changed hash is returned (fresh start), and a stored `summarized` item with a changed hash is returned

### Implementation for User Story 2

- [X] T016 [US2] In `src/invio/graph/nodes/deduplicate.py` add the `changed` branch as the first check for known items (both `content_hash` values not `None` and different); call `ItemRepository.reset_version(item, candidate)` for every changed entry right after classification and BEFORE baseline and the run limit (so a later baseline skip sets `skipped_irrelevant` on top of the reset, and changed entries cut by the run limit stay waiting work — research R7); count in `stats.changed`; add a test that a changed item rejected by baseline ends as `skipped_irrelevant` with `last_error == "baseline"` and the new hash

**Checkpoint**: Changed web pages are reprocessed exactly once per new version

---

## Phase 6: User Story 3 - Retry failed items a limited number of times (Priority: P2)

**Goal**: Known items with status `failed`, or `new` with `attempts ≥ 1` (interrupted run), are retried while `attempts < 3`

**Independent Test**: Mark an item `failed` after each call and call again with the same candidate — it is selected in 3 calls in total and dropped from the 4th

### Tests for User Story 3

- [X] T017 [P] [US3] Add tests to `tests/test_graph_deduplicate.py`: US3 AS1–AS3/SC-003 (loop: call, assert selected and `attempts` rose by one, set status `failed`; 4th call drops it), AS4 (status `new` with `attempts` 1 or 2 → `reason="retry"`; with 3 → dropped), AS5 (statuses `summarized`, `relevant`, `extracted`, `skipped_keyword`, `skipped_irrelevant` with unchanged hash → dropped; parametrize), and a retried `failed` item keeps status `failed` after selection

### Implementation for User Story 3

- [X] T018 [US3] In `src/invio/graph/nodes/deduplicate.py` add the `retry` branch after `changed` (it is mutually exclusive with the `waiting` check of T020 — attempts ≥ 1 vs 0 — so their relative order does not matter): `item.attempts < MAX_ATTEMPTS and (item.status == ItemStatus.FAILED or (item.status == ItemStatus.NEW and item.attempts >= 1))`; count in `stats.retried`; status stays unchanged when selected

**Checkpoint**: Failed and interrupted items are retried at most 3 times

---

## Phase 7: User Story 5 - Cap the size of a run (Priority: P2)

**Goal**: At most `max_items_per_run` items are selected, newest first; items that don't fit become waiting work that later runs pick up from storage

**Independent Test**: `max_items_per_run=5`, 8 new candidates with distinct dates → 5 newest selected, `limit_cut == 3`; next call with empty input returns the 3 cut items

### Tests for User Story 5

- [X] T019 [P] [US5] Add tests to `tests/test_graph_deduplicate.py`: US5 AS1/SC-005 (5 newest of 8, every selected item at least as new as every cut one), AS2 (≤ limit → all), AS3 (cut items stored with status `new`, `attempts == 0`, `run_id is None`), AS4 (next call with empty input returns them with `reason="waiting"`; with fresh candidates they compete by date), a waiting item reported again by a source is counted once, stored-only waiting items are not subject to baseline (job without successful run), a cut changed item is stored reset (new hash, `new`, 0 attempts), and a cut retry item keeps its status and attempts; also check the `DedupStats` invariants from data-model.md

### Implementation for User Story 5

- [X] T020 [US5] In `src/invio/graph/nodes/deduplicate.py` add the `waiting` branch for known items (`status == ItemStatus.NEW and attempts == 0`, checked after `changed`, before `retry`); after baseline, load `ItemRepository.list_waiting(job_id)`, add those whose `url_hash` no source reported (origin = stored, tie-break by item id, never baseline-filtered); sort all eligible entries by `_newest_key`, select the first `limits.max_items_per_run`, leave the rest unselected (unknown ones are still inserted as `new`/0 attempts, `run_id=None`); fill `stats.waiting` and `stats.limit_cut`; ensure `mark_taken` is only called for selected items

**Checkpoint**: All five stories pass independently and together

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Gates, determinism and docs

- [X] T021 [P] Add a determinism test (FR-015) in `tests/test_graph_deduplicate.py`: two fresh jobs with identical stored state and input yield identical `SelectedItem` sequences (ignoring ids) and stats
- [X] T022 [P] Review `src/invio/graph/nodes/deduplicate.py` for clarity: keep the classification in one small private function returning the class, keep `deduplicate` as the readable sequence of steps from research R7, no `Any`, narrow `# type: ignore` only with a justification comment
- [X] T023 Run quality gates: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest --cov` (coverage ≥ 95 %); fix findings
- [X] T024 Walk through `specs/010-gh-issue-14/quickstart.md` scenarios 1–12 and confirm each maps to a passing test; tick the issue's acceptance criteria in the PR description and justify "no new dependency" / "no migration" there

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: T000 first (branch), then T001
- **Foundational (Phase 2)**: depends on Setup — blocks all user stories
- **US1 (Phase 3)**: depends on Foundational; provides insert/select/mark_taken flow the other stories extend
- **US4, US2, US3, US5 (Phases 4–7)**: depend on US1 (same function); otherwise independent of each other, but all edit `deduplicate.py`, so implement them sequentially
- **Polish (Phase 8)**: after all stories

### User Story Dependencies

- **US1 (P1)**: base flow — no story dependency
- **US4 (P1)**: needs US1's flow; independent of US2/US3/US5
- **US2 (P2)**: needs US1; independent of others
- **US3 (P2)**: needs US1; branch order relative to US5's `waiting` check documented in T018/T020
- **US5 (P2)**: needs US1; interacts with US2 (cut changed items reset) and US4 (stored waiting items skip baseline) — tests cover both

### Within Each User Story

- Tests first (must fail), then implementation, then checkpoint run
- Classification branch order for known items: `changed` → `waiting` → `retry` → `drop`

### Parallel Opportunities

- Phase 2: T002, T003, T004 (different test areas) in parallel; T005 and T006 in parallel (different files), T007 after T006; T009 after T005
- Story test tasks (T010, T013, T015, T017, T019) touch the same test file but independent test functions — can be drafted in parallel and merged
- Polish: T021 and T022 in parallel

---

## Parallel Example: Foundational

```bash
Task: "T002 baseline_items config tests in tests/test_job_sections.py"
Task: "T003 ItemRepository additions tests in tests/test_repositories.py"
Task: "T004 has_successful_run tests in tests/test_repositories.py"
Task: "T005 LimitsConfig.baseline_items in src/invio/config/job.py"
Task: "T006 ItemRepository additions in src/invio/db/repositories.py"
```

## Parallel Example: User Story 1

```bash
Task: "T010 US1 tests in tests/test_graph_deduplicate.py"
# then sequentially in src/invio/graph/nodes/deduplicate.py:
Task: "T011 classify new/drop, insert, select, mark_taken"
Task: "T012 structured summary log + caplog test"
```

---

## Implementation Strategy

### MVP First (US1 + US4)

1. Phases 1–2 (setup, config, repository queries, node skeleton)
2. Phase 3 (US1): no reprocessing — validate
3. Phase 4 (US4): first runs don't flood — validate
4. Stop here for a usable MVP if needed

### Incremental Delivery

1. Add US2 (changed pages) → test
2. Add US3 (retries) → test
3. Add US5 (run cap + waiting backlog) → test
4. Polish and gates → PR referencing #14

---

## Notes

- [P] = different files, no dependencies on incomplete tasks
- All writes go through repositories; the node never commits (caller owns `session_scope`)
- ORM objects never leave `deduplicate`; return `SelectedItem` records only
- Commit after each phase on branch `gh-issue-14` (T000)
