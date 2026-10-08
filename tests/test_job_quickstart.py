"""End-to-end walk through specs/001-gh-issue-3/quickstart.md steps 2-6, without the CLI."""

import json
import subprocess
import sys
from pathlib import Path

import jsonschema
import pytest
import yaml

from invio.config import job as job_module
from invio.config.job import (
    Frequency,
    JobConfigError,
    JobYamlLoader,
    Weekday,
    dump_yaml,
    job_json_schema,
    load_yaml,
    write_yaml,
)
from tests.job_helpers import BAD_JOB, EXAMPLE, SCHEMA


def test_quickstart_end_to_end(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Step 2: the reference example loads (SC-001).
    job = load_yaml(str(EXAMPLE))
    assert repr(job.schedule).startswith(
        "ScheduleConfig(frequency=<Frequency.WEEKLY: 'weekly'>, time='07:30', "
        "weekday=<Weekday.MONDAY: 'monday'>"
    )
    assert job.schedule.frequency is Frequency.WEEKLY
    assert job.schedule.weekday is Weekday.MONDAY

    # Step 3: the broken file is rejected with one line per field problem, no cross-field lines.
    bad = tmp_path / "bad-job.yaml"
    bad.write_text(BAD_JOB, encoding="utf-8")
    with pytest.raises(JobConfigError) as info:
        load_yaml(bad)
    assert {line.split(": ", 1)[0] for line in info.value.errors} == {
        "schedule.time",
        "schedule.timezone",
        "notification.to[0]",
        "sources",
        "search.semantic_description",
        "search.min_relevance",
        "llm.frequncy",
    }
    assert str(info.value).startswith(f"invalid job file {bad}:\n")

    # Step 4: round-trip through a file (SC-003); every field written, keys in contract order.
    out = tmp_path / "job-roundtrip.yaml"
    write_yaml(job, out)
    assert load_yaml(out) == job
    text = dump_yaml(job)
    assert out.read_text(encoding="utf-8") == text
    saved = yaml.load(text, Loader=JobYamlLoader)
    assert list(saved) == [
        "schema_version",
        "language",
        "schedule",
        "notification",
        "sources",
        "search",
        "llm",
        "limits",
        "archive",
    ]
    assert "day_of_month: null" in text

    # Step 5: `python -m invio.config.job` regenerates the committed schema byte-for-byte (FR-029)
    # and the example plus the saved file validate against it (SC-005).
    (tmp_path / "docs").mkdir()
    job_module.main()  # the autouse fixture has chdir'ed into tmp_path
    assert capsys.readouterr().out.strip() == str(Path("docs/job.schema.json"))
    generated = tmp_path / "docs" / "job.schema.json"
    assert generated.read_bytes() == SCHEMA.read_bytes()
    schema = json.loads(generated.read_text(encoding="utf-8"))
    jsonschema.validate(
        yaml.load(EXAMPLE.read_text(encoding="utf-8"), Loader=JobYamlLoader), schema
    )
    jsonschema.validate(saved, schema)
    assert schema == job_json_schema()

    # Step 6: the domain records import without heavy dependencies (SC-006).
    code = (
        "import sys, invio.domain; "
        "bad = {'pydantic','yaml','sqlalchemy','httpx2','requests','email_validator'} "
        "& {m.split('.')[0] for m in sys.modules}; "
        "sys.exit(sorted(bad) or 0)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_schema_module_entry_point(tmp_path: Path) -> None:
    """`python -m invio.config.job` writes docs/job.schema.json relative to the cwd (FR-028)."""
    (tmp_path / "docs").mkdir()

    result = subprocess.run(
        [sys.executable, "-m", "invio.config.job"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )

    assert result.stdout.strip() == str(Path("docs/job.schema.json"))
    assert (tmp_path / "docs" / "job.schema.json").read_bytes() == SCHEMA.read_bytes()


def test_stale_schema_is_detectable(tmp_path: Path) -> None:
    """FR-029 / AS 4.3: a schema that no longer matches the models compares unequal."""
    stale = json.loads(SCHEMA.read_text(encoding="utf-8"))
    stale["$defs"]["LimitsConfig"]["properties"]["max_items_per_run"]["default"] = 50

    assert stale != job_json_schema()
