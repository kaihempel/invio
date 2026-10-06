# Quickstart: Validate Item Summarization (gh-issue-17)

## Prerequisites

- `uv sync --locked`
- DB tests use the existing `db_session` fixture (see `tests/conftest.py`); no network, no
  real LLM provider — the scripted `FakeProvider` (`invio.llm.fake`) answers every call and
  records it in `FakeProvider.requests`.

## Run

```bash
uv run pytest tests/test_split_text.py tests/test_summarize_item.py tests/test_job_config.py tests/test_repositories.py tests/test_relevance.py -q
uv run python -m invio.config.job   # regenerate docs/job.schema.json, then check git diff
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

## Scenarios → expected outcome

| # | Setup | Expected |
|---|-------|----------|
| 1 | short body (`fixtures/summarize/short.txt`), one valid reply | `len(fake.requests) == 1`, model = fast; item `summarized`; `ItemSummary.model_validate_json(item.summary)` equals the reply |
| 2 | long body splitting into N ≤ 20 chunks; N chunk replies + 1 combine reply | `len(fake.requests) == N + 1`; first N use fast model, last uses smart; item `summarized` with the combine reply |
| 3 | `split_text` on fixtures (long paragraphs, one huge sentence, no punctuation, CJK, single 50k-char word) with several `(max_tokens, overlap)` pairs | every chunk `estimate_tokens <= max_tokens`; with overlap > 0 every next chunk starts with a **non-empty** tail of the previous of ≤ overlap tokens (also CJK / huge word), new text per chunk ≤ `max_tokens - overlap`; overlap 0 → joining with the separators reproduces the whitespace-normalised text without repeats |
| 4 | `split_text(text, 10, 10)`, `split_text(text, 0, 0)` | `ValueError` |
| 4a | long body; one chunk reply has a single bullet; separate test with `COMBINE_MAX_TOKENS` monkeypatched so two parts fit per group (4 chunks) | single-bullet `ChunkSummary` accepted; 4 fast + 2 + 1 smart requests |
| 5 | body of 25 paragraphs of 2,763 tokens each (25 chunks) | 20 fast calls + combine; combine system message mentions truncation (20 of 25); `summarize.truncated` logged; `outcome.truncated` |
| 6 | job `language: de` | every recorded `system` message contains the German instruction (short, chunk and combine) |
| 7 | job file without `language` / with `language: xx` / `DE` | loads as `en` / `JobConfigError` naming `language` |
| 8 | body with "ignore previous instructions" and `</document>` | text only inside the `<document>` block of `user`; closing tag neutralised in every request; system has untrusted-data rule |
| 9 | reply with 2 bullets, then repair reply with 7 bullets | item `failed`, `last_error` starts with `LLMInvalidOutputError`, `summary` unchanged, usage row recorded |
| 10 | long item, chunk 2 raises `LLMUnavailableError`; next item short and valid | first item `failed` with no further calls for it; second item `summarized`; no exception |
| 11 | `LLMAuthError` scripted | `summarize_items` raises; item unchanged |
| 12 | scenario 2 with `Usage(10, 5)` | N + 1 `llm_usage` rows: N × `summarize_chunk` (fast), 1 × `summarize_combine` (smart) |

See [contracts/summarize-node.md](contracts/summarize-node.md),
[contracts/job-language.md](contracts/job-language.md) and [data-model.md](data-model.md).
