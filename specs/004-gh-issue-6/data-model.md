# Data Model: Schedule Calculation (Next Run Time)

**Feature**: [spec.md](spec.md) | **Date**: 2026-10-04

No new persistent entities and no schema migration. The feature reads one existing model and
fills one existing column.

## ScheduleConfig (existing — `invio.config.job`, read-only here)

| Field | Type | Rules (already enforced by #3) | Used for |
|---|---|---|---|
| `frequency` | `Frequency` (`daily` / `weekly` / `monthly`) | required | Selects the candidate rule (research R2) |
| `time` | `str` `HH:MM` | `00:00`–`23:59` | Local wall-clock time of each occurrence |
| `weekday` | `Weekday \| None` | required for weekly only, otherwise absent | Weekly candidate dates |
| `day_of_month` | `int \| None` | 1–31, required for monthly only, otherwise absent | Monthly candidate dates, clamped to the month length (R3) |
| `timezone` | `str` | exact IANA name present in `zoneinfo.available_timezones()` | Local calendar and DST rules (R1) |

`compute_next_run` performs no validation of its own and relies on these guarantees.

## Reference instant `after` (input)

- `datetime`, **must be timezone-aware**. A naive value raises `ValueError` (R4).
- Any offset is allowed. It is compared as an absolute instant.
- Callers:
  - `JobService` passes `clock()` (aware UTC) on create, update of an enabled job, and
    re-enable.
  - The scheduler (#23) will pass "now" after a run.

## Next run instant (output → `jobs.next_run_at`)

- `datetime` with `tzinfo is UTC`, strictly greater than `after`.
- Persisted by `JobService` in the existing `jobs.next_run_at` column (`UTCDateTime`, stored as
  naive UTC and returned as aware UTC). The column and its lifecycle are unchanged:

| Job event | `next_run_at` |
|---|---|
| create (enabled) | `compute_next_run(schedule, now)` |
| update while enabled | recalculated with `now` |
| update while disabled | stays `None` |
| disable | `None` |
| re-enable | `compute_next_run(schedule, now)` |
| after a run (#23, out of scope) | `compute_next_run(schedule, now)`. Missed occurrences are not replayed (FR-014). |

## Derived values (internal, not stored)

- **Local candidate date**: a calendar date in the schedule's time zone that matches the
  frequency rule.
- **Resolved occurrence**: the candidate date plus `time`, localised with `fold=0` and converted
  to UTC. In a DST gap it shifts forward by the gap length. In a DST overlap it is the first
  occurrence.
