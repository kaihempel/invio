# Research: Assembled Research Workflow (gh-issue-21)

Feature: [spec.md](spec.md) · Plan: [plan.md](plan.md)

## R1 — LangGraph as a new runtime dependency, and which parts of it are used

**Decision**: Add `langgraph` (>= 1.0, < 2) as a runtime dependency. Use `StateGraph` with a
`TypedDict` state, `Send` for the per-item fan-out, conditional edges for routing, and
`compile()` **without a checkpointer**. Every node is `async def`. A graph is compiled once per
run, because its nodes close over the per-run dependencies (R3).

**Rationale**: The issue and the README (`graph/  LangGraph pipelines`) commit the project to
LangGraph, and #29 plugs its video path into the `process_item` subgraph. A checkpointer would
serialize the state after every step. That brings no benefit here (a failed run is not resumed
but re-run, and retries are handled by the deduplication attempts) and would force the state to
be serializable. Sync nodes are run in a thread pool by LangGraph. The run's SQLAlchemy work
session must only be used from one thread (R5), so every node is async and stays on the event
loop.

**Alternatives considered**:
- *Plain `asyncio` orchestration without LangGraph*: simpler, but contradicts the issue, the
  README and the #29 integration point.
- *A compiled module-level graph with dependencies in `config["configurable"]`*: avoids
  rebuilding per run, but the configurable dict is untyped (`Any`) and breaks strict mypy
  (Constitution IV). Compiling a graph of about a dozen nodes costs milliseconds.

The constitution requires every new runtime dependency to be justified in the PR description.
This one is the framework named by the issue.

## R2 — Where `run_job` lives (layering)

**Decision**: Add a new orchestration package, `src/invio/pipeline/`, with `run.py`
(`run_job`, `RunResult`, `JobBusyError`) and `deps.py` (it wires the concrete adapters).
`invio.graph` keeps the state (`graph/state.py`), the graph builder (`graph/build.py`) and the
nodes. It receives notification, next-run computation and source fetching as injected ports
(`graph/ports.py`, `Protocol`s and callables). A new architecture test enforces that
`invio.graph` still imports neither `invio.notify` nor `invio.scheduling`. Only
`invio.pipeline` may import graph, notify, scheduling, sources and llm together.

**Rationale**: `tests/test_graph_layering.py` bans `invio.graph` from importing
`invio.notify`, `invio.scheduling` and `invio.cli`. The synthesis node already mirrors notify's
status rule for this reason. Notification and `compute_next_run` are both needed in the run, so
the composition root has to sit above `graph`. The constitution's dependency direction allows
it (`cli → orchestration (graph, scheduling) → adapters`). Ports also make the acceptance tests
simple: a recording notifier, a raising source and a fixed clock are plain objects.

**Alternatives considered**:
- *`run_job` in `graph/build.py`*: needs `invio.notify` and `invio.scheduling` imports, which
  break the existing layering test.
- *Relax the layering test*: removes a rule that was added deliberately in #18/#20.

## R3 — Run dependencies and ports

**Decision**: `graph/ports.py` defines a frozen dataclass `RunDeps` that is passed to
`build_graph(deps, scope)` (contracts/graph.md):

- `session_factory: sessionmaker[Session]`
- `fetch_source: SourceFetcher`, i.e.
  `async (SourceConfig) -> list[Candidate]` (raises `FetchError`)
- `fetch_page: PageFetcher`, i.e. `async (url) -> str` (the HTML of an item page; raises
  `FetchError`)
- `provider_for: Callable[[LLMConfig], ProviderBinding]`, which returns provider, provider
  name, fast/smart model and registry
- `notify: Notifier`, i.e. `async (digest_id) -> DeliveryReport` (`sent`, `failed`,
  `error: str | None`)
- `next_run: Callable[[ScheduleConfig, datetime], datetime]`
- `clock: Callable[[], datetime]`
- `concurrency: int`, `retry: RetrySettings`, `lock_ttl: timedelta`

`invio.pipeline.deps.default_deps(settings)` builds the production set: `SafeHttpClient`,
`RssFeedSource` and `WebPageSource`, `llm.factory.resolve`, `notify.email.deliver_digest`,
`scheduling.next_run.compute_next_run` and `db.types.utcnow`. It also owns the async context
that closes the HTTP client, the browser renderer and the provider.

**Rationale**: This keeps the layering of R2 and gives every acceptance test a seam. It needs
no network (Constitution III).

## R4 — Retries: at the call boundary, not as node re-execution

**Decision**: Transient failures are retried **around the single external call**:

- *Source fetch*: `fetch_sources` fetches each source through `retrying(fetch_source)`.
- *Item page fetch*: `extract_text` fetches through `retrying(fetch_page)`.
- *LLM calls*: the provider is wrapped in `RetryingProvider`, an `LLMProvider` decorator in
  `invio.llm.retry`. It retries `complete` and `complete_structured` on the transient errors.

The policy is a frozen dataclass, `RetrySettings`, in a new leaf module `invio/retry.py` that
imports nothing from `invio`. It sits next to `domain.py`, so `llm`, `sources` and `graph` can all import
it. Its fields mirror LangGraph's `RetryPolicy` names: `initial_interval=1.0`,
`backoff_factor=2.0`, `max_interval=30.0`, `max_attempts=3` and `jitter=True`. The same module
has `async retrying(call, *, policy, retry_on, retry_after, sleep)`. The wait before attempt
*n* + 1 is `min(max_interval, initial_interval * backoff_factor**(n-1))`, plus up to 10 %
jitter. The wait a server asked for (`retry_after(err)`, for the LLM wrapper
`LLMRateLimitError.retry_after`) is used when it is larger; when it is larger than
`max_interval` the error is raised at once, because an earlier retry would only be
rate-limited again. Each retry logs `retry.attempt` with the error class, attempt number and
wait; giving up early logs `retry.gave_up`. The predicates live next to the errors they
classify: `invio.llm.retry.is_transient_llm` and `invio.sources.errors.is_transient_fetch`.

`retry_on` is a predicate:

- *LLM*: `LLMRateLimitError` and `LLMUnavailableError`.
- *Fetch*: `FetchError`, but not its subclasses `BlockedError`, `TooLargeError` and
  `RenderUnavailableError`, which are permanent.

Every other error is never retried. That includes `LLMAuthError`, `LLMInvalidRequestError`,
`LLMInvalidOutputError` (already repaired once in `structured_with_repair`) and
`ExtractionError`. `sleep` is injected, so tests run without waiting.

Node-level `RetryPolicy` (`add_node(..., retry_policy=...)`) is **not** used for nodes that
write to the database. This holds for the whole graph.

**Rationale**: The existing nodes catch the transient errors themselves. `score_item` and
`summarize_item` turn `PER_ITEM_ERRORS`, which include rate-limit and unavailable errors, into a
`failed` item. A node-level retry policy would therefore never see them. Re-running a whole node
would also repeat work and side effects: earlier chunk calls of a summary, usage rows already
flushed, and `mark_failed` writes. Retrying the one call that failed is cheaper and changes no
node contract. Mirroring LangGraph's `RetryPolicy` field names keeps the
issue's vocabulary without making the `llm` layer import LangGraph, which the layering forbids.

**Alternatives considered**:
- *Node-level `RetryPolicy` on `score_relevance`/`summarize_item`*: never triggers, because the
  errors are caught inside, and it would duplicate side effects.
- *Changing the nodes to let transient errors escape*: breaks the contracts of #16/#17 and
  their tests.
- *`tenacity`*: one more dependency for about 30 lines of logic.

## R5 — Fan-out, the concurrency limit, and the shared work session

**Decision**: `keyword_prefilter` routes through a conditional edge that returns one
`Send("process_item", ItemTask(item_id=…))` for every item that passed. If there is none, it
routes to `join`. `process_item` is a compiled subgraph (R6) added as a node. Each item's
processing is wrapped in `async with deps.semaphore:`, an `asyncio.Semaphore(concurrency)`
created per run (default 4). It is the only bound: LangGraph's `max_concurrency` would add
nothing the semaphore does not already enforce. All items share the run's single
work session. That is safe because every repository call is synchronous and never spans an
`await`, so on the one event-loop thread no two tasks touch the session at the same moment.

The concurrency limit comes from `Settings.max_parallel_items` (`INVIO_MAX_PARALLEL_ITEMS`,
default 4, >= 1). `run_job(..., concurrency=None)` can override it, for tests and #22's
`--max-items`/`--parallel` options.

**Rationale**: Provider rate limits are a property of the deployment (API key tier), not of a
job, so the limit lives in settings rather than `limits`. The semaphore is the measurable bound
for the acceptance test: a fake step counts in-flight items, and the recorded maximum must stay
at or below the limit and reach it. One work session is required by #19 (single commit, R1 of
#19). SQLite allows a single writer, so separate per-item sessions would block on it.

**Alternatives considered**:
- *Only LangGraph `max_concurrency`*: it limits tasks per superstep, but nested subgraph tasks
  make it hard to test as an exact bound, and the issue asks for a semaphore.
- *One session per item, committing per item*: breaks all-or-nothing persistence (#19 FR-001)
  and SQLite locking.

## R6 — The `process_item` subgraph and its error handling

**Decision**: The subgraph has its own state, `ItemState` (`item_id`, `kind`, `outcome`), and
these nodes:

`route_kind` → (`extract_text` | `video_path`) → `score_relevance` → conditional
(`relevant` → `summarize_item`, else → END).

- `video_path` is a pass-through placeholder (clarification 3). It hands the item on to
  `extract_text` unchanged. #29 replaces this node only.
- `extract_text` fetches the item page (R4) **unconditionally** and calls
  `invio.sources.extract.extract_text`. For a web source in page mode, the item URL is the
  source URL (`web.py`, `_candidates`). The run's one `SafeHttpClient` remembers the validators
  of the source fetch, so a conditional GET would come back `304 Not Modified` and the item
  could never be extracted. `SafeHttpClient.get` therefore gains a keyword
  `conditional: bool = True`. `fetch_page` passes `False`, which sends no `If-None-Match` or
  `If-Modified-Since` and does not update the remembered validators. A `NotModified` result for
  an unconditional request cannot happen, and is treated as a bug (`RuntimeError`). The node
  stores the text with the new `ItemRepository.set_extracted(item, text)`, which sets
  `raw_content` and status `extracted`. On `ExtractionError("too_short")` the item continues
  with its title and teaser (`raw_content` stays `None`), so a video page without text behaves
  like a text page without text. The other errors (`FetchError` after retries,
  `ExtractionError("too_large")`) fail the item.
- `score_relevance` and `summarize_item` call the existing `score_item` and `summarize_item`
  with the run's `ScoringContext` and `SummaryContext`.

The whole subgraph runs inside the outer `process_item` node wrapper, which owns the error
boundary:

| Raised inside an item | Effect |
|---|---|
| a per-item LLM error | already handled by the node: item `failed`, outcome returned |
| `FetchError`, `ExtractionError("too_large")`, or any other `Exception` not listed below | the item is marked `failed` with `failure_message(err)`; a `RunError(stage, item_id, error_class)` is appended; the run continues |
| `BudgetExceeded` | the item is left unchanged and the outcome is `budget_stopped`; persist's `unprocessed()` releases it (#19 R4) |
| `LLMAuthError`, `LLMConfigError`, `MissingSettingError` | **run-fatal**: re-raised. The run cannot succeed for any item, so it is recorded `failed` (R8). The other in-flight items are cancelled. |
| `asyncio.CancelledError`, `KeyboardInterrupt` | propagates (R8) |

**Rationale**: This follows step 6 of the issue ("one bad item never aborts the run") without
turning a missing API key into N failed items and a misleading `partial`. Credential and
configuration errors already propagate by contract in #16, #17 and #18.

## R7 — Run state shape

**Decision**: `graph/state.py`:

```text
RunState (TypedDict, total=False)   # job/run ids, config and dry_run live on RunScope
  sources_total: int
  sources_failed: int
  candidates: dict[str, list[Candidate]]          # key = source key (R9)
  counts: StageCounts                              # found / new / after_keyword_filter
  selected: list[int]                              # item ids that passed the prefilter
  taken: list[int]                                 # item ids deduplication took
  items: Annotated[list[ItemResult], operator.add]
  errors: Annotated[list[RunError], operator.add]
  digest: DigestDraft | None
  digest_id: int | None
  status: RunStatus | None
  fatal: RunError | None                           # set by a guarded stage that raised
```

The state holds plain values only (ids and frozen dataclasses), never ORM objects. Nodes load
`Item` rows from the work session by id. `ItemResult` collects the `RelevanceOutcome`, the
`SummaryOutcome` and the item-level error of one item.

**Rationale**: The `operator.add` reducers are what the issue asks for. They make concurrent
`Send` branches merge without losing entries (FR-005), and branch order does not matter. ORM
objects in state would expire on rollback (#19 R7) and tie the state to one session.

## R8 — "finalize always runs": guarded stages, routing, and a safety net

**Decision**: Every stage node is wrapped by `guarded(stage)`. The wrapper catches `Exception`,
logs `run.stage_failed` (stage and error class only) and returns
`{"fatal": RunError(stage, None, class)}`. After each stage a conditional edge routes to
`finalize` when `fatal` is set, otherwise to the next stage. `finalize` is a normal node and the
graph's only path to `END`.

`run_job` additionally wraps `graph.ainvoke` in `try/finally`. If the graph did not reach
`finalize` (cancellation, `KeyboardInterrupt`, or an error inside `finalize` itself), the
`finally` block runs `finalize_fallback`: it records the run `failed`, releases the lock and
re-raises. Finalization is idempotent: it is keyed on the run status (`running` or not) and on
lock ownership (R10).

When a stage fails before the save (`fatal` set, work session open), finalize first rolls back
the work session, then calls the existing `record_failed_run` (#19). That function replays the
budget ledger as usage rows and stores a sanitized error, which keeps the #19 preconditions.

**Rationale**: LangGraph aborts `ainvoke` on an uncaught node exception, so "always runs" needs
both in-graph routing (for testable, normal failures, such as the acceptance criterion
"exception in `fetch_sources`") and a `finally` (for `BaseException`, which a node must not
swallow).

## R9 — Fetching sources, unsupported source types, and the source-status rule

**Decision**: `fetch_sources` iterates the job's enabled sources in configuration order and
fetches them concurrently. This is bounded by the same semaphore; the HTTP client rate-limits
per host anyway. Each source is fetched through the retrying helper, which yields
`candidates[source_key] = list`, where `source_key = f"{index}:{type}"`. Only the order matters
to `deduplicate`. Each result is cut to `limits.max_items_per_source`. Only a `FetchError` that
persists after retries counts as a source failure. Any other exception (a bug, a credential
problem) is **not** caught per source: it reaches the stage guard (R8) and fails the run with
`"<Class>: run failed"`. A source whose fetch still fails with a `FetchError` after retries adds `RunError(stage="fetch_sources", source=key, error_class)` and
increments `sources_failed`. Source types without an adapter yet (`sitemap`,
`youtube_channel`, `youtube_playlist`) are **skipped with a warning** (`source.unsupported`).
They do not count as failed or toward `sources_total`, so the clarified status rule is not
triggered by features that are not built yet.

The status rule (clarification 4, FR-009) extends `persist.decide_status`. `StageCounts` gains
`sources` and `sources_failed`. The order is:

1. `failed`, when `sources >= 1 and sources_failed == sources`, with the new
   `runs.error = "all sources failed"` (`ALL_SOURCES_FAILED_ERROR`);
2. `failed`, when every attempted item failed (as before);
3. `partial`, when `sources_failed > 0`, any item failed, or the budget was exceeded;
4. `succeeded` otherwise.

Then the existing `run_status_after_synthesis` and the delivery rule apply as before. A job
whose enabled sources are all unsupported has `sources == 0` and is not `failed` for that
reason.

**Rationale**: Source keys keep `deduplicate`'s per-source ordering and baseline logic. Skipping
unsupported types prevents a job with a YouTube source from being `partial` forever until #27
and #28 land.

## R10 — The job lock (FR-016), next-run time, and finalize ordering

**Decision**: There are two new `JobRepository` methods, each a single atomic `UPDATE`:

- `claim(job_id, *, now, until) -> bool`:
  `UPDATE jobs SET locked_until=:until WHERE id=:id AND (locked_until IS NULL OR locked_until <= :now)`.
  It returns `rowcount == 1`, in the same form as step 2 of #23.
- `release(job_id, *, until, next_run_at: datetime | None | _Unset) -> bool`:
  `UPDATE jobs SET locked_until=NULL[, next_run_at=:next] WHERE id=:id AND locked_until=:until`.
  It clears only the run's own lock, because the `until` value acts as the ownership token.

`run_job` order:

1. Look up the job (`JobNotFoundError`). A disabled job raises `JobDisabledError`. No lock and
   no run.
2. `claim` in its own committed transaction. If that fails, raise `JobBusyError`. No run is
   created.
3. Start the run (`RunRepository.start`, committed in its own transaction) **in `run_job`**,
   not in a node. Then build the graph and invoke it **inside**
   `run_context(job=name, run_id=str(run_id))`. LangGraph runs each node in its own asyncio
   task, and each task gets a copy of the caller's context at creation. A context variable set
   inside one node is therefore invisible to the next nodes. One set around `ainvoke` reaches
   all of them, including the fan-out tasks and the `finally` safety net.
   `load_job` then validates the stored config and binds the provider (`deps.provider_for`).
   Both are inside the guard, so an invalid config, a missing API key (`MissingSettingError`)
   or an unknown model (`LLMConfigError`) records the run `failed` with no LLM call.
   The error text for an invalid config is built by the extended `_sanitized_error` of #19:
   for a `pydantic.ValidationError` it lists the failing field paths only, never values or
   pydantic's messages (which quote the input), for example
   `"ValidationError: invalid fields search.min_relevance, llm.provider"` (at most 5 paths,
   then `", …"`).
4. `finalize`, in this order:
   (a) close the run: `finalize_run` on success, or `record_failed_run` on `fatal`, in which
   case the work session is rolled back first;
   (b) apply the delivery outcome: `run_status_after_delivery`, then
   `RunRepository.finish(...)` when the status changed;
   (c) compute `next_run_at = deps.next_run(schedule, clock())`. In a dry run the current
   value is kept;
   (d) `release(job_id, until=token, next_run_at=...)` in one transaction;
   (e) return.

Notification runs before finalize, after the work session commit. `deliver_digest` uses its own
sessions and needs the committed digest id.

The lock lifetime is `Settings.run_lock_seconds` (`INVIO_RUN_LOCK_SECONDS`, default 7200, as
in #23). A run must not outlive its lock: the lock's `until` value doubles as the run's
deadline. Every guarded stage up to `persist` and every item node first checks
`deps.clock() >= token` and raises `LockExpiredError`, which fails the run, so a run that took
too long stops at the next boundary instead of racing a run that claimed the expired lock. The
check is a clock comparison, not a database write: a heartbeat `UPDATE` from a second
connection would block on SQLite while the work session holds the write lock. A stage that is
already running when the lock expires is not interrupted; `run_lock_seconds` must stay well
above the longest expected stage. A `next_run_at` back-off after failed runs is
#23's job: finalize uses the plain schedule.

**Rationale**: This is the pairing from clarification 2. `claim` matches #23's SQL, so `run-due`
can call `run_job` directly and treat `JobBusyError` as a skip. Using the `until` value as the
token needs no migration: `locked_until` is stored with microsecond precision on MariaDB
(`DATETIME(6)`), and SQLite keeps it exactly.

## R11 — Dry run (clarification 1, FR-012)

**Decision**: A dry run takes the same path through deduplication, item processing and
synthesis inside the work session. At `persist` it **rolls the work session back** instead of
calling `finalize_run`. This removes item inserts, `mark_taken` attempt increments, statuses,
`raw_content`, summaries, usage rows and the digest. It then finishes the run in a fresh
transaction with `stats = build_stats(...) | {"dry_run": True}` and the status computed by
`decide_status`. `notify` is skipped, and finalize leaves `next_run_at` unchanged but releases
the lock. The digest is only returned in `RunResult.digest`.

For this to work, deduplication's `mark_taken` must stay **uncommitted** in a dry run. In a
normal run the #19 precondition still holds: the run row and the take are committed before the
LLM stages begin. That is done by a commit at the end of `deduplicate`, normal runs only.

**Rationale**: A rollback is the only way that guarantees "every item's status and attempt
count as before" (SC-006). Undoing writes selectively would miss new rows and side columns. The
run row itself is committed earlier, in `load_job`, and is unaffected by the rollback. The
`dry_run` flag in `stats` needs no migration. `STATS_VERSION` stays 1 because the key is
additive and optional (contracts/run-stats-delta.md).

**Alternatives considered**: *A new `runs.dry_run` column*. It would need a migration for a
flag that #22 only displays.

## R12 — Naming: the public `RunResult` vs. the persistence input

**Decision**: Rename `invio.graph.nodes.persist.RunResult` (the input of the save) to
`RunDraft`, to match the existing `DigestDraft`. The public type returned by `run_job` is
`invio.pipeline.run.RunResult`, as in the issue signature. Its fields are `job_id`, `run_id`,
`status`, `dry_run`, `digest: DigestDraft | None`, `stats`, `errors: tuple[RunError, ...]`,
`notifications_sent` and `notifications_failed`.

**Rationale**: Two different `RunResult` types would confuse readers and imports. The rename is
mechanical: one module and its tests, with no behaviour change.

## R13 — Test strategy

- **End-to-end**: a job with an RSS fixture source (`tests/fixtures/feeds`) and article
  fixtures (`tests/fixtures/articles`), served by an `httpx2.MockTransport` behind a real
  `SafeHttpClient` (an allow-listed test network, as in `tests/http_helpers.py`) or by fake
  `fetch_source`/`fetch_page` ports. A scripted `FakeProvider` is keyed by purpose: the script
  order is not deterministic under fan-out, so the plan adds a small purpose-routing fake
  (`tests/pipeline_helpers.py`) on top of `FakeProvider`. The notifier is a recording fake;
  `test_pipeline_email.py` uses the `aiosmtpd` sink from `smtp_helpers` for real delivery.
  SQLite in memory.
- **Concurrency**: a counting gate inside a fake `fetch_page` records the maximum number in
  flight, with 10 items, limits of 4 and 1, and the default.
- **Retries**: a fake `sleep` records the waits. The fake raises a rate-limit error twice, then
  succeeds; `retry_after` is honoured; non-transient errors are attempted once.
- **Failure paths**: a raising `fetch_source` (AC 3), a raising `persist` (via the #19 failure
  injection), a notifier failure, `CancelledError` during item processing, an invalid stored
  config, a busy lock and an expired lock.
- **Dry run**: snapshots of `items`, `digests`, `llm_usage` and `jobs.next_run_at` before and
  after are compared.
- **Layering**: `invio.graph` does not import notify, scheduling or pipeline.
  `invio.pipeline` is not imported by `graph`, `db`, `llm`, `sources` or `notify`.
