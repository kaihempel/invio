# Research: `invio run-due` with Locking and Missed-Run Handling

**Feature**: [spec.md](spec.md) · **Plan**: [plan.md](plan.md) · **Date**: 2026-10-07

The Technical Context had no open `NEEDS CLARIFICATION` items after `/speckit-clarify`. The
items below are the design questions the plan had to settle, each checked against the current
code (`src/invio/pipeline/run.py`, `src/invio/graph/stages.py`, `src/invio/db/repositories.py`,
`src/invio/cli/main.py`, `tests/test_db_job_lock.py`).

---

## R1 — Claiming must also re-check that the job is still due

**Decision**: `JobRepository.claim` gets an optional `due_by: datetime | None` argument. When it
is set, the conditional UPDATE also requires `next_run_at IS NOT NULL AND next_run_at <= due_by`.
`run-due` always claims with `due_by=now`; `invio job run` keeps claiming without it.

**Rationale**: The issue's UPDATE (`… WHERE id = :id AND (locked_until IS NULL OR locked_until
< :now)`) only stops two runs from overlapping. It does not stop a job from running twice for
the same due time. Example: invocation A selects job 7, invocation B selects job 7, A claims it,
runs it, sets `next_run_at` to tomorrow and releases the lock. Then B reaches job 7 and its claim
succeeds, because the lock is free again, so job 7 runs a second time. FR-005 requires "at most
once per due occurrence". Putting the due check into the same atomic UPDATE closes this gap
without an extra round trip or a transaction spanning selection and claim.

**Alternatives considered**:
- *`SELECT … FOR UPDATE SKIP LOCKED` over the due set*: holds row locks for the whole run
  (hours), needs a long transaction, and SQLite (the unit-test database) does not support it.
- *Re-reading `next_run_at` after the claim and releasing when it is in the future*: two
  statements, and the claim is briefly taken for nothing. The single UPDATE is simpler and
  provably atomic.

The existing comparison `locked_until <= now` (an expired lock can be taken at its expiry
instant) stays. It is already pinned down by `test_claim_exactly_at_expiry_succeeds`, which
covers the issue's "stale lock is reclaimed".

## R2 — A claim that fails must tell "busy" from "no longer due"

**Decision**: When the claim with `due_by` fails, `run_job` re-reads the job:
- If it has an unexpired lock, it raises the existing `JobBusyError`.
- Otherwise it raises a new `JobNotDueError` (the job is no longer due because another run just
  finished it).

`run-due` reports the first as **busy** and the second as **skipped (no longer due)**. Disabled
and deleted jobs keep raising `JobDisabledError` and `JobNotFoundError`, which `run-due` reports
as **skipped (disabled)** and **skipped (deleted)**. No run row is created and no lock is changed
in any of these cases (same contract as #22).

## R3 — Where the retry delay is computed and how the failure streak is counted

**Decision**:
- `release_lock(deps, scope, *, status)` in `invio.graph.stages` now receives the run's final
  status.
- For a real run (not a dry run) whose status is `failed` it sets
  `next_run_at = min(regular_next, finished + retry_delay(streak))`. When the schedule cannot be
  read it uses `finished + retry_delay(streak)` alone.
- For `succeeded` and `partial` it keeps today's behaviour: the regular slot.
- **The streak is derived from run history**, with no new column:
  `streak = 1 + RunRepository.failure_streak(job_id, exclude_run_id=current)`.
- `failure_streak` reads the job's latest finished runs, newest first, skips dry runs
  (`stats["dry_run"] is True`) and rows still marked `running` (crashed runs), and counts
  `failed` runs until it reaches the first `succeeded` or `partial` run. It stops counting at
  the cap (R4), so it reads a small, bounded number of rows (`LIMIT 50` covers interleaved dry
  runs) through the existing index `ix_runs_job_id_started_at`.
- If the streak query fails, the streak counts as 1 and the lock is still released (the
  "release always" rule of FR-008).

**Rationale**:
- The run history is already the source of truth for run outcomes. A stored counter would
  need a migration and could drift from the history after a crash.
- Excluding the current run id and adding 1 makes the count correct even when the safety net
  could not write the `failed` status (database hiccup).
- Because the rule lives in the shared `release_lock`, it covers both `run-due` and the manual
  `invio job run` (clarification Q2) with one code path. Dry runs never reach it: `release_lock`
  already skips the schedule for them.

**Alternatives considered**:
- *A `jobs.failure_streak` column*: O(1) and exact, but it needs a migration plus reset logic in
  three places (success, partial, manual edit). Rejected under "simplicity first".
- *Applying the rule only in `run-due`*: rejected by clarification Q2.

## R4 — Retry delay policy and its home

**Decision**: A pure function `retry_delay(streak: int) -> timedelta` in the new module
`invio/scheduling/backoff.py` returns `min(1 h × 2^(streak-1), 24 h)`, with `streak >= 1`: 1 h,
2 h, 4 h, 8 h, 16 h, 24 h, 24 h, … It is injected through a new required `RunDeps.retry_delay`
field, the same way `RunDeps.next_run` injects `compute_next_run`.

**Rationale**: `invio.graph` must not import `invio.scheduling` (`test_pipeline_layering`).
Injection keeps the layering, keeps the policy unit-testable on its own, and lets the run tests
pick a policy. Only three test helpers and `default_deps` construct `RunDeps`, so a required
field is cheap and cannot be forgotten silently. The existing test default
`next_run = plus_one_hour` equals the first retry delay, so existing "failed run sets next run"
tests keep their expected values.

**Alternatives considered**:
- *Putting the policy in `invio/retry.py`*: that module is for call-level retries inside one
  run. Mixing in scheduling policy would blur its purpose.
- *Making the delays configurable now*: not required. The spec's assumption allows it later.

## R5 — Orchestrating one invocation: `invio.pipeline.due.run_due`

**Decision**: A new module `invio/pipeline/due.py` exposes
`async run_due(*, limit: int | None, parallel: int = 1, deps: RunDeps | None = None) -> DueReport`:
1. Inside `_deps_or_default(deps)`, read the clock once (`now`) and select the due set:
   `JobRepository.list_due(now, limit)`. It returns `DueJob(id, name, next_run_at,
   locked_until)` for enabled jobs with `next_run_at <= now`, ordered by `next_run_at, id`, using
   the index `ix_jobs_enabled_next_run_at`.
2. Jobs whose lock is unexpired at selection time are reported **busy** straight away and do not
   count toward `--limit`. `--limit` caps the number of runnable candidates, so it effectively
   counts jobs started, as FR-001 says. Losing a race at claim time is still reported as busy.
3. Each candidate runs through `run_job(job_id, due_by=now_at_claim)`. That is the existing
   claim → run row → graph → finalize path, unchanged apart from R1/R2. Every per-job exception
   is caught and recorded in the report, so one failure never stops the others (FR-011).
   `KeyboardInterrupt` and `asyncio.CancelledError` are not caught.
4. Sequential by default. With `parallel > 1`, an `asyncio.Semaphore(parallel)` limits the
   number of concurrent `run_job` calls. Jobs are started in due order. All runs share one
   `RunDeps` (one engine, one HTTP client, one set of providers), as concurrent item tasks do
   inside a run today.

**Rationale**:
- Reusing `run_job` means the claim, run history, lock-expiry checks (`ensure_lock_held`),
  safety net and `finalize` stay one code path. This covers FR-006, FR-008 and FR-016.
- `invio.pipeline` is the layer the CLI is allowed to call (`test_pipeline_layering`).

**Notes**:
- `--parallel N` multiplies with the per-run item concurrency (`max_parallel_items`, default 4).
  This is documented in the CLI help and README. No automatic scaling is added.
- The SQLAlchemy pool (default 5 + 10 overflow) covers `parallel × short-lived sessions`. Each
  stage opens and closes its own session.

## R6 — Health-check signal

**Decision**:
- A new module `invio/pipeline/healthcheck.py` provides `ping(url: str, *, failed: bool,
  transport=None) -> None`. It sends one `GET` to `url` (success) or to `url.rstrip("/") +
  "/fail"` (failure) using a plain `httpx2.Client`: timeout 3 s (so SC-007's 5 s holds even when the monitor hangs), `follow_redirects=False`, no
  retries.
- Any exception (network error, timeout, non-2xx) is logged as `healthcheck.failed` with only
  the error class and the HTTP status. The URL is never logged, because ping URLs embed a secret
  UUID. `ping` never raises.
- The CLI sends the signal exactly once, after `run_due` returns or raises, with
  `failed = exit_code != 0` (clarification Q1, FR-014).
- `Settings.healthcheck_url` becomes `SecretStr | None`, and a validator accepts only absolute
  `http`/`https` URLs. This is Constitution I: parse at the boundary, and principle V: secret
  types for credentials. Nothing else reads the setting today.

**Rationale**:
- Ping endpoints (healthchecks.io, Uptime Kuma, Cronitor) accept `GET` with `/fail` appended.
- The SSRF-guarded `SafeHttpClient` is meant for untrusted source URLs. An operator-configured
  monitoring URL may legitimately point to localhost or a LAN host, so the plain client is right.

**Alternatives considered**:
- *A `/start` ping at the beginning*: useful for measuring run time, but not required. Can be
  added later.
- *Retrying the ping*: delays the exit. Monitoring services already handle a missing ping.

## R7 — Exit codes and configuration errors

**Decision**: `invio run-due` exits:
- **0** when there were no failed runs and no abort. This includes "nothing due", all jobs busy,
  and partial runs.
- **1** when at least one run failed, or the invocation aborted. An abort can be a database error
  while selecting, an unexpected exception, or `KeyboardInterrupt` (130 is not used, to keep one
  "failed" code for systemd).
- **2** for usage errors (`--limit 0`, `--parallel 0`, non-numeric values) and configuration
  errors (invalid settings, missing `INVIO_DATABASE_URL`).

Health-check behaviour:
- Usage errors are rejected before anything runs, and no signal is sent.
- For configuration errors raised after settings were loaded (e.g. `MissingSettingError`), the
  `/fail` signal is sent when the health-check URL itself is readable.
- When the settings cannot be loaded at all, there is no URL to ping, and only exit 2 is
  reported.

**Rationale**: This matches Constitution II (configuration errors exit 2), the spec's FR-013,
and systemd's "non-zero = failed".

## R8 — A single-command Typer module

**Decision**: `src/invio/cli/commands/run_due.py` defines `app = typer.Typer(…)` with one
`@app.callback(invoke_without_command=True)` that takes the options.
`discover_commands` registers it as `invio run-due` (the underscore becomes a dash). No other
file changes. `SELF_CONFIGURING_GROUPS` is unchanged, so the root callback runs
`setup_runtime(config_exit=2)`.

**Rationale**: `add_typer` always mounts a group. A callback with `invoke_without_command=True`
makes `invio run-due --limit 3` work without an extra sub-command name. This keeps Constitution
II (new commands need no edits elsewhere).

## R9 — Concurrency acceptance test on MariaDB

**Decision**: `tests/test_run_due_concurrency.py` is marked `db` and skipped on SQLite, like
`test_concurrent_claims_grant_exactly_one`. The test:
- Seeds 10 due jobs.
- Starts two threads, each calling `asyncio.run(run_due(deps=…))` behind a `threading.Barrier`.
  Both use the shared server engine and a fake provider.
- Asserts that every job has exactly one new run row and that the union of "ran" outcomes equals
  the 10 jobs. A parametrization repeats the round 20 times (SC-001).

The unit tests on SQLite cover the same logic deterministically. They pre-claim jobs, back-date
locks, and interleave a finished run between selection and claim (R1).

## R10 — Missed slots and clock

**Decision**: There is nothing new to build:
- `compute_next_run(schedule, after=finished)` already ignores past slots ("Missed runs are not
  replayed"), so an overdue job runs once (FR-009).
- Selection, claim and release all use `deps.clock()` in UTC (`UTCDateTime` columns).
- A test pins this down: a daily job three days overdue, two consecutive invocations, one run.
