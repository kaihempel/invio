# Data Model: Item Summarization with Map-Reduce Chunking

No database migration. One new job setting, two validated LLM result models, two transient
types and one repository method.

## ItemSummary (Pydantic, `graph/nodes/summarize_item.py`)

The validated answer of the short call and of every combine call, and the stored final
summary.

| Field | Type | Rules |
|-------|------|-------|
| `headline` | `str` | stripped, non-empty, no line break |
| `bullets` | `list[str]` | 3–6 items, each stripped and non-empty |
| `why_relevant` | `str` | stripped, non-empty (one-sentence takeaway relating the item to the job's interest) |

- `model_config = ConfigDict(extra="forbid")`.
- Persisted as `model_dump_json()` → `{"headline": …, "bullets": […], "why_relevant": …}` in
  `items.summary`; `model_validate_json(items.summary)` round-trips to an equal object.
- Sentence count of `why_relevant` is requested in the prompt, not validated.

- Read back only inside the `graph` layer (`model_validate_json`); `notify` receives rendered,
  plain data and never imports this model (research.md R6).

## ChunkSummary (Pydantic, `graph/nodes/summarize_item.py`)

The validated answer of one map (chunk) call; intermediate, never persisted.

| Field | Type | Rules |
|-------|------|-------|
| `bullets` | `list[str]` | 1–6 items, each stripped and non-empty |

- `model_config = ConfigDict(extra="forbid")`.
- No headline / `why_relevant`: those are produced only by calls that see the whole document
  (short call, combine calls).

## Chunk (transient `str`)

A contiguous slice of an item's body produced by `split_text`.

- `estimate_tokens(chunk) <= max_tokens` (FR-003); new text per chunk ≤ `max_tokens - overlap`.
- With `overlap > 0`, chunk *i+1* begins with a **non-empty** tail of chunk *i* whose estimate
  is `<= overlap` (word-boundary tail preferred, character tail otherwise); with `overlap == 0`
  there is no repeated text.
- Never persisted.

## SummaryContext (frozen kw-only dataclass)

Everything the node needs besides the item.

| Field | Type | Source |
|-------|------|--------|
| `job_id` | `int` | job row |
| `run_id` | `int \| None` | run row |
| `language` | `str` | `JobConfig.language` |
| `semantic_description` | `str` | `JobConfig.search.semantic_description` |
| `provider` | `LLMProvider` | factory |
| `provider_name` | `str` | `JobConfig.llm.provider` |
| `fast_model` / `smart_model` | `str` | `JobConfig.llm.models` |
| `registry` | `ModelRegistry` | cost lookup |
| `items` | `ItemRepository` | |
| `usage` | `UsageRepository` | |

## SummaryOutcome (frozen kw-only dataclass)

| Field | Type | Meaning |
|-------|------|---------|
| `item_id` | `int` | |
| `status` | `ItemStatus` | `SUMMARIZED` or `FAILED` |
| `summary` | `ItemSummary \| None` | `None` when failed |
| `error` | `str \| None` | `"<ErrorClass>: <message>"`, never document text |
| `calls` | `int` | provider calls made for this item (repair requests not counted separately) |
| `chunks` | `int` | chunks summarized (0 for the short path) |
| `truncated` | `bool` | chunks beyond `MAX_CHUNKS` were dropped |

## JobConfig.language (config, `config/job.py`)

| Field | Type | Default | Rules |
|-------|------|---------|-------|
| `language` | `str` | `"en"` | `^[a-z]{2}$` and a key of `ISO_639_1`; error names `language` |

Position in the model (and in `dump_yaml` output): directly after `schema_version`.

## Item (existing ORM row) — fields touched

| Field | Change |
|-------|--------|
| `summary` | JSON of the final `ItemSummary` on success; untouched on failure |
| `status` | `SUMMARIZED` on success, `FAILED` on per-item LLM failure |
| `last_error` | cleared on success; set on failure |

### State transitions

```text
relevant ──summarize ok──────────▶ summarized
relevant ──per-item LLM error────▶ failed        (summary unchanged)
relevant ──auth/config error─────▶ relevant      (step aborts, exception propagates)
```

The node does not check the incoming status; it summarizes the items it is given.

## UsageRecord (existing `llm_usage` row)

One row per provider call: `purpose` ∈ {`summarize`, `summarize_chunk`, `summarize_combine`},
model = fast (short, chunk) or smart (combine), tokens incl. repair request, cost from the
registry, `run_id`.
