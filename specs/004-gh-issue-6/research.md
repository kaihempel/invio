# Research: Schedule Calculation (Next Run Time)

**Feature**: [spec.md](spec.md) | **Plan**: [plan.md](plan.md) | **Date**: 2026-10-04

The Technical Context has no open `NEEDS CLARIFICATION` items. The decisions below settle the
choices that the spec leaves to planning.

## R1 — Resolving local wall-clock times (DST gap and overlap)

- **Decision**: Build the local occurrence as `datetime(y, m, d, hh, mm, tzinfo=ZoneInfo(tz))`
  with the default `fold=0`, then convert it with `.astimezone(UTC)`. No special-case code for
  DST.
- **Rationale**: PEP 495 semantics, as implemented by `zoneinfo`:
  - **Gap** (spring forward): with `fold=0` a nonexistent time is read with the offset that
    applied *before* the transition. 02:30 Europe/Berlin on 2026-03-29 → 02:30+01:00 =
    01:30 UTC = 03:30 CEST. This is the shift by the gap length that the spec requires (FR-010,
    Clarifications).
  - **Overlap** (fall back): `fold=0` selects the first occurrence. 02:30 on 2026-10-25 →
    02:30+02:00 = 00:30 UTC (FR-011).
  - These values were checked against the installed time zone database while the spec was
    written.
- **Alternatives considered**:
  - Clamp the time to the end of the gap (03:00). Rejected in Clarifications.
  - Hand-written detection of gaps and overlaps, round-tripping through UTC and comparing
    offsets. This duplicates what `fold=0` already guarantees and is easy to get wrong for
    gaps that are not one hour long (e.g. Australia/Lord_Howe, 30 minutes).
  - `pytz` or `dateutil`. Rejected: they add a runtime dependency for behaviour the standard
    library already provides.

## R2 — Selecting the next occurrence (strictly after, monotonic)

- **Decision**: Define `next(after) = min{ o ∈ O(schedule) : o > after }`, where `O` is the set
  of resolved UTC occurrences. In practice:
  1. Reject a naive `after` with `ValueError`.
  2. Compute `local = after.astimezone(tz)` and take its local date `d`.
  3. Build a small candidate window of local dates around `d`:
     - daily: `d-1 … d+2`
     - weekly: matching weekdays in `d-1 … d+8`
     - monthly: the configured day, clamped to the month length, for the month of `d-1` and the
       two following months
  4. Resolve each candidate to UTC (R1), keep those `> after`, and return the **minimum**.
- **Rationale**:
  - Taking the minimum over a set makes FR-005 (strictly after), FR-012 (monotonic) and
    FR-013 (an iterated sequence is strictly increasing) hold by construction. No proof is
    needed per frequency.
  - Using the minimum rather than "the first date in calendar order" stays correct in odd zones,
    where a gap shift pushes an occurrence past midnight or a skipped calendar day makes two
    candidate dates resolve to the same instant. Example: Pacific/Apia skipped 2011-12-30.
  - The `d-1` lower bound covers an occurrence that a gap shift moved into the next local day.
    The upper bound always holds at least one occurrence later than `after`.
- **Alternatives considered**:
  - Advance with `after + timedelta(days=1)` in UTC. Rejected: it drifts by an hour across DST
    changes and breaks local wall-clock semantics.
  - `dateutil.rrule` or croniter. Rejected: extra runtime dependency, and their behaviour in DST
    gaps differs from the clarified rule.
  - Looping until a match with no bound. Rejected in favour of a fixed, small window, so the
    run time is constant and predictable (SC-006).

## R3 — Monthly day clamping

- **Decision**: `day = min(schedule.day_of_month, calendar.monthrange(year, month)[1])`. Clamping
  is evaluated for each month separately. Nothing carries over: after Feb 28 the next run is
  Mar 31.
- **Rationale**: This matches FR-009 and US2. `calendar.monthrange` handles leap years.
- **Alternatives considered**: Skip months that are too short (rejected by the spec), or roll
  over into the next month (rejected by the spec).

## R4 — Input contract for `after`

- **Decision**: An aware datetime with any offset is accepted and treated as the instant it
  denotes. A naive datetime raises
  `ValueError("after must be timezone-aware")`. The return value is always
  `tzinfo is datetime.UTC`.
- **Rationale**: FR-004 and FR-006, and constitution principle I (fail fast at boundaries).
  `JobService` already passes aware UTC values (`invio.db.types.utcnow`).
- **Alternatives considered**: Assume that a naive value is UTC. Rejected: guessing the time
  zone hides bugs.

## R5 — Module placement and layering

- **Decision**: Add `src/invio/scheduling/next_run.py` with `compute_next_run`. The module
  imports only the standard library (`calendar`, `datetime`, `zoneinfo`) and the
  `ScheduleConfig` / `Frequency` / `Weekday` types from `invio.config.job`.
  `invio/scheduling/__init__.py` stays import-free. In `invio.services.jobs`, the default
  `next_run` changes from `_next_run_stub` to `compute_next_run`, and the stub is deleted.
- **Rationale**:
  - The issue and the `TODO(#6)` in `services/jobs.py` both name this location, and so did the
    #5 plan (R3 there).
  - The function is a pure leaf with no dependency on orchestration code.
  - The only import from a "higher" package is `services → scheduling.next_run`. It is recorded
    in the plan's Complexity Tracking.
- **Alternatives considered**:
  - Put the function in `invio.config` or `invio.domain`. Rejected: it contradicts the issue,
    the README layout and the existing TODO, and `domain.py` must stay free of `config`
    imports.
  - Leave the stub as the default and wire `compute_next_run` only in `from_settings`. Rejected:
    it keeps a wrong default that tests and future callers could pick up by accident.

## R6 — Property-based testing

- **Decision**: Add `hypothesis` to the **dev** dependency group (`uv add --dev hypothesis`;
  `uv.lock` is updated) and write the property tests in `tests/test_next_run_properties.py`.
  - **Strategies**:
    - frequency
    - time `HH:MM`
    - weekday or day of month (1–31)
    - time zone, sampled from a fixed list with DST, unusual offsets and southern hemisphere
      zones: Europe/Berlin, America/New_York, America/Sao_Paulo, Australia/Lord_Howe,
      Asia/Kolkata, Asia/Kathmandu, Pacific/Apia, Pacific/Chatham, UTC
    - `after`: aware UTC datetimes from 1990-01-01 to 2100-01-01
  - **Properties**:
    - the result is UTC and greater than `after`
    - `a ≤ b ⇒ next(a) ≤ next(b)`
    - iterating 24 times gives a strictly increasing sequence
    - local weekday or day matches the schedule, allowing for a gap shift
  - **Settings**: a fixed `derandomize=True` profile so CI is deterministic (constitution III),
    with enough `max_examples` to satisfy SC-002.
- **Rationale**: This is required by the issue's acceptance criteria. Hypothesis is test-only,
  so no runtime dependency is added. Deterministic mode keeps tests from flaking.
- **Alternatives considered**: Hand-rolled random loops. Rejected: no shrinking, and not the
  tool the issue asks for.

## R7 — Time zone data

- **Decision**: Add no `tzdata` package. Rely on the system IANA database, which
  `ScheduleConfig` already depends on through `zoneinfo.available_timezones()`.
- **Rationale**: Supported targets are Linux and macOS, which both ship tzdata. Adding the
  package would be an unjustified runtime dependency (constitution: Technology constraints).
- **Alternatives considered**: Pin `tzdata` so results do not depend on the host. Deferred:
  revisit if Windows or slim containers without tzdata become a target.

## R8 — Documentation

- **Decision**: Update the README job service section. Replace "`next_run_at` is a placeholder
  (the current time) until scheduling (#6) lands" with a short description of the scheduling
  rules: local time, strictly future, the day-31 clamp, the DST gap and overlap rules, and no
  catch-up of missed runs.
- **Rationale**: Constitution, Development Workflow. User-facing behaviour changes update the
  README in the same PR.
