# Assembled research workflow with parallel item processing and retries (#21)

Closes #21.

## What this adds

`invio.pipeline.run.run_job(job_id, dry_run=False)` runs one job end to end as one LangGraph graph:
`load_job -> fetch_sources -> deduplicate -> keyword_prefilter -> process_item x N -> join ->
synthesize_digest -> persist -> notify -> finalize`. `process_item` is a subgraph
(`extract_text | video_path -> score_relevance -> summarize_item`), bounded by a per-run semaphore
(`INVIO_MAX_PARALLEL_ITEMS`, default 4). `finalize` always runs: it sets `next_run_at` and releases
the job lock, also after a stage exception or a cancellation.

- A bad item never aborts the run (`failed` item plus a `RunError`); a bad source makes the run
  `partial`, all sources failing makes it `failed`.
- The job is locked with the SQL of #23 (`JobRepository.claim`/`release`, `JobBusyError`).
- Dry run: the work session is rolled back; only the run row remains, with `stats.dry_run = true`.
- `RunResult` (persist input) is renamed `RunDraft`, so the public `RunResult` is the return value
  of `run_job`.
- `notify.email._items_found` now reads `stats["found"]` (the #19 layout).
- A run that outlives its job lock (`INVIO_RUN_LOCK_SECONDS`) stops at the next stage or item
  node with `LockExpiredError` and is `failed`, so it cannot race a run that took the lock over.

## New dependency

`langgraph>=1.0,<2` (pulls `langchain-core`, `langsmith`, ...). Justification: issue #21 names
LangGraph, the README lists `graph/ LangGraph pipelines`, and #29 plugs its video path into the
`process_item` subgraph. No checkpointer is used. `mypy --strict` accepts its typing, so no mypy
override was needed. `uv.lock` is updated and CI still runs `uv sync --locked`.

## Deviations from the issue text

- **Call-level retries instead of node-level `RetryPolicy`** (research R4). `score_item` and
  `summarize_item` already turn transient errors into a `failed` item, so a node-level policy
  would never fire, and re-running a node would repeat side effects. The retried calls are the
  source fetch, the item page fetch and each provider request (`invio.retry`,
  `invio.llm.retry.RetryingProvider`). Policy parameters and retried error kinds are the issue's.
  A `Retry-After` longer than `max_interval` is not waited for: the call fails at once.
- New package `invio.pipeline` (composition root): `invio.graph` may not import `notify` or
  `scheduling`, so these are injected through `RunDeps` ports.
- `RunScope` lives in `invio.graph.scope` to avoid an import cycle between `build.py` and
  `stages.py`.
- Source fetch retries only transient failures (allow-list: timeouts, connection/DNS errors,
  invalid responses, render failures, HTTP 408/425/429/5xx); 4xx and malformed content are
  permanent. Source retries do not hold a concurrency slot while backing off; item-level LLM and
  page retries run inside `process_item` and do.
- A run whose status is `failed` stores no empty digest and sends no mail.
- A synthesis fallback lowers the status to `partial` in the same write as the save; only a
  notify failure lowers an already saved `succeeded` run.

## Follow-ups

- #22: `invio job run [--dry-run]` (the library entry point exists; the CLI reachability of
  Constitution II is deferred, see plan.md Complexity Tracking).
- #23: `invio run-due`, failure back-off for `next_run_at`; a lock heartbeat if runs ever need
  to outlive `run_lock_seconds` (today they stop at the deadline).
- Distinct base classes for `JobBusyError`/`JobNotFoundError`.
- #29: replaces the `video_path` placeholder (it passes the item on to `extract_text` unchanged).

## Test plan

- `uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest --cov`
- `uv run pytest -m db tests/test_db_job_lock.py` with `INVIO_TEST_DATABASE_URL` set (concurrent
  claim on MariaDB).
