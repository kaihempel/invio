---

description: "Task list for run persistence with token budget enforcement (#19)"
---

# Tasks: Run Persistence with Token Budget Enforcement

**Input**: Design documents from `specs/013-gh-issue-19/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/budget-and-persist.md](contracts/budget-and-persist.md),
[contracts/run-stats.md](contracts/run-stats.md), [quickstart.md](quickstart.md)

**Tests**: Included. The constitution (III) requires every acceptance criterion to be covered by
an automated test, including the rollback path. Write each story's tests first and see them fail.

**Organization**: Tasks are grouped by user story so each story can be implemented and tested on
its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story the task belongs to (US1–US4)

## Conventions used by every task

- Source in `src/invio/`, tests in `tests/`; nodes are flush-only, only `finalize_run` and
  `record_failed_run` commit.
- Tests use the scripted `FakeProvider` (`invio.llm.fake`, `FakeReply(text, Usage(in, out))`),
  the fixture registry in `tests/fixtures/llm/models.d` and the `db_session` fixture. Tests that
  commit (everything calling `finalize_run` / `record_failed_run`) use `db_engine` +
  `session_factory(db_engine)` + the `clean_jobs` fixture, because `db_session` is a savepoint
  session on MariaDB.
- Structured log events use `logger = logging.getLogger("invio.graph")` with `extra={...}`; never
  log document text, provider messages or the database URL.
- Test setup mirrors the real run order (research R7): the run row and the taken items
  (`RunRepository.start`, `ItemRepository.mark_taken`) are committed **before** the work session
  makes LLM calls, so a rolled-back save keeps the attempt increment.
- Quality gates after each phase: `uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Confirm a green baseline before changing shared plumbing.

- [X] T001 Run the full quality gates on the current tree (`uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest`) and note any pre-existing failures before touching `src/invio/graph/nodes/llm_calls.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The budget tracker with its usage ledger and the budget-aware call helper. Every
story depends on them: US1 replays the ledger, US2 enforces the budget, US3 reads the totals.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T002 [P] Write tests for the tracker in `tests/test_budget.py`: `BudgetTracker(0)` raises `ValueError`; `record()` sums `input_tokens + output_tokens` into `used` and appends to `ledger` in call order; `check()` passes while `used <= limit` and raises `BudgetExceeded` (with `used`, `limit`) once `used > limit`; `exceeded` latches `True` after the first failing `check()` and stays `True`; `record()` never raises even above the limit; `cost_usd` sums only known costs quantized to `Decimal("0.000001")`; `cost_complete` is `False` as soon as one entry has `cost_usd=None`; `calls`, `input_tokens`, `output_tokens` match the ledger
- [X] T003 [P] Create `src/invio/graph/budget.py` per contracts/budget-and-persist.md: frozen `UsageEntry(provider, model, purpose, input_tokens, output_tokens, cost_usd: Decimal | None, created_at: datetime)`, `BudgetExceeded(Exception)` carrying `used` and `limit`, and `BudgetTracker(limit)` with `check()`, `record(entry)`, read-only `ledger` (tuple), `used`, `exceeded`, `input_tokens`, `output_tokens`, `calls`, `cost_usd`, `cost_complete`; module docstring explains the `>` rule and that the ledger is the source for replay after a rollback (research R1, R2)
- [X] T004 [P] Add keyword `created_at: datetime | None = None` to `UsageRepository.add` in `src/invio/db/repositories.py` (default keeps `utcnow()`), and a test in `tests/test_repositories.py` (where the `UsageRepository` tests live) that an explicit `created_at` is stored
- [X] T005 Extend `CallContext` in `src/invio/graph/nodes/llm_calls.py` with `budget: BudgetTracker`; give `call_structured` a keyword `per_item: bool = True`; when `per_item` call `ctx.budget.check()` before the provider call (no provider call, no usage row when it raises); change `_record_usage` to build one `UsageEntry` (cost from `ctx.registry.cost`, `created_at=utcnow()`), write it with `ctx.usage.add(..., created_at=entry.created_at)` and `ctx.budget.record(entry)` — on success and on `LLMInvalidOutputError`; keep `BudgetExceeded` out of `PER_ITEM_ERRORS`; update the module docstring (depends on T003, T004)
- [X] T006 Add required field `budget: BudgetTracker` to `ScoringContext` in `src/invio/graph/nodes/relevance.py` and to `SummaryContext` in `src/invio/graph/nodes/summarize_item.py` (depends on T005)
- [X] T007 Update every context builder in tests to pass a tracker (`budget=BudgetTracker(1_000_000)` unless a test sets one): `_context` in `tests/test_relevance.py` and both `SummaryContext(...)` builders in `tests/test_summarize_item.py` (around lines 79 and 853), plus any other `CallContext` fakes found with `grep -rn "Context(" tests` (depends on T006)
- [X] T008 Add tests to `tests/test_llm_calls.py`: a per-item call over budget raises `BudgetExceeded` with zero `FakeProvider.requests` and no new `llm_usage` row; `per_item=False` runs even when `budget.exceeded`; after a valid call and after an invalid structured answer the tracker ledger has one entry equal (provider, model, purpose, tokens, cost) to the stored usage row (depends on T005, T007)

**Checkpoint**: Gates green; every LLM call is counted on the tracker and per-item calls are blocked once the budget is exceeded.

---

## Phase 3: User Story 1 - Consistent, all-or-nothing run results (Priority: P1) 🎯 MVP

**Goal**: A run's item updates, digest and final status/stats are committed together; on a
failed save everything is rolled back, the usage rows are written again and the run is
`failed` with a sanitized error.

**Independent Test**: quickstart scenarios 1 and 2 — full save commits items, digest, usage
rows and the run; an injected digest failure leaves no item change and no digest, keeps every
usage row, and marks the run `failed` with `finished_at`.

### Tests for User Story 1 ⚠️

> Write these first and make sure they fail before T011–T015.

- [X] T009 [P] [US1] Write committing tests in `tests/test_persist.py` (use `db_engine`, `session_factory`, `clean_jobs`): (a) success — two taken items summarized via `FakeProvider`, `DigestDraft` with both ids, `finalize_run` returns a non-`failed` status and logs one `run.persisted` event carrying `status`, the stage counts, `tokens` and `estimated_cost_usd` (use `caplog`); in a new session the digest exists with `run_id` and `item_ids`, items are `summarized`, one `llm_usage` row per call, run has `finished_at` and `stats["version"] == 1`; (b) rollback — inject a failure in the save (e.g. `DigestDraft` whose write violates a constraint, or monkeypatch `DigestRepository.add` to raise `sqlalchemy.exc.IntegrityError`): no digest, items back to their committed pre-run state, every ledger entry present as a usage row (same tokens, cost, `created_at`), run `failed`, `finished_at` set, `error` starts with the error class and contains neither SQL text nor document text, stats in the recovery layout of contracts/run-stats.md, and one `run.persist_failed` event with the error class only
- [X] T010 [P] [US1] Write tests in `tests/test_persist.py` for `record_failed_run` called directly (stage raised before the save): run `failed`, usage rows replayed, recovery stats layout; when the recovery transaction itself fails (monkeypatch `RunRepository.finish` to raise `sqlalchemy.exc.OperationalError`) it logs `run.record_failed_error` with the error class only and re-raises; and for `DigestDraft` validation: `persist_run` raises `ValueError` for an `item_ids` entry not in `taken`, while `finalize_run` turns the same draft into a `failed` run (`error` starts with `ValueError:`); a draft with empty `item_ids` (or `digest=None`) stores no digest

### Implementation for User Story 1

- [X] T011 [US1] Create `src/invio/graph/nodes/persist.py` with the dataclasses `StageCounts(found, new, after_keyword_filter)`, `DigestDraft(title, body, item_ids)` and `RunResult(job_id, run_id, counts, taken, relevance, summaries, digest, budget)` exactly as in contracts/budget-and-persist.md, plus a module docstring describing the transaction boundary (research R7)
- [X] T012 [US1] Implement `build_stats(result)` in `src/invio/graph/nodes/persist.py` with the token/cost/budget keys of contracts/run-stats.md (`version: 1`, `llm_calls`, `input_tokens`, `output_tokens`, `tokens`, `estimated_cost_usd` as a 6-place decimal string, `cost_complete`, `budget_limit`, `budget_exceeded`) taken from `result.budget`; a private helper `_ledger_stats(budget)` returns exactly the recovery-layout subset so `record_failed_run` can reuse it (stage counts are added in US3)
- [X] T013 [US1] Implement `decide_status(result)` in `src/invio/graph/nodes/persist.py` returning `RunStatus.SUCCEEDED` for now (rules are added in US2 and US4), and `persist_run(session, result)`: validate the digest ids against `taken` (`ValueError`), add the digest via `DigestRepository.add(job_id, title, body, item_ids, run_id=...)` only if `digest` is not `None` and `item_ids` is non-empty, then `RunRepository.finish(run, decide_status(result), stats=build_stats(result))`; flush only
- [X] T014 [US1] Implement `record_failed_run(factory, *, job_id, run_id, budget, error)` in `src/invio/graph/nodes/persist.py`: one `session_scope(factory)` that re-inserts every `budget.ledger` entry with `UsageRepository.add(..., run_id=run_id, purpose=..., cost_usd=..., created_at=...)` and finishes the run `failed` with `_ledger_stats(budget)` and a sanitized error: `failure_message(err)` for `LLMError`, otherwise `f"{type(err).__name__}: run failed"` (never `str(err)` of database errors, research R8); if this transaction fails, log `run.record_failed_error` with the error class and re-raise
- [X] T015 [US1] Implement `finalize_run(factory, session, result)` in `src/invio/graph/nodes/persist.py`: `persist_run` + `session.commit()`, log `run.persisted` with `status` and the full stats dict as fields (FR-012); on `Exception`: `session.rollback()`, log `run.persist_failed` (error class only), `record_failed_run(...)`, return `RunStatus.FAILED`; on `BaseException` that is not `Exception` (e.g. `KeyboardInterrupt`): roll back, run `record_failed_run`, re-raise

**Checkpoint**: US1 tests (T009, T010) pass; a run is saved all-or-nothing and failures keep usage.

---

## Phase 4: User Story 2 - Token budget stops runaway costs (Priority: P1)

**Goal**: Once `limits.max_llm_tokens_per_run` is exceeded no further per-item call starts; the
batches return what is finished, budget-skipped items are released, the digest call still runs
and the run ends `partial`.

**Independent Test**: quickstart scenarios 3–6 — with a 1,000-token budget and 600 tokens per
call exactly two relevance calls are made, no summary call, items 2–5 are released and the run
is `partial` with `skipped_budget == 4`.

### Tests for User Story 2 ⚠️

- [X] T016 [P] [US2] Add tests to `tests/test_relevance.py`: with `BudgetTracker(1000)` and `FakeReply(..., Usage(500, 100))` for every item, `score_items` over 5 items makes exactly 2 provider calls, returns 2 outcomes, leaves items 3–5 unchanged (status, `last_error`, `relevance`) and logs `budget.exceeded` once
- [X] T017 [P] [US2] Add tests to `tests/test_summarize_item.py`: a long body split into several chunks with a budget that runs out after the second chunk call — `summarize_items` returns no outcome for that item, the item is not `failed` and keeps its pre-call status, the chunk usage rows exist; a second item after it gets no call
- [X] T018 [P] [US2] Add tests to `tests/test_db_items.py` for `ItemRepository.release`: `attempts` decremented but never below 0, `run_id` cleared, `relevant`/`new` items become `new`, an untouched `failed` item stays `failed`, released items are returned again by `list_pending`
- [X] T019 [P] [US2] Add tests to `tests/test_persist.py` for budget runs (quickstart 3, 5, 6): `unprocessed(result)` returns taken items without a final state; after `finalize_run` released items have restored `attempts`, `run_id` NULL and status `new`; a digest call made with `per_item=False` after the stop is counted in `stats["tokens"]`; budget exceeded before any item finished stores no digest; status is `partial` in all three cases

### Implementation for User Story 2

- [X] T020 [P] [US2] Implement `ItemRepository.release(items)` in `src/invio/db/repositories.py`: per item `attempts = max(attempts - 1, 0)`, `run_id = None`, `status = ItemStatus.NEW` unless `status == ItemStatus.FAILED`; one flush; docstring cites research R4
- [X] T021 [P] [US2] Make `score_items` in `src/invio/graph/nodes/relevance.py` stop at the first `BudgetExceeded`, log `budget.exceeded` with `used` and `limit`, and return the outcomes completed so far in input order; update its docstring
- [X] T022 [P] [US2] Make `summarize_items` in `src/invio/graph/nodes/summarize_item.py` stop the same way; ensure `summarize_item` lets `BudgetExceeded` propagate from any chunk/combine call without calling `_fail` (item left unchanged); update the module and function docstrings
- [X] T023 [US2] Implement `unprocessed(result)` in `src/invio/graph/nodes/persist.py`: taken items that are not `skipped_keyword` and have no relevance outcome of `skipped_irrelevant`/`failed` and no summary outcome of `summarized`/`failed`; in `persist_run` call `ItemRepository.release(unprocessed(result))` only when `result.budget.exceeded`, before the digest write (depends on T020)
- [X] T024 [US2] Extend `decide_status` in `src/invio/graph/nodes/persist.py`: `result.budget.exceeded` → `RunStatus.PARTIAL` (depends on T013)

**Checkpoint**: US1 and US2 tests pass; the budget caps per-item calls and the run ends `partial`.

---

## Phase 5: User Story 3 - Traceable run statistics and estimated cost (Priority: P2)

**Goal**: `runs.stats` holds every count and cost figure of contracts/run-stats.md, with cost
from the model registry prices.

**Independent Test**: quickstart scenarios 9–11 — known outcome mix gives the expected counts;
an unpriced model sets `cost_complete: false`; stats totals equal `UsageRepository.totals_for_run`.

### Tests for User Story 3 ⚠️

- [X] T025 [P] [US3] Add tests to `tests/test_persist.py`: `build_stats` for a fixed `RunResult` (counts `found`, `new`, `after_keyword_filter` from `StageCounts`; `relevant`, `summarized`, `failed`, `skipped_budget` from outcomes and `unprocessed`); `tokens == input_tokens + output_tokens`; `estimated_cost_usd` equals the sum of `ModelRegistry.cost` per call from the fixture registry; a model missing from the registry gives `cost_complete: false` and a cost of the priced calls only; after `finalize_run` the stored token and cost totals equal `UsageRepository.totals_for_run(run_id)`; a run with no taken items has zero tokens and `"0.000000"` cost

### Implementation for User Story 3

- [X] T026 [US3] Complete `build_stats` in `src/invio/graph/nodes/persist.py` with `found`, `new`, `after_keyword_filter` (from `result.counts`), `relevant` (relevance outcomes with status `relevant`), `summarized` (summary outcomes with status `summarized`), `failed` (distinct item ids failed in either stage), `skipped_budget` (`len(unprocessed(result))` when the budget was exceeded, else 0), keeping the key order of contracts/run-stats.md (depends on T023)

**Checkpoint**: Run records alone show what a run found, processed, failed and cost.

---

## Phase 6: User Story 4 - Accurate run status (Priority: P2)

**Goal**: The final status follows FR-010 exactly and no run stays `running`.

**Independent Test**: table test over the outcomes all-ok, one item failed, all items failed,
budget stop with and without successes, no items, failed save.

### Tests for User Story 4 ⚠️

- [X] T027 [P] [US4] Add a parametrized `decide_status` table test to `tests/test_persist.py`: all succeeded → `succeeded`; no items → `succeeded`; one of three failed → `partial`; every attempted item failed and budget not exceeded → `failed`; budget exceeded with zero successes → `partial`; budget exceeded and one failure → `partial`; plus a committing test that an all-failed run is saved via the normal path (item failures committed, status `failed`, stage counts present, `error` NULL) and that every `finalize_run` path leaves `finished_at` set and status ≠ `running`

### Implementation for User Story 4

- [X] T028 [US4] Complete `decide_status` in `src/invio/graph/nodes/persist.py` in FR-010 order: budget not exceeded and `attempted > 0` and every attempted item failed → `FAILED`; budget exceeded or any failure → `PARTIAL`; else `SUCCEEDED`; `attempted` = distinct item ids with a relevance or summary outcome (depends on T024)

**Checkpoint**: All four stories pass independently.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T029 [P] Update the README section `## Database` (or `## LLM layer`) in `README.md`: run statuses and their rules, the `runs.stats` keys (link `specs/013-gh-issue-19/contracts/run-stats.md`), the token budget behaviour (`limits.max_llm_tokens_per_run`, per-item stop, digest call allowed, items released), and the known limitation that released items are rated again
- [X] T030 [P] Check `tests/test_graph_layering.py` still passes with `invio.graph.budget` and `invio.graph.nodes.persist` (no import of `invio.cli`/`invio.scheduling`); extend its module list if it enumerates modules explicitly
- [X] T031 Run every scenario of `specs/013-gh-issue-19/quickstart.md` (including `uv run pytest -m db tests/test_persist.py` on MariaDB when `INVIO_TEST_DATABASE_URL` is available) and the full quality gates; fix any `mypy --strict` findings without new `Any` beyond the stats dict

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: after Setup; blocks every story.
- **US1 (Phase 3)**: after Phase 2. MVP.
- **US2 (Phase 4)**: after Phase 2; T023/T024 extend `persist.py` from US1 (T011–T013), so run
  after US1 or coordinate edits to `persist.py`. T020–T022 are independent of US1.
- **US3 (Phase 5)**: after US1 (`build_stats`) and T023 (`unprocessed`).
- **US4 (Phase 6)**: after US1 (`decide_status`) and T024.
- **Polish (Phase 7)**: after the stories you ship.

### Within Each Story

- Tests first (they fail), then implementation in the listed order; `persist.py` tasks are
  sequential because they edit one file.

### Parallel Opportunities

- Phase 2: T002, T003, T004 together.
- US1: T009 and T010 together (same file, separate test functions — write in one pass if one
  person edits the file).
- US2: T016, T017, T018 in parallel (different test files); T020, T021, T022 in parallel
  (different source files).
- US3 and US4 tests (T025, T027) can be drafted in parallel once US2 is done.
- Polish: T029 and T030 in parallel.

---

## Parallel Example: User Story 2

```bash
# Tests in parallel (different files):
Task: "T016 budget stop tests in tests/test_relevance.py"
Task: "T017 budget stop tests in tests/test_summarize_item.py"
Task: "T018 release() tests in tests/test_db_items.py"

# Implementation in parallel (different files):
Task: "T020 ItemRepository.release in src/invio/db/repositories.py"
Task: "T021 score_items budget stop in src/invio/graph/nodes/relevance.py"
Task: "T022 summarize_items budget stop in src/invio/graph/nodes/summarize_item.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 + Phase 2 (tracker, ledger, budget-aware calls).
2. Phase 3 (US1): all-or-nothing save with recovery.
3. **Stop and validate**: quickstart scenarios 1–2.

### Incremental Delivery

1. + US2 → cost cap active (scenarios 3–6).
2. + US3 → full statistics (scenarios 9–11).
3. + US4 → final status rules (scenarios 7–8).
4. Polish → README, layering, full quickstart.

### Notes

- Commit after each task or logical group on branch `gh-issue-19`.
- Wiring these functions into the LangGraph workflow is #21; the digest node (#18) must call
  `call_structured(..., per_item=False)`.
- FR-011 "any path": this issue delivers `finalize_run` (save path) and `record_failed_run`
  (stage-raised path). #21 must call `record_failed_run` when any stage raises, and must commit
  the run start and `mark_taken` before the LLM stages (research R7); add both to #21's
  acceptance criteria.
