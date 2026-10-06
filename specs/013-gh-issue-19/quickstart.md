# Quickstart: Validate Run Persistence and Token Budget

Feature: [spec.md](spec.md) · Contracts: [budget-and-persist.md](contracts/budget-and-persist.md),
[run-stats.md](contracts/run-stats.md)

## Prerequisites

- `uv sync --locked`
- No network, no provider credentials: all scenarios use the scripted `FakeProvider` and the
  in-memory SQLite `db_session` fixture (MariaDB optional via `INVIO_TEST_DATABASE_URL`).

## Run the checks

```bash
uv run pytest tests/test_budget.py tests/test_persist.py tests/test_llm_calls.py \
  tests/test_relevance.py tests/test_summarize_item.py -q
uv run pytest -m db tests/test_persist.py -q          # rollback scenarios on the DB
uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest
```

## Scenarios and expected outcomes

| # | Setup | Expected |
|---|---|---|
| 1 | 3 taken items, fake provider scripted for relevance + short summaries, digest draft with 2 ids | commit; items `summarized`, 1 digest with the 2 ids, one usage row per call, run `succeeded`, stats per run-stats.md |
| 2 | Same as 1, digest write forced to fail (e.g. a digest with a NULL title; not a missing `job_id`, the usage replay would hit the same foreign key and the recovery would fail too) | no item change and no digest stored; every usage row present; run `failed`, `finished_at` set, error = class + fixed phrase, recovery stats layout |
| 3 | Budget 1,000 tokens, fake provider reports 600 tokens per call, 5 items; item 1 rated irrelevant, item 2 relevant | exactly 2 relevance calls, then `budget.exceeded`; no summary call; item 1 stays `skipped_irrelevant`, items 2–5 released (`attempts` restored, `run_id` NULL, `new`); run `partial`; `skipped_budget == 4` |
| 4 | Budget stops during a long item's chunk calls | item in progress is not `failed`, is released; chunk usage rows kept |
| 5 | Budget exceeded, digest draft produced with `per_item=False` call | digest call made and counted in `tokens`; run `partial` |
| 6 | Budget exceeded before any item finished | no digest stored; run `partial` |
| 7 | Every attempted item fails (provider unavailable for all) | run `failed` via the normal save; item failures committed |
| 8 | One of three items fails | run `partial` |
| 9 | Model without registry price | `cost_complete: false`; cost sums known prices only |
| 10 | Stats vs usage rows | `input_tokens`/`output_tokens`/`estimated_cost_usd` equal `UsageRepository.totals_for_run` |
| 11 | No items taken | run `succeeded`, zero tokens and cost |

## Inspect a run manually (development database)

```bash
sqlite3 /path/to/invio.sqlite \
  "select id, status, finished_at, json_extract(stats,'$.tokens'), json_extract(stats,'$.estimated_cost_usd') from runs order by id desc limit 5;"
```
