"""CliRunner tests for create --from-file, enable/disable/delete, export/import and the seams."""

import json
import re
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from invio.cli.commands import job as job_module
from invio.config.job import dump_yaml, load_yaml, validate_job
from invio.config.settings import MissingSettingError
from invio.domain import RunStatus
from invio.llm.base import ModelRegistryError
from tests.cli_helpers import JobCli, job_cli  # noqa: F401 (fixture)
from tests.conftest import FakeClock
from tests.job_helpers import BAD_JOB, EXAMPLE

pytestmark = pytest.mark.db

NEXT_RUN_RE = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} (CET|CEST)"


def _create(cli: JobCli, name: str, data: dict[str, Any], *, enabled: bool = True) -> None:
    cli.service.create(name, data)
    if not enabled:
        cli.service.set_enabled(name, False)


def _stored_names(cli: JobCli) -> list[str]:
    return [s.name for s in cli.service.overview()]


@pytest.fixture
def example_file(tmp_path: Path) -> Path:
    target = tmp_path / "ai.yaml"
    shutil.copy(EXAMPLE, target)
    return target


# --- create --from-file ----------------------------------------------------------------------


def test_create_from_file(job_cli: JobCli, example_file: Path) -> None:
    job_cli.set_interactive(False)
    job_cli.forbid_prompts()

    result = job_cli.invoke(["create", "--from-file", str(example_file)])

    assert result.exit_code == 0, result.output
    assert "created job 'ai' (next run: " in result.stdout
    assert _stored_names(job_cli) == ["ai"]
    assert job_cli.service.get_by_name("ai").config == load_yaml(EXAMPLE)


def test_create_from_file_with_name(job_cli: JobCli, example_file: Path) -> None:
    job_cli.set_interactive(False)

    result = job_cli.invoke(["create", "--from-file", str(example_file), "--name", "custom"])

    assert result.exit_code == 0
    assert _stored_names(job_cli) == ["custom"]


def test_create_from_invalid_file(job_cli: JobCli, tmp_path: Path) -> None:
    job_cli.set_interactive(False)
    bad = tmp_path / "bad.yaml"
    bad.write_text(BAD_JOB, encoding="utf-8")

    result = job_cli.invoke(["create", "--from-file", str(bad)])

    assert result.exit_code == 2
    assert "invalid job file" in result.stderr
    assert "schedule.time:" in result.stderr
    assert _stored_names(job_cli) == []


def test_create_from_missing_file(job_cli: JobCli, tmp_path: Path) -> None:
    job_cli.set_interactive(False)

    result = job_cli.invoke(["create", "--from-file", str(tmp_path / "nope.yaml")])

    assert result.exit_code == 2
    assert "cannot read job file" in result.stderr


def test_create_from_file_existing_name(job_cli: JobCli, example_file: Path) -> None:
    job_cli.set_interactive(False)
    job_cli.invoke(["create", "--from-file", str(example_file)])
    before = job_cli.service.get_by_name("ai").updated_at

    result = job_cli.invoke(["create", "--from-file", str(example_file)])

    assert result.exit_code == 1
    assert "Error: job 'ai' already exists" in result.stderr
    assert job_cli.service.get_by_name("ai").updated_at == before


# --- enable / disable ------------------------------------------------------------------------


def test_disable_then_enable(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "a-job", job_data)

    disabled = job_cli.invoke(["disable", "a-job"])
    listing = job_cli.invoke(["list"])

    assert disabled.exit_code == 0 and "disabled job 'a-job'" in disabled.stdout
    assert re.search(r"a-job\s+no\s+.*—", listing.stdout)

    enabled = job_cli.invoke(["enable", "a-job"])

    assert enabled.exit_code == 0
    assert re.search(rf"enabled job 'a-job' \(next run: {NEXT_RUN_RE}\)", enabled.stdout)
    assert job_cli.service.get_by_name("a-job").next_run_at is not None


def test_repeated_enable_and_disable_are_noops(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "a-job", job_data)

    again = job_cli.invoke(["enable", "a-job"])
    assert again.exit_code == 0 and "job 'a-job' is already enabled" in again.stdout

    job_cli.invoke(["disable", "a-job"])
    again = job_cli.invoke(["disable", "a-job"])
    assert again.exit_code == 0 and "job 'a-job' is already disabled" in again.stdout


@pytest.mark.parametrize("command", ["enable", "disable", "delete", "show", "export", "edit"])
def test_unknown_job_exits_1(job_cli: JobCli, command: str) -> None:
    args = [command, "missing", *(["--yes"] if command == "delete" else [])]

    result = job_cli.invoke(args)

    assert result.exit_code == 1
    assert "Error: job 'missing' not found" in result.stderr
    assert result.stdout == ""


def test_disable_broken_job_warns_but_succeeds(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "broken", job_data)
    job_cli.corrupt_timezone("broken")

    result = job_cli.invoke(["disable", "broken"])

    assert result.exit_code == 0
    assert "disabled job 'broken'" in result.stdout
    assert "invalid" in result.stderr and "invio job edit broken" in result.stderr
    (row,) = job_cli.service.overview()
    assert row.enabled is False

    enable = job_cli.invoke(["enable", "broken"])

    assert enable.exit_code == 2
    assert "invalid stored job 'broken':" in enable.stderr


# --- delete ----------------------------------------------------------------------------------


def test_delete_confirmed(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "a-job", job_data)

    result = job_cli.invoke(["delete", "a-job"], input="y\n")

    assert result.exit_code == 0
    assert "Delete job 'a-job' and its run history?" in result.stdout
    assert "deleted job 'a-job'" in result.stdout
    assert _stored_names(job_cli) == []


def test_delete_declined(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "a-job", job_data)

    result = job_cli.invoke(["delete", "a-job"], input="n\n")

    assert result.exit_code == 1
    assert "job not deleted" in result.stderr
    assert "deleted job" not in result.stdout
    assert _stored_names(job_cli) == ["a-job"]


@pytest.mark.parametrize("flag", ["--yes", "-y"])
def test_delete_yes_without_terminal(job_cli: JobCli, job_data: dict[str, Any], flag: str) -> None:
    _create(job_cli, "a-job", job_data)
    job_cli.set_interactive(False)

    result = job_cli.invoke(["delete", "a-job", flag])

    assert result.exit_code == 0
    assert _stored_names(job_cli) == []


def test_delete_without_terminal_refuses(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "a-job", job_data)
    job_cli.set_interactive(False)

    result = job_cli.invoke(["delete", "a-job"])

    assert result.exit_code == 2
    assert "Error: refusing to delete without confirmation; pass --yes" in result.stderr
    assert _stored_names(job_cli) == ["a-job"]


def test_delete_broken_job(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "broken", job_data)
    job_cli.corrupt_timezone("broken")

    result = job_cli.invoke(["delete", "broken", "--yes"])

    assert result.exit_code == 0
    assert _stored_names(job_cli) == []


# --- export / import -------------------------------------------------------------------------


def test_export_to_stdout(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "a-job", job_data)

    result = job_cli.invoke(["export", "a-job"])

    assert result.exit_code == 0
    assert result.stdout == dump_yaml(validate_job(job_data))


def test_export_to_file_round_trips(
    job_cli: JobCli, job_data: dict[str, Any], tmp_path: Path
) -> None:
    _create(job_cli, "a-job", job_data)
    out = tmp_path / "out.yaml"

    result = job_cli.invoke(["export", "a-job", "-o", str(out)])

    assert result.exit_code == 0
    assert f"exported job 'a-job' to {out}" in result.stdout
    assert load_yaml(out) == validate_job(job_data)


def test_export_write_failure(job_cli: JobCli, job_data: dict[str, Any], tmp_path: Path) -> None:
    _create(job_cli, "a-job", job_data)

    result = job_cli.invoke(["export", "a-job", "-o", str(tmp_path / "no" / "dir" / "o.yaml")])

    assert result.exit_code == 1
    assert "Error: cannot write" in result.stderr
    assert "Traceback" not in result.output


def test_export_broken_job(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "broken", job_data)
    job_cli.corrupt_timezone("broken")

    result = job_cli.invoke(["export", "broken"])

    assert result.exit_code == 2
    assert "invalid stored job 'broken':" in result.stderr


def test_import_flow(job_cli: JobCli, job_data: dict[str, Any], tmp_path: Path) -> None:
    job_cli.set_interactive(False)
    job_cli.forbid_prompts()
    _create(job_cli, "a-job", job_data)
    out = tmp_path / "out.yaml"
    job_cli.invoke(["export", "a-job", "-o", str(out)])
    job_cli.invoke(["delete", "a-job", "-y"])

    first = job_cli.invoke(["import", str(out)])
    assert first.exit_code == 0 and "imported job 'out'" in first.stdout

    named = job_cli.invoke(["import", str(out), "--name", "a-job"])
    assert named.exit_code == 0 and "imported job 'a-job'" in named.stdout

    again = job_cli.invoke(["import", str(out), "--name", "a-job"])
    assert again.exit_code == 1 and "already exists" in again.stderr

    text = out.read_text(encoding="utf-8").replace("07:30", "08:15")
    out.write_text(text, encoding="utf-8")
    replaced = job_cli.invoke(["import", str(out), "--name", "a-job", "--replace"])
    assert replaced.exit_code == 0 and "replaced job 'a-job'" in replaced.stdout
    assert job_cli.service.get_by_name("a-job").config.schedule.time == "08:15"


def test_import_replace_of_new_name_says_imported(job_cli: JobCli, example_file: Path) -> None:
    result = job_cli.invoke(["import", str(example_file), "--replace"])

    assert result.exit_code == 0 and "imported job 'ai'" in result.stdout


def test_import_invalid_file(job_cli: JobCli, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(BAD_JOB, encoding="utf-8")

    result = job_cli.invoke(["import", str(bad)])

    assert result.exit_code == 2
    assert _stored_names(job_cli) == []


def test_import_invalid_web_selector_exits_2(job_cli: JobCli, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace('selector: "main"', 'selector: "div["'),
        encoding="utf-8",
    )

    result = job_cli.invoke(["import", str(bad)])

    assert result.exit_code == 2
    assert "sources[1].selector: invalid CSS selector" in result.stderr
    assert _stored_names(job_cli) == []


def test_import_bad_name(job_cli: JobCli, example_file: Path) -> None:
    result = job_cli.invoke(["import", str(example_file), "--name", " x"])

    assert result.exit_code == 1
    assert "Error: invalid job name ' x':" in result.stderr


# --- no traceback on any expected failure -----------------------------------------------------


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (["show", "missing"], 1),
        (["enable", "missing"], 1),
        (["disable", "missing"], 1),
        (["delete", "missing", "-y"], 1),
        (["export", "missing"], 1),
        (["edit", "missing"], 1),
        (["create", "--from-file", "nope.yaml"], 2),
        (["import", "nope.yaml"], 2),
        (["show", "broken"], 2),
        (["export", "broken"], 2),
        (["enable", "broken"], 2),
        (["create"], 2),
        (["delete", "ai"], 2),
    ],
)
def test_expected_failures_never_print_a_traceback(
    job_cli: JobCli, job_data: dict[str, Any], args: list[str], code: int
) -> None:
    _create(job_cli, "ai", job_data, enabled=False)
    _create(job_cli, "broken", job_data)
    job_cli.corrupt_timezone("broken")
    job_cli.invoke(["disable", "broken"])  # so that "enable" is not a no-op
    job_cli.set_interactive(False)
    job_cli.forbid_prompts()

    result = job_cli.invoke(args)

    assert result.exit_code == code, result.output
    assert "Traceback" not in result.output


def test_enable_already_enabled_invalid_job_exits_2(
    job_cli: JobCli, job_data: dict[str, Any]
) -> None:
    _create(job_cli, "broken", job_data)
    job_cli.corrupt_timezone("broken")

    result = job_cli.invoke(["enable", "broken"])

    assert result.exit_code == 2, result.output
    assert "invalid stored job 'broken':" in result.stderr
    assert "already enabled" not in result.output


def test_enable_already_enabled_valid_job_is_a_no_op(
    job_cli: JobCli, job_data: dict[str, Any]
) -> None:
    _create(job_cli, "ai", job_data)

    result = job_cli.invoke(["enable", "ai"])

    assert result.exit_code == 0
    assert "job 'ai' is already enabled" in result.stdout


def test_database_error_hides_details(job_cli: JobCli, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> None:
        raise OperationalError("SELECT secret-sql", {}, Exception("password=hunter2"))

    monkeypatch.setattr(job_cli.service, "overview", boom)

    result = job_cli.invoke(["list"])

    assert result.exit_code == 1
    assert "database error (OperationalError)" in result.stderr
    assert "invio db upgrade" in result.stderr
    assert "hunter2" not in result.output and "secret-sql" not in result.output


@pytest.mark.parametrize("exc", [KeyboardInterrupt, EOFError])
def test_ctrl_c_and_eof_exit_1(
    job_cli: JobCli, monkeypatch: pytest.MonkeyPatch, exc: type[BaseException]
) -> None:
    def boom() -> None:
        raise exc

    monkeypatch.setattr(job_cli.service, "overview", boom)

    result = job_cli.invoke(["list"])

    assert result.exit_code == 1 and "Traceback" not in result.output


# --- default seams ---------------------------------------------------------------------------


def test_default_timezone_from_tz_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TZ", "Europe/Berlin")

    assert job_module._default_timezone() == "Europe/Berlin"


def test_default_timezone_from_localtime_link(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TZ", raising=False)
    monkeypatch.setattr(job_module.os, "readlink", lambda p: "/usr/share/zoneinfo/Asia/Tokyo")
    assert job_module._default_timezone() == "Asia/Tokyo"
    monkeypatch.setattr(job_module.os, "readlink", lambda p: "/somewhere/else")
    assert job_module._default_timezone() == "UTC"

    def missing(path: str) -> str:
        raise OSError

    monkeypatch.setattr(job_module.os, "readlink", missing)
    assert job_module._default_timezone() == "UTC"


def test_is_interactive_needs_both_streams_to_be_terminals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Stream:
        def __init__(self, tty: bool) -> None:
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

    monkeypatch.setattr(sys, "stdin", Stream(True))
    monkeypatch.setattr(sys, "stdout", Stream(True))
    assert job_module._is_interactive() is True
    monkeypatch.setattr(sys, "stdout", Stream(False))
    assert job_module._is_interactive() is False


def test_make_registry_warns_when_broken(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> None:
        raise ModelRegistryError("models.d/x.yaml: bad")

    monkeypatch.setattr(job_module, "default_registry", broken)

    assert job_module._make_registry() is None
    assert "model registry unavailable" in capsys.readouterr().err


def test_make_registry_returns_default_registry() -> None:
    assert job_module._make_registry() is not None


def test_make_service_needs_a_database_setting() -> None:
    with pytest.raises(MissingSettingError):
        job_module._make_service()


# --- QA additions (traceability gaps) --------------------------------------------------------


def _snapshot(cli: JobCli, name: str) -> tuple[str, object, bool]:
    summary = next(s for s in cli.service.overview() if s.name == name)
    raw = json.dumps(cli.service.stored_config(name), sort_keys=True)
    return raw, summary.next_run_at, summary.enabled


@pytest.mark.parametrize("via", ["stdout", "file"])
def test_export_import_round_trip_gives_an_equal_config(
    job_cli: JobCli, tmp_path: Path, via: str
) -> None:
    job_cli.set_interactive(False)
    job_cli.forbid_prompts()
    job_cli.invoke(["create", "--from-file", str(EXAMPLE), "--name", "orig"])
    target = tmp_path / "copy.yaml"
    if via == "stdout":
        exported = job_cli.invoke(["export", "orig"])
        assert exported.exit_code == 0 and exported.stderr == ""
        target.write_text(exported.stdout, encoding="utf-8")
    else:
        assert job_cli.invoke(["export", "orig", "-o", str(target)]).exit_code == 0

    result = job_cli.invoke(["import", str(target)])

    assert result.exit_code == 0, result.output
    assert result.stdout == "imported job 'copy'\n"
    original = job_cli.service.get_by_name("orig")
    copy = job_cli.service.get_by_name("copy")
    assert copy.config == original.config
    assert copy.next_run_at == original.next_run_at
    assert job_cli.invoke(["export", "copy"]).stdout == job_cli.invoke(["export", "orig"]).stdout


def test_import_existing_name_leaves_the_job_untouched(
    job_cli: JobCli, job_data: dict[str, Any], tmp_path: Path
) -> None:
    _create(job_cli, "a-job", job_data)
    before = _snapshot(job_cli, "a-job")
    other = tmp_path / "a-job.yaml"
    other.write_text(dump_yaml(validate_job(job_data)).replace("07:30", "11:11"), encoding="utf-8")

    result = job_cli.invoke(["import", str(other)])  # name from the file stem

    assert result.exit_code == 1
    assert "Error: job 'a-job' already exists" in result.stderr
    assert result.stdout == ""
    assert _snapshot(job_cli, "a-job") == before


def test_import_replace_with_invalid_file_leaves_the_job_untouched(
    job_cli: JobCli, job_data: dict[str, Any], tmp_path: Path
) -> None:
    _create(job_cli, "a-job", job_data)
    before = _snapshot(job_cli, "a-job")
    bad = tmp_path / "bad.yaml"
    bad.write_text(BAD_JOB, encoding="utf-8")

    result = job_cli.invoke(["import", str(bad), "--name", "a-job", "--replace"])

    assert result.exit_code == 2
    assert _snapshot(job_cli, "a-job") == before


def test_import_invalid_file_reports_fields_on_stderr(job_cli: JobCli, tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(BAD_JOB, encoding="utf-8")

    result = job_cli.invoke(["import", str(bad)])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert result.stderr.startswith("invalid job file")
    for field in ("schedule.time:", "notification.to", "sources:"):
        assert field in result.stderr, field


def test_import_without_terminal_never_prompts(job_cli: JobCli, example_file: Path) -> None:
    job_cli.set_interactive(False)
    job_cli.forbid_prompts()

    result = job_cli.invoke(["import", str(example_file)])

    assert result.exit_code == 0
    assert _stored_names(job_cli) == ["ai"]


def test_repeated_enable_and_disable_change_nothing(
    job_cli: JobCli, job_data: dict[str, Any], fake_clock: FakeClock
) -> None:
    _create(job_cli, "a-job", job_data)
    enabled = job_cli.service.get_by_name("a-job")
    fake_clock.advance(timedelta(days=30))  # a recomputation would move next_run_at

    for _ in range(2):
        result = job_cli.invoke(["enable", "a-job"])
        assert result.exit_code == 0 and result.stderr == ""
    after = job_cli.service.get_by_name("a-job")
    assert (after.next_run_at, after.updated_at) == (enabled.next_run_at, enabled.updated_at)

    job_cli.invoke(["disable", "a-job"])
    disabled = job_cli.service.get_by_name("a-job")
    again = job_cli.invoke(["disable", "a-job"])

    assert again.exit_code == 0 and again.stderr == ""
    assert job_cli.service.get_by_name("a-job").updated_at == disabled.updated_at
    assert disabled.next_run_at is None


def test_enable_after_disable_computes_a_fresh_next_run(
    job_cli: JobCli, job_data: dict[str, Any], fake_clock: FakeClock
) -> None:
    _create(job_cli, "a-job", job_data)
    first = job_cli.service.get_by_name("a-job").next_run_at
    job_cli.invoke(["disable", "a-job"])
    fake_clock.advance(timedelta(days=14))

    job_cli.invoke(["enable", "a-job"])

    fresh = job_cli.service.get_by_name("a-job").next_run_at
    assert first is not None and fresh is not None
    assert fresh > fake_clock.now and fresh > first


def test_enable_broken_job_keeps_it_disabled(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "broken", job_data, enabled=False)
    job_cli.corrupt_timezone("broken")

    result = job_cli.invoke(["enable", "broken"])

    assert result.exit_code == 2
    assert result.stdout == ""
    assert "  schedule.timezone: unknown timezone 'Europe/Atlantis'" in result.stderr
    (row,) = job_cli.service.overview()
    assert row.enabled is False


def test_delete_declined_leaves_job_byte_identical(
    job_cli: JobCli, job_data: dict[str, Any]
) -> None:
    _create(job_cli, "a-job", job_data)
    before = _snapshot(job_cli, "a-job")

    result = job_cli.invoke(["delete", "a-job"], input="\n")  # default is No

    assert result.exit_code == 1
    assert "[y/N]" in result.stdout
    assert _snapshot(job_cli, "a-job") == before


def test_delete_removes_run_history(job_cli: JobCli, job_data: dict[str, Any]) -> None:
    _create(job_cli, "a-job", job_data)
    job_cli.add_run("a-job", RunStatus.SUCCEEDED, datetime(2026, 1, 1, tzinfo=UTC))

    result = job_cli.invoke(["delete", "a-job", "--yes"])

    assert result.exit_code == 0 and result.stdout == "deleted job 'a-job'\n"
    _create(job_cli, "a-job", job_data)  # same name again: no stale history
    assert re.search(r"a-job.*never run", job_cli.invoke(["list"]).stdout)


def test_export_to_file_does_not_print_yaml(
    job_cli: JobCli, job_data: dict[str, Any], tmp_path: Path
) -> None:
    _create(job_cli, "a-job", job_data)
    out = tmp_path / "o.yaml"

    result = job_cli.invoke(["export", "a-job", "--output", str(out)])

    assert result.stdout == f"exported job 'a-job' to {out}\n"
    assert out.read_text(encoding="utf-8") == dump_yaml(validate_job(job_data))
