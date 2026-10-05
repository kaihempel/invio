"""Tests for notification, search, llm, limits and root-level validation."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from invio.config.job import JobConfig, JobConfigError, load_yaml
from tests.job_helpers import set_path, validation_errors

REJECTED = [
    ("notification.to", [], "at least 1 item"),
    ("notification.to", ["not-an-email"], "email"),
    ("notification.subject", "  ", "at least 1 character"),
    ("search.semantic_description", " ", "at least 1 character"),
    ("search.min_relevance", 1.5, "less than or equal to 1"),
    ("search.min_relevance", -0.1, "greater than or equal to 0"),
    ("search.keywords", {"any": [""]}, "at least 1 character"),
    ("search.keywords", {"exclude": ["  "]}, "at least 1 character"),
    ("llm.provider", "OpenAI", "Input should be"),
    ("llm.provider", "cohere", "Input should be"),
    ("llm.models.fast", "", "at least 1 character"),
    ("llm.models.smart", "", "at least 1 character"),
    ("schema_version", 2, "schema_version 2 is not supported (max 1)"),
    ("schema_version", 0, "greater than or equal to 1"),
    ("sources", [], "at least 1 item"),
    ("limits.max_items_per_run", True, "valid integer"),
    ("search.min_relevance", False, "valid number"),
    ("notification.send_if_empty", 0, "valid boolean"),
    ("schema_version", True, "valid integer"),
]


@pytest.mark.parametrize(("path", "value", "message"), REJECTED)
def test_rejected(job_data: dict[str, Any], path: str, value: Any, message: str) -> None:
    set_path(job_data, path, value)

    errors = validation_errors(job_data)

    assert len(errors) == 1
    parts = tuple(path.split("."))
    assert errors[0][0][: len(parts)] == parts
    assert message in errors[0][1]


@pytest.mark.parametrize(
    "field",
    [
        "max_items_per_source",
        "max_items_per_run",
        "baseline_items",
        "max_items_in_notification",
        "max_llm_tokens_per_run",
    ],
)
@pytest.mark.parametrize("value", [0, -1, 1.5])
def test_limits_rejected(job_data: dict[str, Any], field: str, value: Any) -> None:
    job_data["limits"] = {field: value}

    errors = validation_errors(job_data)

    assert [loc for loc, _ in errors] == [("limits", field)]


def test_baseline_items_default_and_minimum(job_data: dict[str, Any]) -> None:
    assert JobConfig.model_validate(job_data).limits.baseline_items == 10

    job_data["limits"] = {"baseline_items": 1}

    assert JobConfig.model_validate(job_data).limits.baseline_items == 1


def test_baseline_items_rejects_string(job_data: dict[str, Any]) -> None:
    job_data["limits"] = {"baseline_items": "10"}

    errors = validation_errors(job_data)

    assert [loc for loc, _ in errors] == [("limits", "baseline_items")]
    assert "valid integer" in errors[0][1]


def test_fallback_must_differ(job_data: dict[str, Any]) -> None:
    job_data["llm"]["fallback_provider"] = "openai"

    errors = validation_errors(job_data)

    assert errors == [(("llm",), "Value error, fallback_provider must differ from provider")]


@pytest.mark.parametrize("value", [0, 1, 0.0, 1.0])
def test_min_relevance_bounds_accepted(job_data: dict[str, Any], value: Any) -> None:
    job_data["search"]["min_relevance"] = value

    assert JobConfig.model_validate(job_data).search.min_relevance == value


def test_int_min_relevance_becomes_float(job_data: dict[str, Any]) -> None:
    job_data["search"]["min_relevance"] = 1

    value = JobConfig.model_validate(job_data).search.min_relevance

    assert value == 1.0
    assert isinstance(value, float)


def test_duplicate_recipients_accepted(job_data: dict[str, Any]) -> None:
    job_data["notification"]["to"] = ["a@example.com", "a@example.com"]

    assert len(JobConfig.model_validate(job_data).notification.to) == 2


def test_all_keyword_lists_empty_accepted(job_data: dict[str, Any]) -> None:
    job_data["search"]["keywords"] = {"any": [], "all": [], "exclude": []}

    JobConfig.model_validate(job_data)


def test_overlapping_limits_accepted(job_data: dict[str, Any]) -> None:
    job_data["limits"] = {
        "max_items_per_run": 100,
        "max_items_in_notification": 500,
        "max_items_per_source": 500,
    }

    JobConfig.model_validate(job_data)


@pytest.mark.parametrize(("path", "value", "message"), REJECTED)
def test_rejected_file_line_names_field(
    job_data: dict[str, Any], tmp_path: Path, path: str, value: Any, message: str
) -> None:
    """SC-002/SC-007: the same cases loaded from a file give one line naming the field."""
    set_path(job_data, path, value)
    job_file = tmp_path / "job.yaml"
    job_file.write_text(yaml.safe_dump(job_data, allow_unicode=True), encoding="utf-8")

    with pytest.raises(JobConfigError) as info:
        load_yaml(job_file)

    assert len(info.value.errors) == 1
    location, text = info.value.errors[0].split(": ", 1)
    assert location == path or location.startswith((f"{path}.", f"{path}["))
    assert message in text
    assert not text.startswith("Value error")


def test_limits_section_omitted_vs_null(job_data: dict[str, Any]) -> None:
    job_data["limits"] = None

    errors = validation_errors(job_data)

    assert [loc for loc, _ in errors] == [("limits",)]


@pytest.mark.parametrize(
    "missing",
    ["schedule", "notification", "sources", "search", "llm"],
)
def test_required_sections(job_data: dict[str, Any], missing: str) -> None:
    del job_data[missing]

    assert validation_errors(job_data) == [((missing,), "Field required")]


@pytest.mark.parametrize(
    ("path", "field"),
    [
        ("notification", "to"),
        ("notification", "subject"),
        ("search", "semantic_description"),
        ("llm", "provider"),
        ("llm", "models"),
    ],
)
def test_required_fields(job_data: dict[str, Any], path: str, field: str) -> None:
    del job_data[path][field]

    assert validation_errors(job_data) == [((path, field), "Field required")]
