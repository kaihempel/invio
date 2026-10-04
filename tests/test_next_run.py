"""Tests for ``compute_next_run`` (schedule calculation: local time, clamping, DST, no catch-up)."""

import subprocess
import sys
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from invio.config.job import ScheduleConfig
from invio.scheduling.next_run import compute_next_run

BERLIN = ZoneInfo("Europe/Berlin")


def _schedule(**overrides: Any) -> ScheduleConfig:
    data: dict[str, Any] = {"frequency": "daily", "time": "08:00", "timezone": "Europe/Berlin"}
    return ScheduleConfig(**{**data, **overrides})


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- input contract -----------------------------------------------------------------------


def test_naive_after_is_rejected() -> None:
    with pytest.raises(ValueError, match="after must be timezone-aware"):
        compute_next_run(_schedule(), datetime(2026, 1, 10, 6, 0))


def test_non_utc_offset_is_treated_as_the_same_instant() -> None:
    after = datetime(2026, 1, 10, 7, 0, tzinfo=timezone(timedelta(hours=1)))
    result = compute_next_run(_schedule(), after)
    assert result == compute_next_run(_schedule(), _utc(2026, 1, 10, 6, 0))
    assert result == _utc(2026, 1, 10, 7, 0)
    assert result.tzinfo is UTC


# --- US1: daily, weekly, monthly ----------------------------------------------------------

BERLIN_8 = _schedule()
NY_MONDAY = _schedule(
    frequency="weekly", weekday="monday", time="09:30", timezone="America/New_York"
)
TOKYO_15 = _schedule(frequency="monthly", day_of_month=15, time="06:00", timezone="Asia/Tokyo")


@pytest.mark.parametrize(
    ("schedule", "after", "expected"),
    [
        pytest.param(BERLIN_8, _utc(2026, 1, 10, 6), _utc(2026, 1, 10, 7), id="daily-today"),
        pytest.param(BERLIN_8, _utc(2026, 1, 10, 7), _utc(2026, 1, 11, 7), id="daily-exact-match"),
        pytest.param(
            BERLIN_8,
            datetime(2026, 1, 10, 6, 59, 59, 999999, tzinfo=UTC),
            _utc(2026, 1, 10, 7),
            id="daily-one-microsecond-before",
        ),
        pytest.param(NY_MONDAY, _utc(2026, 1, 14, 12), _utc(2026, 1, 19, 14, 30), id="weekly-next"),
        pytest.param(
            NY_MONDAY, _utc(2026, 1, 19, 14), _utc(2026, 1, 19, 14, 30), id="weekly-same-day"
        ),
        pytest.param(
            NY_MONDAY, _utc(2026, 1, 19, 15), _utc(2026, 1, 26, 14, 30), id="weekly-after-run"
        ),
        pytest.param(TOKYO_15, _utc(2026, 1, 15), _utc(2026, 2, 14, 21), id="monthly-next-month"),
        pytest.param(TOKYO_15, _utc(2026, 12, 20), _utc(2027, 1, 14, 21), id="monthly-year-end"),
        pytest.param(
            _schedule(time="07:00", timezone="Pacific/Auckland"),
            _utc(2026, 1, 10, 12),
            _utc(2026, 1, 10, 18),
            id="daily-local-date-differs-from-utc",
        ),
        pytest.param(
            _schedule(timezone="Asia/Kathmandu"),
            _utc(2026, 1, 10),
            _utc(2026, 1, 10, 2, 15),
            id="daily-offset-with-minutes",
        ),
        pytest.param(
            _schedule(timezone="Asia/Kolkata"),
            _utc(2026, 1, 10),
            _utc(2026, 1, 10, 2, 30),
            id="daily-offset-half-hour",
        ),
        pytest.param(
            _schedule(timezone="UTC"),
            _utc(2028, 2, 28, 9),
            _utc(2028, 2, 29, 8),
            id="daily-leap-day",
        ),
        pytest.param(
            _schedule(frequency="weekly", weekday="friday", timezone="UTC"),
            _utc(2026, 12, 28),
            _utc(2027, 1, 1, 8),
            id="weekly-year-end",
        ),
        pytest.param(
            _schedule(
                frequency="weekly", weekday="monday", time="20:00", timezone="America/Los_Angeles"
            ),
            _utc(2026, 1, 20, 3),  # Tuesday in UTC, still Monday 19:00 in Los Angeles
            _utc(2026, 1, 20, 4),
            id="weekly-local-weekday-differs-from-utc",
        ),
        pytest.param(
            _schedule(
                frequency="monthly", day_of_month=31, time="20:00", timezone="America/Los_Angeles"
            ),
            _utc(2026, 1, 31, 12),
            _utc(2026, 2, 1, 4),  # 31 January 20:00 local is already February in UTC
            id="monthly-local-day-differs-from-utc",
        ),
    ],
)
def test_next_run_examples(schedule: ScheduleConfig, after: datetime, expected: datetime) -> None:
    result = compute_next_run(schedule, after)
    assert result == expected
    assert result.tzinfo is UTC


# --- US2: monthly day clamping ------------------------------------------------------------


def _monthly(day: int, timezone: str = "UTC") -> ScheduleConfig:
    return _schedule(frequency="monthly", day_of_month=day, timezone=timezone)


@pytest.mark.parametrize(
    ("schedule", "after", "expected"),
    [
        pytest.param(_monthly(31), _utc(2026, 1, 31, 9), _utc(2026, 2, 28, 8), id="31-to-feb"),
        pytest.param(_monthly(31), _utc(2028, 1, 31, 9), _utc(2028, 2, 29, 8), id="31-to-leap-feb"),
        pytest.param(_monthly(31), _utc(2026, 2, 28, 9), _utc(2026, 3, 31, 8), id="no-carry-over"),
        pytest.param(_monthly(31), _utc(2026, 4, 2), _utc(2026, 4, 30, 8), id="31-in-april"),
        pytest.param(_monthly(31), _utc(2026, 3, 31, 9), _utc(2026, 4, 30, 8), id="31-to-april"),
        pytest.param(_monthly(30), _utc(2026, 2, 1), _utc(2026, 2, 28, 8), id="30-in-feb"),
        pytest.param(_monthly(29), _utc(2027, 2, 1), _utc(2027, 2, 28, 8), id="29-in-feb"),
        pytest.param(
            _monthly(31, "Europe/Berlin"),
            _utc(2026, 11, 30, 12),
            _utc(2026, 12, 31, 7),
            id="31-berlin",
        ),
    ],
)
def test_monthly_day_clamped_to_month_length(
    schedule: ScheduleConfig, after: datetime, expected: datetime
) -> None:
    result = compute_next_run(schedule, after)
    assert result == expected
    assert result.tzinfo is UTC


def test_monthly_day_31_over_a_year() -> None:
    schedule = _monthly(31)
    days: list[int] = []
    current = _utc(2026, 1, 1)
    for _ in range(12):
        current = compute_next_run(schedule, current)
        days.append(current.day)
    assert days == [31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]


# --- US3: DST transitions -----------------------------------------------------------------

BERLIN_0230 = _schedule(time="02:30")


@pytest.mark.parametrize(
    ("schedule", "after", "expected"),
    [
        pytest.param(BERLIN_0230, _utc(2026, 3, 28, 12), _utc(2026, 3, 29, 1, 30), id="berlin-gap"),
        pytest.param(
            BERLIN_0230, _utc(2026, 10, 24, 12), _utc(2026, 10, 25, 0, 30), id="berlin-overlap"
        ),
        pytest.param(
            BERLIN_0230,
            _utc(2026, 10, 25, 0, 30),
            _utc(2026, 10, 26, 1, 30),
            id="berlin-overlap-exact-first",
        ),
        pytest.param(
            BERLIN_0230,
            _utc(2026, 10, 25, 1),
            _utc(2026, 10, 26, 1, 30),
            id="berlin-overlap-between",
        ),
        pytest.param(
            BERLIN_8, _utc(2026, 3, 28, 8), _utc(2026, 3, 29, 6), id="berlin-8am-across-gap"
        ),
        pytest.param(
            _schedule(time="02:30", timezone="America/New_York"),
            _utc(2026, 3, 7, 12),
            _utc(2026, 3, 8, 7, 30),
            id="new-york-gap",
        ),
        pytest.param(
            _schedule(time="02:15", timezone="Australia/Lord_Howe"),
            _utc(2026, 10, 3),
            _utc(2026, 10, 3, 15, 45),
            id="lord-howe-30-minute-gap",
        ),
        pytest.param(
            _schedule(frequency="weekly", weekday="sunday", time="02:30"),
            _utc(2026, 3, 27),
            _utc(2026, 3, 29, 1, 30),
            id="weekly-gap",
        ),
        pytest.param(
            _schedule(frequency="monthly", day_of_month=29, time="02:30"),
            _utc(2026, 3, 1),
            _utc(2026, 3, 29, 1, 30),
            id="monthly-gap",
        ),
        pytest.param(
            _schedule(frequency="weekly", weekday="sunday", time="02:30"),
            _utc(2026, 10, 23),
            _utc(2026, 10, 25, 0, 30),
            id="weekly-overlap",
        ),
        pytest.param(
            _schedule(frequency="monthly", day_of_month=25, time="02:30"),
            _utc(2026, 10, 1),
            _utc(2026, 10, 25, 0, 30),
            id="monthly-overlap",
        ),
    ],
)
def test_dst_transitions(schedule: ScheduleConfig, after: datetime, expected: datetime) -> None:
    result = compute_next_run(schedule, after)
    assert result == expected
    assert result.tzinfo is UTC


def test_gap_run_happens_on_the_same_local_date() -> None:
    result = compute_next_run(BERLIN_0230, _utc(2026, 3, 28, 12))
    assert result.astimezone(BERLIN).date() == date(2026, 3, 29)


def test_daily_full_year_keeps_local_time() -> None:
    current = _utc(2025, 12, 31, 12)
    results: list[datetime] = []
    for _ in range(365):
        current = compute_next_run(BERLIN_0230, current)
        results.append(current)
    local = [r.astimezone(BERLIN) for r in results]

    first = date(2026, 1, 1)
    assert [x.date() for x in local] == [first + timedelta(days=i) for i in range(365)]
    off_time = [x.date() for x in local if (x.hour, x.minute) != (2, 30)]
    assert off_time == [date(2026, 3, 29)]
    assert local[date(2026, 3, 29).toordinal() - first.toordinal()].hour == 3
    assert results[date(2026, 10, 25).toordinal() - first.toordinal()] == _utc(2026, 10, 25, 0, 30)


# --- US4: missed runs are not replayed ----------------------------------------------------


@pytest.mark.parametrize(
    ("schedule", "last_due", "now", "expected"),
    [
        pytest.param(
            BERLIN_8,
            _utc(2026, 1, 5, 7),
            _utc(2026, 1, 10, 9),
            _utc(2026, 1, 11, 7),
            id="daily-overdue",
        ),
        pytest.param(
            BERLIN_8,
            _utc(2026, 1, 5, 7),
            _utc(2026, 1, 10, 6),
            _utc(2026, 1, 10, 7),
            id="daily-due-today",
        ),
        pytest.param(
            _schedule(frequency="weekly", weekday="monday", timezone="UTC"),
            _utc(2026, 1, 5, 8),
            _utc(2026, 1, 28, 12),  # Wednesday, three weeks overdue
            _utc(2026, 2, 2, 8),
            id="weekly-overdue",
        ),
        pytest.param(
            _schedule(frequency="monthly", day_of_month=1, timezone="UTC"),
            _utc(2026, 1, 1, 8),
            _utc(2026, 3, 10, 12),
            _utc(2026, 4, 1, 8),
            id="monthly-overdue",
        ),
    ],
)
def test_missed_runs_are_not_replayed(
    schedule: ScheduleConfig, last_due: datetime, now: datetime, expected: datetime
) -> None:
    result = compute_next_run(schedule, now)
    assert result == expected
    assert result.tzinfo is UTC
    assert compute_next_run(schedule, last_due) < result


def test_after_downtime_one_run_then_regular_cadence() -> None:
    now = _utc(2026, 1, 10, 9)  # five daily runs missed since 2026-01-05 07:00 UTC
    runs = [compute_next_run(BERLIN_8, now)]
    for _ in range(3):
        runs.append(compute_next_run(BERLIN_8, runs[-1]))
    assert runs == [_utc(2026, 1, day, 7) for day in range(11, 15)]


# --- layering -----------------------------------------------------------------------------


def test_next_run_module_does_not_import_services() -> None:
    code = (
        "import sys, invio.scheduling.next_run; "
        "assert 'invio.services' not in sys.modules and 'invio.db' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
