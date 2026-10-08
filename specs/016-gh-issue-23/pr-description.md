# `invio run-due` with atomic job locking, missed-run collapse, retry backoff and health check (#23)

Closes #23.

## What this adds

- `invio run-due [--limit N] [--parallel N]`: runs every enabled job whose `next_run_at` has
  passed, oldest first, prints one line per due job and a summary, and exits 0 (no run failed),
  1 (a run failed, a job raised, or the invocation aborted) or 2 (usage or configuration).
- Atomic claim: `JobRepository.claim(..., due_by=...)` is still one conditional UPDATE and now
  also requires the job to be enabled and `next_run_at <= due_by`. Of two overlapping
  invocations (or a manual `job run`) exactly one runs a job; a job another invocation finished
  after the selection is reported as `skipped  no longer due` (`JobNotDueError`). An expired
  lock is taken over.
- Missed slots collapse: the next run is computed from the finish time, so a job that missed
  several slots runs once.
- Retry backoff: after a failed real run the next run is `min(regular slot, finish + delay)`,
  with a delay of 1 h doubling to 24 h (`invio.scheduling.backoff`), derived from the job's
  consecutive failed runs (`RunRepository.failure_streak`; dry runs and `running` rows are
  skipped). No migration is needed.
- Health check: `INVIO_HEALTHCHECK_URL` gets exactly one `GET` per invocation (`<url>` on exit 0,
  `<url>/fail` otherwise), timeout 3 s, no redirects, never raising, never logging the URL.
- `--parallel N`: up to N jobs at once, started in due order (one shared `RunDeps`, a semaphore
  and a `TaskGroup`). Lowered to 1 on SQLite.

## Design decisions

- **R1**: the claim and the "still due" check are one UPDATE, so no extra lock table or
  transaction is needed.
- **R3**: the failure streak is derived from `runs` instead of a new column.
- **R6**: `release_lock` takes the final run status and picks the earlier of retry and regular
  slot. A tie counts as the regular slot. The safety net passes the status returned by
  `record_failure` (or `failed` when recording failed).
- A refused claim is classified in a fresh transaction (MariaDB REPEATABLE READ would show the
  stale row otherwise), and anything failing between claim and graph start releases the lock.
- `--limit` counts only runnable jobs: busy jobs are reported but do not use it up, and the
  jobs it cuts off are counted as `deferred N (--limit)` in the summary.
- `busy` and `skipped` outcomes never fail the invocation (overlapping timer ticks must not
  page the monitor); `partial` is not a failure either.

## Behaviour changes to review

- **Failed manual `invio job run`** now also schedules the retry (the next run is up to the
  retry delay away instead of always the regular slot). Its exit code is unchanged. Dry runs
  still change nothing.
- **`INVIO_HEALTHCHECK_URL`** is now a `SecretStr` and must be an absolute `http(s)` URL with a
  host; an invalid value fails settings validation (exit 2). `healthcheck_url` was added to
  `SecretName`.
- `RunDeps` has a new required field `retry_delay`; `RunResult` gains `next_run_at` and
  `retry_scheduled`.
- A failed real run whose stored schedule is unreadable (or whose next regular slot cannot be
  computed) gets `finish + retry delay` as its next run time, so it is not rerun on every
  invocation. Successful, partial and dry runs keep `next_run_at` in that case.

## Tests

`tests/test_run_due.py`, `tests/test_cli_run_due.py`, `tests/test_db_due.py`,
`tests/test_backoff.py`, `tests/test_healthcheck.py`, extensions of the lock, pipeline, failure,
settings and CLI tests, and `tests/test_run_due_concurrency.py` (two concurrent invocations over
10 due jobs, 20 rounds; MariaDB only, skipped on SQLite). See `quickstart.md` for the manual
walk-through.
