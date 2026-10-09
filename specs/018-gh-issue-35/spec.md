# Feature: Usage and cost report (`invio usage`)

**Branch**: `gh-issue-35` (constitution naming; supersedes the issue's `issue/35-add-usage-and-cost-report`) | **Date**: 2026-10-09 | **Issue**: #35 (depends on #19)

## Context

Running several jobs on paid APIs needs visibility into token usage and cost per job and provider.
Usage rows (`llm_usage`, one per LLM call, written by #19) already exist; this adds a read-only report.

## Command contract

```
invio usage [--job NAME] [--since YYYY-MM-DD] [--by provider|model|job|day] [--json]
```

- `--job NAME`: only that job's rows. Unknown job → `Error: job 'NAME' not found`, exit 1.
- `--since YYYY-MM-DD`: only rows with `created_at >= since 00:00 UTC`. Invalid date → usage error, exit 2.
- `--by` (default `model`): one row per provider / model (`provider/model`) / job name / UTC day (`YYYY-MM-DD`).
  Invalid value → exit 2.
- Table (stdout, `rich`): group, calls, input tokens, output tokens, cost (USD), plus a `Total` row.
  No rows → `No usage.` and exit 0.
- `--json`: a single valid JSON document on stdout:
  `{"by", "job", "since", "groups": [{"key", "calls", "input_tokens", "output_tokens", "cost_usd", "cost_complete", "unpriced_models"}], "total": {...same fields minus key}, "unpriced_models": [...]}`.
  `cost_usd` is a decimal string (exact) or `null` when nothing in the group is priced.
- Errors to stderr; missing or unusable `database_url` → exit 2; database error → exit 1; registry load failure → exit 2.

## Cost rules

- Cost is computed from the model registry (`models.d/*.yaml`) on the summed tokens per (group, provider, model),
  via `ModelRegistry.cost`, not from the stored `cost_usd` column, so current prices apply.
- A model missing from the registry is **unpriced**: its tokens are counted, its cost is not counted as zero.
  The group's cost shows as `≥ $X` (or `unknown` when no priced model contributes), `cost_complete=false`,
  the model is listed in `unpriced_models`, and a warning naming each unpriced model goes to stderr
  (also in `--json` mode, so stdout stays pure JSON).
- Each (group, provider, model) cost is rounded to 6 decimals and the total is the sum of the group costs,
  so totals under different `--by` options can differ by a few millionths of a dollar.

## Acceptance criteria (from the issue)

- Totals match the sum of `llm_usage` rows in a fixture database.
- Grouping options work (provider, model, job, day).
- `--json` output is valid JSON.
- Unknown models are flagged, not silently counted as zero.

## Design / layering

- `src/invio/db/repositories.py`: `UsageRepository` grouped aggregation over (job name, provider, model, UTC day),
  joined with `jobs`; the database groups in every case (`DATE(created_at)` is the UTC day on SQLite and
  MariaDB, since `created_at` is stored as naive UTC).
- `src/invio/services/usage.py` (new): `UsageService.from_settings()`, `report(...) -> UsageReport`
  (frozen dataclasses, no ORM objects cross the boundary). Unknown job → `JobNotFoundError`.
- `src/invio/cli/commands/usage.py` (new, auto-discovered): option parsing, `_make_service()` / `_make_registry()`
  seams, table/JSON rendering. CLI must not import `invio.db` (enforced by `tests/test_cli_layering.py`).
- No schema change, no migration.
- Docs: README "Run history" section and "Layout" block.

## Tests

- `tests/test_usage_service.py`: fixture DB totals equal raw row sums; each grouping; `since`/`job` filters;
  unpriced model flagged.
- `tests/test_cli_usage.py`: table output, total row, `--json` parses with `json.loads`, unknown job exit 1,
  bad date / bad `--by` exit 2, warning on stderr.
