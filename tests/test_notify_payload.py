"""Tests for the typed notification payload stored on ``notifications.payload``."""

import json
from datetime import date
from typing import Any

import pytest
from pydantic import ValidationError

from invio.notify.payload import DigestStats, NotificationPayload


def _data(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "schema_version": 1,
        "job_name": "ai-news",
        "subject": "invio: ai-news – 2026-10-05",
        "digest_date": "2026-10-05",
        "is_empty": False,
        "stats": {"items_found": 7, "items_included": 2, "duration_seconds": 192.5},
    }
    data.update(overrides)
    return data


def test_round_trip_preserves_every_field() -> None:
    payload = NotificationPayload.model_validate(_data())

    dumped = payload.model_dump(mode="json")

    assert dumped == _data()
    assert NotificationPayload.model_validate(dumped) == payload
    assert payload.digest_date == date(2026, 10, 5)
    assert payload.stats == DigestStats(items_found=7, items_included=2, duration_seconds=192.5)


def test_digest_date_serializes_as_iso_string() -> None:
    dumped = NotificationPayload.model_validate(_data()).model_dump(mode="json")

    assert dumped["digest_date"] == "2026-10-05"
    json.dumps(dumped)  # storable in the JSON column


def test_optional_stats_may_be_none() -> None:
    stats = {"items_found": None, "items_included": 0, "duration_seconds": None}

    payload = NotificationPayload.model_validate(_data(stats=stats))

    assert payload.stats.items_found is None
    assert payload.stats.duration_seconds is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"unknown": 1},
        {"schema_version": 2},
        {"job_name": ""},
        {"subject": ""},
        {"stats": {"items_found": -1, "items_included": 0, "duration_seconds": None}},
        {"stats": {"items_found": None, "items_included": -1, "duration_seconds": None}},
        {"stats": {"items_found": None, "items_included": 0, "duration_seconds": -0.1}},
        {"stats": {"items_included": 0, "extra": 1}},
    ],
)
def test_invalid_payloads_are_rejected(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        NotificationPayload.model_validate(_data(**overrides))


def test_payload_is_frozen() -> None:
    payload = NotificationPayload.model_validate(_data())

    with pytest.raises(ValidationError):
        payload.job_name = "other"  # type: ignore[misc]
