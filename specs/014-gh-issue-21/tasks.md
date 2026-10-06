---

description: "Task list for the assembled LangGraph research workflow with fan-out and retries (#21)"
---

# Tasks: Assembled Research Workflow with Parallel Item Processing and Retries

**Input**: Design documents from `specs/014-gh-issue-21/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/run-job.md](contracts/run-job.md),
[contracts/graph.md](contracts/graph.md), [contracts/run-stats-delta.md](contracts/run-stats-delta.md),
[quickstart.md](quickstart.md)

**Tests**: Included. Constitution III requires an automated test for every acceptance criterion,
including the rejection paths. Write each story's tests first and confirm they fail.

**Organization**: Tasks are grouped by user story, in spec priority order: US1, US2 and US5
(P1), then US3 and US4 (P2), then US6 (P3). Quickstart scenario numbers (`S<n>`) refer to
[quickstart.md](quickstart.md).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story the task belongs to (US1–US6)

## Conventions used by every task

- Source goes in `src/invio/` and tests in `tests/`. Strict mypy, with no new `Any` except the
  `runs.stats` dict.
- **Layering**:
  - `invio.graph` must never import `invio.notify`, `invio.scheduling`, `invio.cli` or
    `invio.pipeline`. Those are injected through `RunDeps` (research R2, R3).
  - `invio.retry` is a dependency-free leaf. It imports nothing from `invio` except
    `invio.llm.base` and `invio.sources.errors` for the predicates; see T005.
- **Nodes and sessions**:
  - Every graph node is `async def`.
  - State holds ids and frozen dataclasses only, never ORM objects (R7).
  - All items share the run's single work session, and repository calls never span an `await`
    (R5).
- **Transactions**: the only places that commit are `load_job` (run start), `deduplicate`
  (normal runs only), `finalize_run`/`record_failed_run` (#19), the dry-run finish (US6),
  `JobRepository.claim`/`release` (own short transactions) and `deliver_digest` (own
  sessions).
- **Errors and logging**:
  - `RunError` and log events carry error classes, item ids and source keys
    (`"<index>:<type>"`). They never carry URLs with a query string, provider messages,
    database text or document text (FR-011).
  - Logger: `logging.getLogger("invio.graph")` in `graph/` and
    `logging.getLogger("invio.pipeline")` in `pipeline/`, with `extra={...}`.
- **Tests**:
  - Use `tests/pipeline_helpers.py` (T012): the purpose-routed fake provider, fake ports and
    the job factory.
  - Tests that commit use `db_engine` + `session_factory(db_engine)` + `clean_jobs` (as in
    `tests/test_persist.py`).
  - Clock: the `fake_clock` fixture. Sleeps: `RecordingSleep`. No network.
- **Quality gates after each phase**:
  `uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: a green baseline and the new dependency.

- [X] T001 Run the full quality gates on the current tree (`uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest`) and note any failures that already exist before any change.
- [X] T002 Add the runtime dependency `langgraph>=1.0,<2` with `uv add "langgraph>=1.0,<2"`. This updates `pyproject.toml` and `uv.lock`. Then confirm `uv sync --locked` works and that `uv run python -c "from langgraph.graph import StateGraph, START, END; from langgraph.types import Send"` succeeds. Note the justification for the PR description: issue #21, the README `graph/  LangGraph pipelines` line, and the #29 integration point (research R1).
- [X] T003 [P] Check whether `mypy --strict` accepts LangGraph's typing for `StateGraph[...]`, `CompiledStateGraph` and `Send` in a scratch module. If the package lacks `py.typed` or stubs, add a narrow `[[tool.mypy.overrides]] module = ["langgraph.*"]` entry in `pyproject.toml` with a comment explaining why (Constitution IV), and keep `ignore_missing_imports` off for everything else.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the shared types, ports, settings, repository methods, retry helper and test
helpers that every story builds on.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [X] T004 [P] Add two fields to `Settings` in `src/invio/config/settings.py`: `max_parallel_items: int = Field(default=4, ge=1)` (env `INVIO_MAX_PARALLEL_ITEMS`) and `run_lock_seconds: int = Field(default=7200, ge=60)` (env `INVIO_RUN_LOCK_SECONDS`). Add tests in `tests/test_settings.py` (or the existing settings test module found with `grep -rln "class Settings\|get_settings" tests`). The tests cover the defaults, env overrides, and that `0` and `59` are rejected.
- [X] T005 [P] Create the leaf module `src/invio/retry.py` (research R4):
  - `RetrySettings` is a frozen dataclass with `initial_interval: float = 1.0`, `backoff_factor: float = 2.0`, `max_interval: float = 30.0`, `max_attempts: int = 3` and `jitter: bool = True`. `__post_init__` raises `ValueError` unless `max_attempts >= 1`, `initial_interval > 0`, `backoff_factor >= 1` and `max_interval >= initial_interval`.
  - `Sleep = Callable[[float], Awaitable[None]]`.
  - `async def retrying[T](call: Callable[[], Awaitable[T]], *, policy: RetrySettings, retry_on: Callable[[BaseException], bool], what: str, sleep: Sleep = asyncio.sleep, rand: Callable[[], float] = random.random) -> T`. It retries only when `retry_on(err)` holds and attempts remain.
  - The wait before attempt n+1 is `min(policy.max_interval, max(policy.initial_interval * policy.backoff_factor ** (n - 1), retry_after))`. Then, when `jitter`, it is multiplied by `1 + 0.1 * rand()` and capped at `max_interval` again. `retry_after` is `err.retry_after` for an `LLMRateLimitError` with a non-`None` value, otherwise 0.
  - Each retry logs `retry.attempt` with `what`, `attempt`, `wait_s` and `error` (the class name only). The last error is re-raised unchanged.
  - Predicates: `is_transient_llm(err)` is true for `LLMRateLimitError` and `LLMUnavailableError`. `is_transient_fetch(err)` is true for `FetchError` but not for `BlockedError`, `TooLargeError` or `RenderUnavailableError`.
- [X] T006 [P] Write `tests/test_retry.py` for T005 using `RecordingSleep` (a list-appending async fake) and `rand=lambda: 0.0`. It covers:
  - Two failures, then success: 3 calls, sleeps `[1.0, 2.0]`.
  - `max_attempts=3` exhausted: 3 calls, sleeps `[1.0, 2.0]`, and the last error is re-raised (identity check).
  - `retry_after=5` is used when larger and capped at `max_interval`.
  - With `rand=lambda: 1.0` the wait stays ≤ `max_interval` and ≤ 1.1 × base.
  - A non-matching error is called once with no sleep.
  - Each predicate case of T005, including `BlockedError`, `TooLargeError` and `RenderUnavailableError` → `False`, and `FetchError("timeout")` → `True`.
  - `RetrySettings` validation errors.
  - The `retry.attempt` log records via `caplog` carry no error message text.
- [X] T007 [P] Extend `src/invio/db/repositories.py` (contracts/run-stats-delta.md):
  - `JobRepository.get(job_id) -> Job | None`.
  - `JobRepository.claim(job_id, *, now, until) -> bool`: one `update(Job).where(Job.id == job_id, or_(Job.locked_until.is_(None), Job.locked_until < now)).values(locked_until=until)` that returns `rowcount == 1`. It does not commit.
  - `JobRepository.release(job_id, *, until, next_run_at=KEEP) -> bool`: one `update(Job).where(Job.id == job_id, Job.locked_until == until).values(locked_until=None[, next_run_at=…])`. `KEEP` is a module sentinel; `None` is a legal value that clears `next_run_at`.
  - `ItemRepository.set_extracted(item, text)`: `raw_content = text`, `status = ItemStatus.EXTRACTED`, one flush.
  - Docstrings note that the claim SQL matches #23 step 2 and that the `until` value is the ownership token.
- [X] T008 [P] Write `tests/test_db_job_lock.py` for T007 using `db_engine`, `session_factory` and `clean_jobs`, committing each call in its own `session_scope`. It covers:
  - Claim on `NULL` succeeds.
  - A second claim before expiry fails and leaves `locked_until` unchanged.
  - Claim on an expired lock (`locked_until < now`) succeeds.
  - `release` with the right token clears the lock and sets `next_run_at` when given, keeps it with `KEEP`, and clears it with `None`.
  - `release` with a stale token returns `False` and changes nothing.
  - `locked_until` round-trips at microsecond precision.
  - A `db`-marked test runs two threads that claim the same job concurrently on the server engine and expects exactly one `True`. It is skipped on SQLite via `uses_sqlite()`.
  - Also add `set_extracted` tests in `tests/test_db_items.py`.
- [X] T009 Rename `RunResult` → `RunDraft` in `src/invio/graph/nodes/persist.py` (`__all__`, type hints, docstrings) and update every use in `tests/test_persist.py` and any other module found with `grep -rn "RunResult" src tests`. Add these fields, with defaults so existing callers stay valid:
  - `StageCounts.sources: int = 0` and `StageCounts.sources_failed: int = 0`.
  - `RunDraft.dry_run: bool = False`.
  - `RunDraft.store_empty_digest: bool = False`. When true, `persist_run` also stores a digest whose `item_ids` is empty (instead of skipping it). Add a test to `tests/test_persist.py` for both values of the flag.

  Then confirm `tests/test_persist.py` passes unchanged in behaviour (research R12, data-model StageCounts and RunDraft).
- [X] T010 [P] Create `src/invio/graph/state.py` (research R7, data-model.md):
  - `RunStage`: a `Literal` of the 11 stage names in data-model.md.
  - `RunError`: a frozen, slotted, `kw_only` dataclass with `stage: RunStage`, `error_class: str`, `item_id: int | None = None` and `source: str | None = None`. `__post_init__` raises `ValueError` when both `item_id` and `source` are set, or when `error_class` is blank.
  - `ItemTask(item_id: int, kind: ItemType)`.
  - `ItemResult(item_id, relevance: RelevanceOutcome | None, summary: SummaryOutcome | None, extracted: bool, budget_stopped: bool, error: str | None)`.
  - `ItemState`: a `TypedDict` with `item_id`, `kind`, `stage: RunStage`, `extracted`, `relevance`, `summary` and `error`. Each subgraph node sets `stage` to its own name on entry, so the error boundary (T032) can name the node that raised.
  - `RunState`: a `TypedDict(total=False)` with exactly the keys of research R7. `items: Annotated[list[ItemResult], operator.add]` and `errors: Annotated[list[RunError], operator.add]`.
  - A helper `error_of(stage, err, *, item_id=None, source=None) -> RunError` that stores `type(err).__name__` only.
- [X] T011 [P] Create `src/invio/graph/ports.py` (research R3), with no import of `invio.notify` or `invio.scheduling`:
  - Protocols: `SourceFetcher` (`async __call__(config: SourceConfig) -> list[Candidate]`), `PageFetcher` (`async __call__(url: str) -> str`) and `Notifier` (`async __call__(digest_id: int) -> DeliveryReport`).
  - Frozen dataclasses: `DeliveryReport(sent: int, failed: int, error: str | None)` and `ProviderBinding(provider: LLMProvider, provider_name: str, fast_model: str, smart_model: str, registry: ModelRegistry)`.
  - `RunDeps`, a frozen dataclass with `session_factory`, `fetch_source`, `fetch_page`, `provider_for: Callable[[LLMConfig], ProviderBinding]`, `notify`, `next_run: Callable[[ScheduleConfig, datetime], datetime]`, `clock: Callable[[], datetime]`, `concurrency: int = 4`, `retry: RetrySettings = RetrySettings()`, `lock_ttl: timedelta = timedelta(hours=2)` and `sleep: Sleep = asyncio.sleep`.
  - `__post_init__` raises `ValueError` unless `concurrency >= 1` and `lock_ttl > timedelta(0)`.
  - Add `tests/test_graph_state.py`, covering the `RunError` invariants, `error_of` keeping only the class name, and the `RunDeps` validation.
- [X] T012 Create `tests/pipeline_helpers.py`, shared by every `tests/test_pipeline_*.py`:
  - `RoutedFakeProvider(LLMProvider)`: it routes each request to a per-purpose queue or callable, keyed by the `purpose` its caller uses. `relevance`, `summarize*` and `synthesize` are told apart from the system prompt text of `relevance.build_messages`, `summarize_item.build_messages` and `synthesize.build_messages`, because the provider API carries no purpose. Each queue holds `FakeReply` or `LLMError` steps, or `lambda user: FakeReply(...)` keyed by the item title in the user message. It records requests with a timestamp and is deterministic under fan-out.
  - `FakePorts`: `fetch_source` maps a source key → candidates, an exception, or a list of outcomes per attempt. `fetch_page` maps an item url → HTML string or exception, with an optional `asyncio.Event` gate and in-flight counter (`max_in_flight`). `RecordingNotifier` returns a configurable `DeliveryReport` and records digest ids.
  - `RecordingSleep`.
  - `make_job_config(**overrides)`, built from `job_data` with an RSS source and `language="en"`.
  - `store_job(factory, config, *, enabled=True, next_run_at=…, locked_until=None) -> int`.
  - `make_deps(factory, ports, provider, *, clock, concurrency=4, retry=RetrySettings(jitter=False)) -> RunDeps`, which uses the fixture registry `tests/fixtures/llm/models.d`.
  - `snapshot(factory, job_id)`: rows of `items` (all columns), `digests`, `llm_usage` and `notifications`, plus `jobs.next_run_at` and `locked_until`, for comparisons.
  - `article_html(title, paragraphs)`, which builds pages long enough to pass `extract_text`'s `min_chars`, or reads `tests/fixtures/articles/news_article.html`.

**Checkpoint**: gates are green. The retry helper, lock methods, state and ports types exist and
are tested. `RunDraft` is renamed and nothing else has changed.

---

## Phase 3: User Story 1 - One job run produces a digest end to end (Priority: P1) 🎯 MVP

**Goal**: `run_job(job_id)` loads the job, fetches its sources, deduplicates, filters, rates
and summarizes the items, synthesizes and saves the digest, notifies, and finalizes the run.
Finalizing sets `next_run_at` and releases the lock.

**Independent Test**: S1–S4, S24, S25 and S28–S31. With `FakePorts`, `RoutedFakeProvider` and SQLite,
a run of a 3-entry RSS job stores a digest of 3 items, ends `succeeded`, calls the notifier once,
advances `next_run_at` and clears `locked_until`.

### Tests for User Story 1 ⚠️

> Write these first and make sure they fail before T018–T026.

- [X] T013 [P] [US1] Write `tests/test_pipeline_run.py`:
  - **S1**: 3 entries, all relevant. Assert `RunResult.status == RunStatus.SUCCEEDED`, `run_id` matches the only `runs` row, and that row is `succeeded` with `finished_at` set. One `digests` row exists with `item_ids` equal to the 3 ids. Items are `summarized` with `raw_content` set. Stats include `found=3, new=3, after_keyword_filter=3, relevant=3, summarized=3, failed=0, sources=1, sources_failed=0`. `RecordingNotifier` got exactly that digest id, `jobs.next_run_at == recording_next_run(schedule, fake_clock())`, `locked_until is None`, and `RunResult.errors == ()`.
  - **S2**: wrap the ports to record calls. The order of the first calls is fetch_source → fetch_page × 3 (any order) → synthesize request → notifier, and each item page is fetched exactly once.
  - **S3**: a second run gets no LLM requests, ends `succeeded` with `found=3, new=0`, and `next_run_at` is recomputed.
  - **S4**: one item rated 0.1 ends `skipped_irrelevant`, gets no summarize request, and is not in `digest.item_ids`.
  - **Keyword filter**: an item that fails the keyword filter ends `skipped_keyword` and its page is never fetched.
  - **S30 (page-mode web source)**: a `web` source in `mode: page`, served by `httpx2.MockTransport` behind a real `SafeHttpClient` (allow-listed test network, as in `tests/http_helpers.py`). The server sends an `ETag` and answers `304` to any request carrying `If-None-Match`. The item, whose URL equals the source URL, is extracted and summarized, not failed, and the item-page request carried no `If-None-Match`.
  - **S31 (empty digest)**: a run with no new items and `notification.send_if_empty: true` stores one digest with empty `item_ids` and the notifier is called with its id. With `send_if_empty: false`, no digest is stored and the notifier is not called.
- [X] T014 [P] [US1] Write `tests/test_graph_extract.py` for the extract node and video path:
  - An article page is stored via `set_extracted` (status `extracted`, `raw_content` = the extracted text).
  - **S25**: a page shorter than `min_chars` leaves `raw_content` `None` and the item continues (no error).
  - **S24**: a `type="video"` task goes through `video_path`, then `extract_text`, and its outcome equals an article's.
  - `ExtractionError("too_large")` and `TooLargeError` propagate out of the node (the US2 boundary handles them).
  - In `tests/test_http_conditional.py`: after a `200` with `ETag`, `client.get(url, conditional=False)` sends neither `If-None-Match` nor `If-Modified-Since`, returns a `FetchResult` even when the server would answer `304`, and leaves the remembered validators unchanged (a following `get(url)` still sends the original `If-None-Match`).
- [X] T015 [P] [US1] Write `tests/test_graph_build.py`. It checks the topology with `build_graph(deps, job_id=…, run_id=…, token=…, dry_run=False)[0].get_graph()`:
  - The node names are `load_job`, `fetch_sources`, `deduplicate`, `keyword_prefilter`, `process_item`, `join`, `synthesize_digest`, `persist`, `notify` and `finalize`.
  - `finalize` is the only predecessor of `END`.
  - The `process_item` subgraph has `route_kind`, `extract_text`, `video_path`, `score_relevance` and `summarize_item`.
  - With no selected items, `keyword_prefilter` routes straight to `join` and the synthesis gets no entries.
- [X] T016 [P] [US1] Write the run-context test in `tests/test_pipeline_run.py` (**S28**). Capture the records of S1 with a handler that reads `invio.log.job_var` and `run_id_var` at emit time. Every record emitted between `run.started` and `run.finalized` has `job == <job name>` and `run_id == str(run_id)`.
- [X] T017 [P] [US1] In `tests/test_notify_email.py`, add a test that `_items_found` returns the value of `stats["found"]`, still accepts the legacy `stats["items_found"]`, and returns `None` for a missing value or for negative or non-int values (contracts/run-stats-delta.md, integration fix).

### Implementation for User Story 1

- [X] T018 [US1] Make `_items_found` in `src/invio/notify/email.py` read `found` first and fall back to `items_found`, with the existing validation (`type(value) is int and value >= 0`).
- [X] T019 [US1] Create `src/invio/graph/nodes/extract.py` (research R6):
  - `async def extract_item(item: Item, *, fetch_page: PageFetcher, items: ItemRepository) -> bool`: fetch the HTML via `fetch_page(item.url)`, call `invio.sources.extract.extract_text(html, item.url)`, then `items.set_extracted(item, result.text)`, and return `True`. On `ExtractionError` with `reason == "too_short"`, log `extract.too_short` (`item_id` only) and return `False` with no change. Every other exception propagates.
  - `async def video_path(state: ItemState) -> ItemState`: a pass-through placeholder whose docstring says "replaced by #29; must keep the ItemState in/out contract".
  - Read the item by id from the work session inside the node, never from state.
- [X] T020 [US1] Create `src/invio/graph/stages.py`, with one `async` function per stage that takes `(state: RunState, deps: RunDeps, run: RunScope)`. `RunScope` is a small mutable per-run holder created by `build_graph`. It owns the work `Session` (opened lazily), the `BudgetTracker`, the bound `ProviderBinding`, the token, and the `run_context` exit stack.
  - **`load_job`**: the run row and the run context already exist, created by `run_job` (T024). Read the job via `JobRepository.get` and validate `JobConfig.model_validate(job.config)`, letting a `ValidationError` raise so the guard (US5) marks the run failed. Then bind the provider: `scope.binding = deps.provider_for(config.llm)`, letting `MissingSettingError` and `LLMConfigError` raise, so a missing API key or an unknown model fails the run before any LLM call. Create `scope.budget = BudgetTracker(config.limits.max_llm_tokens_per_run)`. Return `config`. Do **not** set context variables here: LangGraph runs each node in a task with a copied context, so they would not reach later nodes (research R10).
  - **`fetch_sources`**: for each enabled source in config order, with key `f"{index}:{source.type}"`:
    - Skip types without an adapter (`sitemap`, `youtube_channel`, `youtube_playlist`), logging `source.unsupported` with the key and type.
    - Otherwise await `deps.fetch_source(source)` and cut the result to `limits.max_items_per_source`.
    - Return `candidates` (a dict in order) and `sources_total`. The failure handling and retries come in US2 and US3.
  - **`deduplicate`**: open the work session, call `deduplicate(session, job_id=…, run_id=…, candidates=…, limits=config.limits)`, then `session.commit()` (normal runs; US6 adds the dry-run branch). Return `taken`, the selected item ids in order, and `counts` (`found` and `new` from `DedupStats`).
  - **`keyword_prefilter`**: run `keyword_filter(item, config.search.keywords, ItemRepository(session))` for each taken item and return `selected` (ids that passed) and `counts.after_keyword_filter`.
  - **`join`**: a no-op that returns `{}`.
  - **`synthesize_digest`**: build `DigestEntry`s with `entries_from_items` from the items whose `ItemResult.summary.status == SUMMARIZED`. Call `synthesize_digest(entries, SynthesisContext(...))` and return a `DigestDraft(title=<rendered subject or job name>, body=result.body, item_ids=result.item_ids)` (or `None` when empty) and `digest_md`. Keep the `SynthesisResult` on the scope for the status rule.
  - **`persist`**: build a `RunDraft` with the taken `Item` rows, the relevance and summary outcomes from `items`, `counts`, `digest` and `budget`. When the synthesis produced no entries, use `DigestDraft(title=…, body="", item_ids=())` with `store_empty_digest=config.notification.send_if_empty and not dry_run`. Call `finalize_run(deps.session_factory, session, draft)`, then apply `run_status_after_synthesis`. If that changed the status, update the run with `RunRepository.finish` in a new committed transaction, keeping stats and error. Return `status` and `digest_id`, the id of the digest row just stored, looked up via `DigestRepository.list_for_job` filtered by `run_id`.
  - **`notify`**: skip when `digest_id is None`. Otherwise `report = await deps.notify(digest_id)`, apply the delivery rule (a mirror of `run_status_after_delivery`: `succeeded` becomes `partial` when `report.failed > 0 or report.error`), store the changed status with `RunRepository.finish` as above, and return `status`.
  - **`finalize`**: compute `next_run_at = deps.next_run(config.schedule, deps.clock())`, call `JobRepository.release(job_id, until=scope.token, next_run_at=next_run_at)` in its own committed transaction, log `run.lock_lost` when it returns `False`, log `run.finalized` (status, `dry_run`, `next_run_at`), close the work session, set `scope.finalized = True`, and return `status`. The run context belongs to `run_job` (T024) and is not touched here.
- [X] T021 [US1] Implement the per-item path in `src/invio/graph/stages.py`:
  - `async def score_relevance(item_state, deps, scope)`, which calls `score_item(item, ScoringContext(...))` with the scope's binding, the `fast` model, `ItemRepository`/`UsageRepository` on the work session, and the budget.
  - `async def summarize(item_state, deps, scope)`, which calls `summarize_item(item, SummaryContext(...))` with the `fast` and `smart` models and `language`/`semantic_description` from the config.
  - Both load the `Item` by id from the work session.
- [X] T022 [US1] Create `src/invio/graph/build.py` (contracts/graph.md):
  - `RunScope`: a mutable dataclass with exactly the fields and lifecycle in data-model.md (`job_id`, `run_id`, `token`, `dry_run`, `session`, `budget`, `binding`, `semaphore`, `synthesis`, `failure`, `finalized`).
  - `build_graph(deps: RunDeps, *, job_id: int, run_id: int, token: datetime, dry_run: bool) -> tuple[CompiledStateGraph, RunScope]` (contracts/graph.md): create the `RunScope`, register the stage nodes as closures over `(deps, scope)`, add the edges in the order of contracts/graph.md, and return the compiled graph together with the scope.
  - The fan-out is `add_conditional_edges("keyword_prefilter", fan_out, ["process_item", "join"])`. `fan_out` returns `[Send("process_item", ItemTask(item_id=i, kind=<item.type>)) for i in selected]`, or `"join"` when `selected` is empty.
  - `process_item` is a node function that runs the compiled item subgraph (`route_kind` → `extract_text` | `video_path` → `score_relevance` → conditional `relevant` → `summarize_item` | `END`). It returns `{"items": [ItemResult(...)]}`.
  - Edges run `process_item` → `join` → `synthesize_digest` → `persist` → `notify` → `finalize` → `END`.
  - Compile without a checkpointer.
  - Module docstring: topology, no checkpointer, async-only nodes, and that retries happen at the call boundary (research R1, R4).
- [X] T023 [US1] Create `src/invio/pipeline/__init__.py` (docstring: composition root above `invio.graph`) and `src/invio/pipeline/deps.py` with `@asynccontextmanager async def default_deps(settings: Settings) -> AsyncIterator[RunDeps]`. It builds:
  - the session factory from `settings.require_secret("database_url")`;
  - one `SafeHttpClient(HttpClientConfig.from_settings(settings))`, with an `RssFeedSource` and a `WebPageSource` dispatched by `source.type` in `fetch_source`, and `fetch_page`, which calls `client.get(url, conditional=False)` and decodes the text. A page-mode web item has the same URL as its source, and a conditional request would come back `304` (research R6). A `NotModified` result here is a bug and raises `RuntimeError`.
  - In `src/invio/sources/http.py`, add the keyword `conditional: bool = True` to `SafeHttpClient.get`. With `False` it sends no remembered validators and does not store the response's validators (contracts/run-stats-delta.md). Keep the default behaviour unchanged, so the existing `tests/test_http_conditional.py` tests still pass;
  - `provider_for`: `resolve(llm_config, "fast", settings, registry=…)` plus the smart model id from the same config, returning a `ProviderBinding`;
  - `notify`: `deliver_digest(factory, digest_id, settings=settings)` mapped to `DeliveryReport(sent, failed, error)`;
  - `next_run=compute_next_run`, `clock=utcnow`, `concurrency=settings.max_parallel_items` and `lock_ttl=timedelta(seconds=settings.run_lock_seconds)`.

  It closes the client, the web source and the provider (`aclose` when present) on exit.
- [X] T024 [US1] Create `src/invio/pipeline/run.py` (contracts/run-job.md):
  - `RunResult`: a frozen dataclass with `job_id`, `run_id`, `status: RunStatus`, `dry_run: bool`, `digest: DigestDraft | None`, `stats: Mapping[str, Any]`, `errors: tuple[RunError, ...]`, `notifications_sent: int` and `notifications_failed: int`.
  - `JobBusyError(LookupError)` with `job_id` and `locked_until`, and `JobDisabledError(ValueError)`.
  - `async def run_job(job_id: int, *, dry_run: bool = False, deps: RunDeps | None = None, concurrency: int | None = None) -> RunResult`:
    1. Use `default_deps(get_settings())` when `deps` is `None`, and override concurrency via `dataclasses.replace` (`ValueError` if < 1).
    2. In one committed transaction: `JobRepository.get` (`JobNotFoundError` from `invio.services.jobs` when missing), refuse disabled jobs (`JobDisabledError`), then `now = deps.clock()`, `token = now + deps.lock_ttl` and `claim(...)`. On `False`, raise `JobBusyError` with the stored `locked_until`. No run row is created.
    3. Start the run with `RunRepository.start(job_id, started_at=now)` in its own committed transaction. If that fails, release the lock (`release(job_id, until=token)`, keeping `next_run_at`) and re-raise.
    4. `graph, scope = build_graph(deps, job_id=job_id, run_id=run.id, token=token, dry_run=dry_run)`. Then, **inside** `with run_context(job=job.name, run_id=str(run.id)):`, log `run.started` and run `final = await graph.ainvoke({"job_id": job_id, "run_id": run.id, "dry_run": dry_run, "items": [], "errors": []}, config={"max_concurrency": deps.concurrency})`. The safety net of T040 must also sit inside this block.
    5. Read the final run row and return a `RunResult`. `errors` is sorted by stage order, then `item_id`.
- [X] T025 [US1] Add `tests/test_pipeline_layering.py` (**S29**), reusing the AST helper of `tests/test_graph_layering.py` (import it, or move `_imports_of` to a shared helper if needed). It asserts that no module under `src/invio/graph/` imports `invio.notify`, `invio.scheduling`, `invio.cli` or `invio.pipeline`, and that `invio.pipeline` is imported only by `invio.cli`. That second check is vacuous until #22, so it only verifies that `db`, `llm`, `sources`, `notify`, `scheduling`, `services` and `graph` do not import `invio.pipeline`. It also asserts that `src/invio/retry.py` imports only the standard library plus `invio.llm.base` and `invio.sources.errors`.
- [X] T026 [US1] Run `tests/test_pipeline_run.py`, `tests/test_graph_extract.py`, `tests/test_graph_build.py`, `tests/test_notify_email.py` and `tests/test_pipeline_layering.py` (T013–T017, T025) until they pass, then run the full gates.

**Checkpoint**: the MVP works. One `run_job` call produces a stored, notified digest and a
finished run, using fakes only.

---

## Phase 4: User Story 2 - One bad item never aborts the run (Priority: P1)

**Goal**: an item that fails ends `failed` with a recorded `RunError`, and the other items are
processed. The run is `partial`, or `failed` when every attempted item fails. Source failures
follow the clarified rule: some failed → `partial`, all failed → `failed`.

**Independent Test**: S5, S6, S7, S21, S22 and S26. Three items, where item 2's page raises
`TooLargeError`: items 1 and 3 end up in the digest, item 2 is `failed` with `last_error`, the
status is `partial`, and there is one `RunError(stage="extract_text", item_id=2)`.

### Tests for User Story 2 ⚠️

- [X] T027 [P] [US2] Write `tests/test_pipeline_failures.py` covering:
  - **S5**: as stated above. Also check `items[2].last_error.startswith("TooLargeError:")`.
  - **S5b**: an unexpected `RuntimeError` inside `score_relevance` for one item (inject it via the provider callable) leaves only that item `failed` with `RuntimeError: ...`, and the others are summarized.
  - **S6**: every relevance call raises `LLMInvalidRequestError(status=400)`, so the status is `failed` and `runs.error == "all attempted items failed"`.
  - **S7**: the provider raises `LLMUnavailableError("secret-token-123 …")`, a source URL has `?token=abc`, and a page contains the sentence "CONFIDENTIAL BODY". None of `secret-token-123`, `token=abc` or `CONFIDENTIAL BODY` appears in `RunResult.errors`, `runs.error`, any `items.last_error`, or any captured log record (`caplog.text`).
  - **S26**: with `max_llm_tokens_per_run` small enough to stop after the first item, the status is `partial`, `budget_exceeded` is true, the unprocessed items are released (attempts back to the pre-run value, status `new`), and the digest contains only the summarized item.
- [X] T028 [P] [US2] Write the source-status tests in `tests/test_pipeline_failures.py`:
  - **S21**: 2 RSS sources, where source `1:rss` raises `FetchError("http_error", url=…, status=500)` on every attempt. The other source's items are in the digest, the status is `partial`, `sources=2`, `sources_failed=1`, and there is one `RunError(stage="fetch_sources", source="1:rss", error_class="FetchError")`.
  - **S22**: every source raises, so the status is `failed`, `runs.error == "all sources failed"`, and finalize ran (`locked_until is None`, `next_run_at` set).
  - **Pending backlog**: with all sources failing and pending items left from an earlier run, the items are still processed and the status is still `failed`.
- [X] T029 [P] [US2] Add unit tests to `tests/test_persist.py` for the extended `decide_status` (research R9, contracts/run-job.md order):
  - `sources=2, sources_failed=2` gives `failed` with `ALL_SOURCES_FAILED_ERROR` as `runs.error` in `persist_run`.
  - `sources=2, sources_failed=1` gives `partial`.
  - `sources=0` (all unsupported) is not failed for that reason.
  - The existing order is kept: all items failed gives `failed` with `ALL_FAILED_ERROR`, and budget exceeded or an item failure gives `partial`.
  - `build_stats` includes `sources` and `sources_failed`.

### Implementation for User Story 2

- [X] T030 [US2] Extend `src/invio/graph/nodes/persist.py`:
  - Add `ALL_SOURCES_FAILED_ERROR: Final = "all sources failed"`.
  - `decide_status`: first `failed` when `counts.sources >= 1 and counts.sources_failed == counts.sources`, then the existing rules, with `partial` also when `counts.sources_failed > 0`.
  - `persist_run` sets `runs.error` to `ALL_SOURCES_FAILED_ERROR` in the first case and `ALL_FAILED_ERROR` in the second.
  - `build_stats` adds `"sources"` and `"sources_failed"`.
  - Update the docstrings.
- [X] T031 [US2] In `fetch_sources` in `src/invio/graph/stages.py`, wrap each source fetch in `try/except FetchError`, catching **only** `FetchError`. Every other exception propagates to the stage guard (US5), so the run fails with `"<Class>: run failed"` as S13 expects (research R9). On a `FetchError`, log `source.failed` with the key and error class, append `error_of("fetch_sources", err, source=key)`, and count it in `sources_failed`. The candidates of healthy sources are kept. Pass `sources_total` and `sources_failed` into `StageCounts` in `persist`.
- [X] T032 [US2] Add the item error boundary in the `process_item` node in `src/invio/graph/build.py` (research R6 table):
  - Run the subgraph inside `try`.
  - `BudgetExceeded` → `ItemResult(budget_stopped=True)` with the item unchanged.
  - `LLMAuthError`, `LLMConfigError`, `MissingSettingError` → re-raise (run-fatal; handled by US5's guard).
  - Any other `Exception` → `ItemRepository.mark_failed(item, failure_message(err))`, log `item.failed` with `item_id`, stage and error class, and return `ItemResult(error=…)` plus `errors=[error_of(<stage>, err, item_id=…)]`. `<stage>` is `ItemState.stage`, the subgraph node that raised (T010). When the subgraph raises before any node has set it, use `"extract_text"`.
  - `CancelledError` and `BaseException` propagate.
  - The relevance and summary outcomes that the nodes already returned as `failed` (`PER_ITEM_ERRORS`) are also reported as `RunError`s with the class taken from their `error` prefix.
- [X] T033 [US2] Make `persist` in `src/invio/graph/stages.py` build `RunDraft.relevance` and `RunDraft.summaries` from every `ItemResult`. An item failed by the boundary before rating needs an outcome that `decide_status` counts as attempted and failed: add a `RelevanceOutcome(item_id, status=FAILED, relevance=None, result=None, error=…)` for extract-stage failures. Document this in a comment. Run T027–T029 until green.

**Checkpoint**: with US1 and US2 together, a single bad item or source never aborts the run.

---

## Phase 5: User Story 5 - Finalization always happens (Priority: P1)

**Goal**: on every exit path (a stage exception, a failed save, a failed delivery, cancellation)
the run leaves `running`, the error is recorded and sanitized, `next_run_at` is recomputed and
the lock is released. `run_job` refuses busy jobs and takes over expired locks (FR-016).

**Independent Test**: S13–S18 and S27. `fetch_source` raising `RuntimeError` gives
`RunResult.status == failed`, `runs.error == "RuntimeError: run failed"`, `next_run_at`
recomputed and `locked_until is None`, and `run_job` returns normally.

### Tests for User Story 5 ⚠️

- [X] T034 [P] [US5] Add to `tests/test_pipeline_failures.py`:
  - **S13**: `fetch_source` raises `RuntimeError("boom secret")`. Expect the run `failed`, `runs.error == "RuntimeError: run failed"`, stats in the #19 recovery layout, `next_run_at` recomputed, `locked_until is None`, `RunResult.errors == (RunError(stage="fetch_sources", error_class="RuntimeError"),)` with `source is None`, the notifier not called, and `"boom secret"` not in `caplog.text`.
  - **S14**: monkeypatch `DigestRepository.add` to raise `sqlalchemy.exc.IntegrityError` (as in the #19 tests). The run is `failed` via `record_failed_run`, each ledger entry becomes exactly one `llm_usage` row, no digest is stored, items are back to their committed post-dedup state, and the lock is released.
  - **S15**: the notifier returns `DeliveryReport(sent=0, failed=1, error=None)`, so the status is `partial` (stored and returned) and finalize ran.
  - **Run-fatal**: `LLMAuthError` from the first relevance call gives a `failed` run with `runs.error` starting with `LLMAuthError:` and no item marked `failed` because of it.
- [X] T035 [P] [US5] Write `tests/test_pipeline_concurrency.py` (lock part):
  - **S17 busy**: `locked_until = fake_clock() + 1h` makes `run_job` raise `JobBusyError` with `locked_until` equal to the stored value. No `runs` row, `snapshot()` unchanged.
  - **S17 expired**: `locked_until = fake_clock() - 1s`; the run proceeds and afterwards `locked_until is None`.
  - **S18**: `asyncio.gather(run_job(j), run_job(j), return_exceptions=True)` gives exactly one `RunResult` and one `JobBusyError`.
  - **Lock lost**: when the stored `locked_until` is changed by another writer mid-run, release returns `False`, `run.lock_lost` is logged, and the foreign lock is kept.
- [X] T036 [P] [US5] Add to `tests/test_pipeline_run.py`:
  - **S27 invalid config**: store a `jobs.config` that fails `JobConfig` validation. Use, for example, `search.min_relevance: 2` and an unknown `llm.provider`. The run is `failed` with `runs.error == "ValidationError: invalid fields search.min_relevance, llm.provider"` (order as pydantic reports them), the error contains neither input value, there are zero provider requests, and the lock is released. Also: with 7 invalid fields the error lists 5 paths followed by `, …`. A missing API key (`MissingSettingError` from `provider_for`) gives a `failed` run with `runs.error == "MissingSettingError: run failed"` and no provider request (U4).
  - **S27 unknown id**: raises `JobNotFoundError`, with no run.
  - **S27 disabled**: raises `JobDisabledError`, with no run and no lock change.
  - `concurrency=0` raises `ValueError` before any database write.
- [X] T037 [P] [US5] Add **S16** to `tests/test_pipeline_failures.py`. Gate `fetch_page` on an `asyncio.Event` and cancel the `run_job` task once at least one item is in flight. `CancelledError` propagates out of `await task`. In a new session the run is `failed`, `locked_until is None`, and no item is left with a half-written status (the work session was rolled back).

### Implementation for User Story 5

- [X] T038 [US5] Add `guarded(stage, fn)` to `src/invio/graph/build.py` (research R8):
  - It wraps a stage function. On `Exception` it logs `run.stage_failed` (stage and error class) and returns `{"fatal": error_of(stage, err), "errors": [same]}`. `BaseException` is not caught.
  - Wrap `load_job`, `fetch_sources`, `deduplicate`, `keyword_prefilter`, `synthesize_digest`, `persist` and `notify`.
  - Replace the straight edges after each guarded stage with `add_conditional_edges(stage, route_after(next), [next, "finalize"])`, where `route_after` returns `"finalize"` when `state.get("fatal")`.
  - The fan-out router also returns `"finalize"` on `fatal`.
- [X] T039 [US5] Extend `finalize` in `src/invio/graph/stages.py`:
  - When `fatal` is set and a run row exists: roll back the work session if it is open (log `run.rollback_failed` on error, as #19 `_recover` does), then call `record_failed_run(deps.session_factory, job_id=…, run_id=…, budget=scope.budget, error=<the original exception>)`. Keep the original exception on the scope inside `guarded`, because `RunError` holds only the class.
  - The run row always exists by the time the graph runs, because `run_job` starts it (T024). A failure in `load_job` therefore goes through the same `record_failed_run` path, with no usage to replay.
  - Then compute `next_run_at` and release, as in US1.
  - Make `finalize` idempotent with a `scope.finalized` flag.
- [X] T040 [US5] Add the safety net to `run_job` in `src/invio/pipeline/run.py`. Wrap `graph.ainvoke` in `try/except BaseException`, and when `scope.finalized` is false:
  1. Roll back the work session.
  2. If a run row exists and is still `running`, call `record_failed_run` with the exception.
  3. Call `JobRepository.release(job_id, until=token, next_run_at=…)`. In a dry run, use `KEEP`.
  4. Re-raise.

  The safety net uses the `RunScope` returned by `build_graph` (T022) and runs inside the `run_context` block of T024, so its log lines carry `job` and `run_id`. If release itself fails, log `run.release_failed` with the error class and re-raise the original error.
- [X] T041 [US5] Classify run-fatal errors in `src/invio/graph/build.py`: `LLMAuthError`, `LLMConfigError` (and its subclasses), `MissingSettingError` and `pydantic.ValidationError` raised from `load_job` all go through `guarded` → `finalize` → `record_failed_run`. `record_failed_run` already sanitizes `LLMError` via `failure_message` and reduces others to `"<Class>: run failed"`. Extend `_sanitized_error` in `src/invio/graph/nodes/persist.py` for `pydantic.ValidationError`: return `"ValidationError: invalid fields " + ", ".join(paths)`, where each path is `".".join(str(p) for p in error["loc"])` over `err.errors(include_input=False, include_url=False)`. Use at most 5 paths, then `", …"`, and never include `msg` or input values (contracts/run-stats-delta.md). Run T034–T037 until green.

**Checkpoint**: all P1 stories are done. Every run leaves `running`, releases its own lock and
sets the next run time, and two runs of one job can never overlap.

---

## Phase 6: User Story 3 - Transient failures are retried with backoff (Priority: P2)

**Goal**: source fetches, item page fetches and LLM requests are retried with exponential
backoff on rate-limit, unavailable and fetch errors. Permanent errors are not retried.

**Independent Test**: S8–S11. The provider raises `LLMRateLimitError(retry_after=2)` twice and
then answers: the item is relevant, the run `succeeded`, and `RecordingSleep.calls == [2.0, 2.0]`.

### Tests for User Story 3 ⚠️

- [X] T042 [P] [US3] Write `tests/test_llm_retry.py` for `RetryingProvider`:
  - `complete` and `complete_structured` are each retried on `LLMRateLimitError` and `LLMUnavailableError` up to `max_attempts`, with the recorded sleeps.
  - `LLMAuthError`, `LLMInvalidRequestError` and `LLMInvalidOutputError` are attempted once.
  - The return value and `Usage` pass through unchanged.
  - `aclose` is forwarded when the inner provider has it.
  - `name` and `timeout_seconds` are passed through if the `LLMProvider` protocol requires them; check `src/invio/llm/base.py`.
- [X] T043 [P] [US3] Write `tests/test_pipeline_retries.py`:
  - **S8**: as in the Independent Test. Exactly 3 relevance requests for that item, and one `llm_usage` row for the successful call only (failed requests record no usage, per `call_text`/`call_structured`).
  - **S9**: `fetch_source` raises `FetchError("timeout", url=…)` once and then returns candidates, so `sources_failed == 0` and the items are processed.
  - **S10**: `LLMUnavailableError` on all 3 attempts for one item makes that item `failed`, the others are processed, the status is `partial`, and the sleeps for that item are `[1.0, 2.0]` (with `RetrySettings(jitter=False)`).
  - **S11**: `LLMAuthError` → 1 request and a `failed` run. `LLMInvalidRequestError` → 1 request and the item failed. `BlockedError` from `fetch_page` → 1 call and the item failed. `BlockedError` from `fetch_source` → 1 call and the source failed.
  - The item page is retried on `FetchError("timeout")` and then extracted.

### Implementation for User Story 3

- [X] T044 [P] [US3] Create `src/invio/llm/retry.py` with `RetryingProvider`, an `LLMProvider` that wraps `inner: LLMProvider` with `policy: RetrySettings` and `sleep: Sleep`. `complete(...)` and `complete_structured(...)` delegate through `invio.retry.retrying(..., retry_on=is_transient_llm, what="llm")`. `aclose()` is delegated when present. The module docstring explains why retries sit at the call boundary (research R4). The `llm` package must not import `invio.graph`.
- [X] T045 [US3] Wire the retries into `src/invio/graph/stages.py`, `src/invio/graph/nodes/extract.py` and `src/invio/graph/build.py`:
  - In `src/invio/graph/stages.py`, `fetch_sources` calls `retrying(lambda: deps.fetch_source(source), policy=deps.retry, retry_on=is_transient_fetch, what="source", sleep=deps.sleep)`.
  - In `src/invio/graph/nodes/extract.py`, `extract_item` takes `retry` and `sleep` and fetches through `retrying(..., retry_on=is_transient_fetch, what="page")`.
  - In `src/invio/graph/build.py` (scope setup), wrap `binding.provider` once per run as `RetryingProvider(binding.provider, policy=deps.retry, sleep=deps.sleep)` before building the node contexts.

  Run T042–T043 until green.

**Checkpoint**: transient errors that clear within the retry limit no longer fail items.

---

## Phase 7: User Story 4 - Parallelism respects provider rate limits (Priority: P2)

**Goal**: items, and source fetches, run in parallel, but never more than `concurrency` at a time
(default 4, `INVIO_MAX_PARALLEL_ITEMS`).

**Independent Test**: S12. Ten items with `concurrency=4` and a counting gate in `fetch_page`:
`max_in_flight == 4` and never more, and all 10 are processed. With `concurrency=1` the maximum
is 1. With the default it is 4.

### Tests for User Story 4 ⚠️

- [X] T046 [P] [US4] Add to `tests/test_pipeline_concurrency.py`:
  - **S12**: 10 RSS entries, with `fetch_page` holding each call on a shared counter: increment, record the max, `await asyncio.sleep(0)` a few times, decrement. Check `max_in_flight == 4` for `concurrency=4`, `== 1` for `run_job(..., concurrency=1)`, and `== 4` for deps built with the default `RunDeps.concurrency`. All 10 are `summarized` in each case.
  - The gate also counts provider requests in flight, which must stay ≤ the limit.
  - The `fetch_sources` stage, with 6 sources and a counting `fetch_source`, also stays ≤ the limit.
- [X] T047 [P] [US4] Add a test to `tests/test_graph_state.py` that the `operator.add` reducers keep every `ItemResult` and `RunError` from 10 parallel branches that finish in reverse order (FR-005). It compares sets of `item_id`, not order.

### Implementation for User Story 4

- [X] T048 [US4] In `src/invio/graph/build.py`, create `scope.semaphore = asyncio.Semaphore(deps.concurrency)` per run. Wrap the whole body of `process_item` (the subgraph invocation and the error boundary) in `async with scope.semaphore:`. In `src/invio/graph/stages.py`, wrap each source fetch in `fetch_sources` in the same semaphore and fetch the sources with `asyncio.gather`, keeping the config order in the result dict. Keep `config={"max_concurrency": deps.concurrency}` in `run_job` as a second bound. Run T046–T047 until green.

**Checkpoint**: parallel processing is bounded and measurable.

---

## Phase 8: User Story 6 - Dry run for safe testing (Priority: P3)

**Goal**: `run_job(job_id, dry_run=True)` produces the digest in the result only. It sends no
mail, leaves `next_run_at` unchanged, and keeps no item, digest or usage change. Only a run row
marked `stats.dry_run = true` remains.

**Independent Test**: S19–S20. Take a `snapshot()` before and after a dry run: items, digests,
usage and notifications are identical, `next_run_at` is byte-equal, the notifier was not called,
`RunResult.digest` is set, `runs.stats["dry_run"] is True`, and the lock is released. A
following normal run processes the same items with `attempts == 1`.

### Tests for User Story 6 ⚠️

- [X] T049 [P] [US6] Add to `tests/test_pipeline_run.py`:
  - **S19**: as in the Independent Test. It also checks that new candidates did not create `items` rows and that `RunResult.notifications_sent == 0`.
  - **S20**: S19, then a normal run. Every one of the 3 items is `summarized` with `attempts == 1`, and a digest exists.
  - **Dry run with a stage failure**: `fetch_source` raises, so the run is `failed`, `next_run_at` is unchanged, the lock is released, and there are no usage rows.
  - **Dry run with an existing lock**: busy and expired behaviour is the same as for a normal run.
- [X] T050 [P] [US6] Add to `tests/test_persist.py`: the dry-run finish writes stats with `dry_run: True`, `sources` and `sources_failed`, and token figures from the ledger, while `UsageRepository.totals_for_run` is zero (contracts/run-stats-delta.md, invariant exception).

### Implementation for User Story 6

- [X] T051 [US6] Add `finish_dry_run(factory, session, draft) -> RunStatus` to `src/invio/graph/nodes/persist.py`. First compute `status = decide_status(draft)` and `stats = build_stats(draft) | {"dry_run": True}` while the items are still readable. Then `session.rollback()`. Then, in a new `session_scope`, `RunRepository.finish(run, status, stats=stats, error=<ALL_* constant as in persist_run or None>)`. Return the status. On an exception, log and fall back to `record_failed_run` (its usage replay must be skipped for dry runs: add a keyword `replay_usage: bool = True` to `record_failed_run` and pass `False`). Log `run.persisted` with `dry_run=True`.
- [X] T052 [US6] Make `src/invio/graph/stages.py` and `src/invio/pipeline/run.py` dry-run aware:
  - `deduplicate` skips its commit when `dry_run`.
  - `persist` calls `finish_dry_run` instead of `finalize_run` and still applies `run_status_after_synthesis`.
  - `notify` is skipped (status unchanged, `notifications_sent=0`).
  - `finalize` and the safety net pass `next_run_at=KEEP` to `release`.
  - `record_failed_run` on `fatal` uses `replay_usage=False`.
  - `RunResult.digest` is taken from state, because no digest row exists.

  Run T049–T050 until green.

**Checkpoint**: every user story works independently and together.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [X] T053 [P] Add a `### Running a job` section after `### Runs, statistics and the token budget` in `README.md`. It covers:
  - `invio.pipeline.run.run_job(job_id, dry_run=False)` and what it returns or raises (`JobBusyError`, `JobDisabledError`, `JobNotFoundError`);
  - the stage order;
  - the final status rule (sources, items, budget, fallback digest, delivery);
  - the retry policy defaults and which errors are retried;
  - `INVIO_MAX_PARALLEL_ITEMS` and `INVIO_RUN_LOCK_SECONDS`;
  - the dry-run semantics;
  - known limitations: no lock heartbeat (a run longer than `run_lock_seconds` can be overtaken), unsupported source types are skipped, and video items use page text until #29.

  Also add `pipeline/  run orchestration (run_job)` to the package layout list near README line 549, and update `docs/` if an environment-variable reference lists `INVIO_*` settings (find it with `grep -rn "INVIO_SMTP_HOST" docs README.md`).
- [X] T054 [P] Update `specs/013-gh-issue-19/contracts/run-stats.md` with a short "Additions (#21)" note that links `specs/014-gh-issue-21/contracts/run-stats-delta.md`, so stats consumers (#22, #35) find the new keys.
- [X] T055 Run every quickstart scenario S1–S31 (`uv run pytest tests/test_pipeline_*.py tests/test_graph_*.py tests/test_retry.py tests/test_llm_retry.py tests/test_db_job_lock.py -q`), the MariaDB run (`uv run pytest -m db tests/test_db_job_lock.py tests/test_pipeline_concurrency.py` when `INVIO_TEST_DATABASE_URL` is set), and the full gates. Fix any `mypy --strict` findings without adding `Any` beyond `runs.stats` and the justified LangGraph override from T003.
- [X] T056 Draft the PR description in `specs/014-gh-issue-21/pr-description.md` (pasted into the PR, not merged as product docs). It references #21, justifies the new `langgraph` dependency, lists the deviation from the issue text (call-level retries instead of node-level `RetryPolicy`, research R4) and the rename `RunResult` → `RunDraft`, and notes the follow-ups: #22 (CLI), #23 (`run-due`, failure back-off), #29 (video path).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: after Setup, and blocks every story. T009 (rename) must land
  before any `persist` change. T012 depends on T010 and T011.
- **US1 (Phase 3)**: after Phase 2. This is the MVP.
- **US2 (Phase 4)**: after US1. It extends `stages.py`, `build.py` and `persist.py` from US1.
- **US5 (Phase 5)**: after US1. It is independent of US2 except for the edits to `build.py` and
  `stages.py`, so run it after US2 or coordinate the edits. T035 (lock tests) only needs US1.
- **US3 (Phase 6)**: after US1. T042 and T044 only need Phase 2, and can be done any time.
  T045 also touches `extract.py`, `stages.py` and `build.py`.
- **US4 (Phase 7)**: after US1. It is independent of US2, US3 and US5 in logic, but edits
  `build.py` and `stages.py`.
- **US6 (Phase 8)**: after US1 and US5. It reuses the guarded `finalize`, the safety net and
  `record_failed_run`.
- **Polish (Phase 9)**: after the stories that ship.

### Within Each Story

Write the tests first and see them fail, then implement in the listed order. Tasks that edit
`src/invio/graph/stages.py`, `src/invio/graph/build.py` or
`src/invio/graph/nodes/persist.py` are sequential within and across stories.

### Parallel Opportunities

- Phase 1: T003 runs alongside T002 once the lock file is updated.
- Phase 2: T004, T005, T007, T010 and T011 run in parallel (different files). Then T006 and
  T008. T012 comes last.
- US1: tests T013–T017 run in parallel (different files). T018 and T019 run in parallel with
  each other, before T020.
- US2: T027–T029 can be drafted in parallel. T027 and T028 share a file, so write them in one
  pass if one person edits it.
- US5: T034–T037 run in parallel, in different files except T034 and T037, which share a file.
- US3: T042, T043 and T044 run in parallel. T044 can even start right after Phase 2.
- US4: T046 and T047 run in parallel.
- US6: T049 and T050 run in parallel.
- Polish: T053 and T054 run in parallel.

---

## Parallel Example: Phase 2

```bash
# Independent foundations (different files):
Task: "T004 Settings max_parallel_items / run_lock_seconds in src/invio/config/settings.py"
Task: "T005 RetrySettings + retrying() in src/invio/retry.py"
Task: "T007 JobRepository.get/claim/release + ItemRepository.set_extracted in src/invio/db/repositories.py"
Task: "T010 RunState/ItemState/RunError in src/invio/graph/state.py"
Task: "T011 RunDeps and ports in src/invio/graph/ports.py"
```

## Parallel Example: User Story 1

```bash
# Tests first (different files):
Task: "T013 end-to-end scenarios S1–S4 in tests/test_pipeline_run.py"
Task: "T014 extract/video path in tests/test_graph_extract.py"
Task: "T015 topology in tests/test_graph_build.py"
Task: "T017 _items_found in tests/test_notify_email.py"

# Then independent sources:
Task: "T018 notify fix in src/invio/notify/email.py"
Task: "T019 extract node in src/invio/graph/nodes/extract.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 and Phase 2.
2. Phase 3 (US1): a full run with a digest, the notifier, the next run time and the lock released.
3. **Stop and validate**: S1–S4, S24, S25, S28–S31.

### Incremental Delivery

1. Add US2: item and source failures are isolated (S5–S7, S21, S22, S26).
2. Add US5: finalize always runs, and the busy lock is enforced (S13–S18, S27). All P1 stories are done.
3. Add US3: retries (S8–S11).
4. Add US4: the concurrency bound (S12).
5. Add US6: dry run (S19, S20).
6. Polish: README, the contract note, the full quickstart and the PR description.

### Notes

- Commit after each task or logical group on branch `gh-issue-21`.
- Every acceptance criterion of issue #21 maps to a story: AC1 to US1 (S1), AC2 to US2 (S5),
  AC3 to US5 (S13), AC4 to US4 (S12) and AC5 to US6 (S19).
