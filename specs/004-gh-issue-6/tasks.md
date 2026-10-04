---

description: "Task list for schedule calculation (next_run_at)"
---

# Tasks: Schedule Calculation (Next Run Time)

**Input**: Design documents from `/specs/004-gh-issue-6/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/python-api.md, quickstart.md

**Tests**: Required. Spec FR-016 and constitution principle III ask for parameterised example tests
plus a Hypothesis property test. Within each story, write the tests first and confirm they fail
before implementing.

**Organization**: Tasks are grouped by user story (spec.md US1–US4) so each story can be
implemented and verified on its own.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1–US4)

## Path Conventions

Single project: `src/invio/`, `tests/` (flat `test_<area>.py` files), run everything with `uv run`.

## Shared conventions for all tasks

- **Module**: `src/invio/scheduling/next_run.py`
  - Imports **only** the standard library (`calendar`, `datetime`, `zoneinfo`) and
    `invio.config.job` (`Frequency`, `ScheduleConfig`, `Weekday`). Never import
    `invio.services` or `invio.db` (plan, Complexity Tracking).
  - `src/invio/scheduling/__init__.py` stays as is (docstring only, no imports).
- **Public API**: `compute_next_run(schedule: ScheduleConfig, after: datetime) -> datetime`, with
  `__all__ = ["compute_next_run"]`. The contract is in `contracts/python-api.md`.
- **Resolution rule (research R1)**:
  - Build each occurrence as
    `datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(schedule.timezone))` with the default
    `fold=0`, then `.astimezone(UTC)`.
  - Do not write any special-case DST code.
- **Selection rule (research R2)**: Return `min(o for o in candidates if o > after)` over a
  fixed window of local dates around `d = after.astimezone(zone).date()`:
  - daily: `d-1 … d+2`
  - weekly: dates in `d-1 … d+8` whose `weekday()` matches
  - monthly: the month of `d-1` plus the two following months
- **Tests**:
  - Build schedules with `ScheduleConfig(**data)`, following the style of
    `tests/test_job_schedule.py`.
  - Use aware UTC datetimes via `datetime(..., tzinfo=UTC)`.
  - Every test asserts both the expected instant **and** `result.tzinfo is UTC`.
  - Style: ruff and mypy strict. No `# type: ignore` unless it is narrow and justified.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Feature branch and the dev dependency for the property tests

- [x] T001 Create the feature branch `gh-issue-6` from the latest `main`, as the constitution's Development Workflow requires and following PRs #42/#43: `git fetch origin && git switch -c gh-issue-6 origin/main`. If the worktree branch `worktree-track-cli-sched` holds the spec commits, base the branch on it instead with `git switch -c gh-issue-6`. All later commits go to `gh-issue-6`. Then add Hypothesis to the dev dependency group with `uv add --dev hypothesis`. This updates `pyproject.toml` `[dependency-groups].dev` and `uv.lock`. Verify with `uv sync --locked` and `uv run python -c "import hypothesis"`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Module skeleton, input contract and shared helpers that every story uses

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T002 [P] Create `tests/test_next_run.py`. Add the module docstring and helpers:
  - `_schedule(**overrides) -> ScheduleConfig`, with base data `{"frequency": "daily", "time": "08:00", "timezone": "Europe/Berlin"}`
  - `_utc(*args) -> datetime`, returning `datetime(*args, tzinfo=UTC)`

  Add failing tests for the input contract (research R4, contract "Errors"):
  - (a) A naive `after` raises `ValueError` matching `"after must be timezone-aware"`.
  - (b) An `after` with a non-UTC offset is treated as the same instant. A daily 08:00 Europe/Berlin schedule with `after = datetime(2026, 1, 10, 7, 0, tzinfo=timezone(timedelta(hours=1)))` must give the same result as `after = _utc(2026, 1, 10, 6, 0)`, namely `_utc(2026, 1, 10, 7, 0)`.
  - (c) The result's `tzinfo is UTC`.
- [x] T003 Create `src/invio/scheduling/next_run.py`:
  - Module docstring summarising the rules: local time, strictly after, day clamp, DST gap shifts by the gap length, DST overlap uses the first occurrence, no catch-up.
  - `__all__ = ["compute_next_run"]`.
  - A private `_resolve(day: date, at: time, zone: ZoneInfo) -> datetime` that returns `datetime.combine(day, at, tzinfo=zone).astimezone(UTC)` with `fold=0`. Comment that fold=0 gives "gap → shift forward by gap length, overlap → first occurrence" (PEP 495).
  - A private `_parse_time(value: str) -> time` for the validated `"HH:MM"` string.
  - `compute_next_run`, which:
    - raises `ValueError("after must be timezone-aware")` when `after.tzinfo is None or after.utcoffset() is None`
    - computes `zone = ZoneInfo(schedule.timezone)` and `local_day = after.astimezone(zone).date()`
    - dispatches on `schedule.frequency` to a candidate-date function
    - returns `min(o for o in (_resolve(day, at, zone) for day in candidates) if o > after)`

  For now, implement only the daily candidate function `_daily_candidates(local_day)` (`local_day - 1 … local_day + 2`) and raise `NotImplementedError` for weekly and monthly. T002 tests (a)–(c) must pass. Depends on T002.

**Checkpoint**: `uv run pytest tests/test_next_run.py -q` is green. The module exists with the contract enforced.

---

## Phase 3: User Story 1 - Daily, weekly and monthly jobs run at the configured local time (Priority: P1) 🎯 MVP

**Goal**:
- All three frequencies return the earliest local-time occurrence strictly after `after`, in UTC.
- `JobService` uses the real calculation instead of the stub (FR-001–FR-009 except clamping, FR-015).

**Independent Test**:
- Parameterised examples for daily, weekly and monthly (day 15) compare against expected UTC instants.
- A `JobService` created without a `next_run` override stores `compute_next_run(schedule, clock())`.

### Tests for User Story 1 ⚠️ (write first, ensure they fail)

- [x] T004 [US1] In `tests/test_next_run.py`, add `test_next_run_examples`, a parameterised test with `pytest.param(..., id=...)`. Each case asserts `compute_next_run(schedule, after) == expected` and `expected.tzinfo is UTC`. Cases from spec US1 and the contract table:
  - daily 08:00 Europe/Berlin: `after` 2026-01-10 06:00 UTC → 2026-01-10 07:00 UTC
  - daily 08:00 Europe/Berlin: `after` exactly 2026-01-10 07:00 UTC → 2026-01-11 07:00 UTC ("strictly after")
  - daily 08:00 Europe/Berlin: `after` 2026-01-10 06:59:59.999999 UTC → 2026-01-10 07:00 UTC (one microsecond before)
  - weekly monday 09:30 America/New_York: `after` Wednesday 2026-01-14 12:00 UTC → Monday 2026-01-19 14:30 UTC
  - weekly monday 09:30 America/New_York: `after` Monday 2026-01-19 14:00 UTC (09:00 local) → 2026-01-19 14:30 UTC (same day)
  - weekly monday 09:30 America/New_York: `after` Monday 2026-01-19 15:00 UTC (after the run) → 2026-01-26 14:30 UTC
  - monthly day 15 06:00 Asia/Tokyo: `after` 2026-01-15 00:00 UTC (09:00 local, after the run) → 2026-02-14 21:00 UTC (= 2026-02-15 06:00 JST)
  - monthly day 15 06:00 Asia/Tokyo: `after` 2026-12-20 00:00 UTC → 2027-01-14 21:00 UTC (year boundary)
  - daily 07:00 Pacific/Auckland: `after` 2026-01-10 12:00 UTC (= 2026-01-11 01:00 NZDT) → 2026-01-10 18:00 UTC (= 2026-01-11 07:00 local; the local date differs from the UTC date)
  - daily 08:00 Asia/Kathmandu (+05:45): `after` 2026-01-10 00:00 UTC → 2026-01-10 02:15 UTC
- [x] T005 [P] [US1] In `tests/test_job_service.py`, add `test_default_next_run_uses_schedule_calculation`:
  - Build a `JobService` with the test database session factory and `clock=fake_clock`, but **without** `next_run=...`, mirroring how the existing `job_service` fixture in `tests/conftest.py` builds it (keep `clean_jobs`, with no recording override).
  - Create a job from `job_data`.
  - Assert `record.next_run_at == compute_next_run(record.config.schedule, fake_clock.now)` and `record.next_run_at > fake_clock.now`, which proves the stub is no longer the default.
  - **Re-enable** (FR-015, SC-005):
    - `service.set_enabled(name, False)` and assert `next_run_at is None`.
    - `fake_clock.advance(timedelta(days=3))`, then `service.set_enabled(name, True)`.
    - Assert `next_run_at == compute_next_run(schedule, fake_clock.now)` and `> fake_clock.now`.
  - **Update while enabled**:
    - Advance the clock, then `service.update(name, ...)` with the same config but a changed `schedule.time`.
    - Assert `next_run_at == compute_next_run(new_schedule, fake_clock.now)`.
    - The signature is `update(name: str, config: JobConfig | Mapping[str, Any])`, a full config replacement.

### Implementation for User Story 1

- [x] T006 [US1] In `src/invio/scheduling/next_run.py`, implement `_weekly_candidates(local_day, weekday: Weekday)`: dates in `local_day - 1 … local_day + 8` where `day.weekday() == _WEEKDAY_INDEX[weekday]`. `_WEEKDAY_INDEX` is a module constant mapping `Weekday.MONDAY → 0 … Weekday.SUNDAY → 6`. Remove the weekly `NotImplementedError`. Depends on T003.
- [x] T007 [US1] In `src/invio/scheduling/next_run.py`, implement `_monthly_candidates(local_day, day_of_month: int)`. Start from the month of `local_day - timedelta(days=1)`, step through that month and the two following months (handling the December → January year rollover), and return `date(year, month, day_of_month)`. Clamping comes in US2. Remove the monthly `NotImplementedError`. Depends on T006 (same file). T004 must now pass, except for cases that need clamping (none in T004).
- [x] T008 [US1] In `src/invio/services/jobs.py`:
  - Import `compute_next_run` from `invio.scheduling.next_run`.
  - Change the `JobService.__init__` default to `next_run: NextRun = compute_next_run`.
  - Delete `_next_run_stub` and its `TODO(#6)` comment.
  - Keep the `NextRun` alias and everything else unchanged.

  Then run `uv run pytest tests/test_job_service.py -q`. T005 and all existing tests must pass; the existing tests inject `recording_next_run` and are unaffected. Depends on T007.

**Checkpoint**: US1 complete. `uv run pytest tests/test_next_run.py tests/test_job_service.py -q` is green, and new jobs get a real future `next_run_at`.

---

## Phase 4: User Story 2 - Monthly jobs on days that do not exist in every month (Priority: P1)

**Goal**: Days 29–31 fall back to the last day of shorter months, month by month, with no carry-over (FR-009, research R3).

**Independent Test**: Parameterised monthly cases for days 31 and 30 across Feb (leap and non-leap), Apr and Mar.

### Tests for User Story 2 ⚠️

- [x] T009 [US2] In `tests/test_next_run.py`, add the parameterised `test_monthly_day_clamped_to_month_length` with schedule monthly `day_of_month=31`, `time="08:00"`, `timezone="UTC"` unless noted:
  - `after` 2026-01-31 09:00 → 2026-02-28 08:00
  - `after` 2028-01-31 09:00 → 2028-02-29 08:00 (leap year)
  - `after` 2026-02-28 09:00 → 2026-03-31 08:00 (no carry-over)
  - `after` 2026-04-02 00:00 → 2026-04-30 08:00
  - `after` 2026-03-31 09:00 → 2026-04-30 08:00
  - `day_of_month=30`, `after` 2026-02-01 00:00 → 2026-02-28 08:00
  - `day_of_month=29`, `after` 2027-02-01 00:00 → 2027-02-28 08:00
  - `day_of_month=31`, timezone Europe/Berlin, `after` 2026-11-30 12:00 UTC → 2026-12-31 07:00 UTC

  Also add `test_monthly_day_31_over_a_year`: iterate 12 times from `_utc(2026, 1, 1)` and assert the local days are `[31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]`.

### Implementation for User Story 2

- [x] T010 [US2] In `_monthly_candidates` in `src/invio/scheduling/next_run.py`, clamp each month separately: `date(year, month, min(day_of_month, calendar.monthrange(year, month)[1]))`. T009 must pass. Depends on T007 and T009.

**Checkpoint**: US1 and US2 green.

---

## Phase 5: User Story 3 - Correct behaviour across DST transitions (Priority: P2)

**Goal**:
- A nonexistent local time shifts forward by the gap length on the same day (FR-010, Clarifications).
- An ambiguous local time uses only the first occurrence (FR-011).

**Independent Test**: Parameterised cases at Europe/Berlin 02:30 around 2026-03-29 and 2026-10-25, plus zones with non-1h gaps.

### Tests for User Story 3 ⚠️

- [x] T011 [US3] In `tests/test_next_run.py`, add the parameterised `test_dst_transitions`:
  - Europe/Berlin daily 02:30:
    - `after` 2026-03-28 12:00 UTC → 2026-03-29 01:30 UTC (= 03:30 CEST). Also assert `result.astimezone(ZoneInfo("Europe/Berlin")).date() == date(2026, 3, 29)` ("runs at a valid time that day", issue AC).
    - `after` 2026-10-24 12:00 UTC → 2026-10-25 00:30 UTC (first occurrence, 02:30 CEST)
    - `after` 2026-10-25 00:30 UTC (exactly the first occurrence) → 2026-10-26 01:30 UTC (02:30 CET; the second occurrence at 01:30 UTC on 10-25 is never returned)
    - `after` 2026-10-25 01:00 UTC (between the occurrences) → 2026-10-26 01:30 UTC
  - Europe/Berlin daily 08:00 across the transition: `after` 2026-03-28 08:00 UTC → 2026-03-29 06:00 UTC (08:00 CEST; the UTC instant shifts by 1h)
  - America/New_York daily 02:30: `after` 2026-03-07 12:00 UTC → 2026-03-08 07:30 UTC (= 03:30 EDT)
  - Australia/Lord_Howe daily 02:15 (30-minute gap at 02:00 → 02:30 on 2026-10-04): `after` 2026-10-03 00:00 UTC → 2026-10-03 15:45 UTC (= 2026-10-04 02:45 +11:00; this was checked against `zoneinfo`)
  - Weekly sunday 02:30 Europe/Berlin: `after` 2026-03-27 00:00 UTC → 2026-03-29 01:30 UTC (gap rule also applies to weekly)
  - Monthly day 29 02:30 Europe/Berlin: `after` 2026-03-01 00:00 UTC → 2026-03-29 01:30 UTC (gap rule also applies to monthly)

  Also add `test_daily_full_year_keeps_local_time` (SC-003):
  - Start from `x = _utc(2025, 12, 31, 12)` with a daily 02:30 Europe/Berlin schedule.
  - Iterate `x = compute_next_run(schedule, x)` 365 times and convert each result to local time.
  - Assert:
    - (a) the local dates are exactly the 365 consecutive dates of 2026, one run per date
    - (b) the local time is 02:30 on 364 dates and 03:30 on 2026-03-29
    - (c) on 2026-10-25 the run is at 00:30 UTC, the first occurrence
- [x] T012 [US3] Run T011 against the implementation. It should pass without code changes because of `fold=0` (research R1). If any case fails, fix `_resolve` or the candidate window in `src/invio/scheduling/next_run.py` without adding special-case DST branches, and document the cause in a comment. Make sure the `_resolve` docstring states both DST rules with the Berlin example. Depends on T010 and T011.

**Checkpoint**: US1–US3 green.

---

## Phase 6: User Story 4 - Overdue jobs are scheduled once, not replayed (Priority: P2)

**Goal**: With `after = now`, only the next upcoming occurrence is returned, however many runs were missed (FR-014).

**Independent Test**: An overdue daily, weekly or monthly job gets exactly the next occurrence after "now".

### Tests for User Story 4 ⚠️

- [x] T013 [US4] In `tests/test_next_run.py`, add the parameterised `test_missed_runs_are_not_replayed`:
  - daily 08:00 Europe/Berlin, last due 2026-01-05 07:00 UTC, now = 2026-01-10 09:00 UTC → 2026-01-11 07:00 UTC
  - daily, now = 2026-01-10 06:00 UTC → 2026-01-10 07:00 UTC (today)
  - weekly monday 08:00 UTC, three weeks overdue → the next Monday after now
  - monthly day 1 08:00 UTC, two months overdue → the 1st of the next month after now

  For each case, also assert `compute_next_run(schedule, last_due)` is earlier than the result, to show the function depends only on `after`. No implementation change is expected; if one fails, fix `src/invio/scheduling/next_run.py`. Depends on T010.

**Checkpoint**: All user stories are independently verified.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T014 [P] Create `tests/test_next_run_properties.py` with Hypothesis (research R6).
  - **Profile**: register and load `settings(derandomize=True, max_examples=500, deadline=None)` locally in the module with `@settings(...)`, so CI is deterministic (constitution III).
  - **Strategies**:
    - `schedules()`: build a valid `ScheduleConfig` from frequency, `time` `f"{h:02d}:{m:02d}"`, `weekday` for weekly only and `day_of_month` 1–31 for monthly only.
    - Timezone: `sampled_from(["UTC", "Europe/Berlin", "America/New_York", "America/Sao_Paulo", "Australia/Lord_Howe", "Asia/Kolkata", "Asia/Kathmandu", "Pacific/Apia", "Pacific/Chatham"])`.
    - `instants()`: `datetimes(min_value=datetime(1990, 1, 1), max_value=datetime(2100, 1, 1), timezones=just(UTC))`.
  - **Properties**:
    - (1) `result.tzinfo is UTC` and `result > after` (FR-004, FR-005)
    - (2) For `a, b` sorted, `next(a) <= next(b)` (FR-012)
    - (3) Iterating `x = next(x)` 24 times gives a strictly increasing list (FR-013)
    - (4) The result is a resolved occurrence of the schedule. There must be a local date `D` among the local dates of `result - 1 day` and `result` such that `D` matches the frequency rule (weekly: `D.weekday()` equals the configured weekday; monthly: `D.day == min(day_of_month, calendar.monthrange(D.year, D.month)[1])`; daily: any date) **and** `datetime.combine(D, time, tzinfo=zone).astimezone(UTC) == result`. This covers DST-shifted results without special cases.
    - (5) Results from consecutive iterations are at most 1 period plus 1 day apart (daily ≤ 2 days, weekly ≤ 8 days, monthly ≤ 32 days), and never less than 1 period minus the maximum gap (daily ≥ 22 h), so no occurrence is skipped or doubled.
- [x] T015 [P] In `tests/test_next_run.py`, add `test_next_run_module_does_not_import_services`. Run `subprocess.run([sys.executable, "-c", "import sys, invio.scheduling.next_run; assert 'invio.services' not in sys.modules and 'invio.db' not in sys.modules"], check=True)`. This guards the layering mitigation in the plan's Complexity Tracking.
- [x] T016 [P] Update `README.md` (job service section around the line "`next_run_at` is a placeholder (the current time) until scheduling (#6) lands"). Replace that sentence with: `next_run_at` is computed by `invio.scheduling.next_run.compute_next_run`:
  - the configured local time in the job's time zone, stored as UTC and always strictly in the future
  - monthly day 29–31 falls back to the last day of shorter months
  - a time skipped by a DST jump runs shifted by the gap (02:30 → 03:30); a repeated time runs at its first occurrence
  - missed runs are not replayed

  Optionally update the Layout line `scheduling/   scheduling` to `scheduling/   next-run calculation (next_run.py)`.
- [x] T017 Run the quality gates exactly as CI does and fix any findings in the touched files: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and `uv run pytest --cov` (`fail_under = 95`; `next_run.py` must be fully covered, including the naive-input branch). Depends on T001–T016.
- [x] T018 Run the manual spot check from `specs/004-gh-issue-6/quickstart.md` section 2 and confirm the printed values match the reference table in `specs/004-gh-issue-6/contracts/python-api.md`. Depends on T017.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (T001)**: none. Needed only by T014; it can run in parallel with Phase 2.
- **Foundational (T002 → T003)**: blocks all user stories.
- **US1 (T004–T008)**: depends on Phase 2.
- **US2 (T009–T010)**: depends on T007 (`_monthly_candidates` exists).
- **US3 (T011–T012)**: depends on T010, so all frequencies are complete.
- **US4 (T013)**: depends on T010. It can run in parallel with US3 (both only add tests in the same file, so sequence the edits).
- **Polish (T014–T018)**: T014 depends on T001 and T010. T015 and T016 can start any time after T003 and T008. T017 and T018 run last.

### User Story Dependencies

- **US1 (P1)**: MVP. It replaces the stub and makes all frequencies work for days that exist.
- **US2 (P1)**: extends US1's monthly candidates (same function).
- **US3 (P2)** and **US4 (P2)**: verification stories on top of US1 and US2. Implementation changes are expected only if a test exposes a gap.

### Within Each User Story

- Tests come first and fail first. Then the implementation, then the checkpoint run.
- `src/invio/scheduling/next_run.py` and `tests/test_next_run.py` are shared files, so tasks touching the same file run sequentially.

### Parallel Opportunities

- T001 ∥ T002
- T005 (`tests/test_job_service.py`) ∥ T004 (`tests/test_next_run.py`)
- T014 (`tests/test_next_run_properties.py`) ∥ T015 ∥ T016 (`README.md`)

---

## Parallel Example: User Story 1

```bash
# Write both US1 test sets at once (different files):
Task: "T004 [US1] parameterised daily/weekly/monthly examples in tests/test_next_run.py"
Task: "T005 [US1] default JobService uses compute_next_run in tests/test_job_service.py"
# Then implement sequentially in src/invio/scheduling/next_run.py: T006 → T007, then T008 in services/jobs.py
```

## Parallel Example: Polish

```bash
Task: "T014 Hypothesis properties in tests/test_next_run_properties.py"
Task: "T015 import-isolation test in tests/test_next_run.py"
Task: "T016 README scheduling paragraph in README.md"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. T001–T003: dependency, contract and skeleton.
2. T004–T008: all frequencies plus JobService wiring. **Stop and validate.** New jobs no longer
   come due immediately.

### Incremental Delivery

1. Add US2 (clamping). This closes the issue's acceptance criterion "day 31 → Feb 28/29, Apr 30".
2. Add US3 (DST tests). This closes the acceptance criterion "02:30 Europe/Berlin on
   spring-forward day".
3. Add US4 (missed runs).
4. Polish: the Hypothesis property test (acceptance criterion "monotonic, strictly increasing"),
   the layering guard, the README, and the CI gates.

### Issue Acceptance Criteria → Tasks

| Acceptance criterion (issue #6) | Tasks |
|---|---|
| Daily, weekly and monthly parameterised tests | T004, T009, T011, T013 |
| Monthly day 31 → Feb 28/29, Apr 30 | T009, T010 |
| 02:30 Europe/Berlin on spring-forward date runs at a valid time that day | T011, T012 |
| Result always timezone-aware UTC and `> after` | T002, T004, T014 |
| Hypothesis: monotonic, strictly increasing | T001, T014 |

---

## Notes

- [P] tasks touch different files and have no dependencies on incomplete tasks.
- Commit after each phase checkpoint on branch `gh-issue-6`, with commit messages referencing #6.
- The PR description must justify the new dev-only dependency (`hypothesis`) and mention the
  recorded `services → scheduling.next_run` layering exception.
