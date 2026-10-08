# Contract: pipeline, repository and settings changes for `run-due`

**Spec**: FR-002–FR-011, FR-016 · **Research**: R1–R6 · **Data model**: [../data-model.md](../data-model.md)

## `invio.db.repositories.JobRepository`

### `list_due(now: datetime, *, limit: int | None = None) -> list[DueJob]` (new)

Enabled jobs with `next_run_at IS NOT NULL AND next_run_at <= now`, ordered by `next_run_at`,
then `id`. `limit` (≥ 1) applies to the SQL result. Read-only. `run_due` calls it without a
limit and applies `--limit` after setting the busy jobs aside.

### `claim(job_id, *, now, until, due_by: datetime | None = None) -> bool` (changed)

As before: one conditional UPDATE that sets `locked_until = until` if
`locked_until IS NULL OR locked_until <= now`. With `due_by`, the same UPDATE also requires
`next_run_at IS NOT NULL AND next_run_at <= due_by`. Of several concurrent callers at most one
gets `True`. Does not commit.

## `invio.db.repositories.RunRepository`

### `failure_streak(job_id: int, *, exclude_run_id: int, cap: int) -> int` (new)

Counts `failed` runs of the job, newest first. It skips `exclude_run_id`, runs still `running`,
and dry runs (`stats["dry_run"] is True`). It stops at the first `succeeded`/`partial` run or
when the count reaches `cap`. Reads at most 50 rows. Read-only.

## `invio.scheduling.backoff` (new, pure)

```python
RETRY_BASE = timedelta(hours=1)
RETRY_MAX = timedelta(hours=24)
RETRY_STREAK_CAP = 6


def retry_delay(
    streak: int,
) -> timedelta: ...  # min(RETRY_BASE * 2**(streak-1), RETRY_MAX); ValueError if streak < 1
```

## `invio.graph.ports.RunDeps` (changed)

New required field `retry_delay: Callable[[int], timedelta]`, placed after `clock`.
`invio.pipeline.deps.default_deps` passes `invio.scheduling.backoff.retry_delay`.

## `invio.graph.stages.release_lock(deps, scope, *, status: RunStatus) -> datetime | None` (changed)

- Dry run: release only, `next_run_at` kept (unchanged).
- `succeeded` / `partial`: regular slot as before, or kept when the schedule is unreadable or
  `next_run` raises.
- `failed`:
  - `streak = 1 + failure_streak(...)` (1 if that query raises).
  - `retry = clock() + deps.retry_delay(streak)`.
  - `next_run_at = min(regular, retry)`, or `retry` when the schedule is unreadable or
    `next_run` raises.
- Logs `run.retry_scheduled` (`streak`, `next_run_at`) when the retry time was chosen.
- The release itself is unchanged: one UPDATE guarded by the token; `run.lock_lost` when it no
  longer matches.

Callers: `finalize` passes the final `status`. The `run_job` safety net passes the status
returned by `record_failure`, or `failed` when that raised.

## `invio.pipeline.run` (changed)

- `run_job(job_id, *, …, due_by: datetime | None = None)` passes `due_by` to `claim`. When the
  claim fails it re-reads the job:
  - unexpired lock → `JobBusyError` (unchanged);
  - otherwise, with `due_by` set → new `JobNotDueError(job_id, next_run_at)`;
  - otherwise → `JobBusyError` as today.
- No run row is created and no lock changed on any refusal.
- `RunResult` gains `next_run_at: datetime | None` and `retry_scheduled: bool`, copied from the
  scope after `release_lock` set them (`None`/`False` when `next_run_at` was kept).
- `JobNotDueError` is exported next to `JobBusyError` and `JobDisabledError`.

## `invio.pipeline.due` (new)

```python
async def run_due(
    *, limit: int | None = None, parallel: int = 1, deps: RunDeps | None = None
) -> DueReport: ...
```

- `limit < 1` or `parallel < 1` → `ValueError` before anything is read.
- `deps` defaults to the production wiring for the duration of the call (like `run_job`).
- Selection, outcome mapping and parallelism as described in research R5. `ran` outcomes carry
  the `RunResult` status, run id and the job's new `next_run_at`.
- Per-job exceptions (other than `KeyboardInterrupt` / `CancelledError`) become `error`
  outcomes and are logged with the error class only.
- Errors before the first job (settings, database during selection) propagate to the caller.

## `invio.pipeline.healthcheck` (new)

```python
def ping(url: str, *, failed: bool, transport: httpx2.BaseTransport | None = None) -> None: ...
```

One `GET` to `url` or `url.rstrip("/") + "/fail"`, with timeout 3 s and no redirects. It never
raises. On failure it logs `healthcheck.failed` with `error` (class name) or `status` (HTTP code)
only. `transport` exists for tests.

## `invio.config.settings.Settings.healthcheck_url` (changed)

`SecretStr | None`. The validator accepts only `http`/`https` URLs with a host. Invalid values
fail settings validation with a message naming the field (exit 2 through `setup_runtime`).
