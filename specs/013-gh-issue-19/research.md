# Research: Run Persistence with Token Budget Enforcement

Feature: [spec.md](spec.md) · Plan: [plan.md](plan.md)

## R1 — Where usage rows are written, and how they survive a failed save

**Decision**: Usage rows keep being written by `call_structured` through the run's work
session (flush-only, as today), so they commit atomically with the run's results on success.
In addition every call's usage is appended to an in-memory ledger on the run's
`BudgetTracker`. When the atomic save fails (or the run fails before the save), the work
session is rolled back and a *recovery* step opens a fresh session that re-inserts every
ledger entry and marks the run `failed` with the ledger totals, in one small transaction.

**Rationale**: SQLite (development and all `db` tests) allows a single writer. The run's work
session flushes item updates (relevance, failures, summaries) long before the save, so it holds
the database write lock for the whole run. Committing each usage row from a second session
would block on that lock and fail with "database is locked". Replaying from a ledger keeps a
single writer at any moment, behaves identically on SQLite and MariaDB, and satisfies the
clarification (usage survives a failed save). The `llm.call` / `llm.error` log events already
carry tokens and cost per call, so a killed process still leaves a record (spec FR-001a).

**Alternatives considered**:
- *Separate committing session per call* — rejected: deadlocks on SQLite; on MariaDB it works
  but would make the two backends behave differently.
- *Commit each item as it finishes* — rejected: breaks the all-or-nothing save (FR-001).
- *Savepoint around the results* — rejected: usage rows are inserted between item updates, so
  rolling back the savepoint removes them too.

## R2 — Budget tracker shape and where the check happens

**Decision**: New module `invio/graph/budget.py` with `BudgetTracker(limit)` and
`BudgetExceeded(Exception)`. `record(entry)` adds one call's input + output tokens and its
ledger entry; `check()` raises `BudgetExceeded` when `used > limit` and latches `exceeded`.
`call_structured` gets a keyword `per_item: bool = True`: for per-item calls it calls
`ctx.budget.check()` *before* the provider call; after every call (also an invalid answer) it
records usage on the tracker and as a row. The digest node (#18) passes `per_item=False`, so its
single call is never blocked (clarification 2) but still counted.

**Rationale**: Every provider call already goes through `call_structured`, so one hook covers
relevance, short summaries, chunk maps and combine calls without touching each node's control
flow. Checking before the call bounds the overshoot to the one call in flight (calls are
sequential) plus the digest call (SC-003). `>` follows the issue wording ("above
max_tokens_per_run").

**Alternatives considered**:
- *Tracker inside graph state* — rejected for now: there is no assembled graph yet (#21); a
  tracker on the node context works for both direct calls and the later graph.
- *Check only between items* — rejected: a long video transcript can need 20+ chunk calls; the
  check must stop inside an item too.

## R3 — What a budget stop does inside the batch nodes

**Decision**: `BudgetExceeded` is not an `LLMError`, so the per-item `except PER_ITEM_ERRORS`
handlers never catch it: the item being processed is left untouched (not failed). The batch
functions `score_items` and `summarize_items` catch `BudgetExceeded`, log `budget.exceeded`
once and return the outcomes collected so far. The caller derives the unprocessed items as
"taken but without a completed outcome" via `persist.unprocessed(...)`.

**Rationale**: Keeps the existing `list[Outcome]` return types (no churn for current callers
and tests) while letting a stopped stage hand its finished work to the digest step (FR-005).

**Alternatives considered**: a new `skipped_budget` outcome per remaining item — rejected:
needs the item list up front and duplicates what the tracker already knows.

## R4 — Releasing budget-skipped items

**Decision**: New `ItemRepository.release(items)`: `attempts -= 1` (never below 0),
`run_id = None`, and `status = new` unless the item is still `failed` (a retry that was taken
but not touched yet). Called by the persist step inside the atomic save.

**Rationale**: Items are taken with `mark_taken` (attempts + 1, run linked). `list_pending` only
selects `new` and `failed` items, so an item already scored `relevant` but not summarized would
never be selected again if its status stayed `relevant`. Resetting to `new` makes it pending
again and restoring attempts keeps the retry limit meaning "this item kept failing"
(clarification 3). Its relevance is re-rated by the next run — the tokens for that rating are
spent twice; accepted and documented as a known limitation.

**Alternatives considered**: widening `list_pending` to `relevant` items — rejected: changes
deduplication/selection rules owned by #13 and would skip re-checking content of changed items.

## R5 — Run statistics contents and source of the figures

**Decision**: `runs.stats` stores a versioned JSON object (see
[contracts/run-stats.md](contracts/run-stats.md)): `version`, the stage counts `found`, `new`,
`after_keyword_filter`, `relevant`, `summarized`, `failed`, `skipped_budget`, the token figures
`input_tokens`, `output_tokens`, `tokens`, `llm_calls`, the cost figures `estimated_cost_usd`
(decimal string, 6 places) and `cost_complete`, and the budget figures `budget_limit` and
`budget_exceeded`. `found` and `new` come from `DedupStats`; `after_keyword_filter` from the
keyword stage; `relevant`, `summarized`, `failed` from the outcomes; token and cost figures from
the tracker ledger.

**Rationale**: The constitution requires versioned formats. The ledger holds exactly the rows
written for the run, so its totals equal `UsageRepository.totals_for_run` by construction
(FR-009, verified in tests). `cost_complete` is false when any call's model has no registry
price (`ModelRegistry.cost` returns `None`), as clarified in the spec (FR-008). Cost is a
string to avoid float rounding in JSON.

## R6 — Final status decision

**Decision**: Pure function `decide_status(result)` in `persist.py` for a run that reaches the
save; the error path is always `failed` (set by `record_failed_run`). Order: error → `failed`; budget not exceeded and `attempted > 0` and
`failed == attempted` → `failed`; budget exceeded or `failed > 0` → `partial`; else
`succeeded`. `attempted` = items with at least one completed per-item outcome.

**Rationale**: Mirrors FR-010 one to one and is trivially table-testable.

## R7 — Shape of the persist step and who owns the transaction

**Decision**: `persist.py` exposes:
- `build_stats(...)` and `decide_status(...)` — pure.
- `persist_run(session, result) -> Run` — flush-only writes (release skipped items, add digest
  if any, finish the run with status and stats) in the caller's work session.
- `finalize_run(factory, session, result) -> RunStatus` — calls `persist_run`, commits; on any
  exception rolls back and calls `record_failed_run`.
- `record_failed_run(factory, *, run_id, job_id, tracker, error)` — fresh `session_scope`:
  re-inserts the ledger usage rows, finishes the run `failed` with stats holding the ledger
  totals and a sanitized error. Also used by the orchestrator (#21) when a stage raises before
  the save.

**Transaction start**: the orchestrator (#21) MUST commit the run row and the deduplication
result (`mark_taken`: attempts + 1, `run_id`) in their own transaction *before* the LLM stages
open the work session. Otherwise a failed save would also roll back the attempt increment, and
an item that makes every save fail would be retried forever instead of reaching
`max_attempts`. After a rolled-back save the taken items are `new` with `attempts >= 1`, which
`list_pending` already treats as retryable.

**Rationale**: Nodes stay flush-only (existing convention); only the finalize step commits, which
keeps the all-or-nothing boundary in one place and makes the rollback test a single failure
injection (e.g. a digest write rejected by the database).

**Alternatives considered**: wiring into a LangGraph node now — deferred to #21, which assembles
the graph; the functions above are what that node will call.

## R8 — Error text and logging

**Decision**: The run's `error` uses `failure_message(err)` for LLM errors (error class +
structured facts, never provider text). For database errors (`SQLAlchemyError`) and any other
exception it stores the error class plus a fixed phrase only (`"IntegrityError: run failed"`; the phrase
is neutral because it covers a failing stage as well as a failing save): SQLAlchemy messages quote the statement and its parameters, which can contain
document text or the database URL. Structured events: `budget.exceeded` (used, limit), `run.persisted` (status, counts,
tokens, cost), `run.persist_failed` (error class), `run.rollback_failed`, `run.record_failed_error` (error
class). `budget.exceeded` is logged once, by the stage that first hits the limit. All inside the existing run log context
(`job`, `run_id`).

## Known limitations

- Items released after a budget stop are re-rated by the next run (R4).
- If the process is killed mid-run, usage is only in the logs (R1); the run row stays `running`
  until the stale-run handling of #23 closes it.
