"""Tests for the generated JSON Schema (docs/job.schema.json)."""

import json
from pathlib import Path

import jsonschema
import yaml

from invio.config.job import SUPPORTED_SCHEMA_VERSION, JobYamlLoader, job_json_schema

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = REPO_ROOT / "docs" / "job.schema.json"
EXAMPLE = REPO_ROOT / "docs" / "job.example.yaml"


def test_committed_schema_is_current() -> None:
    committed = json.loads(SCHEMA.read_text(encoding="utf-8"))

    assert committed == job_json_schema(), (
        "docs/job.schema.json is stale; run: uv run python -m invio.config.job"
    )


def test_example_validates_against_schema() -> None:
    data = yaml.load(EXAMPLE.read_text(encoding="utf-8"), Loader=JobYamlLoader)

    jsonschema.validate(data, job_json_schema())


def test_schema_shape() -> None:
    schema = job_json_schema()

    assert schema["additionalProperties"] is False
    sources = schema["properties"]["sources"]["items"]
    assert set(sources["discriminator"]["mapping"]) == {
        "rss",
        "web",
        "sitemap",
        "youtube_channel",
        "youtube_playlist",
    }
    assert len(sources["oneOf"]) == 5


def test_schema_bounds_schema_version() -> None:
    version = job_json_schema()["properties"]["schema_version"]

    assert (version["minimum"], version["maximum"]) == (1, SUPPORTED_SCHEMA_VERSION)
