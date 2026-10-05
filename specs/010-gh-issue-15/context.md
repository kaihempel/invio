# Context: Keyword Prefilter (gh-issue-15)

**Branch**: `gh-issue-15` · **Issue**: #15 · **Milestone**: M2 Pipeline · **Depends on**: #3 (job config, merged)

## Goal

A cheap, LLM-free prefilter drops obviously irrelevant items before any token is spent. The
rules come from the job config `search.keywords` (`any`, `all`, `exclude`).

## Existing building blocks

| What | Where | Notes |
|------|-------|-------|
| `KeywordsConfig` (`any`, `all`, `exclude`: `list[str]`, entries `min_length=1`) | `src/invio/config/job.py` | part of `SearchConfig.keywords` |
| `ItemStatus.SKIPPED_KEYWORD` | `src/invio/domain.py` | already in the DB enum (migration 0001) |
| `Item` (`title`, `teaser`, `raw_content`, `status`) | `src/invio/db/models.py` | `raw_content` holds the extracted text when present |
| `ItemRepository` | `src/invio/db/repositories.py` | has `add`/`get`/`seen`/`list_for_job`; no status update yet |
| `invio.graph` | `src/invio/graph/__init__.py` | empty package; this issue adds the first node |

## Design

1. **`src/invio/graph/nodes/keyword_filter.py`** (the issue's `scout/graph/nodes/keyword_filter.py`):
   - `MatchResult`: frozen kw-only dataclass `matched: bool`, `hits: list[str]` (configured terms
     that occur in the text, in config order `any`, `all`, `exclude`, without duplicates).
   - `matches(text: str, keywords: KeywordsConfig) -> MatchResult` — pure.
   - `item_text(title, teaser, text) -> str`: title + teaser + extracted text (if any), joined
     by newlines, `None`/blank parts skipped.
   - `keyword_filter(item: Item, keywords: KeywordsConfig, items: ItemRepository) -> MatchResult`:
     evaluates `item_text(item.title, item.teaser, item.raw_content)`; a rejected item is
     persisted with status `skipped_keyword`.
2. **Matching**: Unicode NFC normalization + `casefold()` on text and terms; word boundaries via
   `(?<!\w)…(?!\w)` (Unicode `\w`), so terms such as `C++` work; a multi-word term matches as a
   phrase with any whitespace run between its words. Terms are stripped; blank terms ignored.
3. **Rules** (in order): any `exclude` hit → reject; every `all` term must occur; `any` needs at
   least one hit when non-empty; both `any` and `all` empty → pass (unless excluded).
4. **Persistence**: `ItemRepository.set_status(item, status)` updates the status and flushes.

## Acceptance criteria → tests

| Criterion | Test |
|-----------|------|
| `any`/`all`/`exclude` combinations | `tests/test_keyword_filter.py`, parameterized |
| Umlauts (`Äpfel`) and case differences | same |
| Empty keyword config lets all items pass | same |
| Rejected items stored as `skipped_keyword` | same (db marker, `db_session` fixture) + `tests/test_repositories.py` for `set_status` |

## Quality gates

```bash
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

Coverage must stay ≥ 95 %.

## Implementation decisions

- **Term patterns are cached** (`functools.lru_cache(maxsize=1024)` on the stripped term), so a
  job's terms compile once per process instead of once per item.
- **Folding**: `NFC` first, then `casefold()`, on both text and terms. `casefold()` expands
  `ß` to `ss`, so `Straße` matches `STRASSE`; `Äpfel` does not match `Apfel` (umlauts are not
  stripped to their base letters).
- **Phrase splitting**: a term is split on any whitespace, so `large   language model` behaves
  like `large language model`; a phrase needs at least one whitespace character between
  words (`large language` does not match `largelanguage`). Hyphens and other punctuation are
  boundaries (`app` matches `app-store`); `_` and digits are word characters (`app` does not
  match `app_store`, `C++` does not match `C++20`).
- **Hit deduplication** is by the folded form, first configured spelling wins: `any: [LLM]`,
  `all: [llm]` reports `["LLM"]` once. Reported spellings are the stripped config terms.
- **Blank terms** cannot pass `KeywordsConfig` validation (`min_length=1`, strings stripped);
  the matcher still ignores them for configs built with `model_construct` (tested that way).
- **`item_text`** skips whitespace-only parts too, not only `None`/empty ones. Because parts
  are joined by newlines, a phrase can span title and teaser (`large` + `language` matches
  `large language`), but two parts never glue into one word.
- **`keyword_filter`** only writes on rejection; a passing item keeps its status (it does not
  advance the status itself) and nothing is flushed. `set_status` flushes but never commits,
  like the other repository writes.
