# Contract: Python API of `invio.graph.nodes.deduplicate` and repository additions

Consumers: graph builder / `fetch_sources → deduplicate → keyword_prefilter` (#21), keyword
filter (#15), persistence (#19). Signatures are the contract; bodies are implementation detail.
All datetimes are timezone-aware UTC. Callers own the unit of work (`session_scope`).

## `invio.config.job` (addition)

```python
class LimitsConfig(_StrictModel):
    max_items_per_source: StrictInt = Field(default=20, ge=1)
    max_items_per_run: StrictInt = Field(default=100, ge=1)
    baseline_items: StrictInt = Field(default=10, ge=1)  # new
    max_items_in_notification: StrictInt = Field(default=20, ge=1)
    max_llm_tokens_per_run: StrictInt = Field(default=200000, ge=1)
```

Job file: `limits.baseline_items` (optional). Invalid values fail validation with the field
path `limits.baseline_items` (CLI exit code 2).

## `invio.db.repositories` (additions)

```python
class ItemRepository:
    def find_many(self, job_id: int, url_hashes: Collection[str]) -> dict[str, Item]:
        """Return the job's stored items for ``url_hashes``, keyed by url_hash (batched IN)."""

    def list_waiting(self, job_id: int) -> list[Item]:
        """Return the job's items with status ``new`` and 0 attempts, ordered by id."""

    def reset_version(self, item: Item, candidate: Candidate) -> Item:
        """Store a new content version: content_hash/title/teaser/published_at from
        ``candidate``; status ``new``, attempts 0, last_error ``None``, run_id ``None``."""

    def mark_taken(self, items: Iterable[Item], run_id: int) -> None:
        """Link ``items`` to ``run_id`` and increment each item's attempts by one."""

    def mark_skipped(self, item: Item, status: ItemStatus, *, reason: str) -> Item:
        """Set ``status`` and store ``reason`` in ``last_error``."""


class RunRepository:
    def has_successful_run(self, job_id: int) -> bool:
        """Whether the job has any run with status ``succeeded`` or ``partial``."""
```

All write methods flush and never commit (existing repository convention).

## `invio.graph.nodes.deduplicate`

```python
MAX_ATTEMPTS: Final = 3
BASELINE_REASON: Final = "baseline"

SelectReason = Literal["new", "changed", "retry", "waiting"]


@dataclass(frozen=True, slots=True, kw_only=True)
class SelectedItem:
    id: int
    url: str
    url_hash: str
    type: ItemType
    title: str
    published_at: datetime | None
    teaser: str | None
    content_hash: str | None
    attempts: int
    reason: SelectReason


@dataclass(frozen=True, slots=True, kw_only=True)
class DedupStats:
    found: int
    new: int
    changed: int
    retried: int
    waiting: int
    dropped: int
    baseline_skipped: int
    limit_cut: int
    selected: int
    baseline: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class DedupResult:
    items: list[SelectedItem]  # newest first
    stats: DedupStats


def deduplicate(
    session: Session,
    *,
    job_id: int,
    run_id: int,
    candidates: Mapping[str, Sequence[Candidate]],
    limits: LimitsConfig,
) -> DedupResult:
    """Select the items this run processes and record the outcome for the job.

    Per-job dedupe, changed-version detection, retries (< MAX_ATTEMPTS), baseline mode
    (no succeeded/partial run → newest ``limits.baseline_items`` per source key), waiting
    backlog, ``limits.max_items_per_run`` newest-first. Flushes, never commits. Logs one
    structured ``deduplicated`` line with the counts of ``DedupStats``.
    """
```

### Behavioural guarantees

- Same stored state + same input → same result (deterministic ordering, see data-model).
- Calling twice with identical candidates and no cut items: second call returns `[]`.
- No item appears twice in `items`; `len(items) <= limits.max_items_per_run`.
- Never raises for empty input; returns stored waiting items (if any) up to the limit.
- Database errors propagate unchanged (caller's unit of work rolls back).
