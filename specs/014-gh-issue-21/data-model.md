# Data Model: Assembled Research Workflow (gh-issue-21)

Feature: [spec.md](spec.md) · Research: [research.md](research.md)

There is **no schema migration**. The changes are new in-memory types, two `JobRepository`
methods, one `ItemRepository` method, an optional `stats` key and one new `runs.error`
constant.

## In-memory types

### RunError (`invio.graph.state`, frozen dataclass)

| Field | Type | Rule |
|---|---|---|
| `stage` | `Literal["load_job", "fetch_sources", "deduplicate", "keyword_prefilter", "extract_text", "score_relevance", "summarize_item", "synthesize_digest", "persist", "notify", "finalize"]` | the stage where it happened |
| `error_class` | `str` | `type(err).__name__`, the only error detail kept |
| `item_id` | `int \| None` | set for item-level errors |
| `source` | `str \| None` | the source key (`"<index>:<type>"`) for source errors; never the URL |

Invariant: neither field holds provider, database or document text (FR-011). At most one of
`item_id` and `source` is set.

### ItemTask (`invio.graph.state`, frozen dataclass)

This is the `Send` payload of the fan-out: `item_id: int` and `kind: ItemType`
(`"article" | "video"`).

### ItemResult (`invio.graph.state`, frozen dataclass)

| Field | Type | Meaning |
|---|---|---|
| `item_id` | `int` | |
| `relevance` | `RelevanceOutcome \| None` | `None` if the item stopped before rating |
| `summary` | `SummaryOutcome \| None` | `None` unless the item was rated `relevant` and summarized (or failed while summarizing) |
| `extracted` | `bool` | whether `raw_content` was stored in this run |
| `budget_stopped` | `bool` | `BudgetExceeded` left the item unchanged |
| `error` | `str \| None` | `failure_message(err)` for an item failed by the error boundary (R6) |

### RunState (`invio.graph.state`, `TypedDict`, `total=False`)

See research R7 for the full field list. The reducers are `items` and `errors`, both
`Annotated[list[...], operator.add]`. Every other key is replaced on write. `fatal: RunError |
None` routes to `finalize` (R8). The run's identity, the validated config and the dry-run flag
are not state: they live on `RunScope`.

### ItemState (`invio.graph.state`, `TypedDict`)

This is the state of the `process_item` subgraph: `item_id`, `kind`, `stage: RunStage` (the
subgraph node currently running, set by each node on entry so the error boundary can name it
in its `RunError`), `extracted: bool`, `relevance: RelevanceOutcome | None`,
`summary: SummaryOutcome | None` and `error: str | None`.

### RunScope (`invio.graph.build`, mutable dataclass, one per run)

This is the per-run holder that the node closures and `run_job`'s safety net share. It is never
part of the LangGraph state.

| Field | Type | Set by | Meaning |
|---|---|---|---|
| `job_id`, `run_id` | `int` | `new_scope` | from `run_job` |
| `token` | `datetime` | `new_scope` | the claimed `locked_until`: the ownership token for `release` and the run's deadline (`LockExpiredError`) |
| `dry_run` | `bool` | `new_scope` | |
| `config` | `JobConfig \| None` | `load_job` | the validated job config |
| `session` | `Session \| None` | `deduplicate` (opened lazily) | the run's single work session |
| `budget` | `BudgetTracker` | `load_job` | `limits.max_llm_tokens_per_run` |
| `binding` | `ProviderBinding \| None` | `load_job` | wrapped in `RetryingProvider` |
| `semaphore` | `asyncio.Semaphore` | `new_scope` | `deps.concurrency`; the only concurrency bound |
| `synthesis` | `SynthesisResult \| None` | `synthesize_digest` | for `run_status_after_synthesis` |
| `delivery` | `DeliveryReport \| None` | `notify` | the notifier's report, or the counts of the run's `notifications` rows when it raised |
| `failure` | `BaseException \| None` | `guarded` | the original exception (`RunError` keeps only its class) for `record_failed_run` |
| `finalized` | `bool` | `finalize` | makes finalize idempotent and tells the safety net whether to act |

Lifecycle: `run_job` creates it with `new_scope` after the run row exists and passes it to
`build_graph`, so a build failure reaches the same safety net. `finalize`
closes `session` (commit or rollback already done) and sets `finalized`. If `ainvoke` ends with
`finalized` still false, `run_job` rolls back `session`, records the failure and releases the
lock (R8).
The wrapper turns it into one `ItemResult` and appends it to `RunState.items`.

### RunDeps (`invio.graph.ports`, frozen dataclass)

See research R3. Validation rules: `concurrency >= 1`; `lock_ttl > 0`;
`retry.max_attempts >= 1`; `retry.initial_interval > 0`; `retry.backoff_factor >= 1`;
`retry.max_interval >= retry.initial_interval`. A violation raises `ValueError` at
construction.

### RetrySettings (`invio.retry`, frozen dataclass)

`initial_interval: float = 1.0`, `backoff_factor: float = 2.0`, `max_interval: float = 30.0`,
`max_attempts: int = 3` and `jitter: bool = True`. The rules are the ones listed under
RunDeps.

### StageCounts (`invio.graph.nodes.persist`, extended)

| Field | Change |
|---|---|
| `found`, `new`, `after_keyword_filter` | unchanged |
| `sources` | **new**, `int = 0`: enabled sources with an adapter |
| `sources_failed` | **new**, `int = 0`: sources still failing after retries |

Defaults keep existing callers and tests valid.

### RunDraft (`invio.graph.nodes.persist`, renamed from `RunResult`)

It has the same fields and behaviour as before (R12) and adds `dry_run: bool = False` and
`store_empty_digest: bool = False`. When the latter is true, `persist_run` also stores a digest
with empty `item_ids` (for `notification.send_if_empty`). It is set by the `persist` stage in
normal runs only.

### RunResult (`invio.pipeline.run`, frozen dataclass, public)

| Field | Type |
|---|---|
| `job_id` | `int` |
| `run_id` | `int` |
| `status` | `RunStatus` (final: after synthesis and delivery rules) |
| `dry_run` | `bool` |
| `digest` | `DigestDraft \| None` (the only copy in a dry run) |
| `stats` | `Mapping[str, Any]` (as stored in `runs.stats`) |
| `errors` | `tuple[RunError, ...]` (stage, item and source errors, in arrival order) |
| `notifications_sent` | `int` (0 in a dry run) |
| `notifications_failed` | `int` |

### Errors (`invio.pipeline.run`)

- `JobBusyError(LookupError)`: another run holds an unexpired lock. Attributes: `job_id` and
  `locked_until`.
- `JobDisabledError(ValueError)`: the job is disabled.
- `JobNotFoundError`: reused from `invio.services.jobs`.

## Persistence changes

### `jobs` (no column change)

| Column | Change in behaviour |
|---|---|
| `locked_until` | set by `JobRepository.claim` at run start to `now + run_lock_seconds`; cleared by `JobRepository.release` only while it still equals the claimed value |
| `next_run_at` | set by finalize to `next_run(schedule, clock())` after every non-dry run, whatever its status; unchanged in a dry run |

### `runs` (no column change)

| Column | Change |
|---|---|
| `status` | can be lowered from `succeeded` to `partial` after delivery (`run_status_after_delivery`) |
| `stats` | optional key `"dry_run": true` (only on dry runs); new keys `"sources"` and `"sources_failed"` (contracts/run-stats-delta.md) |
| `error` | new fixed text `"all sources failed"` (`ALL_SOURCES_FAILED_ERROR`); a stage failure keeps #19's sanitized form `"<Class>: run failed"` |

### `items` (no column change)

The new `ItemRepository.set_extracted(item, text)` sets `raw_content = text` and
`status = extracted`, with one flush. In a dry run every item change is rolled back (R11).

## State transitions

### Run

```text
            claim fails ─────────────► (no run; JobBusyError)
job ─claim─► load_job ─start─► running ──stages──► persist ──► notify ──► finalize ──► succeeded | partial | failed
                                  │                                          ▲
                                  └──── any guarded stage raises (fatal) ────┘   (record_failed_run → failed)
```

### Item, within one run

```text
new/failed(retry) ──take──► [prefilter] ──reject──► skipped_keyword
                                  │pass
                                  ▼
                       extract_text ──ok──► extracted ──► score ──► relevant ──► summarize ──► summarized
                            │                               │            └──► failed
                            │too_short (keeps teaser)       ├──► skipped_irrelevant
                            ▼                               └──► failed
                         score …
                       FetchError / too_large / unexpected ──► failed (last_error = failure_message)
BudgetExceeded anywhere ──► unchanged ──► released at persist (#19)
dry run ──► every transition above rolled back
```
