# Contract: `invio.pipeline.run.run_job`

The public entry point for one job run. Callers are #22 (`invio job run`), #23 (`invio run-due`)
and the tests.

```python
async def run_job(
    job_id: int,
    *,
    dry_run: bool = False,
    deps: RunDeps | None = None,  # None → invio.pipeline.deps.default_deps(get_settings())
    concurrency: int | None = None,  # None → deps.concurrency (INVIO_MAX_PARALLEL_ITEMS, default 4)
) -> RunResult: ...
```

## Preconditions and refusals (no run row is created, no lock is changed)

| Situation | Raises |
|---|---|
| no job with `job_id` | `JobNotFoundError` |
| job disabled | `JobDisabledError` |
| `jobs.locked_until > now` (another run) | `JobBusyError(job_id, locked_until)` |
| `concurrency < 1` | `ValueError` |

An expired lock (`locked_until <= now`) is taken over (FR-016).

## Postconditions (every other path, including stage failures)

1. Exactly one `runs` row exists for the call. Its status is not `running` and is the same
   value as `RunResult.status`.
2. `jobs.locked_until` no longer holds this run's claim.
3. In a normal run, `jobs.next_run_at == deps.next_run(config.schedule, deps.clock())`,
   computed at finalize. In a dry run it is unchanged.
4. In a dry run, no `digests`, `llm_usage` or `notifications` rows are added, and every `items`
   row is unchanged (status, attempts, run_id, raw_content, summary, relevance), including items
   that did not exist before. `runs.stats["dry_run"] is True`.
5. `RunResult.errors` lists every recorded `RunError`. None of them contains secrets, URLs with
   a query string, or provider, database or document text.

The function **returns** a `RunResult` for `succeeded`, `partial` and `failed` runs. It
**raises** in only three cases:

- one of the refusals above;
- `CancelledError` or `KeyboardInterrupt`, re-raised after the finalize safety net has recorded
  the run `failed` and released the lock;
- an error in the recovery transaction of `record_failed_run` (#19). The run may then stay
  `running`, and the caller must exit non-zero. The lock is still released in a separate
  attempt.

## Final status rule (in order)

1. A guarded stage raised → `failed` (`runs.error = "<Class>: run failed"`, the LLM facts
   for an `LLMError`, or the field paths for a config `ValidationError`).
2. Every source with an adapter failed after retries (and at least one exists) → `failed`
   (`runs.error = "all sources failed"`).
3. Every attempted item failed (budget not exceeded) → `failed`
   (`runs.error = "all attempted items failed"`).
4. Any source failed, any item failed, or the budget was exceeded → `partial`.
5. Otherwise `succeeded`.
6. Then `run_status_after_synthesis` applies (a fallback digest turns `succeeded` into
   `partial`).
7. Then `run_status_after_delivery` applies, in normal runs only (a failed or skipped
   recipient turns `succeeded` into `partial`).

## Run-fatal vs. item-level errors

| Error | Scope |
|---|---|
| per-item LLM errors (`PER_ITEM_ERRORS`) after retries | item `failed` |
| `FetchError` on the item page after retries, `ExtractionError("too_large")`, any other `Exception` in item processing | item `failed` |
| `ExtractionError("too_short")` | none: the item continues with title and teaser |
| `BudgetExceeded` | item left unchanged and released at save |
| `FetchError` of a source after retries | source failed (status rules 2 and 4) |
| `LLMAuthError`, `LLMConfigError`, `MissingSettingError`, `JobConfigError` | run `failed` |
| `pydantic.ValidationError` of the stored job config (in `load_job`) | run `failed`, `runs.error = "ValidationError: invalid fields <path>[, <path>…]"` (paths only, at most 5) |
| `FetchError` per source | that source failed. Any other exception in `fetch_sources` fails the run (guarded stage) |
| any exception outside item processing | run `failed` (guarded stage) |

## Retry policy (`invio.retry.RetrySettings`, defaults)

`max_attempts=3`, `initial_interval=1.0 s`, `backoff_factor=2.0`, `max_interval=30 s`,
`jitter=True` (+0–10 %). `LLMRateLimitError.retry_after` is used when it is larger than the
computed wait, still capped at `max_interval`. Retried: `LLMRateLimitError`,
`LLMUnavailableError`, and `FetchError` except `BlockedError`, `TooLargeError` and
`RenderUnavailableError`. Applied per external call: source fetch, item page fetch and
provider request.

## Settings (new, `INVIO_` prefix)

| Setting | Default | Rule |
|---|---|---|
| `max_parallel_items` | 4 | int, >= 1 |
| `run_lock_seconds` | 7200 | int, >= 60 |

## Logging (structured, inside `run_context(job=<name>, run_id=<runs.id>)`)

`run.started`, `run.stage_failed` (stage, error class), `source.failed` (source key, error
class), `source.unsupported` (source key, type), `retry.attempt` (what, attempt, wait_s, error
class), `item.failed` (item_id, stage, error class), `run.finalized` (status, dry_run,
next_run_at), and `run.lock_lost` (when release finds a different lock). Events that already
exist (`deduplicated`, `relevance.*`, `summarize.*`, `synthesize.*`, `run.persisted`,
`budget.exceeded`) are unchanged.
