# Implementation Plan: Scheduled Execution of Due Jobs with Locking and Missed-Run Handling

**Branch**: `gh-issue-23` | **Date**: 2026-10-07 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/016-gh-issue-23/spec.md`

## Summary

This plan adds **`invio run-due [--limit N] [--parallel N]`**. A systemd timer calls it to run
every due job unattended. It is built on the claim → run → finalize path of `invio job run`
(#22), which is not duplicated.

- **Selection.** A new `JobRepository.list_due` reads enabled jobs with
  `next_run_at <= now (UTC)`, ordered by due time. Jobs that are still locked are reported as
  busy. `--limit` caps how many jobs are started.
- **Claiming.** The existing atomic `claim` UPDATE gains a `due_by` condition, so a job finished
  by a parallel invocation between selection and claim is not run a second time (research R1).
  Expired locks are reclaimed (`locked_until <= now`, unchanged).
- **Running.** A new `invio.pipeline.due.run_due` calls `run_job` for each candidate, one after
  another or with up to N at once (`asyncio.Semaphore`). One job's failure never stops the
  others.
- **Finishing.** The shared `release_lock` now receives the run's final status. After a failed
  real run, manual or scheduled (clarification Q2), it sets
  `next_run_at = min(regular slot, now + retry_delay(streak))`. The delay is 1 h doubling up to
  24 h. The streak is derived from run history, so no migration is needed (R3, R4). Missed slots
  are already collapsed by `compute_next_run` (R10).
- **Health check.** A new `invio.pipeline.healthcheck.ping` sends one `GET <url>` on exit 0 and
  `<url>/fail` on a non-zero exit (clarification Q1). It never affects the outcome.
  `Settings.healthcheck_url` becomes a validated `SecretStr` (R6).

There is no schema change and no new dependency.

## Technical Context

**Language/Version**: Python 3.12+

**Primary Dependencies**: all of them already exist. Typer for the command, SQLAlchemy 2.x for
the repository queries, `httpx2` for the health-check ping, and Pydantic v2 / pydantic-settings
for the settings validator. LangGraph is unchanged.

**Storage**: the existing `jobs` (`next_run_at`, `locked_until`, index
`ix_jobs_enabled_next_run_at`) and `runs` (index `ix_runs_job_id_started_at`) tables. No
migration (see [data-model.md](data-model.md)).

**Testing**: pytest with pytest-asyncio.
- **Unit and integration tests** run on in-memory SQLite. They use `tests.pipeline_helpers.make_deps`
  (fake provider and sources, fixed clock) and Typer's `CliRunner` with `job`-style test seams.
- **Health-check tests** use an `httpx2.MockTransport`.
- **The concurrency acceptance test** runs two threads against MariaDB
  (`INVIO_TEST_DATABASE_URL`, marked `db`, skipped on SQLite).

**Target Platform**: a Linux server; invoked by a systemd timer (oneshot service).

**Project Type**: CLI tool / library (single project).

**Performance Goals**: an invocation with nothing due finishes in < 5 s (SC-007): one indexed
SELECT and no HTTP except the optional ping. Its 3 s timeout keeps the total under 5 s even
when the monitor is down.

**Constraints**:
- At most one execution per job per due occurrence across processes (FR-005).
- The lock is always released when a run ends (FR-008).
- Layering: `graph` must not import `scheduling` or `pipeline`; `cli` must not import `db` or
  `graph`.
- No secrets in logs.

**Scale/Scope**: tens of jobs per server; `--parallel` typically 1–3. Each invocation reads the
due set once.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
|---|---|---|
| I. Strict contracts at boundaries | CLI options validated (≥ 1) before any work. `healthcheck_url` parsed into a validated `SecretStr` at the settings boundary. Shared types (`DueJob`, `DueReport`) are defined once. | ✅ |
| II. CLI-first | New auto-discovered module `cli/commands/run_due.py` → `invio run-due` with no edits elsewhere (R8). Report on stdout, logs on stderr. Exit codes 0/1/2, configuration errors 2. | ✅ |
| III. Test-covered behaviour | Every acceptance scenario is mapped to a test ([quickstart.md](quickstart.md) table). Fakes only; no network. The MariaDB test is explicitly an integration test required by the issue. | ✅ |
| IV. Quality gates mirror CI | No new tools; ruff, mypy strict and pytest as usual. No new suppressions planned. | ✅ |
| V. Secrets stay secret, runs observable | The ping URL is a `SecretStr` and never logged (only the error class or status). Each run keeps its run context (`job`, `run_id`). New events: `run_due.started/finished`, `run.retry_scheduled`, `healthcheck.failed`. | ✅ |
| Architecture: dependency direction | `cli` → `pipeline` (due, healthcheck) → `graph`/`db`/`scheduling`. The retry policy is injected into `graph` through `RunDeps.retry_delay`, like `next_run` (R4). | ✅ |
| Simplicity first | No new table or column. The streak is derived from run history. Parallelism is a semaphore over the existing `run_job`. | ✅ |
| Workflow: docs updated | README: `invio run-due`, retry behaviour (it also affects `invio job run`), health-check semantics, example systemd units. | ✅ planned |

**Post-design re-check (after Phase 1)**: there are no violations. One behaviour change to an
existing command is deliberate and documented: a failed manual real `invio job run` now
schedules the retry time instead of the next regular slot (spec clarification Q2, FR-010). It is
listed in the README and the PR description.

## Project Structure

### Documentation (this feature)

```text
specs/016-gh-issue-23/
├── spec.md
├── plan.md              # this file
├── research.md          # R1–R10
├── data-model.md        # no schema change; derived streak, new in-memory types
├── quickstart.md        # validation guide
├── contracts/
│   ├── cli-run-due.md         # `invio run-due` options, output, exit codes, ping
│   └── pipeline-run-due.md    # repository, RunDeps, release_lock, run_due, ping, settings
├── checklists/requirements.md
└── tasks.md             # /speckit-tasks (not created here)
```

### Source Code (repository root)

```text
src/invio/
├── cli/commands/
│   └── run_due.py            # NEW: `invio run-due` (callback with invoke_without_command)
├── pipeline/
│   ├── due.py                # NEW: run_due(), DueOutcome, DueReport
│   ├── healthcheck.py        # NEW: ping(url, failed=…)
│   ├── run.py                # CHANGED: due_by, JobNotDueError, safety net passes status
│   └── deps.py               # CHANGED: wires RunDeps.retry_delay
├── graph/
│   ├── ports.py              # CHANGED: RunDeps.retry_delay
│   └── stages.py             # CHANGED: release_lock(status=…) with retry rule
├── scheduling/
│   └── backoff.py            # NEW: retry_delay(), RETRY_* constants
├── db/repositories.py        # CHANGED: JobRepository.list_due, claim(due_by), DueJob;
│                             #          RunRepository.failure_streak
└── config/settings.py        # CHANGED: healthcheck_url → validated SecretStr

tests/
├── test_backoff.py                 # NEW: delay sequence, cap, invalid streak
├── test_db_due.py                  # NEW: list_due order/filters, claim(due_by), failure_streak
├── test_db_job_lock.py             # EXTENDED: claim with due_by
├── test_run_due.py                 # NEW: run_due outcomes, limit, parallel, missed slots, retry
├── test_run_due_concurrency.py     # NEW: two threads on MariaDB (db marker)
├── test_healthcheck.py             # NEW: success/fail URL, timeout/error swallowed, no URL logged
├── test_cli_run_due.py             # NEW: output, exit codes, option validation, ping wiring
├── test_pipeline_failures.py       # EXTENDED: manual failed run → retry time; dry run unchanged
├── test_settings.py                # EXTENDED: healthcheck_url validation
└── pipeline_helpers.py, test_graph_state.py, test_pipeline_concurrency.py
                                    # CHANGED: pass retry_delay to RunDeps

README.md                           # `invio run-due`, retry rule, health check, systemd example
```

**Structure Decision**: single project, using the existing package layout. The new
orchestration goes into `invio.pipeline` (the only layer the CLI calls for runs), the pure
policy into `invio.scheduling`, and the queries into `invio.db.repositories`.

## Complexity Tracking

There are no constitution violations to justify.
