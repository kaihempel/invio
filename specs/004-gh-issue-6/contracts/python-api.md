# Contract: `invio.scheduling.next_run`

**Feature**: [../spec.md](../spec.md) | **Date**: 2026-10-04

## `compute_next_run`

```python
from datetime import datetime
from invio.config.job import ScheduleConfig


def compute_next_run(schedule: ScheduleConfig, after: datetime) -> datetime: ...
```

Matches `invio.services.jobs.NextRun` (`Callable[[ScheduleConfig, datetime], datetime]`).

### Preconditions

- `schedule` is a validated `ScheduleConfig`.
- `after` is timezone-aware. Any offset is allowed.

### Postconditions

| Id | Guarantee | Spec |
|---|---|---|
| P1 | `result.tzinfo is datetime.UTC` | FR-004 |
| P2 | `result > after` | FR-005 |
| P3 | `result` is the **earliest** resolved occurrence of the schedule that is `> after` | FR-007–FR-009 |
| P4 | `a <= b` ⇒ `compute_next_run(s, a) <= compute_next_run(s, b)` | FR-012 |
| P5 | Iterating `x = compute_next_run(s, x)` yields a strictly increasing sequence with one occurrence per period | FR-013 |
| P6 | Pure: same inputs → same output. No clock, I/O or logging. | FR-002 |

### Resolution rules

- Occurrence dates are evaluated in `ZoneInfo(schedule.timezone)`.
- Weekly runs use `schedule.weekday`. Monthly runs use
  `min(day_of_month, days_in_month)`, evaluated for each month separately.
- When the local time does not exist because clocks jump forward, the run moves forward by
  the length of the jump. Example: Europe/Berlin, 2026-03-29, 02:30 → 03:30 CEST = 01:30 UTC.
- When the local time occurs twice because clocks fall back, the first occurrence is used.
  Example: Europe/Berlin, 2026-10-25, 02:30 → 02:30 CEST = 00:30 UTC. The second 02:30 is
  never returned.

### Errors

| Condition | Exception |
|---|---|
| `after.tzinfo is None` or `after.utcoffset() is None` | `ValueError("after must be timezone-aware")` |

### Reference examples (normative; mirrored in the tests)

| Schedule | `after` (UTC) | Result (UTC) |
|---|---|---|
| daily 08:00 Europe/Berlin | 2026-01-10 06:00 | 2026-01-10 07:00 |
| daily 08:00 Europe/Berlin | 2026-01-10 07:00 | 2026-01-11 07:00 |
| monthly day 31 08:00 UTC | 2026-01-31 09:00 | 2026-02-28 08:00 |
| monthly day 31 08:00 UTC | 2028-01-31 09:00 | 2028-02-29 08:00 |
| monthly day 31 08:00 UTC | 2026-02-28 09:00 | 2026-03-31 08:00 |
| monthly day 31 08:00 UTC | 2026-04-02 00:00 | 2026-04-30 08:00 |
| daily 02:30 Europe/Berlin | 2026-03-28 12:00 | 2026-03-29 01:30 |
| daily 02:30 Europe/Berlin | 2026-10-24 12:00 | 2026-10-25 00:30 |
| daily 02:30 Europe/Berlin | 2026-10-25 00:30 | 2026-10-26 01:30 |

## `invio.services.jobs.JobService` (changed default)

- The `next_run` keyword now defaults to `compute_next_run`. Its signature is unchanged.
- `_next_run_stub` and its `TODO(#6)` are removed.
- Behaviour is otherwise unchanged:
  - `next_run_at = next_run(cfg.schedule, clock())` on create, update while enabled, and
    re-enable.
  - `None` when the job is disabled.
