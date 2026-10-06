# Data Model: Digest Synthesis (gh-issue-18)

No database schema change. All entities below are in-memory values of the
`invio.graph.nodes.synthesize` module; storage of the digest (`digests` table) belongs to the
pipeline issue.

## DigestEntry (input, frozen dataclass)

| Field | Type | Source / rule |
|-------|------|---------------|
| `item_id` | `int` | `Item.id` |
| `url` | `str` | `Item.url`; non-empty |
| `title` | `str` | `Item.title` |
| `relevance` | `float \| None` | `Item.relevance` (`Decimal` → `float`); `None` sorts last |
| `published_at` | `datetime \| None` | `Item.published_at` (UTC); `None` sorts after dated entries |
| `summary` | `ItemSummary` | `ItemSummary.model_validate_json(Item.summary)` (#17) |

Built by `entries_from_items(items)`: only items with status `summarized` and a valid summary
JSON; others are skipped (count logged as `synthesize.skipped`, ids only).

**Order (FR-002)**: relevance descending → `published_at` descending (undated last) →
`item_id` ascending. `sort_entries(entries)` is pure and total.

## ItemSummary (existing, #17)

`headline` (non-blank, one line), `bullets` (3–6 non-blank), `why_relevant` (non-blank). Not
changed by this feature.

## SynthesisContext (frozen dataclass)

`job_id`, `run_id`, `language` (ISO 639-1, from `JobConfig.language`), `semantic_description`
(the job's research interest), `provider` (`LLMProvider`), `provider_name`, `smart_model`,
`registry` (`ModelRegistry`), `usage` (`UsageRepository`). Satisfies the `CallContext`
protocol of `llm_calls`.

## UrlFilterResult (frozen dataclass, post-check output)

| Field | Type | Meaning |
|-------|------|---------|
| `text` | `str` | answer with every non-allowed URL removed (R5) |
| `removed` | `int` | number of URLs removed |
| `linked` | `frozenset[str]` | allowed URLs still present as link destinations |

## SynthesisResult (output, frozen dataclass)

| Field | Type | Rule |
|-------|------|------|
| `body` | `str` | Markdown digest; `""` for no entries |
| `item_ids` | `tuple[int, ...]` | ids of all entries in FR-002 order; `()` for no entries |
| `fallback` | `bool` | `True` when the fallback digest was built (FR-015) |
| `error` | `str \| None` | `failure_message(err)` or `"unusable answer: <reason>"` when `fallback`; never item text |
| `removed_urls` | `int` | from `UrlFilterResult.removed`; `0` for empty / fallback |
| `missing_items` | `int` | entries listed under "More items"; `0` for empty / fallback |
| `calls` | `int` | model calls made (`0` or `1`) |

Invariants: `body == ""` ⇔ `item_ids == ()`; `fallback` ⇒ `calls == 1` and `error is not None`;
every URL in `body` is the URL of some entry.

## Digest body layout

Model path:

URLs are written bare, or as `<url>` when they contain parentheses (research R6).

```text
<intro + thematic "##" sections written by the model, after filter_urls>

## More items                       ← only when missing_items > 0 (heading in job language)
- [Title](url) — why_relevant

## Worth a closer look              ← always (heading in job language)
- [Title](url) — why_relevant       ← top 3 entries (all when fewer)
```

Fallback path:

```text
<fixed intro in job language, with item count>

## <fixed heading, e.g. "All items">
- [Title](url) — headline
  why_relevant

## Worth a closer look
- [Title](url) — why_relevant
```

## Run status (FR-016)

`run_status_after_synthesis(planned, result)`: `SUCCEEDED` + `result.fallback` → `PARTIAL`;
every other combination returns `planned` unchanged (`PARTIAL`/`FAILED` stay).

## Usage record

One `llm_usage` row per synthesis call: `purpose="synthesize"`, `model=smart_model`, tokens and
cost from the provider's `Usage`. None for empty input or a call that raised before answering.
