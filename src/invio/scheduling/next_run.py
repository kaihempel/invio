"""Schedule calculation: the next UTC run time of a job.

``compute_next_run`` returns the earliest occurrence of a schedule that is strictly after a given
instant. It is pure (no clock, I/O or logging). The rules:

- Occurrences are evaluated at the configured local time in the job's time zone; the result is
  always a UTC datetime.
- Monthly jobs on day 29-31 fall back to the last day of shorter months, month by month.
- A local time skipped by a DST jump runs shifted forward by the length of the jump (into the
  next local day only if the gap ends at midnight); a local time that occurs twice runs at its
  first occurrence.
- Missed runs are not replayed: only ``after`` matters, never the last due time.
"""

import calendar
from datetime import UTC, date, datetime, time, timedelta
from typing import assert_never
from zoneinfo import ZoneInfo

from invio.config.job import Frequency, ScheduleConfig, Weekday

__all__ = ["compute_next_run"]


def _resolve(day: date, at: time, zone: ZoneInfo) -> datetime:
    """Return the UTC instant of local ``day`` at ``at``.

    ``fold=0`` (PEP 495) makes both DST edge cases come out right without special code: a
    nonexistent time is read with the offset from before the jump, which shifts it forward by
    the gap length (Europe/Berlin 2026-03-29 02:30 -> 03:30 CEST = 01:30 UTC), and a repeated
    time resolves to its first occurrence (2026-10-25 02:30 -> 02:30 CEST = 00:30 UTC).
    """
    return datetime.combine(day, at, tzinfo=zone).astimezone(UTC)


def _daily_candidates(local_day: date) -> list[date]:
    """Days -1..+2 around ``local_day``; day -1 catches a gap shift past midnight."""
    return [local_day + timedelta(days=offset) for offset in range(-1, 3)]


def _weekly_candidates(local_day: date, weekday: Weekday) -> list[date]:
    """Matching weekdays in days -1..+8 around ``local_day`` (one or two of them)."""
    days = (local_day + timedelta(days=offset) for offset in range(-1, 9))
    return [day for day in days if day.weekday() == weekday.number]


def _monthly_candidates(local_day: date, day_of_month: int) -> list[date]:
    """The run day in the month of ``local_day - 1`` and the next two, clamped per month."""
    start = local_day - timedelta(days=1)
    candidates: list[date] = []
    for step in range(3):
        index = start.year * 12 + start.month - 1 + step
        year, month = divmod(index, 12)
        month += 1
        last_day = calendar.monthrange(year, month)[1]
        candidates.append(date(year, month, min(day_of_month, last_day)))
    return candidates


def _candidate_days(schedule: ScheduleConfig, local_day: date) -> list[date]:
    # The windows are fixed and wide enough that a gap shift past midnight is still found and at
    # least one occurrence lies after ``after``.
    match schedule.frequency:
        case Frequency.DAILY:
            return _daily_candidates(local_day)
        case Frequency.WEEKLY:
            if schedule.weekday is None:
                raise ValueError("weekly schedule without weekday")
            return _weekly_candidates(local_day, schedule.weekday)
        case Frequency.MONTHLY:
            if schedule.day_of_month is None:
                raise ValueError("monthly schedule without day_of_month")
            return _monthly_candidates(local_day, schedule.day_of_month)
        case _:
            assert_never(schedule.frequency)


def compute_next_run(schedule: ScheduleConfig, after: datetime) -> datetime:
    """Return the earliest occurrence of ``schedule`` strictly after ``after``, in UTC.

    ``schedule`` must be validated (``ScheduleConfig.model_validate``); ``after`` must be
    timezone-aware (any offset). A naive ``after`` or a schedule missing its ``weekday`` /
    ``day_of_month`` raises ``ValueError``.
    """
    if after.utcoffset() is None:
        raise ValueError("after must be timezone-aware")
    zone = ZoneInfo(schedule.timezone)
    at = schedule.local_time
    local_day = after.astimezone(zone).date()
    occurrences = (_resolve(day, at, zone) for day in _candidate_days(schedule, local_day))
    return min(occurrence for occurrence in occurrences if occurrence > after)
