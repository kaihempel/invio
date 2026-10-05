# Contract: Relevance Scoring Node (`invio.graph.nodes.relevance`)

Internal Python API consumed by the pipeline graph (wired in a later issue). Signatures are
binding; bodies are not part of this contract.

## Public names (`__all__`)

`MAX_DOCUMENT_CHARS`, `RelevanceResult`, `RelevanceOutcome`, `ScoringContext`,
`build_messages`, `score_item`, `score_items`

## Constants

```python
MAX_DOCUMENT_CHARS: Final = 4000  # body characters sent after the title
PURPOSE: Final = "relevance"  # llm_usage.purpose
```

## Types

```python
class RelevanceResult(BaseModel):  # extra="forbid"
    score: float  # strict JSON number (no bool, no numeric string), 0..1, finite
    reason: str  # non-blank
    key_points: list[str]


@dataclass(frozen=True, kw_only=True, slots=True)
class RelevanceOutcome:
    item_id: int
    status: ItemStatus  # RELEVANT | SKIPPED_IRRELEVANT | FAILED
    relevance: Decimal | None
    result: RelevanceResult | None
    error: str | None


@dataclass(frozen=True, kw_only=True, slots=True)
class ScoringContext:
    job_id: int
    run_id: int | None
    search: SearchConfig  # semantic_description, min_relevance
    provider: LLMProvider  # from invio.llm.factory.resolve(llm, "fast")
    provider_name: str  # llm_config.provider.value
    model: str  # llm_config.models.fast
    registry: ModelRegistry  # for cost
    items: ItemRepository
    usage: UsageRepository
```

## Functions

```python
def build_messages(
    title: str, teaser: str | None, text: str | None, semantic_description: str
) -> tuple[str, str]:
    """Pure. Return (system, user)."""
```

- `system` contains the task, `semantic_description`, the 0..1 scale and the untrusted-data
  rule (research R3).
- `user` is exactly one `<document>…</document>` block; title and body inside it have
  delimiter tags neutralised; body is cut to `MAX_DOCUMENT_CHARS` with a `[truncated]` marker.
- Document text never appears in `system`.

```python
async def score_item(item: Item, ctx: ScoringContext) -> RelevanceOutcome:
```

- Calls `ctx.provider.complete_structured(system, user, RelevanceResult, model=ctx.model,
  temperature=0.0)` exactly once.
- Records usage (one row, `purpose="relevance"`) on success and on `LLMInvalidOutputError`.
- Success: `ItemRepository.set_relevance(item, relevance, status)`; status is `RELEVANT` iff
  the two-decimal relevance `>= min_relevance`.
- `LLMInvalidOutputError | LLMUnavailableError | LLMRateLimitError | LLMInvalidRequestError`:
  `ItemRepository.mark_failed(item, error)`; returns a `FAILED` outcome; does not raise.
- `LLMAuthError`, `LLMConfigError` and any other exception: propagate; the item is unchanged.
- Flushes, never commits.

```python
async def score_items(items: Iterable[Item], ctx: ScoringContext) -> list[RelevanceOutcome]:
```

- Scores sequentially in input order; one outcome per item, same order.
- A per-item failure never stops the loop; a propagating error stops it (items already
  scored keep their flushed state).
- Transactions belong to the caller: commit after a normal return. When an error propagates,
  the items scored before it (and their `llm_usage` rows) are flushed but not committed; the
  caller decides whether to commit them (keep the spent tokens on record) or roll back.
- Delimiter neutralisation matches ASCII `<`/`>` only; look-alikes pass through (see
  `context.md`, Known limitations). The schema check is the backstop.

## Repository additions (`invio.db.repositories.ItemRepository`)

```python
def set_relevance(self, item: Item, relevance: Decimal, status: ItemStatus) -> None:
    """Store relevance and status, clear last_error, flush."""


def mark_failed(self, item: Item, error: str) -> None:
    """Set status FAILED and last_error, flush (relevance unchanged)."""
```
