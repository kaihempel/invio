# Context: LLM Relevance Scoring (gh-issue-16)

**Branch**: `gh-issue-16` · **Issue**: #16 · **Milestone**: M2 Pipeline · **Depends on**: #15 (keyword prefilter), #7/#8 (LLM layer), #3 (job config)

## Goal

After the keyword prefilter, every item is rated by the job's `fast` model against
`search.semantic_description`. The score decides whether the item reaches summarization
(`relevant`) or is dropped (`skipped_irrelevant`). The document is untrusted input and must not
be able to steer the rating.

## Existing building blocks

| What | Where | Notes |
|------|-------|-------|
| `LLMProvider.complete_structured(system, user, schema, *, model, temperature)` | `src/invio/llm/base.py` | returns `(value, Usage)`; one repair request, usage summed |
| `LLMInvalidOutputError` (`.usage`), `LLMUnavailableError`, `LLMRateLimitError`, `LLMInvalidRequestError`, `LLMAuthError` | `src/invio/llm/base.py` | `LLMConfigError` is a `ValueError`, not an `LLMError` |
| `FakeProvider`, `FakeReply` | `src/invio/llm/fake.py` | scripted provider; records requests |
| `ModelRegistry.cost(model, usage)` | `src/invio/llm/registry.py` | `Decimal` or `None` for an unknown model |
| `SearchConfig` (`semantic_description`, `min_relevance`) | `src/invio/config/job.py` | |
| `Item.relevance` (`Numeric(3, 2)`), `status`, `last_error` | `src/invio/db/models.py` | no schema change |
| `UsageRepository.add(...)` | `src/invio/db/repositories.py` | one `llm_usage` row |
| `item_text` | `src/invio/graph/nodes/keyword_filter.py` | same text basis as the prefilter |

## Design

1. **`src/invio/graph/nodes/relevance.py`**:
   - `RelevanceResult` (`extra="forbid"`): `score` 0..1, finite, not a bool; `reason` non-blank
     after stripping; `key_points: list[str]`.
   - `RelevanceOutcome` / `ScoringContext`: frozen kw-only slotted dataclasses.
   - `build_messages(title, teaser, text, semantic_description) -> (system, user)` — pure.
   - `score_item(item, ctx)` and `score_items(items, ctx)` (sequential, input order).
2. **Prompt**: the system message holds the task, `<interest>` with the job's description, the
   0..1 scale and the untrusted-data rule. The user message is exactly one
   `<document><title>…</title><content>…</content></document>` block. The body is `item_text`
   of teaser and text, cut to `MAX_DOCUMENT_CHARS = 4000` plus `[truncated]`.
3. **Neutralisation**: every case-insensitive `<\s*/?\s*(document|title|content)\b[^<>]*>?` in
   title and body gets its `<`/`>` swapped for `‹`/`›` (U+2039/U+203A) before truncation. The
   pattern also catches spaces around the slash (`< /document>`) and unterminated tags
   (`…</document` at the end), which the template's own closing tag would otherwise complete.
4. **Threshold**: the score is quantised to two decimals (`Decimal(str(score))`, `ROUND_HALF_UP`)
   and the stored value is compared with `min_relevance`, so relevance and status never disagree.
5. **Usage**: one `llm_usage` row per call (`purpose="relevance"`), also on
   `LLMInvalidOutputError` (`err.usage`, both requests).
6. **Errors**: invalid output, unavailable, rate limited and rejected requests mark the item
   `failed` (`last_error = "<ErrorClass>: <message>"`; for invalid output the fixed message
   `invalid structured answer after repair`) and scoring continues; `LLMAuthError`,
   `LLMConfigError` and any other exception propagate.
7. **Persistence**: `ItemRepository.set_relevance` and `mark_failed` flush, never commit.

## Acceptance criteria → tests

| Criterion | Test |
|-----------|------|
| Relevance and status stored, threshold boundaries and rounding | `tests/test_relevance.py` (threshold section) |
| Prompt content, truncation | `tests/test_relevance.py` (prompt section) |
| Injection text stays inside one `<document>` block, delimiters neutralised | `tests/test_relevance.py` (injection section), fixture `tests/fixtures/relevance/injection.txt` |
| Invalid answers are rejected by the schema | `tests/test_relevance.py` (`RelevanceResult`, failures) |
| One usage row per call | `tests/test_relevance.py` (usage section) |
| One bad item does not stop the run; auth/config errors do | `tests/test_relevance.py` (failures section) |
| `set_relevance` / `mark_failed` | `tests/test_repositories.py` |
| Layering | `tests/test_graph_layering.py` |

## Quality gates

```bash
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

Coverage must stay ≥ 95 %.

## Implementation decisions

- **Score quantisation** goes through `str(score)` so `0.595` becomes `0.60` and not `0.59`
  through binary float error.
- **Bool scores** are rejected by a before-validator; otherwise pydantic would coerce `true` to
  `1.0`. `allow_inf_nan=False` rejects `NaN` and infinities.
- **Truncation after neutralisation**: the cut can never land inside a real delimiter tag
  (the replacement keeps the length).
- **Usage before status**: usage is recorded right after the call returns (or fails with
  invalid output), so a failure while storing the relevance does not lose the cost row.
- **`attempts` is untouched**: retry policy is out of scope.

## Known limitations

- The repair request built by `structured_with_repair` repeats the model's previous answer
  outside the `<document>` block. The document itself stays inside the block and the repaired
  answer is schema-validated.
- The neutralise regex can rewrite harmless prose such as `<content of x>` (it looks like a
  `content` tag). The rewrite is deterministic and harmless: only the brackets change.
- `LLMInvalidOutputError` messages may contain answer-derived key names (for example an unknown
  extra field), because pydantic reports `<loc>: <message>`. A document could steer the model
  into choosing such a key, so the node stores a fixed message for invalid output instead of
  the validation errors; `last_error` never carries model- or document-chosen text. The
  trade-off is less detail when debugging a failed item.
- Neutralisation matches ASCII `<`/`>` only. Look-alikes such as a zero-width space after `<`
  or fullwidth `＜/document＞` are passed through unchanged. They are not real delimiters, but a
  model could read them as such; the schema check still limits the impact to a valid 0..1 score.
- The title is sent in full (FR-005); its length is bounded only by `Item.title` (`String(1000)`).
- Unavailable and rate-limit errors fail only the current item (FR-009), so a provider outage
  marks the remaining items `failed` one by one, each after its own timeout. A circuit breaker
  (stop the step after N consecutive systemic errors) and a retry policy for `failed` items
  belong with the graph wiring.
