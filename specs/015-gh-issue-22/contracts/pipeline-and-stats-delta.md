# Contract delta: pipeline API, progress port, `runs.stats` (still version 1)

This builds on [#21 run-job](../../014-gh-issue-21/contracts/run-job.md),
[#21 run-stats-delta](../../014-gh-issue-21/contracts/run-stats-delta.md) and
[#19 run-stats](../../013-gh-issue-19/contracts/run-stats.md).

## `invio.pipeline.run`

```python
async def run_job(
    job_id: int,
    *,
    dry_run: bool = False,
    deps: RunDeps | None = None,
    concurrency: int | None = None,
    max_items: int | None = None,  # new: >= 1, else ValueError (before any write)
    observer: RunObserver | None = None,  # new: progress sink, never fails the run
) -> RunResult: ...


async def run_job_by_name(
    name: str,
    *,
    dry_run: bool = False,
    deps: RunDeps | None = None,
    concurrency: int | None = None,
    max_items: int | None = None,
    observer: RunObserver | None = None,
) -> RunResult:
    """Resolve ``name`` to the job id, then ``run_job``. Unknown name → JobNotFoundError(name),
    raised before any run row or lock change."""
```

`RunResult` gains three fields: `error: str | None` (`runs.error`), `started_at: datetime` and
`finished_at: datetime | None`. They are read from the run row together with `status`.

`max_items` sets the run's effective `limits.max_items_per_run` to `min(max_items, configured)`,
in memory only. The stored job config is never written.

## `invio.graph.ports`

```python
@dataclass(slots=True)
class ProgressCounts:
    found: int = 0
    new: int = 0
    after_keyword_filter: int = 0
    selected: int = 0
    processed: int = 0
    relevant: int = 0
    failed: int = 0

    def snapshot(self) -> "ProgressSnapshot": ...  # mutable counter -> frozen copy


@dataclass(frozen=True, slots=True)
class ProgressSnapshot:  # same fields as ProgressCounts; what an event carries (deviation)
    ...


@dataclass(frozen=True, slots=True, kw_only=True)
class ProgressEvent:
    kind: Literal["stage", "item"]
    stage: RunStage
    counts: ProgressSnapshot  # a frozen copy
    item_id: int | None = None
    title: str | None = None
    outcome: Literal["relevant", "irrelevant", "failed"] | None = None
    message: str | None = None


class RunObserver(Protocol):
    def __call__(self, event: ProgressEvent) -> None: ...
```

Guarantees:
- Events are delivered on the event-loop thread.
- Stage events come in stage order. Item events come in completion order.
- `counts` only ever grow during a run.
- An observer exception (also one raised while the snapshot is built) is logged
  (`run.observer_failed`, class only) and ignored. Only `Exception` is caught, never
  `BaseException`. After its first failure the observer is dropped and not called again.
- `ProgressEvent`, `ProgressSnapshot`, `RunObserver` and `RunDeps` are re-exported from
  `invio.pipeline`, so the CLI does not import `invio.graph`.
- Item titles are cleaned (control characters and ANSI escapes removed, cut at 300 characters)
  and item URLs are stored without query string and fragment (`ItemRef`).

## `invio.db.repositories.RunRepository` additions

```python
def record_errors(
    self, run_id: int, entries: Sequence[Mapping[str, Any]], *, omitted: int = 0
) -> bool:
    """Merge ``errors`` (and ``errors_omitted`` when > 0) into ``runs.stats``; flush only.
    False (no change) for an unknown run or a run still ``running``. ``status``, ``error`` and
    ``finished_at`` are never touched. A NULL ``stats`` becomes {"version": 1, "errors": [...]}."""


def list_recent(
    self, *, job_id: int | None = None, limit: int = 20
) -> list[tuple[Run, str, dict[str, Any]]]:
    """(run, job name, job config) tuples, newest first (started_at DESC, id DESC); one query.
    The config (deviation) lets the service derive the job's time zone without a second query."""
```

## `invio.graph.errors` (new module)

```python
MAX_STORED_ERRORS: Final = 100


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredError:
    stage: RunStage
    error_class: str
    message: str
    item_id: int | None = None
    title: str | None = None
    url: str | None = None
    source: str | None = None

    def to_json(self) -> dict[str, Any]: ...  # omits None fields


def stored_errors(
    errors: Sequence[RunError],
    items: Sequence[ItemResult],
    refs: Mapping[int, ItemRef],
    failure: BaseException | None,
) -> tuple[list[StoredError], int]:
    """Pure: sorted (stage order, item id), de-duplicated, capped at MAX_STORED_ERRORS;
    returns (entries, omitted)."""
```

## `runs.stats` new keys (version stays 1)

| Key | Type | Present | Meaning |
|---|---|---|---|
| `llm_calls_unpriced` | int | wherever `llm_calls` is | calls whose cost is unknown |
| `processed` | int | normal and dry-run layouts (not the recovery layout) | items that reached a relevance or summary outcome (rated, summarized or failed; deviation: both stages, so an item failed at `summarize_item` counts) |
| `errors` | list[object] | runs closed by `finalize` or by the safety net | see data-model §1 |
| `errors_omitted` | int | only when > 0 | entries beyond the cap |

The `errors` list is written by `finalize`, after the status has been saved and before the lock
is released, in its own transaction. A failure of that write is logged
(`run.errors_not_recorded`) and does not change the run's status. When the graph does not
reach `finalize` (Ctrl-C, cancellation, a crash), `run_job`'s safety net stores the fatal
error as the only entry, at the run-level stage that was running.

The #19 invariant "token keys equal `UsageRepository.totals_for_run`" is unchanged for
non-dry runs. `llm_calls_unpriced` is checked against the ledger.

## `invio.graph.nodes.persist`

`_sanitized_error` is renamed to public `sanitized_error` (same behaviour), so that
`invio.graph.errors` can reuse it.

## `invio.services.runs` (new)

```python
class RunNotFoundError(LookupError):
    run_id: int


class RunService:
    def __init__(self, session_factory: sessionmaker[Session]) -> None: ...
    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "RunService": ...
    def list(self, *, job: str | None = None, limit: int = 20) -> list[RunSummary]: ...
    def get(self, run_id: int) -> RunDetail: ...
```

The record types are described in data-model §3.
