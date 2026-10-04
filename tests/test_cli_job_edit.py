"""CliRunner tests for ``invio job edit`` (fake editor, never a real one)."""

import json
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import typer
import yaml

from invio.cli.commands import job as job_module
from invio.cli.editor import EditorError
from invio.config.job import dump_yaml, validate_job
from tests.cli_helpers import JobCli, fake_editor, job_cli  # noqa: F401 (fixture)

pytestmark = pytest.mark.db


@pytest.fixture
def stored(job_cli: JobCli, job_data: dict[str, Any]) -> str:
    """Create ``a-job`` and return its YAML."""
    job_cli.service.create("a-job", job_data)
    return dump_yaml(validate_job(job_data))


def _time(cli: JobCli, name: str = "a-job") -> str:
    return cli.service.get_by_name(name).config.schedule.time


def test_valid_change(job_cli: JobCli, stored: str) -> None:
    editor = fake_editor(stored.replace("07:30", "09:00"))
    job_cli.use_editor(editor)

    result = job_cli.invoke(["edit", "a-job"])

    assert result.exit_code == 0, result.output
    assert "updated job 'a-job' (next run: " in result.stdout
    assert editor.received == [stored]
    record = job_cli.service.get_by_name("a-job")
    assert record.config.schedule.time == "09:00"
    assert record.next_run_at is not None


@pytest.mark.parametrize("returned", [None, "same"])
def test_no_changes(job_cli: JobCli, stored: str, returned: str | None) -> None:
    before = job_cli.service.get_by_name("a-job").updated_at
    job_cli.use_editor(fake_editor(stored if returned == "same" else None))

    result = job_cli.invoke(["edit", "a-job"])

    assert result.exit_code == 0
    assert "no changes" in result.stdout
    assert job_cli.service.get_by_name("a-job").updated_at == before


@pytest.mark.parametrize(
    "broken",
    ["schedule: [\n", "schedule:\n  time: '25:00'\n"],
    ids=["invalid-yaml", "validation-error"],
)
def test_invalid_then_abort(job_cli: JobCli, stored: str, broken: str) -> None:
    before = job_cli.service.stored_config("a-job")
    job_cli.use_editor(fake_editor(broken))

    result = job_cli.invoke(["edit", "a-job"], input="n\n")

    assert result.exit_code == 1
    assert "invalid job file" in result.stderr
    assert "Re-open the editor?" in result.stdout
    assert "edit aborted; job unchanged" in result.stderr
    assert job_cli.service.stored_config("a-job") == before


def test_invalid_then_retry_then_valid(job_cli: JobCli, stored: str) -> None:
    invalid = stored.replace("07:30", "25:00")
    editor = fake_editor(invalid, stored.replace("07:30", "10:15"))
    job_cli.use_editor(editor)

    result = job_cli.invoke(["edit", "a-job"], input="y\n")

    assert result.exit_code == 0, result.output
    assert editor.received == [stored, invalid]  # the second round shows the edited text
    assert _time(job_cli) == "10:15"


def test_editor_failure_leaves_job_unchanged(job_cli: JobCli, stored: str) -> None:
    job_cli.use_editor(fake_editor(EditorError("editor 'vi' exited with status 3")))

    result = job_cli.invoke(["edit", "a-job"])

    assert result.exit_code == 1
    assert "edit aborted; job unchanged" in result.stderr
    assert "exited with status 3" in result.stderr
    assert _time(job_cli) == "07:30"
    assert "updated job" not in result.stdout


def test_missing_job(job_cli: JobCli) -> None:
    job_cli.use_editor(fake_editor())

    result = job_cli.invoke(["edit", "missing"])

    assert result.exit_code == 1
    assert "not found" in result.stderr


def test_broken_stored_job_is_repaired(job_cli: JobCli, stored: str) -> None:
    job_cli.corrupt_timezone("a-job")
    raw = job_cli.service.stored_config("a-job")
    repaired = stored.replace("07:30", "08:00")
    editor = fake_editor(repaired)
    job_cli.use_editor(editor)

    result = job_cli.invoke(["edit", "a-job"])

    assert result.exit_code == 0, result.output
    assert "invalid stored job 'a-job':" in result.stderr
    assert editor.received == [yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)]
    assert job_cli.invoke(["show", "a-job"]).exit_code == 0


def test_name_key_is_rejected_and_name_unchanged(job_cli: JobCli, stored: str) -> None:
    job_cli.use_editor(fake_editor("name: other\n" + stored))

    result = job_cli.invoke(["edit", "a-job"], input="n\n")

    assert result.exit_code == 1
    assert "name:" in result.stderr
    assert [s.name for s in job_cli.service.overview()] == ["a-job"]


def test_vanished_job_during_edit(job_cli: JobCli, stored: str) -> None:
    def delete_then_edit(text: str) -> str:
        job_cli.service.delete("a-job")
        return text.replace("07:30", "09:00")

    job_cli.use_editor(delete_then_edit)

    result = job_cli.invoke(["edit", "a-job"])

    assert result.exit_code == 1
    assert "not found" in result.stderr


def test_default_edit_seam_delegates_to_editor(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(job_module.editor, "edit_text", lambda text: seen.append(text) or "x")

    assert job_module._edit_text("abc") == "x"
    assert seen == ["abc"]


# --- QA additions (traceability gaps) --------------------------------------------------------


RENAME = "name: other\n"


def _snapshot(cli: JobCli, name: str = "a-job") -> tuple[str, object, bool]:
    """The stored config as canonical JSON plus next run and enabled (SC-006: byte-for-byte)."""
    raw = json.dumps(cli.service.stored_config(name), sort_keys=True)
    summary = next(s for s in cli.service.overview() if s.name == name)
    return raw, summary.next_run_at, summary.enabled


def test_retry_then_close_without_saving_does_not_succeed(job_cli: JobCli, stored: str) -> None:
    """Regression: reopening after an error and closing unsaved printed 'no changes', exit 0."""
    before = _snapshot(job_cli)
    invalid = stored.replace("07:30", "25:00")
    editor = fake_editor(invalid, None)  # second round: closed without saving
    job_cli.use_editor(editor)

    result = job_cli.invoke(["edit", "a-job"], input="y\nn\n")

    assert result.exit_code == 1, result.output
    assert "no changes" not in result.stdout
    assert result.stderr.count("invalid job file") == 2  # the text is re-validated
    assert result.stdout.count("Re-open the editor?") == 2
    assert "edit aborted; job unchanged" in result.stderr
    assert editor.received == [stored, invalid]
    assert _snapshot(job_cli) == before


def test_retry_then_editor_failure_leaves_job_unchanged(job_cli: JobCli, stored: str) -> None:
    before = _snapshot(job_cli)
    job_cli.use_editor(
        fake_editor("schedule: [\n", EditorError("editor 'vi' exited with status 1"))
    )

    result = job_cli.invoke(["edit", "a-job"], input="y\n")

    assert result.exit_code == 1
    assert "edit aborted; job unchanged" in result.stderr
    assert _snapshot(job_cli) == before


@pytest.mark.parametrize(
    "broken",
    ["schedule: [\n", "schedule:\n  time: '25:00'\n", RENAME],
    ids=["invalid-yaml", "validation-error", "rename-attempt"],
)
def test_invalid_then_abort_is_byte_identical(job_cli: JobCli, stored: str, broken: str) -> None:
    before = _snapshot(job_cli)
    text = broken + stored if broken == RENAME else broken
    job_cli.use_editor(fake_editor(text))

    result = job_cli.invoke(["edit", "a-job"], input="n\n")

    assert result.exit_code == 1
    assert "Traceback" not in result.output
    assert "updated job" not in result.stdout
    assert "invalid job file" not in result.stdout  # errors are diagnostics: stderr only
    assert _snapshot(job_cli) == before


def test_validation_errors_name_the_field(job_cli: JobCli, stored: str) -> None:
    job_cli.use_editor(fake_editor(stored.replace("Europe/Berlin", "Europe/Atlantis")))

    result = job_cli.invoke(["edit", "a-job"], input="n\n")

    assert result.exit_code == 1
    assert "  schedule.timezone: unknown timezone 'Europe/Atlantis'" in result.stderr


def test_schedule_change_recalculates_next_run(job_cli: JobCli, stored: str) -> None:
    before = job_cli.service.get_by_name("a-job").next_run_at
    job_cli.use_editor(fake_editor(stored.replace("weekday: monday", "weekday: friday")))

    result = job_cli.invoke(["edit", "a-job"])

    assert result.exit_code == 0, result.output
    after = job_cli.service.get_by_name("a-job").next_run_at
    assert before is not None and after is not None and after != before
    assert after.astimezone(ZoneInfo("Europe/Berlin")).weekday() == 4  # friday


def test_no_changes_on_broken_job_keeps_it_broken(job_cli: JobCli, stored: str) -> None:
    job_cli.corrupt_timezone("a-job")
    before = _snapshot(job_cli)
    job_cli.use_editor(fake_editor(None))

    result = job_cli.invoke(["edit", "a-job"])

    assert result.exit_code == 0
    assert "no changes" in result.stdout
    assert "invalid stored job 'a-job':" in result.stderr
    assert _snapshot(job_cli) == before


def test_broken_job_errors_shown_before_editor_opens(job_cli: JobCli, stored: str) -> None:
    job_cli.corrupt_timezone("a-job")
    seen: list[str] = []

    def editor(text: str) -> None:
        seen.append("editor")
        return None

    job_cli.use_editor(editor)
    real_echo = typer.echo

    def echo(message: str = "", *, err: bool = False, nl: bool = True) -> None:
        if "invalid stored job" in message:
            seen.append("errors")
        real_echo(message, err=err, nl=nl)

    job_cli.monkeypatch.setattr(typer, "echo", echo)

    job_cli.invoke(["edit", "a-job"])

    assert seen == ["errors", "editor"]
