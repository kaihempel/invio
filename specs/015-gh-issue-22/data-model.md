# Data Model: Manual Job Runs with Dry-Run Output and Run History (#22)

**No schema change, no migration.** All persisted additions are new keys inside `runs.stats`,
which stays at version 1. The other additions are in-memory types.

---

## 1. `runs.stats` additions (persisted, version 1, additive)

| Key | Type | Present | Written by | Meaning |
|---|---|---|---|---|
| `llm_calls_unpriced` | int | every layout that has `llm_calls` | `_ledger_stats` | ledger entries with `cost_usd is None` |
| `processed` | int | normal and dry-run layouts | `build_stats` | items that reached a relevance or summary outcome (rated, summarized or failed) |
| `errors` | list[StoredError] | every finished run | `finalize` or the safety net → `RunRepository.record_errors` | the run's errors in stage order, then by item id (R1, R2) |
| `errors_omitted` | int | only when > 0 | same | errors beyond `MAX_STORED_ERRORS` (100) that were not stored |

**Legacy runs**: runs recorded before this feature (or whose error-list write failed) have no
`errors` key. Runs closed by `run_job`'s safety net store the fatal error only. Readers must treat a missing `errors` key as "unavailable", not as "no
errors".

### StoredError (JSON object)

| Field | Type | Required | Rule |
|---|---|---|---|
| `stage` | string | yes | one of `RunStage` |
| `error_class` | string | yes | non-blank; `type(err).__name__` |
| `message` | string | yes | sanitized (R3): item → outcome `error`; fatal → `sanitized_error(failure)`; source → `"<Class>: source failed"`; other → `"<Class>"`. Max 500 chars. |
| `item_id` | int | no | set for item errors; mutually exclusive with `source` |
| `title` | string | no | item title captured at deduplication, max 300 chars |
| `url` | string | no | item URL captured at deduplication |
| `source` | string | no | source key `"<index>:<type>"`, never the source URL |

Validation lives in `invio.graph.errors.StoredError`, a frozen dataclass with a `to_json()`
method. The read side in `invio.services.runs` parses the entries leniently: an entry that does
not parse is shown as `"<unreadable error entry>"` rather than failing `run show`.

---

## 2. In-memory types (graph / pipeline)

### `RunScope` additions (`invio.graph.scope`)

| Field | Type | Default | Purpose |
|---|---|---|---|
| `max_items` | `int \| None` | `None` | per-run cap from `--max-items`; applied in `load_job` as `min(max_items, limits.max_items_per_run)` |
| `observer` | `RunObserver \| None` | `None` | progress sink (R5) |
| `progress` | `ProgressCounts` | zeros | cumulative counts reported to the observer |
| `item_refs` | `dict[int, ItemRef]` | `{}` | title and URL of every taken item, captured in `deduplicate` |

### `ItemRef` (`invio.graph.scope`, frozen)

`title: str`, `url: str`. Both are untrusted: the title has control characters and ANSI escapes removed and is cut at 300 characters; the URL has no query string or fragment (they can hold tokens).

### `ProgressCounts` (`invio.graph.ports`, mutable dataclass)

`found`, `new`, `after_keyword_filter`, `selected`, `processed`, `relevant`, `failed`: all `int`,
default 0.

### `ProgressSnapshot` (`invio.graph.ports`, frozen)

Same fields as `ProgressCounts`. `ProgressCounts.snapshot()` returns one; a `ProgressEvent`
carries a snapshot, never the mutable counter.

### `ProgressEvent` (`invio.graph.ports`, frozen)

| Field | Type | Notes |
|---|---|---|
| `kind` | `Literal["stage", "item"]` | |
| `stage` | `RunStage` | the stage that completed (`stage` events) or the item's last stage (`item` events) |
| `counts` | `ProgressSnapshot` (frozen copy) | always set |
| `item_id` | `int \| None` | `item` events only |
| `title` | `str \| None` | `item` events only |
| `outcome` | `Literal["relevant", "irrelevant", "failed"] \| None` | `item` events only |
| `message` | `str \| None` | sanitized failure text for `failed` |

### `RunObserver` (`invio.graph.ports`, Protocol)

`def __call__(self, event: ProgressEvent) -> None`. Must be quick and must not block. Any
exception it raises is swallowed and logged as `run.observer_failed`.

### Emission points

| After | Event | Counts updated |
|---|---|---|
| `deduplicate` | stage | `found`, `new` |
| `keyword_prefilter` | stage | `after_keyword_filter`, `selected` |
| each `process_item` result | item + stage(`score_relevance`/`summarize_item`) | `processed`, `relevant`, `failed` |
| `synthesize_digest`, `persist`, `notify`, `finalize` | stage | — |

### `run_job` / `run_job_by_name` signature changes (`invio.pipeline.run`)

New keyword-only parameters: `max_items: int | None = None`, `observer: RunObserver | None = None`.
`RunResult` gains `error: str | None` (`runs.error`), `started_at: datetime` and
`finished_at: datetime | None`. Its `stats` carries the new keys.

---

## 3. Read model (`invio.services.runs`)

### `RunSummary` (frozen)

| Field | Type | Source |
|---|---|---|
| `id` | int | `runs.id` |
| `job_name` | str | `jobs.name` |
| `status` | `RunStatus` | `runs.status` |
| `dry_run` | bool | `stats.get("dry_run", False)` |
| `started_at` | datetime (UTC) | `runs.started_at` |
| `finished_at` | `datetime \| None` | `runs.finished_at` |
| `found`, `new`, `relevant` | `int \| None` | stats (`None` when absent, e.g. recovered runs) |
| `timezone` | str | the job's `schedule.timezone` if its config validates, else `"UTC"` (deviation: on `RunSummary`, so `run list` can format times) |

### `RunDetail` (frozen)

All `RunSummary` fields, plus:

| Field | Type | Notes |
|---|---|---|
| `error` | `str \| None` | `runs.error` |
| `stats` | `Mapping[str, Any]` | full stats dict (read-only copy) |
| `errors` | `tuple[RunErrorView, ...] \| None` | `None` = unavailable (legacy run) |
| `errors_omitted` | int | 0 when absent |
| `timezone` | str | inherited from `RunSummary` |

### `RunErrorView` (frozen)

`stage: str`, `error_class: str`, `message: str`, `item_id: int | None`, `title: str | None`,
`url: str | None`, `source: str | None`.

### Errors

- `RunNotFoundError(LookupError)` with `run_id`.
- `JobNotFoundError` (existing, from `invio.services.jobs`) for an unknown `--job` filter.

---

## 4. State transitions

There are no new statuses. `runs.status` keeps the lifecycle from #21:
`running → succeeded | partial | failed`.

`record_errors` runs only once the status is no longer `running`. It never changes `status`,
`error` or `finished_at`. It merges keys into `stats` only.
