# Data Model: `invio run-due`

**Feature**: [spec.md](spec.md) · **Research**: [research.md](research.md)

There is **no schema change and no migration**. The feature uses the existing `jobs` and `runs`
tables. It adds repository queries and in-memory result types.

## Persistent entities (existing)

### `jobs` (unchanged)

| Column | Use in this feature |
|---|---|
| `id` | identity; tie-breaker in due order |
| `name` | shown in the invocation report |
| `enabled` | only enabled jobs are due |
| `next_run_at` (UTC) | due when `<= now`; `NULL` is never due; written on release |
| `locked_until` (UTC) | claim expiry and ownership token; `NULL` = unclaimed |

Index `ix_jobs_enabled_next_run_at (enabled, next_run_at)` serves the due query.

**State transitions of one job** (`now` = clock at that step):

```text
idle/not due ──(now ≥ next_run_at)──▶ due
due ──claim(now, until=now+lock_ttl, due_by=now) ok──▶ claimed (locked_until = token)
due ──claim fails, lock unexpired──▶ due (reported busy)
claimed ──run ends, release(token, next_run_at=…)──▶ idle (locked_until = NULL)
claimed ──process killed──▶ stale (locked_until in the past once expired)
stale ──claim(now ≥ locked_until)──▶ claimed (reclaimed)
claimed ──token no longer matches (reclaimed by another run)──▶ release is a no-op (lock_lost)
```

**`next_run_at` written on release** (real runs only; dry runs keep it):

| Final run status | Schedule readable | New `next_run_at` |
|---|---|---|
| succeeded / partial | yes | `next_run(schedule, finished)` (regular slot) |
| succeeded / partial | no | unchanged (existing #22 behaviour) |
| failed | yes | `min(next_run(schedule, finished), finished + retry_delay(streak))` |
| failed | no | `finished + retry_delay(streak)` |

### `runs` (unchanged)

| Column | Use in this feature |
|---|---|
| `job_id`, `started_at` | newest-first scan for the failure streak (index `ix_runs_job_id_started_at`) |
| `status` | `failed` extends the streak; `succeeded`/`partial` end it; `running` is skipped |
| `stats["dry_run"]` | `true` → skipped by the streak (dry runs never count) |

## Derived values

### Failure streak

`streak(job, current_run) = 1 + failure_streak(job_id, exclude_run_id=current_run)`

`failure_streak` reads the job's runs newest first (`ORDER BY started_at DESC, id DESC LIMIT
50`), skipping the current run, rows with `status = running` and dry runs. It counts `failed`
rows until the first `succeeded`/`partial` row, and stops counting at `RETRY_STREAK_CAP` (6, the
first streak that reaches the 24 h cap). If the query fails, the streak counts as 1.

### Retry delay (`invio.scheduling.backoff.retry_delay`)

| streak | 1 | 2 | 3 | 4 | 5 | ≥ 6 |
|---|---|---|---|---|---|---|
| delay | 1 h | 2 h | 4 h | 8 h | 16 h | 24 h |

`retry_delay(streak) = min(timedelta(hours=1) * 2 ** (streak - 1), timedelta(hours=24))`;
`streak < 1` raises `ValueError`.

## In-memory types (new)

### `DueJob` (`invio.db.repositories`)

Frozen dataclass returned by `JobRepository.list_due`: `id: int`, `name: str`, `next_run_at:
datetime`, `locked_until: datetime | None`.

### `DueOutcome` (`invio.pipeline.due`)

| Field | Type | Notes |
|---|---|---|
| `job_id` | `int` | |
| `job_name` | `str` | |
| `kind` | `Literal["ran", "busy", "skipped", "error"]` | `error`: `run_job` raised an unexpected exception before or outside a run result |
| `run_id` | `int \| None` | set when `kind == "ran"` |
| `status` | `RunStatus \| None` | final status when `kind == "ran"` |
| `reason` | `str \| None` | sanitized, single line: `locked until …`, `no longer due`, `disabled`, `deleted`, error class |
| `locked_until` | `datetime \| None` | for `busy` |
| `next_run_at` | `datetime \| None` | the job's new next run time after a `ran` outcome, when known (from `RunResult.next_run_at`) |
| `retry` | `bool` | `True` when that next run time came from the retry delay (from `RunResult.retry_scheduled`) |

### `RunScope` / `RunResult` (changed)

`release_lock` stores what it wrote on the scope (`scope.next_run_at: datetime | None`,
`scope.retry_scheduled: bool`); `RunResult` gains the same two fields (`None` / `False` for dry
runs and when `next_run_at` was kept).

### `DueReport` (`invio.pipeline.due`)

| Field | Type | Notes |
|---|---|---|
| `started_at` | `datetime` | the invocation's `now` |
| `due` | `int` | due jobs found (busy included, before `--limit`) |
| `outcomes` | `tuple[DueOutcome, ...]` | in due order |
| `failed` | `bool` (property) | any outcome `ran` with `status == failed`, or any `error` |
| `exit_code` | `int` (property) | `1 if failed else 0` |

Counts in the summary line are derived from `outcomes`.

### `RunDeps` (changed)

New required field `retry_delay: Callable[[int], timedelta]`. Production wiring:
`invio.scheduling.backoff.retry_delay`. Test helpers pass it explicitly.

### `Settings.healthcheck_url` (changed)

Type `SecretStr | None` (was `str | None`). The validator accepts only absolute `http`/`https`
URLs with a host and rejects anything else with a message naming `INVIO_HEALTHCHECK_URL`.
