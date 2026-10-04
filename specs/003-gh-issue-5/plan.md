# Implementation Plan: Job Management Service and Record Access Layer

**Branch**: `gh-issue-5` | **Date**: 2026-10-04 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/003-gh-issue-5/spec.md` (GitHub issue #5)

## Summary

Add a thin repository layer (`src/invio/db/repositories.py`, one class per table, flush-only)
and a `JobService` (`src/invio/services/jobs.py`) that is the single entry point for job
management: create, get by name, list, update (full replacement), enable/disable, delete,
YAML import/export. Every write validates through `JobConfig` (new public `validate_job`) and
stores `model_dump(mode="json")`; reads re-validate and return frozen `JobRecord`s.
`next_run_at` is computed by an injectable `next_run(schedule, after)` (stub returns `after`
until #6). Each operation runs in one short-lived `session_scope()` (new in
`invio.db.session`), translates storage errors into `JobExistsError`/`JobNotFoundError`, and
logs one structured line per successful change.

## Technical Context

**Language/Version**: Python 3.12 (uv-managed)

**Primary Dependencies**: Existing only — SQLAlchemy ≥ 2.1, Pydantic v2, PyYAML. No new
runtime or dev dependencies.

**Storage**: Existing schema from #4 (MariaDB in production, SQLite in tests); no migration

**Testing**: pytest with the existing `db` marker and `db_engine`/`db_session` fixtures
(in-memory SQLite by default, MariaDB opt-in via `INVIO_TEST_DATABASE_URL`)

**Target Platform**: Linux (cron/systemd) and macOS dev

**Project Type**: CLI application / library modules (single project)

**Performance Goals**: Not performance-critical (tens of jobs); new tests < 10 s (SC-005)

**Constraints**: `mypy --strict` clean; no SQLAlchemy types/errors cross the service
boundary; no config contents in logs; `invio.domain` stays stdlib-only; no file overlap with
the parallel #6 worktree (`scheduling/next_run.py` is not created here)

**Scale/Scope**: ~350 LOC source (repositories ~150, service ~170, helpers ~30), ~500 LOC tests

All unknowns resolved in [research.md](./research.md) (R1–R13); no NEEDS CLARIFICATION remain.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Pre-design | Post-design |
|---|---|---|---|
| I. Strict Contracts at Boundaries | Validate at boundary; contracts defined once; fail fast | ✅ FR-003/004 | ✅ `validate_job` is the single validation entry reused by `load_yaml` and the service (R5); stored config re-validated on read (FR-007a) |
| II. CLI-First Operation | User functionality via CLI | ⚠️ Library layer only; CLI wiring is #9 (spec assumption) — recorded in Complexity Tracking | ⚠️ `JobService.from_settings()` prepared for the CLI; no command added; justified deviation, see Complexity Tracking |
| III. Test-Covered Behaviour | Every acceptance criterion tested, incl. rejection paths; no DB server needed | ✅ FR-021 | ✅ mapping in [quickstart.md](./quickstart.md); SQLite default, MariaDB opt-in (R13) |
| IV. Quality Gates Mirror CI | ruff, format, mypy strict, pytest; locked deps | ✅ | ✅ no new deps; `Any` only for JSON stats/payload columns (already `dict[str, Any]` in models) |
| V. Secrets / Observability | No secrets in logs; structured logs | ✅ FR-014a | ✅ `job_name`/`event` only, no config (R12); DB URL via `require_secret` |
| Architecture: dependency direction | `cli` → orchestration → adapters → config/domain | ✅ | ✅ `services` → `db`, `config`; `db.repositories` → `db.models`, `domain`; nothing in `db` imports `services` (R1) |
| Architecture: simplicity | No speculative abstractions | ✅ | ✅ no generic base repository, no DTOs for history tables, stub instead of scheduling module (R3, R11) |
| Workflow: branch `gh-issue-<N>`; docs | | ✅ `gh-issue-5` | ✅ README: short "Job service" developer section + layout (`services/`); no CLI/format change |

Result: **PASS with one recorded deviation** (Principle II, see Complexity Tracking).

## Project Structure

### Documentation (this feature)

```text
specs/003-gh-issue-5/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   └── python-api.md    # JobService, JobRecord, errors, repositories, session_scope, validate_job
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
src/invio/
├── config/
│   └── job.py                   # + validate_job(); load_yaml reuses it
├── db/
│   ├── __init__.py              # docstring: + repositories
│   ├── session.py               # + session_scope()
│   └── repositories.py          # NEW: Job/Run/Item/Digest/Notification/UsageRepository, UsageTotals
└── services/
    ├── __init__.py              # NEW: package docstring
    └── jobs.py                  # NEW: JobService, JobRecord, errors, _next_run_stub

tests/
├── conftest.py                  # + job_service fixture (fake clock/next_run; cleans jobs on MariaDB)
├── test_job_validate.py         # NEW: validate_job messages == load_yaml messages
├── test_db_session_scope.py     # NEW: commit / rollback / close
├── test_repositories.py         # NEW: all six repositories (US4)
└── test_job_service.py          # NEW: US1–US3, FR-007a, FR-014, FR-014a

README.md                        # + job service section, layout update
```

**Structure Decision**: Single project, existing `src/invio/` layout. The issue's
`scout/db/repositories.py` and `scout/services/jobs.py` map to `src/invio/db/repositories.py`
and the new `src/invio/services/jobs.py`.

## Implementation Notes (for /speckit-tasks)

- Order: `validate_job` (+ refactor `load_yaml`, tests) → `session_scope` (+ tests) →
  repositories (+ tests) → `JobService` errors/record/stub → create/get/list → update/
  set_enabled/delete → import/export → logging → README.
- `JobRecord` construction happens inside the scope; `expire_on_commit=False` keeps timestamps
  readable after commit, but `updated_at` set by `onupdate` must be refreshed (`flush()` then
  read) before leaving the scope.
- Create race test: insert a conflicting job via a second session between the pre-check and
  flush (monkeypatch `JobRepository.get_by_name` to return `None`) and assert
  `JobExistsError`.
- `JobConfig` inputs are re-validated via `validate_job(config.model_dump(mode="json"))`.
- Import→export equality: compare `load_yaml(export)` with `load_yaml(original)` for
  `docs/job.example.yaml` and the `job_data` fixture written to a temp file.
- Invalid stored config test: write a job row directly with `config={"bogus": 1}` via
  `db_session`, commit, then call `get_by_name` / `list`.
- On MariaDB the service fixture must delete created jobs at teardown (shared server engine).
- `JobService.list` shadows the builtin inside the class body: annotate with
  `builtins.list[...]` (or a module-level alias) so mypy resolves the return type.
- When #6 merges: change `next_run` default to `invio.scheduling.next_run.compute_next_run`
  (follow-up, not part of this issue).

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Principle II (CLI-First): job create/update/enable/delete/import/export ship as a Python API without `invio` commands | Issue #5 is the service layer that the CLI (#9), scheduler (#23) and a later API share; #9 ("CLI job management with interactive creation wizard") owns the commands and is planned on the `cli-sched` track. Nothing user-visible ships in this issue: there is no command, config or file-format change, so operators see no behaviour that bypasses the CLI | Adding thin `invio job import/export/…` commands here would duplicate and pre-empt #9's command design (wizard, output format, exit codes) and create merge conflicts with that parallel worktree |
