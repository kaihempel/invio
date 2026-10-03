"""Tests for ScheduleConfig validation."""

from typing import Any

import pytest
from pydantic import ValidationError

from invio.config.job import ScheduleConfig, Weekday

BASE_DAILY: dict[str, Any] = {"frequency": "daily", "time": "07:30", "timezone": "Europe/Berlin"}
BASE_WEEKLY = {**BASE_DAILY, "frequency": "weekly", "weekday": "monday"}
BASE_MONTHLY = {**BASE_DAILY, "frequency": "monthly", "day_of_month": 15}


def _without(data: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in data.items() if k != key}


@pytest.mark.parametrize(
    ("data", "loc", "message"),
    [
        (_without(BASE_WEEKLY, "weekday"), (), "weekday is required when frequency is 'weekly'"),
        (
            _without(BASE_MONTHLY, "day_of_month"),
            (),
            "day_of_month is required when frequency is 'monthly'",
        ),
        (
            {**BASE_DAILY, "weekday": "monday"},
            (),
            "weekday is only allowed when frequency is 'weekly'",
        ),
        (
            {**BASE_WEEKLY, "day_of_month": 3},
            (),
            "day_of_month is only allowed when frequency is 'monthly'",
        ),
        (
            {**BASE_MONTHLY, "weekday": "monday"},
            (),
            "weekday is only allowed when frequency is 'weekly'",
        ),
        ({**BASE_MONTHLY, "day_of_month": 0}, ("day_of_month",), "greater than or equal to 1"),
        ({**BASE_MONTHLY, "day_of_month": 32}, ("day_of_month",), "less than or equal to 31"),
        ({**BASE_DAILY, "time": "25:00"}, ("time",), "String should match pattern"),
        ({**BASE_DAILY, "time": "7:5"}, ("time",), "String should match pattern"),
        ({**BASE_DAILY, "time": "24:00"}, ("time",), "String should match pattern"),
        ({**BASE_DAILY, "time": "12:60"}, ("time",), "String should match pattern"),
        ({**BASE_DAILY, "timezone": "Europe/Atlantis"}, ("timezone",), "unknown timezone"),
        ({**BASE_WEEKLY, "weekday": "Funday"}, ("weekday",), "Input should be"),
        ({**BASE_WEEKLY, "weekday": "mon"}, ("weekday",), "Input should be"),
        ({**BASE_WEEKLY, "weekday": 1}, ("weekday",), "Input should be"),
        ({**BASE_MONTHLY, "day_of_month": True}, ("day_of_month",), "valid integer"),
        ({**BASE_MONTHLY, "day_of_month": 15.0}, ("day_of_month",), "valid integer"),
    ],
)
def test_schedule_rejected(data: dict[str, Any], loc: tuple[str, ...], message: str) -> None:
    with pytest.raises(ValidationError) as info:
        ScheduleConfig.model_validate(data)

    errors = info.value.errors()
    assert [e["loc"] for e in errors] == [loc]
    assert message in errors[0]["msg"]


@pytest.mark.parametrize("raw", ["Monday", "MONDAY", "monday"])
def test_weekday_case_insensitive(raw: str) -> None:
    schedule = ScheduleConfig.model_validate({**BASE_WEEKLY, "weekday": raw})

    assert schedule.weekday is Weekday.MONDAY


def test_monthly_day_31_accepted() -> None:
    assert ScheduleConfig.model_validate({**BASE_MONTHLY, "day_of_month": 31}).day_of_month == 31


@pytest.mark.parametrize(
    "raw",
    [
        "0\u0663:30",  # ARABIC-INDIC DIGIT THREE
        "07:3\u0660",  # ARABIC-INDIC DIGIT ZERO
        "1\u0967:00",  # DEVANAGARI DIGIT ONE
        "\uff10\uff17:30",  # fullwidth digits
        "07.30",
        "0730",
        "07:30:00",
        "",
    ],
)
def test_time_must_be_ascii_hh_mm(raw: str) -> None:
    with pytest.raises(ValidationError) as info:
        ScheduleConfig.model_validate({**BASE_DAILY, "time": raw})

    assert [e["loc"] for e in info.value.errors()] == [("time",)]


@pytest.mark.parametrize("raw", ["00:00", "09:59", "19:09", "20:00", "23:59", " 07:30 "])
def test_time_bounds_accepted(raw: str) -> None:
    assert ScheduleConfig.model_validate({**BASE_DAILY, "time": raw}).time == raw.strip()


@pytest.mark.parametrize("weekday", list(Weekday))
def test_every_weekday_accepted_in_any_case(weekday: Weekday) -> None:
    for raw in (weekday.value, weekday.value.upper(), weekday.value.title()):
        assert ScheduleConfig.model_validate({**BASE_WEEKLY, "weekday": raw}).weekday is weekday


@pytest.mark.parametrize("tz", ["", "Europe", "../etc/passwd", "/UTC", "Europe/Berlin/x"])
def test_bad_timezone_names_rejected(tz: str) -> None:
    with pytest.raises(ValidationError) as info:
        ScheduleConfig.model_validate({**BASE_DAILY, "timezone": tz})

    assert [e["loc"] for e in info.value.errors()] == [("timezone",)]


@pytest.mark.parametrize("tz", ["europe/berlin", "EUROPE/BERLIN", "utc"])
def test_timezone_case_must_match_iana_name(tz: str) -> None:
    with pytest.raises(ValidationError, match="unknown timezone"):
        ScheduleConfig.model_validate({**BASE_DAILY, "timezone": tz})
