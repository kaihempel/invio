# Contract: Summarization Node (`invio.graph.nodes.summarize_item`)

Internal Python API consumed by the pipeline graph (later issue) and by tests.

## Constants

```python
CHUNK_MAX_TOKENS: Final = 3000
CHUNK_OVERLAP_TOKENS: Final = 200
SHORT_TEXT_MAX_TOKENS: Final = CHUNK_MAX_TOKENS
COMBINE_MAX_TOKENS: Final = CHUNK_MAX_TOKENS  # budget of one combine request's parts
MAX_CHUNKS: Final = 20
```

## Result models

- `ItemSummary` — final (and intermediate combine) result: `headline`, 3–6 `bullets`,
  `why_relevant`; stored as JSON. See data-model.md.
- `ChunkSummary` — map result per chunk: 1–6 `bullets`; never stored.

## Functions

```python
def estimate_tokens(text: str) -> int
```
`ceil(len(text) / 4)`; pure.

```python
def split_text(text: str, max_tokens: int, overlap: int) -> list[str]
```
- Raises `ValueError` if `max_tokens < 1` or not `0 <= overlap < max_tokens`.
- `[]` for empty/whitespace-only text; `[text]` if `estimate_tokens(text) <= max_tokens`.
- Otherwise ordered chunks with `estimate_tokens(c) <= max_tokens` for every chunk; new text
  per chunk is at most `max_tokens - overlap` tokens, cut at paragraph, then sentence, then
  word, then character boundaries; when `overlap > 0`, chunk *i+1* starts with a **non-empty**
  tail of chunk *i* of at most `overlap` estimated tokens (word-boundary tail preferred,
  character tail when there is no word boundary), followed by the separator that stood between
  the two in the text (blank line, space, or nothing inside a cut word), so words and numbers
  never run together; the separator counts towards the new-text cap. Pure and deterministic.

```python
def build_messages(
    kind: Literal["short", "chunk", "combine"],
    *, title: str, content: str, interest: str, language: str,
    part: int | None = None, parts: int | None = None, truncated_from: int | None = None,
) -> tuple[str, str]
```
Returns `(system, user)`. System: task + `<interest>` + shape rules + "Write all text in
{name} (ISO 639-1 `{code}`)" + untrusted-data rule (+ part info for `chunk`, truncation note
for `combine` when `truncated_from` is set). User: one `<document>` block with neutralised
`<title>` and `<content>`. Pure.

```python
async def summarize_item(item: Item, ctx: SummaryContext) -> SummaryOutcome
async def summarize_items(items: Iterable[Item], ctx: SummaryContext) -> list[SummaryOutcome]
```
- Body = teaser + extracted text. Short path: exactly one `fast` call (`ItemSummary`); also
  taken with the normalised text when a body over the threshold splits into a single chunk
  (whitespace runs). Long path: `min(len(chunks), MAX_CHUNKS)` `fast` calls (`ChunkSummary`), then `smart` combine
  calls (`ItemSummary`, parts grouped to ≤ `COMBINE_MAX_TOKENS`, ≥ 2 per group; a soft
  budget) until one summary remains.
- Success: `ItemRepository.set_summary(item, summary.model_dump_json())` → status
  `summarized`, `last_error` cleared.
- Per-item errors (`LLMInvalidOutputError`, `LLMUnavailableError`, `LLMRateLimitError`,
  `LLMInvalidRequestError`): remaining calls skipped, `ItemRepository.mark_failed`, outcome
  `FAILED`; the next item continues.
- `LLMAuthError`, `LLMConfigError` and other exceptions propagate.
- One `llm_usage` row per provider call (`summarize` / `summarize_chunk` /
  `summarize_combine`), including invalid answers (`err.usage`).
- Writes are flushed, never committed. `summarize_items` is sequential and returns outcomes
  in input order.

## Log events (stderr JSON, no document text)

| Event | Level | Extra |
|-------|-------|-------|
| `summarize.done` | INFO | `item_id`, `calls`, `chunks`, `truncated` |
| `summarize.truncated` | WARNING | `item_id`, `kept`, `dropped` |
| `summarize.failed` | WARNING | `item_id`, `error` (class name) |

## Shared helper (`invio.graph.nodes.prompting`)

```python
def neutralise(text: str) -> str
```
Moved unchanged from `relevance._neutralise`; swaps `<`/`>` of `document`/`title`/`content`
tags for U+2039/U+203A. `relevance.py` imports it.

```python
def document_message(title: str, content: str) -> str
```
The user message of both nodes: one `<document>` block with neutralised title (cut to
`MAX_TITLE_CHARS = 500`) and content.

## Shared helper (`invio.graph.nodes.llm_calls`)

```python
PER_ITEM_ERRORS  # (LLMInvalidOutputError, LLMUnavailableError, LLMRateLimitError, LLMInvalidRequestError)
async def call_structured(ctx, schema, *, model, purpose, system, user) -> T
def failure_message(err: Exception) -> str
```
`call_structured` records one `llm_usage` row per call (also `err.usage` on an invalid answer).
`failure_message` gives `"<ErrorClass>: <message>"`: a fixed message for an invalid answer,
and only provider, model, HTTP status and retry-after for other LLM errors (the provider's own
message can echo the request). Used by the relevance and summarization nodes.

## Repository addition (`invio.db.repositories.ItemRepository`)

```python
def set_summary(self, item: Item, summary: str) -> None
```
Sets `summary`, status `SUMMARIZED`, clears `last_error`, flushes.
