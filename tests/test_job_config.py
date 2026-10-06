"""Tests for the JobConfig root model: happy path, defaults, immutability, unknown keys."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from invio.config.job import JobConfig, LimitsConfig
from tests.job_helpers import set_path


def test_valid_job_loads(job_data: dict[str, Any]) -> None:
    job = JobConfig.model_validate(job_data)

    assert job.schedule.time == "07:30"
    assert job.notification.to == ["research@example.com"]
    assert len(job.sources) == 1
    assert job.llm.models.smart == "gpt-large"


def test_defaults_applied(job_data: dict[str, Any]) -> None:
    job = JobConfig.model_validate(job_data)

    assert job.schema_version == 1
    assert job.notification.send_if_empty is False
    assert job.search.min_relevance == 0.6
    assert job.search.keywords.any == []
    assert job.search.keywords.all == []
    assert job.search.keywords.exclude == []
    assert job.llm.fallback_provider is None
    assert job.limits == LimitsConfig()
    assert job.limits.max_items_per_source == 20
    assert job.limits.max_items_per_run == 100
    assert job.limits.max_items_in_notification == 20
    assert job.limits.max_llm_tokens_per_run == 200000


def test_models_are_frozen(job_data: dict[str, Any]) -> None:
    job = JobConfig.model_validate(job_data)

    with pytest.raises(ValidationError):
        job.schema_version = 2  # type: ignore[misc]
    with pytest.raises(ValidationError):
        job.schedule.time = "08:00"  # type: ignore[misc]


def test_equal_by_value(job_data: dict[str, Any]) -> None:
    assert JobConfig.model_validate(job_data) == JobConfig.model_validate(copy.deepcopy(job_data))


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("foo", 1),
        ("schedule.frequncy", "daily"),
        ("notification.cc", ["a@example.com"]),
        ("search.keywords.anny", ["x"]),
        ("search.semantic_descripton", "typo"),
        ("llm.fallback", "anthropic"),
        ("llm.models.slow", "x"),
        ("limits.max_items", 3),
    ],
)
def test_unknown_keys_rejected(job_data: dict[str, Any], path: str, value: Any) -> None:
    set_path(job_data, path, value)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        JobConfig.model_validate(job_data)


# --- language ------------------------------------------------------------------------------


def test_language_defaults_to_english(job_data: dict[str, Any]) -> None:
    assert JobConfig.model_validate(job_data).language == "en"


def test_language_accepts_iso_code(job_data: dict[str, Any]) -> None:
    job_data["language"] = "de"
    assert JobConfig.model_validate(job_data).language == "de"


@pytest.mark.parametrize("value", ["xx", "DE", "deu", "german", ""])
def test_language_rejects_invalid(job_data: dict[str, Any], value: str) -> None:
    job_data["language"] = value
    with pytest.raises(ValidationError, match="language"):
        JobConfig.model_validate(job_data)


def test_language_unknown_code_message(job_data: dict[str, Any]) -> None:
    job_data["language"] = "xx"
    with pytest.raises(ValidationError, match="unknown ISO 639-1 language code 'xx'"):
        JobConfig.model_validate(job_data)
