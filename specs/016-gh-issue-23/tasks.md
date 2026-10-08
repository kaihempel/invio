---

description: "Task list for `invio run-due` with locking and missed-run handling (#23)"
---

# Tasks: Scheduled Execution of Due Jobs with Locking and Missed-Run Handling

**Input**: Design documents from `specs/016-gh-issue-23/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/cli-run-due.md](contracts/cli-run-due.md),
[contracts/pipeline-run-due.md](contracts/pipeline-run-due.md), [quickstart.md](quickstart.md)

**Tests**: Included. Constitution III and SC-008 require an automated test for every acceptance
scenario, including the rejection paths. Write each story's tests first and confirm they fail
before implementing.

**Organization**: Tasks are grouped by user story in spec priority order:
- P1: US1 (run due jobs), US2 (no double runs), US3 (stale lock reclaim)
- P2: US4 (missed slots once), US5 (retry with growing delay), US6 (health check)
- P3: US7 (`--parallel`)

`R<n>` refers to the decisions in [research.md](research.md).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story the task belongs to (US1–US7)

## Conventions used by every task

- Source goes in `src/invio/` and tests in `tests/`. mypy runs in strict mode. Add no new
  `# type: ignore` beyond the existing `rowcount` pattern in `repositories.py`, and justify
  every one.
- **Layering** (checked by `tests/test_pipeline_layering.py` and `tests/test_cli_layering.py`):
  - `invio.cli` never imports `invio.db` or `invio.graph`. It calls `invio.pipeline.due`,
    `invio.pipeline.healthcheck` and `invio.pipeline.run`.
  - `invio.graph` never imports `invio.scheduling`, `invio.pipeline`, `invio.notify` or
    `invio.cli`. The retry policy reaches `graph` only through `RunDeps.retry_delay` (R4).
  - Only `invio.cli` imports `invio.pipeline`.
- **Time**: every timestamp is a UTC-aware `datetime` taken from `deps.clock()`. Never call
  `datetime.now()` in new code.
- **Output and logging**:
  - Results go to stdout. JSON logs go to stderr through `logging.getLogger("invio.pipeline")`
    with `extra={...}`.
  - Never log a URL, a database URL or an exception message that may embed one. Log only the
    error class name (`type(err).__name__`).
- **Repositories never commit.** Callers use `session_scope(factory)`.
- **Test infrastructure**:
  - Tests use `tests.pipeline_helpers.make_deps` (fake ports, `RoutedFakeProvider`, fixed
    clock), `tests.db_helpers.make_job`, and the `db_engine` / `clean_jobs` fixtures.
  - Tests that need two DB connections are marked
    `@pytest.mark.skipif(uses_sqlite(), reason="needs two connections; SQLite tests share one")`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: confirm a green baseline. No new dependencies or project files are needed.

- [X] T001 Create and switch to the feature branch the constitution requires ("Development Workflow": branch `gh-issue-<N>`): `git switch -c gh-issue-23` from an up-to-date `main`, and confirm that `git branch --show-current` prints `gh-issue-23`. Then run `uv sync --locked && uv run ruff check && uv run mypy src && uv run pytest -q` in the repository root and note any pre-existing failures in `specs/016-gh-issue-23/tasks.md` under "Notes" before changing code

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the repository queries, the due-aware claim and the `run_job` refusal type that
every story builds on (R1, R2).

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

### Tests for the foundation (write first, must fail)

- [X] T002 [P] Create `tests/test_db_due.py` (`pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_jobs")]`) testing `JobRepository.list_due(now, limit=None)`:
  - **Included**: enabled jobs with `next_run_at <= now`, including `next_run_at == now`.
  - **Excluded**: jobs with `next_run_at > now`, disabled jobs, and jobs with `next_run_at IS NULL`.
  - **Order**: `next_run_at` ascending, then `id`.
  - **Limit**: `limit=2` returns only the first two.
  - **Result type**: `DueJob(id, name, next_run_at, locked_until)` objects, with `locked_until` filled for locked jobs.
- [X] T003 [P] Extend `tests/test_db_job_lock.py` with tests for `claim(..., due_by=...)`:
  - A due job is claimed.
  - A job with `next_run_at > due_by` is not claimed, and `locked_until` stays `NULL`.
  - A job with `next_run_at IS NULL` is not claimed.
  - An expired lock on a still-due job is claimed.
  - Without `due_by`, the old behaviour is unchanged (a future `next_run_at` is still claimable).
- [X] T004 [P] Extend `tests/test_pipeline_run.py` with `run_job(job_id, due_by=now)` tests:
  - A job whose `next_run_at` lies after `due_by` raises `JobNotDueError` (carrying `job_id` and `next_run_at`), creates no run row and leaves `locked_until` unchanged.
  - A job with an unexpired lock still raises `JobBusyError`.
  - Without `due_by`, a failed claim raises `JobBusyError` exactly as before.

### Implementation for the foundation

- [X] T005 Add the frozen, slotted dataclass `DueJob` (`id: int`, `name: str`, `next_run_at: datetime`, `locked_until: datetime | None`) and `JobRepository.list_due(self, now: datetime, *, limit: int | None = None) -> list[DueJob]` to `src/invio/db/repositories.py`:
  - One SELECT of `Job.id, Job.name, Job.next_run_at, Job.locked_until` with `WHERE Job.enabled IS TRUE AND Job.next_run_at IS NOT NULL AND Job.next_run_at <= now ORDER BY Job.next_run_at, Job.id`.
  - Apply `.limit(limit)` when given.
  - It uses the index `ix_jobs_enabled_next_run_at`. Read-only.
- [X] T006 Add the keyword argument `due_by: datetime | None = None` to `JobRepository.claim` in `src/invio/db/repositories.py`:
  - When it is set, add `Job.next_run_at.is_not(None), Job.next_run_at <= due_by` to the same conditional UPDATE's WHERE clause (R1). There is still exactly one statement.
  - Update the docstring: "of several concurrent callers at most one gets `True`; with `due_by`, a job finished by another run since selection is not claimed".
- [X] T007 Add the exception `class JobNotDueError(LookupError)` to `src/invio/pipeline/run.py`, with `__init__(self, job_id: int, next_run_at: datetime | None)` and the message `f"job {job_id} is no longer due (next run {next_run_at})"`. Export it in `__all__`.
- [X] T008 Add the parameter `due_by: datetime | None = None` to `run_job` and to `_run` in `src/invio/pipeline/run.py`, and pass it to `jobs.claim(job_id, now=now, until=token, due_by=due_by)`. On a failed claim, `session.refresh(job)`, then:
  - if `job.locked_until is not None and job.locked_until > now`, raise `JobBusyError(job_id, job.locked_until)`;
  - else if `due_by is not None`, raise `JobNotDueError(job_id, job.next_run_at)`;
  - else raise `JobBusyError` as before.

  Document `due_by` in the docstring. `run_job_by_name` is unchanged (it never passes `due_by`).

**Checkpoint**: T002–T004 pass. `uv run pytest tests/test_db_job_lock.py tests/test_pipeline_run.py` is green.

---

## Phase 3: User Story 1 - Run every job that is due, unattended (Priority: P1) 🎯 MVP

**Goal**: `invio run-due [--limit N]` runs all due enabled jobs sequentially in due order,
moves each to its next regular slot, reports per job on stdout, and exits 0/1/2.

**Independent Test**: Seed one job due long ago, one due recently, one due in the future and one
disabled but due. Invoke `run_due` with fake deps and verify:
- exactly the two due enabled jobs ran, oldest first;
- both now have a future `next_run_at`;
- `--limit 2` with five due jobs runs only the two oldest;
- nothing due → report `nothing due`, exit 0.

### Tests for User Story 1 (write first, must fail)

- [X] T009 [P] [US1] Create `tests/test_run_due.py` (async, `pytest.mark.db`). It uses `make_deps(..., clock=fixed)` and seeds jobs through `tests.db_helpers.make_job` with valid configs, like `tests/test_pipeline_run.py`. Test:
  - (a) due set and order: run order is recorded via the notifier/fake ports or by run `started_at`/id order;
  - (b) future and disabled jobs are untouched: no run row, `next_run_at` unchanged;
  - (c) after a successful run, `locked_until IS NULL` and `next_run_at == deps.next_run(schedule, clock())`;
  - (d) nothing due → `DueReport.outcomes == ()`, `due == 0`, `exit_code == 0`. For SC-007, also count statements with a SQLAlchemy `before_cursor_execute` listener on the engine: exactly one SELECT on `jobs`, and no INSERT or UPDATE. Monkeypatch `run_job` so the test fails if it is called;
  - (e) `limit=2` over five due jobs → two `ran`, three still due;
  - (f) a job whose run fails (provider raises) → `ran` with `status failed`, the other jobs still run, `exit_code == 1`;
  - (g) a job deleted, or disabled between selection and claim (monkeypatch `JobRepository.list_due` to return a stale `DueJob`) → `skipped` with reason `deleted`/`disabled`;
  - (h) `limit=0` or `parallel=0` → `ValueError` before any query.
- [X] T010 [P] [US1] Create `tests/test_cli_run_due.py` using `CliRunner` against `invio.cli.main.app`, monkeypatching the seams `run_due_module._run_due` (returns a canned `DueReport`) and `run_due_module._ping` (records calls). Test:
  - (a) the stdout line formats exactly as in `contracts/cli-run-due.md`: `ran`/`busy`/`skipped` lines in due order, times in the job's time zone, the `error  <name>  <ErrorClass>` line for an `error` outcome, and the summary `due N · ran N (a succeeded, b partial, c failed) · busy N · skipped N`, with ` · error N` appended only when N > 0;
  - (b) nothing due prints exactly `nothing due`;
  - (c) exit codes: 0 for all succeeded/partial or nothing due, 1 when any run failed or any `error` outcome exists;
  - (d) `--limit 0` and `--parallel 0` exit 2 with `Error: --limit must be at least 1` / `Error: --parallel must be at least 1` on stderr. `--limit abc` exits 2 with Typer's own usage error, whose stderr names `--limit`. In all three cases `_run_due` and `_ping` are never called;
  - (e) a `SQLAlchemyError` raised by `_run_due` exits 1 with the standard `mapped_errors` message;
  - (f) `MissingSettingError` exits 2;
  - (g) job names with control characters are passed through `strip_control`.

### Implementation for User Story 1

- [X] T011 [US1] Create `src/invio/pipeline/due.py` with:
  - `DueOutcome`, a frozen slotted dataclass with fields `job_id: int`, `job_name: str`, `kind: Literal["ran", "busy", "skipped", "error"]`, `run_id: int | None = None`, `status: RunStatus | None = None`, `reason: str | None = None`, `locked_until: datetime | None = None`, `next_run_at: datetime | None = None`, `retry: bool = False`, and a `timezone: str = "UTC"` used only for display.
  - `DueReport`, a frozen dataclass with `started_at: datetime`, `due: int`, `outcomes: tuple[DueOutcome, ...]`, the property `failed -> bool` (any `ran` with `status is RunStatus.FAILED`, or any `error`) and the property `exit_code -> int` (`1 if failed else 0`).

  Export `run_due`, `DueOutcome` and `DueReport` in `__all__`.
- [X] T012 [US1] Add `next_run_at: datetime | None = None` and `retry_scheduled: bool = False` to `RunScope` in `src/invio/graph/scope.py`. Set `scope.next_run_at` in `release_lock` in `src/invio/graph/stages.py` (to the value written, or `None` when kept). Add the matching fields `next_run_at: datetime | None` and `retry_scheduled: bool` to `RunResult` in `src/invio/pipeline/run.py` and fill them from the scope in `_run`. `retry_scheduled` stays `False` until US5.
- [X] T013 [US1] Implement `async def run_due(*, limit: int | None = None, parallel: int = 1, deps: RunDeps | None = None) -> DueReport` in `src/invio/pipeline/due.py` (sequential path):
  - Validate `limit`/`parallel >= 1` (`ValueError`) before anything is read.
  - Use `_deps_or_default` from `invio.pipeline.run`, then read `now = deps.clock()` once.
  - In one `session_scope`, call `JobRepository(session).list_due(now)`.
  - Split the result: jobs with `locked_until is not None and locked_until > now` become `busy` outcomes (reason `locked until …`). The rest are candidates; keep the first `limit` of them.
  - Put the per-job work in a private helper `async def _run_one(job: DueJob, deps: RunDeps) -> DueOutcome` (US7 reuses it). For each candidate in order, it calls `await run_job(job.id, deps=deps, due_by=deps.clock())` and maps the result:
    - `RunResult` → `ran` (run id, status, `result.next_run_at`, `result.retry_scheduled`);
    - `JobBusyError` → `busy`;
    - `JobNotDueError` → `skipped` "no longer due";
    - `JobDisabledError` → `skipped` "disabled";
    - `JobNotFoundError` → `skipped` "deleted";
    - any other `Exception` → `error` with `reason=type(err).__name__`, logged as `run_due.job_error` with only the class name.
  - Never catch `KeyboardInterrupt` or `CancelledError`.
  - Log `run_due.started` (`due`, `candidates`) and `run_due.finished` (counts, `exit_code`).
  - Return the `DueReport` with outcomes in due order (busy jobs keep their due position).
  - Fill `DueOutcome.timezone` from the job's stored schedule when readable. Add a small read of `config["schedule"]["timezone"]` in the same selection session via `JobRepository.get`, falling back to `"UTC"`.
- [X] T014 [US1] Create `src/invio/cli/commands/run_due.py`:
  - `app = typer.Typer(help="Run every job that is due (for a systemd timer).", no_args_is_help=False)` with one `@app.callback(invoke_without_command=True)` function `run_due_command(limit: int | None = Option(None, "--limit", help=...), parallel: int = Option(1, "--parallel", help=...))`.
  - The help text notes that `--parallel` multiplies with `INVIO_MAX_PARALLEL_ITEMS`.
  - Validate both options at least 1 with `fail("Error: --limit must be at least 1", 2)` / `fail("Error: --parallel must be at least 1", 2)`.
  - Test seams: `_run_due(limit, parallel) -> DueReport` (runs `asyncio.run(run_due(limit=..., parallel=...))`) and `_ping(url, failed)`.
  - Wrap the call in `mapped_errors(config_exit=2)`.
  - Print the report with a `_print_report(report)` helper that follows `contracts/cli-run-due.md`. Use `invio.cli.run_output.format_time(dt, outcome.timezone)` and `strip_control`, and print `nothing due` for an empty report.
  - Finally `raise typer.Exit(report.exit_code)` when it is non-zero.
  - The module must not import `invio.db` or `invio.graph`.
- [X] T015 [US1] Add `"commands/run_due.py"` to the module tuple in `test_the_cli_reaches_the_run_through_the_pipeline_and_services_only` in `tests/test_pipeline_layering.py`. Add a test to `tests/test_cli.py` asserting that `invio --help` lists `run-due` and that `invio run-due --help` shows `--limit` and `--parallel`.

**Checkpoint**: `invio run-due` works end to end on SQLite (quickstart §2 steps 1–2). US1 tests are green. This is the MVP.

---

## Phase 4: User Story 2 - Overlapping invocations never run the same job twice (Priority: P1)

**Goal**: a job claimed by another invocation, or by a manual `job run`, is skipped as busy. A
job finished by a parallel invocation between selection and claim is not run again. Two
concurrent processes never run the same job.

**Independent Test**: on MariaDB, two threads each call `run_due` at the same moment over the
same 10 due jobs. Every job gets exactly one new run per round, repeated over 20 rounds.

### Tests for User Story 2 (write first, must fail)

- [X] T016 [P] [US2] Add to `tests/test_run_due.py`:
  - (a) a due job pre-claimed with `locked_until = now + 1h` → `busy` outcome with `locked_until`; other due jobs still run; its lock is unchanged; it does not count toward `limit`;
  - (b) the race of R1: monkeypatch `run_job` (or wrap `JobRepository.claim`) so that, between selection and claim, the job's `next_run_at` is moved to the future. The outcome is `skipped` "no longer due", no run row is created, and `exit_code == 0`;
  - (c) the busy race: the lock is taken by someone else between selection and claim → `busy`.
- [X] T017 [P] [US2] Create `tests/test_run_due_concurrency.py` (`pytestmark = [pytest.mark.db, pytest.mark.usefixtures("clean_jobs")]`, skipped on SQLite via `uses_sqlite()`):
  - Seed 10 due jobs.
  - Build one `make_deps` per thread over `session_factory(db_engine)` with a fake provider and a shared fixed clock.
  - Start two `threading.Thread`s that `barrier.wait()` and then call `asyncio.run(run_due(deps=...))`.
  - Assert that every job has exactly one run row created in this round, that the union of `ran` job ids from both reports equals the 10 jobs, and that the intersection is empty.
  - Parametrize `round` over `range(20)` (SC-001).
- [X] T018 [P] [US2] Add a CLI test to `tests/test_cli_run_due.py`: a report with a `busy` outcome prints `busy  <name>  locked until <time in job tz>` and exits 0.

### Implementation for User Story 2

- [X] T019 [US2] Verify that `run_due` in `src/invio/pipeline/due.py` reports busy jobs at their due position and excludes them from the `limit` count, and that a `busy` result from `JobBusyError` carries `exc.locked_until`. Fix any gaps that T016–T017 reveal. No new module is expected; the guarantee comes from T006/T008/T013.

**Checkpoint**: T016–T018 are green. With `INVIO_TEST_DATABASE_URL` set, `uv run pytest -m db tests/test_run_due_concurrency.py` passes 20/20 rounds.

---

## Phase 5: User Story 3 - A crashed run does not block a job forever (Priority: P1)

**Goal**: a lock past its expiry is reclaimed by the next invocation. A run that outlived its
lock stops and never overwrites the new owner's lock or `next_run_at`.

**Independent Test**: a due job with `locked_until = now - 1s` is run by `run_due` and ends
unlocked. A due job with `locked_until = now + 1h` is skipped and keeps its lock.

### Tests for User Story 3 (write first, must fail)

- [X] T020 [P] [US3] Add to `tests/test_run_due.py`:
  - (a) a due job with `locked_until = now - timedelta(seconds=1)` → `ran`, then `locked_until IS NULL`;
  - (b) a due job with `locked_until == now` → `ran` (expiry instant is reclaimable);
  - (c) a due job with `locked_until = now + 1h` → `busy`, lock unchanged;
  - (d) lock lost: a run whose clock passes its token mid-run (advance the fake clock beyond `lock_ttl` from a fake source fetch, then let another claim take the job) does not clear the new lock and does not change `next_run_at`. The `run.lock_lost` warning is logged.

### Implementation for User Story 3

- [X] T021 [US3] Confirm that no production change is needed: reclaiming is `claim`'s `locked_until <= now`, and lock loss is handled by `ensure_lock_held` plus the token-guarded `release`. If T020(d) shows that `release_lock` can still overwrite `next_run_at` after losing the lock, fix `JobRepository.release`/`release_lock` in `src/invio/db/repositories.py` / `src/invio/graph/stages.py` so that `next_run_at` is written only together with a successful token match. Today it is one UPDATE guarded by the token, so this is expected to hold.

**Checkpoint**: all P1 stories are green. MVP plus concurrency safety is complete.

---

## Phase 6: User Story 4 - Missed slots after downtime run once (Priority: P2)

**Goal**: a job overdue by several slots runs once and then waits for its next future slot.

**Independent Test**: a daily job with `next_run_at = now - 3 days`; two consecutive `run_due`
calls produce exactly one run, and `next_run_at > now` after the first.

### Tests for User Story 4 (write first, must fail)

- [X] T022 [P] [US4] Add to `tests/test_run_due.py`, using `deps` with `next_run=compute_next_run` (from `invio.scheduling.next_run`) and a daily schedule at 07:00 Europe/Berlin:
  - (a) `next_run_at` three days in the past → the first `run_due` gives one `ran`, and `next_run_at` equals the first 07:00 Berlin slot after the clock;
  - (b) a second `run_due` with the same clock → `nothing due` (no outcomes) and still one run row in total;
  - (c) the `DueOutcome.next_run_at` of the `ran` outcome equals the stored value.

### Implementation for User Story 4

- [X] T023 [US4] No new code is expected (R10): `release_lock` computes the next run from the finish time via `compute_next_run`, which never replays past slots. If T022 fails, fix the computation in `release_lock` in `src/invio/graph/stages.py` to use `deps.clock()` at release time as `after`.

**Checkpoint**: US4 is green.

---

## Phase 7: User Story 5 - Failed runs are retried later with growing delay (Priority: P2)

**Goal**: after a failed real run, manual or scheduled, `next_run_at = min(regular slot, finish
+ retry_delay(streak))`, where `retry_delay` is 1 h, 2 h, 4 h, 8 h, 16 h, then 24 h. A
succeeded or partial run resets the streak. Dry runs never change the schedule or the streak.

**Independent Test**: with a failing provider, the first failure sets `next_run_at` to finish +
1 h; advancing the clock and failing again sets + 2 h; a success returns to the regular slot. A
failed manual `job run` follows the same rule; a dry run changes nothing.

### Tests for User Story 5 (write first, must fail)

- [X] T024 [P] [US5] Create `tests/test_backoff.py` for `invio.scheduling.backoff.retry_delay`:
  - Streaks 1–7 map to `1h, 2h, 4h, 8h, 16h, 24h, 24h`.
  - `retry_delay(100) == 24h`.
  - `retry_delay(0)` and `retry_delay(-1)` raise `ValueError`.
  - Constants: `RETRY_BASE == timedelta(hours=1)`, `RETRY_MAX == timedelta(hours=24)`, `RETRY_STREAK_CAP == 6`.
- [X] T025 [P] [US5] Add `failure_streak` tests to `tests/test_db_due.py`. Seed runs with `make_run` helpers (or `RunRepository.start` plus a status update) with increasing `started_at`, and check `RunRepository.failure_streak(job_id, exclude_run_id=..., cap=6)`:
  - (a) no runs → 0;
  - (b) `failed, failed, succeeded(older)` → 2;
  - (c) `partial` ends the streak;
  - (d) dry runs (`stats={"dry_run": True}`) are skipped, whatever their status;
  - (e) `running` rows are skipped;
  - (f) `exclude_run_id` is skipped;
  - (g) eight consecutive failures with `cap=6` → 6;
  - (h) only runs of the given job count.
- [X] T026 [P] [US5] Add to `tests/test_run_due.py`, with `retry_delay=invio.scheduling.backoff.retry_delay` and a `next_run` that returns `after + 7 days` so the retry is always earlier:
  - (a) the first failure → `next_run_at == finish + 1h` and `DueOutcome.retry is True`;
  - (b) advance the clock past it and fail again → `+ 2h`, then `+ 4h`;
  - (c) a success after failures → `next_run_at == next_run(schedule, finish)` and `retry is False`;
  - (d) a regular slot earlier than the retry (`next_run` returns `after + 30 min`) → the regular slot wins and `retry is False`;
  - (e) a second `run_due` immediately after a failure → `nothing due` (no tight loop, SC-004);
  - (f) a failing `failure_streak` query (monkeypatch it to raise `SQLAlchemyError`) → the delay of streak 1 is used and the lock is still released;
  - (g) a failed run whose stored schedule is unreadable (corrupt `config["schedule"]`) → `next_run_at == finish + retry_delay(streak)`.
- [X] T027 [P] [US5] Extend `tests/test_pipeline_failures.py`:
  - (a) a failed manual `run_job` (no `due_by`) sets `next_run_at` by the retry rule and counts toward the streak of a following `run_due` failure (expected `+ 2h`);
  - (b) a failed dry run leaves `next_run_at` unchanged and is not counted (the next real failure gets `+ 1h`);
  - (c) the safety-net path (graph raises outside `finalize`, as in the existing safety-net tests) also applies the retry rule;
  - (d) a run whose notifier fails (status `partial`) uses the regular slot.
- [X] T028 [P] [US5] Add to `tests/test_cli_run_due.py`: a `ran … failed` outcome with `retry=True` prints `next <time> (retry)`. Add to `tests/test_cli_job_run.py`: a failed `invio job run` still exits 1 (unchanged), and `invio job show` afterwards shows the retry time (regression for the changed #22 behaviour).

### Implementation for User Story 5

- [X] T029 [P] [US5] Create `src/invio/scheduling/backoff.py`:
  - Module docstring stating the policy.
  - Constants `RETRY_BASE = timedelta(hours=1)`, `RETRY_MAX = timedelta(hours=24)` and `RETRY_STREAK_CAP = 6`.
  - `def retry_delay(streak: int) -> timedelta` returning `min(RETRY_BASE * 2 ** (streak - 1), RETRY_MAX)`, raising `ValueError(f"streak must be >= 1, got {streak}")` for `streak < 1`. Guard against overflow by clamping `streak` to `RETRY_STREAK_CAP` before exponentiation.
  - `__all__`.
- [X] T030 [P] [US5] Add `RunRepository.failure_streak(self, job_id: int, *, exclude_run_id: int, cap: int) -> int` to `src/invio/db/repositories.py`:
  - SELECT `Run.id, Run.status, Run.stats` `WHERE Run.job_id == job_id AND Run.id != exclude_run_id ORDER BY Run.started_at.desc(), Run.id.desc() LIMIT 50`.
  - Iterate in Python: skip rows with `status == RunStatus.RUNNING` or `(stats or {}).get("dry_run") is True`; count `FAILED`; stop at `SUCCEEDED`/`PARTIAL` or when `count == cap`.
  - Read-only.
- [X] T031 [US5] Add the required field `retry_delay: Callable[[int], timedelta]` directly after `clock` in `RunDeps` in `src/invio/graph/ports.py`. Pass `retry_delay=retry_delay` (imported from `invio.scheduling.backoff`) in `default_deps` in `src/invio/pipeline/deps.py`. Update every other constructor:
  - `tests/pipeline_helpers.py::make_deps`: new keyword `retry_delay: Callable[[int], timedelta] = backoff.retry_delay`, passed through;
  - `tests/test_graph_state.py` (`values["retry_delay"]`);
  - `tests/test_pipeline_concurrency.py` (`values["retry_delay"]`).
- [X] T032 [US5] Change `release_lock(deps, scope)` to `release_lock(deps, scope, *, status: RunStatus) -> datetime | None` in `src/invio/graph/stages.py`:
  - Dry run: unchanged.
  - Compute `regular` as today (`None` when the schedule is unreadable or `next_run` raises).
  - If `status is RunStatus.FAILED`:
    - `streak = 1 + RunRepository(session).failure_streak(scope.job_id, exclude_run_id=scope.run_id, cap=_STREAK_READ_CAP)` in its own `session_scope`. Define the module constant `_STREAK_READ_CAP = 6` with a comment that it mirrors `invio.scheduling.backoff.RETRY_STREAK_CAP`: graph cannot import scheduling, and `retry_delay` clamps larger streaks anyway.
    - A raising streak query → `streak = 1`, logged `run.streak_unreadable` with the class name.
    - `retry = deps.clock() + deps.retry_delay(streak)`.
    - `next_run_at = retry if regular is None or retry < regular else regular`.
    - `scope.retry_scheduled = next_run_at == retry and (regular is None or retry < regular)`.
    - Log `run.retry_scheduled` with `streak` and `next_run_at` (ISO) when the retry time was chosen.
  - Keep the token-guarded single `release` UPDATE.
  - Set `scope.next_run_at`.
- [X] T033 [US5] Update the callers of `release_lock`:
  - `finalize` in `src/invio/graph/stages.py` passes `status=status`.
  - `_safety_net` in `src/invio/pipeline/run.py` captures `status = record_failure(...)`. It defaults to `RunStatus.FAILED` when `record_failure` raises, so replace the bare `contextlib.suppress` with try/except that keeps the default, and passes it on.
  - Adjust the monkeypatched `boom` signatures in `tests/test_pipeline_failures.py` and `tests/test_pipeline_review.py` to accept `**kwargs`.
- [X] T034 [US5] Fill `RunResult.retry_scheduled` from `scope.retry_scheduled` in `_run` in `src/invio/pipeline/run.py`. Map it to `DueOutcome.retry` in `src/invio/pipeline/due.py`. Print the `(retry)` suffix in `_print_report` in `src/invio/cli/commands/run_due.py`.

**Checkpoint**: US5 is green, and the existing `tests/test_pipeline_*` suite is still green, since `plus_one_hour` equals the first retry delay.

---

## Phase 8: User Story 6 - External health monitoring (Priority: P2)

**Goal**: exactly one ping per invocation: `GET <url>` on exit 0, and `GET <url>/fail` on a
non-zero exit or on a configuration error after settings loaded. A ping never changes the
outcome and never logs the URL.

**Independent Test**: a `MockTransport` recorder sees the success path, the failed-run path and
the abort path. With no URL configured there is no request; with an unreachable monitor the
exit code is unchanged.

### Tests for User Story 6 (write first, must fail)

- [X] T035 [P] [US6] Create `tests/test_healthcheck.py` for `invio.pipeline.healthcheck.ping` with `httpx2.MockTransport`:
  - (a) `failed=False` → one `GET` to the exact URL;
  - (b) `failed=True` → `GET <url>/fail`, and a trailing slash in the URL is not doubled;
  - (c) a 500 response → no exception, and the log record `healthcheck.failed` has `status=500`;
  - (d) a transport that raises `httpx2.ConnectError` / `httpx2.TimeoutException` → no exception, and the log has the `error` class name;
  - (e) no log record and no exception message contains the URL or its path (use a URL with a secret-looking UUID and assert it is absent from `caplog.text`);
  - (f) the client is created with a 3 s timeout and `follow_redirects=False`: a 302 response is logged as failed status 302 and not followed.
- [X] T036 [P] [US6] Extend `tests/test_settings.py`:
  - `INVIO_HEALTHCHECK_URL=https://hc-ping.com/uuid` loads as a `SecretStr` whose `get_secret_value()` is the URL, and whose `repr` does not contain the URL.
  - `ftp://x`, `not a url` and `https://` (no host) fail validation with a message naming `healthcheck_url`.
  - The unset value is `None`.
- [X] T037 [P] [US6] Add to `tests/test_cli_run_due.py`, with a settings override that has a health-check URL:
  - (a) exit 0 → `_ping` called once with `failed=False` after the report is printed;
  - (b) a failed run → `failed=True`, exit 1;
  - (c) `_run_due` raises `SQLAlchemyError` → `failed=True`, exit 1;
  - (d) `MissingSettingError` → `failed=True`, exit 2;
  - (e) `--parallel 0` → no ping;
  - (f) no URL configured → `_ping` never called;
  - (g) `_ping` raising is impossible by contract, but a ping that logs a failure leaves the exit code unchanged (use the real `ping` with a failing transport).

### Implementation for User Story 6

- [X] T038 [P] [US6] Change `healthcheck_url: str | None = None` to `healthcheck_url: SecretStr | None = None` in `src/invio/config/settings.py`. Add a `field_validator("healthcheck_url")` (mode `after`) that parses the secret value with `urllib.parse.urlsplit` and requires `scheme in {"http", "https"}` and a non-empty `netloc`. Otherwise raise `ValueError("must be an absolute http(s) URL")`, without echoing the value. Update `.env.example` only if its comment needs it.
- [X] T039 [P] [US6] Create `src/invio/pipeline/healthcheck.py` with `def ping(url: str, *, failed: bool, transport: httpx2.BaseTransport | None = None) -> None`:
  - Compute `target = url.rstrip("/") + "/fail" if failed else url`.
  - Use `with httpx2.Client(timeout=3.0, follow_redirects=False, transport=transport) as client: response = client.get(target)`.
  - Treat a non-2xx response as a failure: log `healthcheck.failed` with `extra={"status": response.status_code}`.
  - Catch `Exception` and log `healthcheck.failed` with `extra={"error": type(err).__name__}`.
  - On success, log `healthcheck.sent` with `extra={"failed": failed}`.
  - Never raise, and never log `url`/`target`.
  - Logger: `logging.getLogger("invio.pipeline")`. `__all__ = ["ping"]`.
- [X] T040 [US6] Wire the ping into `src/invio/cli/commands/run_due.py`:
  - Read `url = get_settings().healthcheck_url` inside a guard. If the settings fail to load, use no URL.
  - Run the invocation in a `try/except typer.Exit as exit_: code = exit_.exit_code` block (covering `mapped_errors` exits 1/2 and the report exit). For any other `Exception`, log `run_due.aborted` (class name only), set `code = 1`, and re-raise after pinging. For `KeyboardInterrupt`, ping `failed=True` and re-raise.
  - In `finally`, when `url` is set and the option validation passed, call `_ping(url.get_secret_value(), failed=code != 0)` exactly once.
  - Option validation happens before the `try`, so usage errors never ping.

**Checkpoint**: US6 is green. Quickstart §2 steps 1 and 5 show the pings in the stand-in monitor log.

---

## Phase 9: User Story 7 - Run several due jobs in parallel when allowed (Priority: P3)

**Goal**: `--parallel N` runs up to N due jobs concurrently, starting them in due order. The
default of 1 stays strictly sequential.

**Independent Test**: four due jobs whose fake source fetch records start/end times and awaits a
shared `asyncio.Event`. Without `--parallel` no two runs overlap; with `parallel=2` at most two
overlap, and each job runs exactly once.

### Tests for User Story 7 (write first, must fail)

- [X] T041 [P] [US7] Add to `tests/test_run_due.py`. Wrap `deps.fetch_source` with an async recorder that increments or decrements an "active runs" counter and records the maximum, yielding with `await asyncio.sleep(0)` a few times. Check:
  - (a) `parallel=1` → max active 1, and the start order equals the due order;
  - (b) `parallel=2` with four jobs → max active 2, four `ran` outcomes, outcomes still in due order in the report;
  - (c) `parallel=3` with `limit=2` → only two jobs run.

### Implementation for User Story 7

- [X] T042 [US7] Extend `run_due` in `src/invio/pipeline/due.py`. When `parallel > 1`:
  - Create `asyncio.Semaphore(parallel)` and one task per candidate via `asyncio.TaskGroup`. Each task acquires the semaphore before calling the per-job helper extracted in T013 (`_run_one(job, deps) -> DueOutcome`).
  - Tasks are created in due order, so starts follow due order.
  - Collect outcomes by candidate index so the report stays in due order.
  - `_run_one` already converts every `Exception` into an outcome, so one failing job never cancels the group. `KeyboardInterrupt`/`CancelledError` propagate and cancel the rest (their safety nets release the locks).
  - Keep the sequential loop for `parallel == 1`.

**Checkpoint**: all user stories are green.

---

## Phase 10: Polish & Cross-Cutting Concerns

- [X] T043 [P] Update `README.md`:
  - Replace the "`invio run-due` follows in #23" sentence and add an `invio run-due` section: synopsis, options, output and exit codes from `contracts/cli-run-due.md`, and the `--parallel` × `INVIO_MAX_PARALLEL_ITEMS` note.
  - Document the retry rule (1 h doubling to 24 h, never later than the regular slot; also applies to failed real `invio job run`; dry runs never change the schedule).
  - Document the health-check semantics (success on exit 0, `/fail` otherwise; URL treated as a secret).
  - Add example `invio-run-due.service` (`Type=oneshot`, `ExecStart=… invio run-due`) and `invio-run-due.timer` (`OnCalendar=*:0/5`, `Persistent=true`) units.
- [X] T044 [P] Update the `INVIO_HEALTHCHECK_URL` comment in `.env.example` to state "pinged after every `invio run-due`; `<url>/fail` on a non-zero exit".
- [X] T045 Run the full gates: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src` and `uv run pytest`. If a MariaDB is available, also run `INVIO_TEST_DATABASE_URL=… uv run pytest -m db`. Fix every finding.
- [X] T046 Walk through `specs/016-gh-issue-23/quickstart.md` §2 manually on a local SQLite database, and record any deviation from the expected output in `specs/016-gh-issue-23/quickstart.md`.
- [X] T047 Write `specs/016-gh-issue-23/pr-description.md`, following `specs/015-gh-issue-22/pr-description.md`:
  - summary, design decisions R1/R3/R6;
  - the behaviour change for failed manual `invio job run` (clarification Q2);
  - the new setting type of `INVIO_HEALTHCHECK_URL`;
  - the test matrix (link to quickstart);
  - "Closes #23".

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: depends on Setup and blocks every user story.
- **US1 (Phase 3)**: depends on Foundational. It is the MVP, and every later story extends its
  `run_due` and CLI.
- **US2 (Phase 4)**, **US3 (Phase 5)** and **US4 (Phase 6)**: depend on US1. They are mostly
  tests and can run in parallel with each other.
- **US5 (Phase 7)**: depends on US1 (`DueOutcome`, the CLI printer, `RunResult.next_run_at`).
  It is independent of US2–US4.
- **US6 (Phase 8)**: depends on US1 (the CLI command). It is independent of US2–US5.
- **US7 (Phase 9)**: depends on US1 (`_run_one` extracted in T013). Ideally it comes after US2,
  so the concurrency tests also cover parallel mode.
- **Polish (Phase 10)**: after all the stories it documents (US1, US5, US6, US7).

### Within each story

Write tests first and confirm they fail. Then implement in this order: repository/pure modules →
graph/pipeline → CLI.

### Task-level dependencies worth noting

- T005/T006 → T008 → T013.
- T012 → T013 (the `ran` outcome reads `RunResult.next_run_at`).
- T029 → T031 → T032 → T033 → T034. T030 comes before T032.
- T038 → T040, and T039 → T040.
- T013 → T042.

## Parallel Example: Foundational

```text
T002 tests/test_db_due.py      | T003 tests/test_db_job_lock.py | T004 tests/test_pipeline_run.py
then T005 → T006 (same file, sequential) and T007 (different file) in parallel, then T008
```

## Parallel Example: User Story 5

```text
T024 tests/test_backoff.py | T025 tests/test_db_due.py | T026 tests/test_run_due.py
T027 tests/test_pipeline_failures.py | T028 tests/test_cli_run_due.py + tests/test_cli_job_run.py
then T029 src/invio/scheduling/backoff.py | T030 src/invio/db/repositories.py
then T031 → T032 → T033 → T034
```

## Parallel Example: User Story 6

```text
T035 tests/test_healthcheck.py | T036 tests/test_settings.py | T037 tests/test_cli_run_due.py
then T038 src/invio/config/settings.py | T039 src/invio/pipeline/healthcheck.py
then T040 src/invio/cli/commands/run_due.py
```

## Implementation Strategy

### MVP first (US1 only)

1. Phase 1 → Phase 2 → Phase 3.
2. **Stop and validate**: quickstart §2 steps 1–2. Due jobs run sequentially, move to their
   next slot, and the report and exit codes are correct.

### Incremental delivery

1. Add US2 + US3 (concurrency and stale locks). This is the safety needed before enabling the
   systemd timer in production.
2. Add US4 (pinning missed slots) and US5 (retry). US5 changes manual `job run` too, so release
   it together with the README note.
3. Add US6 (health check). Enable monitoring in production.
4. Add US7 (`--parallel`) when there are enough jobs to need it.

### Notes

- [P] tasks touch different files and have no dependency on an incomplete task.
- Tasks in the same story that touch `tests/test_run_due.py` or `src/invio/pipeline/due.py` are
  sequential, even across stories.
- Commit after each checkpoint.
