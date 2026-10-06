# Contract: Run Statistics (`runs.stats`, version 1)

JSON object written once per run when it leaves `running`. Consumers (#35 usage report, #22 job
run output) MUST check `version` and ignore unknown keys.

```json
{
  "version": 1,
  "found": 42,
  "new": 17,
  "after_keyword_filter": 12,
  "relevant": 7,
  "summarized": 6,
  "failed": 1,
  "skipped_budget": 0,
  "llm_calls": 20,
  "input_tokens": 48211,
  "output_tokens": 5120,
  "tokens": 53331,
  "estimated_cost_usd": "0.010305",
  "cost_complete": true,
  "budget_limit": 200000,
  "budget_exceeded": false,
  "over_budget": false
}
```

| Key | Type | Meaning |
|---|---|---|
| `version` | int | always `1` for this layout |
| `found` | int | candidates reported by sources (`DedupStats.found`) |
| `new` | int | candidates never seen before for the job (`DedupStats.new`) |
| `after_keyword_filter` | int | items that passed the keyword filter in this run |
| `relevant` | int | items rated `relevant` in this run |
| `summarized` | int | items summarized in this run |
| `failed` | int | items that failed in this run (relevance or summary) |
| `skipped_budget` | int | items released because of a budget stop |
| `llm_calls` | int | provider calls recorded for the run (digest included) |
| `input_tokens`, `output_tokens` | int | sums over the run's usage rows |
| `tokens` | int | `input_tokens + output_tokens` |
| `estimated_cost_usd` | string | sum of known `cost_usd` values, 6 decimal places |
| `cost_complete` | bool | `false` if any call used a model without a registered price |
| `budget_limit` | int | the job's `limits.max_llm_tokens_per_run` |
| `budget_exceeded` | bool | whether the budget stopped per-item calls |
| `over_budget` | bool | `tokens > budget_limit` (also true when only the last call overshot) |

Recovery layout (run `failed` by the recovery step): only `version`, `llm_calls`, the token keys,
the cost keys, `budget_limit`, `budget_exceeded` and `over_budget` are present; stage counts are omitted because
the run's results were discarded.

Invariants (tested):
- `tokens == input_tokens + output_tokens`.
- `input_tokens`, `output_tokens`, `estimated_cost_usd` equal `UsageRepository.totals_for_run`.
- `estimated_cost_usd` equals the per-call `ModelRegistry.cost` sum for the recorded tokens.
