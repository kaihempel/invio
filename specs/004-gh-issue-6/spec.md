# Feature Specification: Schedule Calculation (Next Run Time)

**Feature Branch**: `gh-issue-6`

**Created**: 2026-10-04

**Status**: Draft

**Input**: User description: "GitHub issue #6: [FEAT] Implement schedule calculation (next_run_at). Jobs run daily, weekly or monthly at a local time (HH:MM) in a given IANA time zone, configured via the existing schedule configuration (frequency, time, weekday, day_of_month 1-31, timezone). The next run is stored in UTC and must handle DST transitions and short months correctly. Depends on #3 (job config). Requirements: (1) a pure calculation that takes a schedule and a reference instant ('after') and returns the next run instant, UTC in / UTC out, replacing the temporary 'run immediately' stub used by the job service (TODO #6); (2) compute in local time, then convert to UTC; (3) monthly: if day_of_month exceeds the month length, use the last day of that month; (4) DST: nonexistent local times (spring forward) shift to the next valid time; ambiguous times (fall back) use the first occurrence; (5) always return a strictly future time relative to 'after'; (6) missed runs: because the calculation is based on after = now, an overdue job is scheduled only once. Acceptance criteria: daily, weekly and monthly cases covered by parameterized tests; monthly on day 31 yields Feb 28/29, Apr 30 etc.; a job at 02:30 Europe/Berlin on the spring-forward date runs at a valid time that day; result always timezone-aware UTC and > after; property-based test confirms monotonic, strictly increasing results."

## Clarifications

### Session 2026-10-04

- Q: When a job's local time does not exist on a day because clocks jump forward (e.g. 02:30 Europe/Berlin on 2026-03-29), when should the job run that day? → A: Shift forward by the length of the gap (02:30 → 03:30 local, 01:30 UTC).

## User Scenarios & Testing *(mandatory)*

The "users" of this feature are the job service (which records a job's next run time on
create, update and re-enable) and the upcoming scheduler (which decides which jobs are due and
reschedules them after a run) — and, through them, the operator who expects a job configured
for "every Monday at 08:00 Europe/Berlin" to run at exactly that wall-clock time, all year
round. Today the job service uses a placeholder that makes every job due immediately; this
feature replaces it with the real calendar calculation.

### User Story 1 - Daily, weekly and monthly jobs run at the configured local time (Priority: P1)

An operator configures a job to run daily, weekly on a given weekday, or monthly on a given day
of the month, at a local time in their time zone. Whenever the job is created, updated,
re-enabled or has just run, the system determines the next moment it should run and stores it
as a UTC instant, so that the job fires at the configured local wall-clock time.

**Why this priority**: This is the core of scheduling. Without it every job is due
immediately (current placeholder behaviour) and the scheduler cannot be shipped.

**Independent Test**: For a set of schedules (daily, weekly, monthly) and reference instants,
compute the next run and compare it with the expected UTC instant; each case can be verified in
isolation without any storage or scheduler.

**Acceptance Scenarios**:

1. **Given** a daily schedule at 08:00 Europe/Berlin and a reference instant of 2026-01-10 06:00 UTC (07:00 local), **When** the next run is calculated, **Then** the result is 2026-01-10 07:00 UTC (08:00 local, same day).
2. **Given** the same daily schedule and a reference instant of 2026-01-10 07:00 UTC (exactly 08:00 local), **When** the next run is calculated, **Then** the result is 2026-01-11 07:00 UTC (the following day), because the result must lie strictly after the reference.
3. **Given** a weekly schedule on Monday at 09:30 America/New_York and a reference instant on a Wednesday, **When** the next run is calculated, **Then** the result is the following Monday at 09:30 New York time, expressed in UTC.
4. **Given** a weekly schedule on Monday at 09:30 and a reference instant on a Monday at 09:00 local, **When** the next run is calculated, **Then** the result is that same Monday at 09:30 local.
5. **Given** a monthly schedule on day 15 at 06:00 Asia/Tokyo and a reference instant after this month's run, **When** the next run is calculated, **Then** the result is the 15th of next month at 06:00 Tokyo time, expressed in UTC.
6. **Given** a schedule in a non-UTC time zone, **When** the next run is calculated, **Then** the result is a time-zone-aware UTC instant (never a naive or local-offset value).

---

### User Story 2 - Monthly jobs on days that do not exist in every month (Priority: P1)

An operator configures a job to run monthly on day 29, 30 or 31 (typically meaning "at the end
of the month"). In months that are shorter than the configured day, the job runs on the last
day of that month instead of being skipped or spilling into the next month.

**Why this priority**: Without this rule, jobs configured for day 31 would silently skip about
five months a year, or the calculation would fail outright. It is an explicit acceptance
criterion of the issue.

**Independent Test**: Calculate successive monthly runs for day 31 over a full year, including
a leap year, and check the resulting dates.

**Acceptance Scenarios**:

1. **Given** a monthly schedule on day 31 and a reference instant in late January 2026 after the January run, **When** the next run is calculated, **Then** the result falls on 2026-02-28.
2. **Given** a monthly schedule on day 31 and a reference instant in late January 2028 (leap year) after the January run, **When** the next run is calculated, **Then** the result falls on 2028-02-29.
3. **Given** a monthly schedule on day 31 and a reference instant in early April, **When** the next run is calculated, **Then** the result falls on April 30.
4. **Given** a monthly schedule on day 31 whose February run (on the 28th) has just happened, **When** the next run is calculated, **Then** the result falls on March 31 (the configured day is used again as soon as the month is long enough; shortening is never carried over).
5. **Given** a monthly schedule on day 30 and a reference instant in February, **When** the next run is calculated, **Then** the result falls on the last day of February.

---

### User Story 3 - Correct behaviour across daylight saving time transitions (Priority: P2)

An operator in a time zone with daylight saving time configures a job at a local time that
falls into the hour skipped in spring or the hour repeated in autumn. The job still runs
exactly once on that day, at a well-defined, valid moment.

**Why this priority**: DST transitions happen only twice a year and only affect jobs whose
configured time falls within the transition hour, but getting them wrong means a missed or
doubled digest. It is an explicit acceptance criterion of the issue.

**Independent Test**: Calculate the next run for schedules at 02:30 Europe/Berlin with
reference instants on the day before each DST transition, and check the resulting instants.

**Acceptance Scenarios**:

1. **Given** a daily schedule at 02:30 Europe/Berlin and a reference instant on 2026-03-28 (the day before the spring-forward transition, when local time jumps from 02:00 to 03:00), **When** the next run is calculated, **Then** the result falls on 2026-03-29 at a valid local time of that day — 03:30 CEST (01:30 UTC), i.e. the nonexistent time is shifted forward by the length of the gap.
2. **Given** a daily schedule at 02:30 Europe/Berlin and a reference instant on 2026-10-24 (the day before the fall-back transition, when local time 02:00–03:00 occurs twice), **When** the next run is calculated, **Then** the result is the first occurrence of 02:30 on 2026-10-25 — 02:30 CEST (00:30 UTC).
3. **Given** the same fall-back schedule and a reference instant between the first and the second occurrence of 02:30 on 2026-10-25, **When** the next run is calculated, **Then** the result is 2026-10-26 02:30 CET — the job does not run a second time on the repeated hour.
4. **Given** a schedule whose local time is not affected by a transition (e.g. 08:00), **When** the next run is calculated across a transition date, **Then** the job runs at 08:00 local on both sides, i.e. its UTC instant shifts by one hour.

---

### User Story 4 - Overdue jobs are scheduled once, not replayed (Priority: P2)

When invio was not running for a while (machine asleep, service stopped) a job may have missed
one or more scheduled runs. When the scheduler catches up, the job runs once and its next run is
calculated from "now", so missed occurrences are not replayed one after another.

**Why this priority**: Prevents a burst of duplicate digests after downtime. The behaviour
falls out of calculating from the current instant, but it must be explicit and verified.

**Independent Test**: For a daily job whose last scheduled run is several days in the past,
calculate the next run with the reference set to "now" and check that it is the next upcoming
occurrence after now, not one of the missed ones.

**Acceptance Scenarios**:

1. **Given** a daily job at 08:00 whose stored next run was five days ago, **When** the next run is calculated with the current instant as reference, **Then** the result is the next 08:00 after the current instant (today or tomorrow), and none of the four missed days produce additional runs.

---

### Edge Cases

- Reference instant exactly equal to a scheduled occurrence → the following occurrence is returned (strictly later).
- Reference instant one microsecond before a scheduled occurrence → that occurrence is returned.
- Reference instant without time zone information (naive) → rejected with a clear error; it is never silently interpreted as local or UTC.
- Reference instant with a non-UTC offset → interpreted as the absolute instant it denotes; the result is still UTC.
- Weekly schedule where the reference instant is on the configured weekday but after the configured time → next week's occurrence.
- Monthly schedule on day 31 when the reference is on the 31st after the run → last day of the next month (e.g. 30 April after 31 March).
- Local date differs from UTC date (e.g. Pacific/Auckland or America/Los_Angeles): weekday and day-of-month are always evaluated in the schedule's local time zone, not in UTC.
- Year boundaries (December → January) and leap days (Feb 29) are handled like any other date.
- A DST gap that coincides with the configured monthly or weekly occurrence date → same gap rule as for daily schedules.
- Time zones without DST (e.g. UTC, Asia/Tokyo) and time zones with unusual offsets (e.g. Asia/Kolkata +05:30, Asia/Kathmandu +05:45) → result is still correct to the minute.
- Historical or future changes to a zone's rules → the calculation follows the time zone database available at runtime.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a calculation that, given a job's schedule and a reference instant, returns the next instant at which the job should run.
- **FR-002**: The calculation MUST be pure and deterministic: its result depends only on the schedule, the reference instant and the time zone database; it MUST NOT read the clock, storage or any other external state.
- **FR-003**: The calculation MUST determine occurrences in the schedule's local time zone (local date, weekday, day of month and wall-clock time) and only then convert the chosen occurrence to UTC.
- **FR-004**: The returned instant MUST be time-zone-aware and expressed in UTC.
- **FR-005**: The returned instant MUST be strictly later than the reference instant.
- **FR-006**: The calculation MUST reject a reference instant that carries no time zone information with a clear error; reference instants with any explicit offset MUST be accepted and treated as the absolute instant they denote.
- **FR-007**: For a daily schedule, the result MUST be the earliest occurrence of the configured local time on any day that is strictly after the reference instant.
- **FR-008**: For a weekly schedule, the result MUST be the earliest occurrence of the configured local time on the configured weekday that is strictly after the reference instant.
- **FR-009**: For a monthly schedule, the result MUST be the earliest occurrence of the configured local time on the configured day of the month that is strictly after the reference instant; when the configured day exceeds the length of a month, the last day of that month MUST be used for that month only.
- **FR-010**: When the configured local time does not exist on a given date because clocks move forward, the occurrence MUST still happen on that date, shifted forward by the length of the gap (e.g. 02:30 during a 02:00→03:00 jump becomes 03:30 local); it MUST NOT be clamped to the end of the gap (03:00).
- **FR-011**: When the configured local time occurs twice on a given date because clocks move back, only the first occurrence MUST be used; the second occurrence MUST never be returned.
- **FR-012**: Results MUST be monotonic: for two reference instants where the first is not later than the second, the result for the first MUST NOT be later than the result for the second.
- **FR-013**: Repeatedly feeding the result back as the next reference instant MUST produce a strictly increasing sequence of instants with exactly one occurrence per scheduled period (day, week or month).
- **FR-014**: The calculation MUST NOT replay missed occurrences: given a reference instant of "now", it returns only the next upcoming occurrence, regardless of how many occurrences were missed.
- **FR-015**: The job service MUST use this calculation instead of the current placeholder whenever it records a job's next run time (create, update, re-enable), so that newly created or re-enabled jobs are no longer due immediately.
- **FR-016**: Every acceptance criterion MUST be covered by automated tests: parameterized example-based tests for daily, weekly and monthly schedules (including short months, leap years and both DST transitions), plus a property-based test that checks FR-004, FR-005, FR-012 and FR-013 over generated schedules and reference instants.

### Key Entities *(include if feature involves data)*

- **Schedule**: The existing, already validated schedule part of a job configuration — frequency (daily, weekly, monthly), local time of day (HH:MM), weekday (weekly only), day of month 1–31 (monthly only) and IANA time zone name. This feature reads it; it does not change its shape or validation.
- **Reference instant ("after")**: The absolute moment after which the next run is sought — typically "now" or the moment a run was due.
- **Next run instant**: The absolute moment, in UTC, at which the job should next run; stored on the job record by the job service and later read by the scheduler.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: For every supported frequency, 100% of the documented example cases (including the day-31, leap-year and both DST-transition examples above) produce the expected instant.
- **SC-002**: Over at least several hundred generated combinations of schedule, time zone and reference instant, every result is in UTC, strictly after its reference instant, monotonic, and iterating produces a strictly increasing sequence — with zero counterexamples.
- **SC-003**: A job configured for a given local time runs at that local wall-clock time on 100% of days throughout a full calendar year, except on a DST spring-forward day where that time does not exist, where it runs exactly once at the shifted valid time.
- **SC-004**: After downtime of any length, an overdue job produces exactly one run before returning to its regular cadence.
- **SC-005**: Newly created and re-enabled jobs are no longer immediately due unless their configured time is actually the next upcoming occurrence.
- **SC-006**: Each next-run calculation does a fixed, small amount of work: it examines at most a dozen candidate dates and never searches open-endedly. Recalculating for hundreds of jobs per scheduler tick therefore has no noticeable cost. This is verified by design review, not by timing tests, which would be non-deterministic.

## Assumptions

- The schedule configuration from #3 already guarantees valid input (known IANA zone name, HH:MM time, weekday present only for weekly, day of month 1–31 present only for monthly); this feature does not re-validate it.
- "Shift to the next valid time" for a nonexistent local time means shifting forward by the size of the gap (02:30 → 03:30 for a one-hour jump; confirmed in Clarifications). This applies to gaps of any length (e.g. 30-minute transitions), and the run stays on the same day as the acceptance criterion requires.
- Although the issue names the module path `scout/scheduling/next_run.py`, the project's package is `invio`, so the calculation lives in the project's scheduling area (`invio.scheduling`) and keeps the issue's signature: schedule plus reference instant in, UTC instant out. It must plug into the job service's existing next-run hook without changing that hook's shape.
- The time zone database available on the host (with the project's fallback data package where needed) is the source of truth for DST rules.
- The calculation is not responsible for deciding *when* to call it (e.g. which instant the scheduler passes after a run); the scheduler (#23) passes "now" (or the run's due time), and missed-run behaviour follows from that.
- Dates beyond the supported calendar range (year 9999) are out of scope.
- Clearing the next run time on disable and the scheduler loop itself are out of scope; they are covered by #5 and #23.
