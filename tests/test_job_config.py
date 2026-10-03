"""Tests for the JobConfig root model: happy path, defaults, immutability, unknown keys."""

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from invio.config.job import JobConfig, LimitsConfig


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


def _add_root(d: dict[str, Any]) -> None:
    d["foo"] = 1


def _add_schedule(d: dict[str, Any]) -> None:
    d["schedule"]["frequncy"] = "daily"


def _add_notification(d: dict[str, Any]) -> None:
    d["notification"]["cc"] = ["a@example.com"]


def _add_keywords(d: dict[str, Any]) -> None:
    d["search"]["keywords"] = {"anny": ["x"]}


def _add_search(d: dict[str, Any]) -> None:
    d["search"]["semantic_descripton"] = "typo"


def _add_llm(d: dict[str, Any]) -> None:
    d["llm"]["fallback"] = "anthropic"


def _add_models(d: dict[str, Any]) -> None:
    d["llm"]["models"]["slow"] = "x"


def _add_limits(d: dict[str, Any]) -> None:
    d["limits"] = {"max_items": 3}


@pytest.mark.parametrize(
    "mutate",
    [
        _add_root,
        _add_schedule,
        _add_notification,
        _add_keywords,
        _add_search,
        _add_llm,
        _add_models,
        _add_limits,
    ],
)
def test_unknown_keys_rejected(job_data: dict[str, Any], mutate: Any) -> None:
    mutate(job_data)

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        JobConfig.model_validate(job_data)
