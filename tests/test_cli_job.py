"""CliRunner tests for ``invio job`` (list, show, create --from-file, lifecycle, import/export)."""

import re
from datetime import UTC, datetime
from typing import Any

import pytest
import yaml

from invio.cli.commands import job as job_module
from invio.config.job import dump_yaml, validate_job
from invio.domain import RunStatus
from tests.cli_helpers import JobCli, job_cli  # noqa: F401 (fixture)

pytestmark = pytest.mark.db

NEXT_RUN_RE = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} (CET|CEST)"
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _create(cli: JobCli, name: str, data: dict[str, Any], *, enabled: bool = True) -> None:
    cli.service.create(name, data)
    if not enabled:
        cli.service.set_enabled(name, False)


def test_help_lists_all_nine_commands(job_cli: JobCli) -> None:
    result = job_cli.invoke(["--help"])

    assert result.exit_code == 0
    # Typer forces a Rich terminal on CI (GITHUB_ACTIONS), so help output may carry ANSI codes.
    output = ANSI_RE.sub("", result.output)
    for command in (
        "create",
        "list",
        "show",
        "edit",
        "enable",
        "disable",
        "delete",
        "export",
        "import",
    ):
        assert re.search(rf"^\s*│?\s*{command}\s", output, re.MULTILINE), command


def test_fmt_next_run() -> None:
    when = datetime(2026, 10, 5, 5, 0, tzinfo=UTC)

    assert job_module._fmt_next_run(when, "Europe/Berlin") == "2026-10-05 07:00 CEST"
    assert job_module._fmt_next_run(None, "UTC") == "—"


# --- list ------------------------------------------------------------------------------------


def test_list_empty(job_cli: JobCli) -> None:
    result = job_cli.invoke(["list"])

    assert result.exit_code == 0
    assert "no jobs yet — create one with 'invio job create'" in result.stdout


def test_list_rows(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "b-job", job_data, enabled=False)
    _create(job_cli, "a-job", job_data)

    result = job_cli.invoke(["list"])

    assert result.exit_code == 0
    lines = [line.strip() for line in result.stdout.splitlines()]
    for header in ("NAME", "ENABLED", "FREQUENCY", "NEXT RUN", "LAST STATUS"):
        assert header in lines[0]
    assert lines[1].startswith("a-job") and lines[2].startswith("b-job")
    assert re.search(
        rf"a-job\s+yes\s+weekly mon 07:30 Europe/Berlin\s+{NEXT_RUN_RE}\s+never run", lines[1]
    )
    assert re.search(r"b-job\s+no\s+weekly mon 07:30 Europe/Berlin\s+—\s+never run", lines[2])


@pytest.mark.parametrize(
    ("schedule", "text"),
    [
        ({"frequency": "daily", "time": "06:00"}, "daily 06:00 UTC"),
        ({"frequency": "monthly", "day_of_month": 31, "time": "06:00"}, "monthly 31 06:00 UTC"),
    ],
)
def test_list_frequency_text(
    job_cli: JobCli, job_data: dict[str, Any], schedule: dict[str, Any], text: str
) -> None:
    job_data["schedule"] = {**schedule, "timezone": "UTC"}
    _create(job_cli, "j", job_data)

    assert text in job_cli.invoke(["list"]).stdout


def test_list_shows_last_run_status(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "a-job", job_data)
    job_cli.add_run("a-job", RunStatus.FAILED, datetime(2026, 1, 1, tzinfo=UTC))

    assert re.search(r"a-job.*failed", job_cli.invoke(["list"]).stdout)


def test_list_invalid_config(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "broken", job_data)
    job_cli.corrupt_timezone("broken")

    result = job_cli.invoke(["list"])

    assert result.exit_code == 0
    assert re.search(r"broken\s+yes\s+—\s+—\s+invalid config", result.stdout)


def test_list_missing_database_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    from typer.testing import CliRunner

    from invio.cli.main import app

    result = CliRunner().invoke(app, ["job", "list"])

    assert result.exit_code == 2
    assert "Configuration error:" in result.stderr


# --- show ------------------------------------------------------------------------------------


def test_show(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "a-job", job_data)

    result = job_cli.invoke(["show", "a-job"])

    assert result.exit_code == 0
    header, _, body = result.stdout.partition("\n\n")
    assert "name: a-job" in header
    assert "enabled: yes" in header
    assert re.search(rf"next run: {NEXT_RUN_RE}", header)
    assert body == dump_yaml(validate_job(job_data))


def test_show_missing(job_cli: JobCli) -> None:
    result = job_cli.invoke(["show", "missing"])

    assert result.exit_code == 1
    assert "Error: job 'missing' not found" in result.stderr


def test_show_invalid_stored_job(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "broken", job_data)
    job_cli.corrupt_timezone("broken")

    result = job_cli.invoke(["show", "broken"])

    assert result.exit_code == 2
    assert yaml.safe_load(result.stdout)["schedule"]["timezone"] == "Europe/Atlantis"
    assert "invalid stored job 'broken':" in result.stderr
    assert "  schedule.timezone: unknown timezone 'Europe/Atlantis'" in result.stderr


# --- QA additions (traceability gaps) --------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["list"],
        ["show", "x"],
        ["edit", "x"],
        ["enable", "x"],
        ["disable", "x"],
        ["delete", "x", "--yes"],
        ["export", "x"],
        ["import", "x.yaml"],
        ["create", "--from-file", "x.yaml"],
        ["create"],
    ],
)
def test_every_command_without_database_setting_exits_2(
    monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    """Regression: most commands built the service outside the error mapping (traceback, 1)."""
    from typer.testing import CliRunner

    from invio.cli.main import app

    monkeypatch.setattr(job_module, "_is_interactive", lambda: True)

    result = CliRunner().invoke(app, ["job", *args])

    assert result.exit_code == 2, result.output
    assert "Configuration error: INVIO_DATABASE_URL" in result.stderr
    assert result.stdout == ""
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_list_empty_writes_nothing_to_stderr(job_cli: JobCli) -> None:
    result = job_cli.invoke(["list"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert "NAME" not in result.stdout  # no empty table


def test_list_broken_job_next_to_valid_jobs(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "c-job", job_data)
    _create(job_cli, "b-broken", job_data)
    _create(job_cli, "a-job", job_data)
    job_cli.corrupt_timezone("b-broken")

    result = job_cli.invoke(["list"])

    assert result.exit_code == 0
    assert result.stderr == ""
    rows = [line.strip() for line in result.stdout.splitlines()[1:] if line.strip()]
    assert [row.split()[0] for row in rows] == ["a-job", "b-broken", "c-job"]
    assert re.search(r"b-broken\s+yes\s+—\s+—\s+invalid config", rows[1])
    assert re.search(rf"c-job\s+yes\s+weekly mon 07:30 Europe/Berlin\s+{NEXT_RUN_RE}", rows[2])


def test_list_invalid_config_takes_precedence_over_run_status(
    job_cli: JobCli, job_data: dict[str, Any]
) -> None:
    _create(job_cli, "broken", job_data)
    job_cli.add_run("broken", RunStatus.SUCCEEDED, datetime(2026, 1, 1, tzinfo=UTC))
    job_cli.corrupt_timezone("broken")

    assert re.search(r"broken.*invalid config", job_cli.invoke(["list"]).stdout)


@pytest.mark.parametrize("status", list(RunStatus))
def test_list_shows_every_run_status(
    job_cli: JobCli, job_data: dict[str, Any], status: RunStatus
) -> None:
    _create(job_cli, "a-job", job_data)
    job_cli.add_run("a-job", RunStatus.FAILED, datetime(2026, 1, 1, tzinfo=UTC))
    job_cli.add_run("a-job", status, datetime(2026, 2, 1, tzinfo=UTC))  # newest wins

    assert re.search(rf"a-job.*\s{status.value}\s*$", job_cli.invoke(["list"]).stdout, re.M)


def test_show_success_writes_nothing_to_stderr(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "a-job", job_data, enabled=False)

    result = job_cli.invoke(["show", "a-job"])

    assert result.exit_code == 0
    assert result.stderr == ""
    assert "enabled: no" in result.stdout
    assert "next run: —" in result.stdout


def test_show_invalid_stored_job_keeps_errors_off_stdout(
    job_cli: JobCli, job_data: dict[str, Any]
) -> None:
    _create(job_cli, "broken", job_data)
    job_cli.corrupt_timezone("broken")

    result = job_cli.invoke(["show", "broken"])

    assert result.exit_code == 2
    assert "invalid stored job" not in result.stdout
    assert "Traceback" not in result.output


def test_list_piped_output_is_not_truncated(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    name = "weekly-ai-research-digest-for-the-whole-team"
    job_data["schedule"]["timezone"] = "America/Argentina/Buenos_Aires"
    _create(job_cli, name, job_data)

    result = job_cli.invoke(["list"], env={"COLUMNS": None})

    assert result.exit_code == 0, result.output
    assert "…" not in result.stdout
    row = rf"^\s*{name}\s+yes\s+.*America/Argentina/Buenos_Aires.*never run\s*$"
    assert re.search(row, result.stdout, re.M)
