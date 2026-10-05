# Quickstart: Validate deduplication, baseline mode and run limits

## Prerequisites

- `uv sync --locked`
- Default test database is in-memory SQLite; for MariaDB set `INVIO_TEST_DATABASE_URL`.

## Run the checks

```bash
uv run pytest tests/test_graph_deduplicate.py -q          # node scenarios (US1–US5)
uv run pytest -m db -q                                     # incl. repository additions
uv run pytest tests/test_job_schema.py tests/test_job_sections.py -q   # config + schema
uv run ruff check && uv run ruff format --check && uv run mypy
uv run pytest --cov                                        # full suite, coverage ≥ 95 %
```

If `test_committed_schema_is_current` fails: `uv run python -m invio.config.job` regenerates
`docs/job.schema.json`.

## Scenarios that must pass

Setup per test: `make_job`, a `Run` in status `running` as the current run,
`LimitsConfig(...)` and candidates from `make_candidate` with distinct `published_at`.
API: [contracts/python-api.md](./contracts/python-api.md); states:
[data-model.md](./data-model.md).

| # | Spec | Setup | Expected |
|---|------|-------|----------|
| 1 | US1 AS2, SC-001 | job with a `succeeded` run; call twice with identical candidates | 2nd call `items == []`, no new rows |
| 2 | US1 AS3 | same candidate for job A and job B | selected for both |
| 3 | US1 AS4 | same URL twice in input | stored and selected once |
| 4 | US2 AS1–3, SC-002 | stored web item hash X; candidate hash Y, then Y again; feed item without hash | Y → `reason="changed"`, stored hash Y; Y again → dropped; no hash → dropped |
| 5 | US3, SC-003 | item marked `failed` after each run | selected in 3 runs total, dropped from 4th |
| 6 | US3 AS4 | item `new` with attempts 1 (interrupted) | retried; with attempts 3 → dropped |
| 7 | US4 AS1–2, SC-004 | no successful run; source A 25, source B 5, `baseline_items=10` | 10 newest of A + 5 of B; 15 of A `skipped_baseline`; rerun → `[]` |
| 8 | US4 AS3–4 | only `failed` earlier runs → baseline; a `partial` run → no baseline | as stated |
| 9 | US5 AS1–2, SC-005 | `max_items_per_run=5`, 8 new candidates | 5 newest, `limit_cut=3` |
| 10 | US5 AS3–4 | cut items, next call with empty input | cut items stored `new`/0 attempts/`run_id=None`; returned next call |
| 11 | Edge | undated candidates and ties | undated last; ties by source order then position |
| 12 | Config | `baseline_items` 0, -1, "10" | validation error at `limits.baseline_items` |

Each selected item has `attempts` incremented by exactly one and `run_id` = current run; one
`deduplicated` log record carries the counts.
