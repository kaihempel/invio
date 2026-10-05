# Data Model: LLM Relevance Scoring (gh-issue-16)

No schema change and no migration (clarification Q1). Existing tables are reused.

## RelevanceResult (new, in-memory, `invio.graph.nodes.relevance`)

The validated answer of the `fast` model for one item.

| Field | Type | Rules |
|-------|------|-------|
| `score` | float | 0 ≤ score ≤ 1, finite, not a boolean |
| `reason` | str | non-blank after stripping |
| `key_points` | list[str] | may be empty; returned to the caller, never persisted |

Unknown keys are rejected (`extra="forbid"`).

## RelevanceOutcome (new, in-memory)

What `score_item` returns for one item; frozen kw-only dataclass.

| Field | Type | Meaning |
|-------|------|---------|
| `item_id` | int | id of the scored item |
| `status` | `ItemStatus` | `RELEVANT`, `SKIPPED_IRRELEVANT` or `FAILED` |
| `relevance` | `Decimal \| None` | stored two-decimal score; `None` when failed |
| `result` | `RelevanceResult \| None` | full model answer; `None` when failed |
| `error` | `str \| None` | `"<ErrorClass>: <message>"` when failed |

## Item (existing, `items` table) — fields written by this feature

| Field | Written value |
|-------|---------------|
| `relevance` | `Numeric(3,2)`: score quantised to 2 decimals (`ROUND_HALF_UP`); unchanged on failure |
| `status` | see state transitions |
| `last_error` | `None` on success; error text on failure |

`attempts`, `summary` and all other fields are untouched.

### State transitions

```text
<input status, typically extracted/new after the keyword prefilter>
  ├─ valid answer, relevance ≥ min_relevance ──► relevant
  ├─ valid answer, relevance <  min_relevance ──► skipped_irrelevant
  ├─ per-item LLM error (invalid output, unavailable,
  │   rate limited, invalid request) ─────────────► failed
  └─ auth/config error or unexpected exception ──► unchanged (step aborts)
```

The step does not filter by input status: it scores the items it is given (clarification Q2).

## LlmUsage (existing, `llm_usage` table) — one row per scoring call

| Field | Value |
|-------|-------|
| `job_id`, `run_id` | from the scoring context (`run_id` may be `None`) |
| `provider` | `llm_config.provider.value` |
| `model` | the job's `llm.models.fast` |
| `purpose` | `"relevance"` |
| `input_tokens`, `output_tokens` | from the call's `Usage` (repair request summed in) |
| `cost_usd` | `ModelRegistry.cost(model, usage)` (may be `None`) |

Written on success and on `LLMInvalidOutputError` (using `err.usage`); not written for
errors that carry no usage.

## Inputs read (existing config)

- `SearchConfig.semantic_description` (non-empty str) → system message.
- `SearchConfig.min_relevance` (0..1, default 0.6) → threshold.
- `LLMConfig.provider`, `LLMConfig.models.fast` → provider name and model id.
