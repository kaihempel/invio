"""Property tests for ``compute_next_run`` (Hypothesis, deterministic for CI)."""

import calendar
from datetime import UTC, date, datetime, time, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo

from hypothesis import example, given, settings
from hypothesis import strategies as st

from invio.config.job import ScheduleConfig
from invio.scheduling.next_run import compute_next_run

PROFILE = settings(derandomize=True, max_examples=500, deadline=None)

TIMEZONES = [
    "UTC",
    "Europe/Berlin",
    "America/New_York",
    "America/Sao_Paulo",
    "Australia/Lord_Howe",
    "Asia/Kolkata",
    "Asia/Kathmandu",
    "Pacific/Apia",
    "Pacific/Chatham",
]
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


@st.composite
def schedules(draw: st.DrawFn) -> ScheduleConfig:
    frequency = draw(st.sampled_from(["daily", "weekly", "monthly"]))
    data: dict[str, Any] = {
        "frequency": frequency,
        "time": f"{draw(st.integers(0, 23)):02d}:{draw(st.integers(0, 59)):02d}",
        "timezone": draw(st.sampled_from(TIMEZONES)),
    }
    if frequency == "weekly":
        data["weekday"] = draw(st.sampled_from(WEEKDAYS))
    if frequency == "monthly":
        data["day_of_month"] = draw(st.integers(1, 31))
    return ScheduleConfig(**data)


def instants() -> st.SearchStrategy[datetime]:
    return st.datetimes(
        min_value=datetime(1990, 1, 1),
        max_value=datetime(2100, 1, 1),
        timezones=st.just(UTC),
    )


def _matches_rule(schedule: ScheduleConfig, day: date) -> bool:
    if schedule.frequency == "weekly":
        return day.weekday() == WEEKDAYS.index(str(schedule.weekday))
    if schedule.frequency == "monthly":
        assert schedule.day_of_month is not None
        return day.day == min(schedule.day_of_month, calendar.monthrange(day.year, day.month)[1])
    return True


@PROFILE
@given(schedule=schedules(), after=instants())
def test_result_is_utc_and_strictly_after(schedule: ScheduleConfig, after: datetime) -> None:
    result = compute_next_run(schedule, after)
    assert result.tzinfo is UTC
    assert result > after


@PROFILE
@given(schedule=schedules(), a=instants(), b=instants())
def test_monotonic(schedule: ScheduleConfig, a: datetime, b: datetime) -> None:
    low, high = sorted((a, b))
    assert compute_next_run(schedule, low) <= compute_next_run(schedule, high)


@PROFILE
@given(schedule=schedules(), after=instants())
def test_iteration_is_strictly_increasing(schedule: ScheduleConfig, after: datetime) -> None:
    results = [after]
    for _ in range(24):
        results.append(compute_next_run(schedule, results[-1]))
    assert all(earlier < later for earlier, later in pairwise(results))


@PROFILE
@given(schedule=schedules(), after=instants())
def test_result_is_a_resolved_occurrence(schedule: ScheduleConfig, after: datetime) -> None:
    zone = ZoneInfo(schedule.timezone)
    hours, minutes = schedule.time.split(":")
    at = time(int(hours), int(minutes))
    result = compute_next_run(schedule, after)
    # A gap shift can move the run onto the day after its calendar date.
    local_date = result.astimezone(zone).date()
    candidates = [local_date, local_date - timedelta(days=1)]
    assert any(
        _matches_rule(schedule, day)
        and datetime.combine(day, at, tzinfo=zone).astimezone(UTC) == result
        for day in candidates
    )


@PROFILE
@given(schedule=schedules(), after=instants())
@example(
    schedule=ScheduleConfig(
        frequency="weekly", weekday="friday", time="08:00", timezone="Pacific/Apia"
    ),
    after=datetime(2011, 12, 25, tzinfo=UTC),
)
def test_no_occurrence_is_skipped_or_doubled(schedule: ScheduleConfig, after: datetime) -> None:
    first = compute_next_run(schedule, after)
    second = compute_next_run(schedule, first)
    gap = second - first
    # Upper bounds: one period plus at most one hour of fall-back. Lower bounds allow DST jumps;
    # weekly drops to 6 days because Pacific/Apia skipped 2011-12-30 when it crossed the date line.
    max_gap, min_gap = {
        "daily": (timedelta(hours=25), timedelta(hours=22)),
        "weekly": (timedelta(days=7, hours=1), timedelta(days=6)),
        "monthly": (timedelta(days=31, hours=1), timedelta(days=27, hours=22)),
    }[str(schedule.frequency)]
    assert min_gap <= gap <= max_gap
