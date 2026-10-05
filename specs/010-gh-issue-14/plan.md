# Implementation Plan: Per-Job Deduplication, Baseline Mode and Run Limits

**Branch**: `gh-issue-14` | **Date**: 2026-10-05 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/010-gh-issue-14/spec.md` (GitHub issue #14)

## Summary

Add the pipeline's deduplication step as a plain, synchronous function
`deduplicate(session, *, job_id, run_id, candidates, limits) -> DedupResult` in
`src/invio/graph/nodes/deduplicate.py` (the issue's `scout/graph/nodes/deduplicate.py`). It
receives the candidates grouped by source, looks up the job's stored items in one batched
query, classifies each candidate (new / changed version / retry / waiting / drop), applies
baseline mode when the job has no `succeeded` or `partial` run, merges the job's waiting
backlog from storage, applies `max_items_per_run` newest-first, and persists the outcome
through `ItemRepository`: unknown items inserted, changed versions reset, baseline rejects
stored as `skipped_irrelevant` with `last_error = "baseline"`, items cut by the run limit
stored as waiting (`new`, 0 attempts), selected items linked to the run with `attempts + 1`.
It returns plain frozen records (no ORM objects) plus counts and logs one structured summary
line. LangGraph wiring stays with #21; this issue adds no new dependency and no migration.
`LimitsConfig` gains `baseline_items` (default 10, ≥ 1), mirrored in the JSON schema, example
job file and creation wizard.

## Technical Context

**Language/Version**: Python 3.12 (uv-managed)

**Primary Dependencies**: Existing only — SQLAlchemy ≥ 2.1, Pydantic v2. No new runtime or
dev dependencies (LangGraph arrives with #21; the node is a plain function it can wrap).

**Storage**: Existing schema from #4 (`items.attempts`, `items.content_hash`,
`items.last_error`, `items.run_id`, unique `(job_id, url_hash)`); no migration

**Testing**: pytest with the `db` marker and `db_session` fixture (in-memory SQLite by
default, MariaDB opt-in via `INVIO_TEST_DATABASE_URL`); factories in `tests/db_helpers.py`
(`make_job`, `make_run`, `make_item`, `make_candidate`)

**Target Platform**: Linux server (cron/systemd), same as the rest of invio

**Project Type**: Single Python package with CLI (`src/invio/`)

**Performance Goals**: One run handles a few hundred candidates per job: one batched lookup
of known items (chunked `IN`), one query for the waiting backlog, one query for run history;
inserts reuse the idempotent `ItemRepository.add`

**Constraints**: Deterministic output (FR-015); caller owns the unit of work (repositories
flush, never commit); ORM objects never leave the function; structured logging only

**Scale/Scope**: ~1 new module (+ package `__init__`), 5 repository methods, 1 config field,
docs/wizard updates, 1 new test module plus repository/config test additions

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | How this plan complies |
|-----------|--------|------------------------|
| I. Strict Contracts at Boundaries | ✅ | `baseline_items` is a `StrictInt` with `ge=1` on the strict `LimitsConfig` (unknown keys still rejected); the node consumes the shared `Candidate` record and validated `LimitsConfig` instead of re-declaring them; the job file `schema_version` is unchanged because the new field is optional with a default. |
| II. CLI-First Operation | ✅ | No new command: the node runs inside `invio job run` / `run-due` (#21–#23). The new limit is settable via job files and the creation wizard. |
| III. Test-Covered Behaviour | ✅ | Every acceptance scenario of US1–US5 and the edge cases map to tests on the in-memory database; `baseline_items` rejection paths (0, negative, non-int) tested; no network or LLM. |
| IV. Quality Gates Mirror CI | ✅ | Typed frozen dataclasses, no `Any` in new public API; `docs/job.schema.json` regenerated so `test_committed_schema_is_current` stays green; coverage ≥ 95 %. |
| V. Secrets Stay Secret, Runs Stay Observable | ✅ | One structured `info` line per call with counts only (no URLs/titles); `job`/`run_id` come from the caller's `run_context`. |
| Layering | ✅ | `invio.graph` → `invio.db`, `invio.config`, `invio.domain` (downward only). `invio.sources` untouched, so TID251 is unaffected. |
| Simplicity | ✅ | Plain function, no node/graph framework abstraction; repository additions are exactly the queries the node needs. |
| Docs in same PR | ✅ | `docs/job.example.yaml`, `docs/job.schema.json` and wizard prompt updated for the new limit. |

No violations — Complexity Tracking stays empty.

**Post-design re-check (after Phase 1)**: unchanged, all ✅. The design adds no new layers,
dependencies or schema changes; the only user-facing change (`limits.baseline_items`) is
documented.

## Project Structure

### Documentation (this feature)

```text
specs/010-gh-issue-14/
├── plan.md              # This file
├── research.md          # Phase 0: decisions and rationale
├── data-model.md        # Phase 1: records, classification and item state transitions
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/
│   └── python-api.md    # Phase 1: public Python API of the node + repository additions
├── checklists/
│   └── requirements.md  # Spec quality checklist
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
src/invio/
├── config/
│   └── job.py                 # LimitsConfig.baseline_items (default 10, ge=1)
├── cli/
│   └── wizard.py              # _LIMIT_QUESTIONS: + ("baseline_items", "Baseline items per source")
├── db/
│   └── repositories.py        # ItemRepository.find_many / list_waiting / reset_version /
│                              #   mark_taken / mark_skipped; RunRepository.has_successful_run
└── graph/
    └── nodes/
        ├── __init__.py        # new package
        └── deduplicate.py     # deduplicate(), DedupResult, SelectedItem, DedupStats

docs/
├── job.example.yaml           # limits.baseline_items: 10
└── job.schema.json            # regenerated (uv run python -m invio.config.job)

tests/
├── test_graph_deduplicate.py  # new: US1–US5 + edge cases (db marker)
├── test_db_items.py           # + repository method tests (or test_db_records.py)
├── test_db_records.py         # + has_successful_run
└── test_job_sections.py       # + baseline_items default / bounds
```

**Structure Decision**: Single-package layout as established. The node lives in a new
`invio.graph.nodes` package so #15–#19 can add their nodes next to it and #21 can import
them into the graph builder.

## Complexity Tracking

No constitution violations to justify.
