# Implementation Plan: Run Persistence with Token Budget Enforcement

**Branch**: `gh-issue-19` | **Date**: 2026-10-06 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/013-gh-issue-19/spec.md`

## Summary

Make every run traceable and cost-capped. A per-run `BudgetTracker` (new `invio.graph.budget`)
is attached to the node contexts; `call_structured` checks it before every per-item provider
call and records every call's tokens and cost on it (and, as today, as an `llm_usage` row). When
the budget (`limits.max_llm_tokens_per_run`) is exceeded, `BudgetExceeded` stops the relevance
and summarization batches without failing the item in progress; the digest call (#18) passes
`per_item=False` and always runs. A new `invio.graph.nodes.persist` module builds the versioned
run statistics, decides the final status (FR-010), releases budget-skipped items (attempt undone,
back to `new`), stores the digest and finishes the run — all flushed in the run's work session
and committed once. If that commit (or an earlier stage) fails, the session is rolled back and a
recovery step re-inserts the usage rows from the tracker's in-memory ledger and marks the run
`failed` with a sanitized error. No migration; LangGraph wiring follows in #21.

## Technical Context

**Language/Version**: Python 3.12+

**Primary Dependencies**: SQLAlchemy 2.x (existing repositories, `session_scope`), Pydantic v2
(existing job config), existing `invio.llm` layer (`Usage`, `ModelRegistry.cost`). No new
dependencies.

**Storage**: existing `runs` (`status`, `stats` JSON, `error`, `finished_at`), `items`,
`digests`, `llm_usage`; no schema change.

**Testing**: pytest with the scripted `FakeProvider`, `db_session` fixture (SQLite in-memory,
optional MariaDB); failure injection for the rollback path; no network.

**Target Platform**: Linux server, unattended cron/systemd runs.

**Project Type**: CLI tool / pipeline library (`src/invio`).

**Performance Goals**: negligible overhead — one integer comparison per LLM call; one extra
bulk write only on the failure path.

**Constraints**: single DB writer at a time (SQLite, research R1); calls are sequential, so
overshoot ≤ one per-item call + the digest call; no secrets or document text in `runs.error`.

**Scale/Scope**: ≤ `max_items_per_run` (default 100) items and a few hundred LLM calls per run;
ledger kept in memory.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
|---|---|---|
| I. Strict contracts | Run stats are a versioned JSON layout (`version: 1`, contracts/run-stats.md); budget limit comes from the validated job config; `BudgetTracker` rejects `limit < 1` | PASS |
| II. CLI-first | No new command; the run status drives the exit code of `invio job run` (#22). Nothing here prints to stdout | PASS (n/a) |
| III. Test-covered | Every acceptance scenario maps to quickstart scenarios 1–11, incl. the rollback path; FakeProvider + local SQLite only | PASS |
| IV. Quality gates | Strict mypy, no `Any` beyond the JSON stats dict (justified, matches `Run.stats`) | PASS |
| V. Secrets / observability | Error text = class + fixed phrase for DB errors (R8); structured events `budget.exceeded`, `run.persisted`, `run.persist_failed` inside the run context | PASS |
| Layering | `graph` → `db`, `llm`, `config`, `domain`; `db.repositories` gains `release` and a `created_at` keyword only; no upward import (`test_graph_layering`) | PASS |
| Simplicity | No new tables, no graph state machinery before #21; tracker on the existing node contexts | PASS |

Post-design re-check (after Phase 1): unchanged, all PASS.

## Project Structure

### Documentation (this feature)

```text
specs/013-gh-issue-19/
├── spec.md
├── plan.md              # this file
├── research.md          # R1–R8 decisions
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── budget-and-persist.md
│   └── run-stats.md
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks (not created here)
```

### Source Code (repository root)

```text
src/invio/
├── graph/
│   ├── budget.py                 # NEW: UsageEntry, BudgetTracker, BudgetExceeded
│   └── nodes/
│       ├── llm_calls.py          # CHANGED: CallContext.budget, per_item check, ledger record
│       ├── relevance.py          # CHANGED: ScoringContext.budget; score_items stops on budget
│       ├── summarize_item.py     # CHANGED: SummaryContext.budget; summarize_items stops on budget
│       └── persist.py            # NEW: StageCounts, DigestDraft, RunResult, unprocessed,
│                                 #      build_stats, decide_status, persist_run, finalize_run,
│                                 #      record_failed_run
└── db/
    └── repositories.py           # CHANGED: ItemRepository.release, UsageRepository.add(created_at)

tests/
├── test_repositories.py          # CHANGED: UsageRepository.add(created_at)
├── test_budget.py                # NEW: tracker arithmetic, check/latch, cost_complete
├── test_llm_calls.py             # CHANGED: per_item check, ledger matches rows
├── test_relevance.py             # CHANGED: budget stop mid-batch
├── test_summarize_item.py        # CHANGED: budget stop between chunks
├── test_db_items.py              # CHANGED: release()
└── test_persist.py               # NEW: stats, status table, atomic save, rollback + recovery
```

**Structure Decision**: Single project. The tracker lives in `invio.graph` (it is run state
used by the graph's nodes); persistence orchestration is a graph node module alongside the
other nodes, matching the issue's `nodes/persist.py` (renamed from `scout/` to `invio/`).
Callers (#21) must commit the run start and the deduplication take before the LLM stages and
must call `record_failed_run` when a stage raises (research R7, FR-011).
README `docs` update: describe run statuses, `runs.stats` and the token budget behaviour.

## Spec adjustments made during planning

- FR-001a: usage rows are kept across a failed save by replaying them after the rollback
  (research R1); a killed process leaves only the structured log record.
- FR-006: released items go back to `new` (or stay `failed` if untouched retries) so the next
  run selects them; relevance of relevant-but-unsummarized items is rated again (research R4).

## Complexity Tracking

No constitution violations to justify.
