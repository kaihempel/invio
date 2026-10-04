# Implementation Plan: Schedule Calculation (Next Run Time)

**Branch**: `gh-issue-6` | **Date**: 2026-10-04 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/004-gh-issue-6/spec.md`

## Summary

Add the pure function `invio.scheduling.next_run.compute_next_run(schedule, after)`. It turns a
validated `ScheduleConfig` (daily / weekly / monthly, local `HH:MM`, IANA time zone) and an aware
reference instant into the earliest occurrence strictly after that instant, as aware UTC.

How it works:

- Occurrences are built in local time with `zoneinfo` using `fold=0`. This gives both DST rules
  without special-case code: in a spring-forward gap the run moves forward by the gap length,
  and in a fall-back overlap the first occurrence is used (research R1).
- Monthly runs use the configured day, clamped to the month length (R3).
- The result is the minimum resolved candidate `> after` from a small, fixed window of local
  dates (R2). This makes "strictly after", monotonicity and a strictly increasing sequence hold
  by construction.

`JobService` switches its `next_run` default from `_next_run_stub` to `compute_next_run`.
Hypothesis is added as a dev dependency for the property tests.

## Technical Context

**Language/Version**: Python 3.12+ (CI and local `.venv`: CPython 3.14)

**Primary Dependencies**:
- Standard library only at runtime: `zoneinfo`, `datetime`, `calendar`
- Existing `invio.config.job.ScheduleConfig`
- New **dev-only** dependency: `hypothesis` (R6)

**Storage**: N/A. Uses the existing `jobs.next_run_at` column (`UTCDateTime`); no migration.

**Testing**: pytest, with parameterised example tests and Hypothesis property tests
(`derandomize=True`). Coverage gate `fail_under = 95`.

**Target Platform**: Linux and macOS, using the system IANA tz database (R7)

**Project Type**: Single Python project (CLI and library), `src/invio/`

**Performance Goals**: Bounded work per call (SC-006). The candidate window is fixed at no more
than 10 dates and there are no unbounded loops. This is checked in code review; no timing test is
written, because timing tests are non-deterministic (constitution III).

**Constraints**:
- Pure and deterministic: no clock, I/O or logging
- Naive `after` → `ValueError`
- Result `tzinfo is UTC` and `> after`
- mypy strict and ruff clean

**Scale/Scope**:
- One new module of about 60 lines and two test modules
- A one-line default change plus stub removal in `services/jobs.py`
- README update

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle / Constraint | Requirement | Pre-research | Post-design |
|---|---|---|---|
| I. Strict contracts at boundaries | Typed inputs, fail fast | ✅ input is the validated `ScheduleConfig` | ✅ naive `after` raises `ValueError` (R4); contract in `contracts/python-api.md` |
| II. CLI-first | User functionality reachable via CLI | ✅ n/a: no new user command; used through `JobService` (CLI #7, scheduler #23) | ✅ |
| III. Test-covered behaviour | Every acceptance criterion tested; deterministic; no network/DB | ✅ | ✅ example table plus Hypothesis with `derandomize=True`; pure unit tests (R6) |
| IV. Quality gates mirror CI | ruff, mypy strict, locked deps | ✅ | ✅ `uv add --dev hypothesis` updates `uv.lock`; no `type: ignore` planned |
| V. Secrets / observability | No secrets; structured logging | ✅ n/a: pure function does not log | ✅ |
| Tech: new runtime dependencies justified | — | ✅ none (stdlib `zoneinfo`; no `tzdata`, R7) | ✅ dev-only `hypothesis`, justified in the PR |
| Tech: dependency direction | Lower layers do not import higher ones | ⚠️ `services` → `scheduling.next_run` | ⚠️ accepted; see Complexity Tracking |
| Tech: simplicity first | No speculative abstractions | ✅ | ✅ one function, no scheduler/rrule abstraction |
| Workflow: docs updated | README for behaviour changes | ✅ | ✅ R8: README job-service paragraph |

Gate result: **PASS**, with one recorded deviation.

## Project Structure

### Documentation (this feature)

```text
specs/004-gh-issue-6/
├── plan.md              # This file
├── research.md          # Phase 0: R1–R8
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   └── python-api.md    # Phase 1: compute_next_run contract + reference examples
├── checklists/
│   └── requirements.md  # spec quality checklist
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
src/invio/
├── config/job.py               # unchanged: ScheduleConfig, Frequency, Weekday
├── scheduling/
│   ├── __init__.py             # unchanged, must stay import-free
│   └── next_run.py             # NEW: compute_next_run (stdlib + invio.config.job only)
└── services/jobs.py            # CHANGED: next_run default = compute_next_run; remove _next_run_stub + TODO(#6)

tests/
├── test_next_run.py            # NEW: parameterised daily/weekly/monthly, day-31 clamp, leap year,
│                               #      DST gap/overlap (Europe/Berlin, America/New_York, Lord_Howe),
│                               #      exact-match, non-UTC offset input, naive input error, missed runs
├── test_next_run_properties.py # NEW: Hypothesis — UTC & > after, monotonic, strictly increasing,
│                               #      local weekday/day matches
└── test_job_service.py         # + one test: default JobService (no next_run override) stores
                                #   compute_next_run(schedule, clock()) instead of "now"

pyproject.toml / uv.lock        # dev group: + hypothesis
README.md                       # job-service paragraph: replace placeholder note with scheduling rules
```

**Structure Decision**: Single-project layout as described in the README.

- The calculation lives in the existing `invio.scheduling` package, at the path named by the
  issue and the `TODO(#6)`.
- Tests stay flat under `tests/`, following the existing `test_<area>.py` convention.

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| `invio.services.jobs` imports `invio.scheduling.next_run`, while the scheduler in `invio.scheduling` (#23) will import `invio.services`. This is a package-level two-way edge. | `JobService` must compute `next_run_at` on create/update/re-enable (#5 FR). The issue, the README layout and the existing `TODO(#6)` place the calculation in `scheduling/next_run.py`. | (a) Moving the function to `config` or `domain` contradicts the issue and layout, and `domain` must not import `config`. (b) Keeping the stub as the default and wiring the real function only in `from_settings` leaves a wrong default that tests or callers may pick up silently. **Mitigation**: `next_run.py` is a leaf (stdlib plus `invio.config.job` only), and `scheduling/__init__.py` stays import-free, so no module-level import cycle can form. A test asserts that importing `invio.scheduling.next_run` does not import `invio.services`. |
