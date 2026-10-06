# Contract: Digest Synthesis Node (`invio.graph.nodes.synthesize`)

Internal Python API consumed by the pipeline graph (later issue) and by tests. Entities are
defined in [data-model.md](../data-model.md).

## Constants

```python
DIGEST_MAX_OUTPUT_TOKENS: Final = 4000
CLOSER_LOOK_COUNT: Final = 3
PURPOSE_SYNTHESIZE: Final = "synthesize"  # llm_usage.purpose
```

## Functions

```python
def entries_from_items(items: Iterable[Item]) -> list[DigestEntry]
```
Keeps items with status `summarized` and a summary that validates as `ItemSummary`; logs
`synthesize.skipped` (`count`, `item_ids`) when any are dropped. Returns entries in FR-002 order.

```python
def sort_entries(entries: Iterable[DigestEntry]) -> list[DigestEntry]
```
Pure; relevance desc (`None` last) → `published_at` desc (`None` last) → `item_id` asc.

```python
def build_messages(entries: Sequence[DigestEntry], *, interest: str, language: str) -> tuple[str, str]
```
Pure. System: task, `<interest>`, output rules (intro; `##` theme headings; inline links
`[title](url)` with the exact given URLs only; every statement from the items; no reference
links, raw HTML, images, `#` title or closing section), language instruction, untrusted-data
rule. User: one `<document>` block per entry (`document_message`), neutralised. Raises
`KeyError` for a non-ISO-639-1 `language`.

```python
def filter_urls(markdown: str, allowed: Collection[str]) -> UrlFilterResult
```
Pure post-check (research R5). Guarantees: every URL-like string left in `text` is exactly an
element of `allowed`; an inline link with an unknown destination keeps its text; images are
replaced by their alt text; idempotent (`filter_urls(r.text, allowed).text == r.text`); when
`text` is parsed with the notifier's CommonMark settings, every link destination is an element
of `allowed` and no image or raw HTML remains (fails closed otherwise). `linked` is the set of
allowed URLs that are link destinations in that parsed output (so a link inside a code span does
not count, a used reference link to an allowed URL does).

```python
def render_closing(entries: Sequence[DigestEntry], language: str) -> str
def render_more_items(entries: Sequence[DigestEntry], language: str) -> str
def render_fallback(entries: Sequence[DigestEntry], language: str) -> str
```
Pure; titles/takeaways/headlines cleaned by `_escape` (URL-like runs removed, `` \ ` * _ [ ] < > & `` backslash-escaped), URLs bare or in `<…>` form when they contain parentheses; headings and fixed texts
from the `en`/`de` table, English for any other code. `render_closing` lists the first
`CLOSER_LOOK_COUNT` of the (already sorted) entries.

```python
async def synthesize_digest(entries: Sequence[DigestEntry], ctx: SynthesisContext) -> SynthesisResult
```
- No entries → `SynthesisResult(body="", item_ids=(), fallback=False, error=None,
  removed_urls=0, missing_items=0, calls=0)`; no provider call, no usage row.
- Otherwise sorts entries, makes exactly one `ctx.provider.complete(...)` call with
  `model=ctx.smart_model`, `temperature=0.0`, `max_tokens=DIGEST_MAX_OUTPUT_TOKENS`, records one
  usage row (`purpose="synthesize"`), strips one surrounding code fence, applies `filter_urls`,
  checks structure (non-blank, ≥ 1 ATX heading outside code fences), appends
  `render_more_items` (if any entry URL not in `linked`) and `render_closing`.
- `PER_ITEM_ERRORS` or an unusable answer → `render_fallback(...)` with `fallback=True`,
  `error` set, log `synthesize.fallback` (`error` = class name or `unusable_answer`).
- `LLMAuthError`, `LLMConfigError` and other exceptions propagate unchanged.
- Logs `synthesize.done` (`items`, `removed_urls`, `missing_items`, `fallback`); never digest
  text, item text or URLs.

```python
def run_status_after_synthesis(planned: RunStatus, result: SynthesisResult) -> RunStatus
```
`PARTIAL` if `planned is SUCCEEDED and result.fallback`, else `planned`.

## Addition to `invio.graph.nodes.llm_calls`

```python
async def call_text(
    ctx: CallContext, *, model: str, purpose: str, system: str, user: str, max_tokens: int
) -> str
```
One `provider.complete(...)` call (`temperature=0.0`) and one `llm_usage` row on success;
exceptions propagate (no usage is reported by a failed request).
