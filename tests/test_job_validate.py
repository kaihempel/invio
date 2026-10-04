"""Tests for ``validate_job``, the mapping-level validation entry point."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from invio.config.job import JobConfig, JobConfigError, load_yaml, validate_job
from tests.job_helpers import BAD_JOB


def test_valid_mapping_equals_model_validate(job_data: dict[str, Any]) -> None:
    assert validate_job(job_data) == JobConfig.model_validate(job_data)


def test_invalid_mapping_matches_load_yaml_errors(tmp_path: Path) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(BAD_JOB, encoding="utf-8")
    with pytest.raises(JobConfigError) as from_file:
        load_yaml(path)
    with pytest.raises(JobConfigError) as from_mapping:
        validate_job(yaml.safe_load(BAD_JOB))
    assert from_mapping.value.path is None
    assert from_file.value.path == path
    assert from_mapping.value.errors == from_file.value.errors


def test_unknown_key_is_reported(job_data: dict[str, Any]) -> None:
    job_data["bogus"] = 1
    with pytest.raises(JobConfigError) as info:
        validate_job(job_data)
    assert any(
        line.startswith("bogus:") and "Extra inputs are not permitted" in line
        for line in info.value.errors
    )


def test_message_header_without_path(job_data: dict[str, Any]) -> None:
    job_data["bogus"] = 1
    with pytest.raises(JobConfigError) as info:
        validate_job(job_data)
    assert str(info.value).startswith("invalid job file:")
