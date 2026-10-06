# Data Model: Run Persistence with Token Budget Enforcement

Feature: [spec.md](spec.md) · Research: [research.md](research.md)

No schema migration: every column used already exists (`runs.status` includes `partial`,
`runs.stats` is JSON, `llm_usage.cost_usd` is `NUMERIC(12,6)`).

## Stored entities (existing tables)

### Run (`runs`)

| Field | Use in this feature |
|---|---|
| `status` | `running` → `succeeded` / `partial` / `failed` (see state machine below) |
| `finished_at` | always set when the run leaves `running` (FR-011) |
| `stats` | versioned run statistics, [contracts/run-stats.md](contracts/run-stats.md) |
| `error` | sanitized error text for `failed` runs (research R8), `"all attempted items failed"` when the items failed; `NULL` otherwise |

State transitions (one write, inside the atomic save or the recovery step):

```text
running ──(save ok, rules of FR-010)──▶ succeeded | partial | failed (all attempted items failed)
running ──(save fails / stage raises)─▶ failed (recovery step, results discarded, usage kept)
```

### Item (`items`)

Changes made by this feature (all inside the atomic save):

| Transition | When | Effect |
|---|---|---|
| taken → released | budget stop, item has no completed outcome | `attempts -= 1` (≥ 0), `run_id = NULL`, `status = new` unless still `failed` |

Precondition: the take (`attempts + 1`, `run_id` set) is committed before the work session
starts (research R7), so a rolled-back save leaves the item `new` (or `failed`) with `attempts >= 1`
(retryable) and the attempt still counts.

All other item transitions (`relevant`, `skipped_irrelevant`, `summarized`, `failed`) come from
the existing nodes; this feature only decides whether they are committed (save ok) or rolled
back (save failed).

### Digest (`digests`)

Written once per run by the save step when the digest step produced a result with at least one
item (`title`, `body`, `item_ids`, `run_id`, `job_id`). No empty digest is stored.

### LLM usage (`llm_usage`)

One row per provider call (a repair request is summed into its call). Written during the run
through the work session; on a failed save re-inserted from the tracker ledger by the recovery
step (research R1). `cost_usd` is `NULL` for a model without a registered price.

## In-memory entities (new)

### BudgetTracker (`invio.graph.budget`)

| Field | Type | Rule |
|---|---|---|
| `limit` | `int` | job `limits.max_llm_tokens_per_run` (≥ 1) |
| `used` | `int` | sum of `input_tokens + output_tokens` of every recorded call |
| `exceeded` | `bool` | latched `True` by the first failing `check()` |
| `ledger` | `list[UsageEntry]` | one entry per recorded call, in call order |

- `check()` raises `BudgetExceeded(used, limit)` when `used > limit`.
- `record(entry)` never raises; it counts digest calls too.
- Derived: `input_tokens`, `output_tokens`, `cost_usd` (sum of known costs), `cost_complete`
  (no entry with unknown cost), `calls`.

### UsageEntry

Immutable copy of one usage row: `provider`, `model`, `purpose`, `input_tokens`,
`output_tokens`, `cost_usd: Decimal | None`, `created_at`.

### StageCounts

Counts the orchestrator collects before the save: `found`, `new` (from `DedupStats`),
`after_keyword_filter` (keyword stage), plus the taken items.

### RunResult

Input of the save step: `job_id`, `run_id`, `StageCounts`, taken items, relevance outcomes,
summary outcomes, optional digest draft (`title`, `body`, `item_ids`), the `BudgetTracker`.

Validation rules:
- `digest.item_ids` must be a subset of the run's taken item ids.
- Finished items = taken items that reached a final state in this run: `skipped_keyword`, or an
  outcome of `skipped_irrelevant`, `summarized` or `failed`. Only when `tracker.exceeded` is
  true, every other taken item (not rated yet, or rated `relevant` but not summarized) is
  released. An item that was `failed` before the run and not touched keeps `failed`.
