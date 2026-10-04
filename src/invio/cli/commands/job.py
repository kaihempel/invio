"""``invio job``: create, inspect, edit and manage research jobs.

Results go to stdout; errors, warnings and logs go to stderr. Exit codes: 0 success, 1 not
found / already exists / invalid name / aborted / declined, 2 invalid configuration or usage
(see ``specs/005-gh-issue-9/contracts/cli-job.md``). The module-level ``_make_*``,
``_is_interactive`` and ``_edit_text`` functions are test seams.
"""

import contextlib
import os
import sys
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

import typer
import yaml
from rich.console import Console
from rich.table import Table
from sqlalchemy.exc import SQLAlchemyError

from invio.cli import editor
from invio.cli.errors import fail
from invio.cli.prompts import Prompter, QuestionaryPrompter, WizardAborted
from invio.cli.source_check import HttpSourceChecker, SourceChecker
from invio.cli.wizard import WARNING_NO_REGISTRY, run_wizard
from invio.config.job import (
    Frequency,
    JobConfigError,
    ScheduleConfig,
    dump_yaml,
    known_timezones,
    loads_yaml,
)
from invio.config.settings import MissingSettingError
from invio.llm.base import ModelRegistryError
from invio.llm.registry import ModelRegistry, default_registry
from invio.services.jobs import (
    JobExistsError,
    JobNameError,
    JobNotFoundError,
    JobRecord,
    JobService,
    StoredJobConfigError,
    check_job_name,
)

app = typer.Typer(help="Manage research jobs.", no_args_is_help=True)

_DASH = "—"


# --- test seams ------------------------------------------------------------------------------


def _make_service() -> JobService:
    return JobService.from_settings()


def _make_prompter() -> Prompter:
    return QuestionaryPrompter()


def _make_checker() -> SourceChecker:
    return HttpSourceChecker()


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _edit_text(text: str) -> str | None:
    return editor.edit_text(text)


def _make_registry() -> ModelRegistry | None:
    """Return the model registry, or ``None`` (with a warning) if it cannot be loaded."""
    try:
        return default_registry()
    except ModelRegistryError as exc:
        typer.echo(WARNING_NO_REGISTRY.format(exc), err=True)
        return None


def _default_timezone() -> str:
    """``$TZ`` if it is a known zone, else the ``/etc/localtime`` target, else ``UTC``."""
    zones = known_timezones()
    tz = os.environ.get("TZ", "")
    if tz in zones:
        return tz
    try:
        target = os.readlink("/etc/localtime")
    except OSError:
        return "UTC"
    _, _, name = target.partition("zoneinfo/")
    return name if name in zones else "UTC"


# --- helpers ---------------------------------------------------------------------------------


@contextlib.contextmanager
def _errors() -> Iterator[None]:
    """Map the expected exceptions to a message on stderr and an exit code (no traceback)."""
    try:
        yield
    except (JobNotFoundError, JobExistsError) as exc:
        raise fail(f"Error: {exc}", 1) from exc
    except JobNameError as exc:
        raise fail(f"Error: invalid job name '{exc.name}': {exc.rule}", 1) from exc
    except JobConfigError as exc:  # includes StoredJobConfigError
        raise fail(str(exc), 2) from exc
    except MissingSettingError as exc:
        raise fail(f"Configuration error: {exc}", 2) from exc
    except (WizardAborted, KeyboardInterrupt, EOFError) as exc:
        raise fail("aborted; nothing saved", 1) from exc
    except SQLAlchemyError as exc:
        # Deliberately only the type name: the message may embed the database URL or SQL.
        raise fail(
            f"Error: database error ({type(exc).__name__}); "
            "is the schema current? run 'invio db upgrade'",
            1,
        ) from exc


def _fmt_next_run(next_run_at: datetime | None, tz: str) -> str:
    """Render ``next_run_at`` as ``YYYY-MM-DD HH:MM <TZ abbr>`` in ``tz`` (``—`` if unset)."""
    if next_run_at is None:
        return _DASH
    return next_run_at.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")


def _fmt_frequency(schedule: ScheduleConfig) -> str:
    when = f"{schedule.time} {schedule.timezone}"
    if schedule.frequency is Frequency.WEEKLY and schedule.weekday is not None:
        return f"weekly {schedule.weekday.value[:3]} {when}"
    if schedule.frequency is Frequency.MONTHLY:
        return f"monthly {schedule.day_of_month} {when}"
    return f"daily {when}"


_PIPE_WIDTH = 1_000


def _yes_no(flag: bool) -> str:
    return "yes" if flag else "no"


# --- list / show -----------------------------------------------------------------------------


@app.command("list")
def list_jobs() -> None:
    """List all jobs."""
    with _errors():
        summaries = _make_service().overview()
    if not summaries:
        typer.echo("no jobs yet — create one with 'invio job create'")
        return
    table = Table(box=None)
    for column in ("NAME", "ENABLED", "FREQUENCY", "NEXT RUN", "LAST STATUS"):
        table.add_column(column, no_wrap=True)
    for row in summaries:
        config = row.config
        if config is None:
            frequency, next_run, status = _DASH, _DASH, "invalid config"
        else:
            frequency = _fmt_frequency(config.schedule)
            next_run = _fmt_next_run(row.next_run_at, config.schedule.timezone)
            status = row.last_run_status.value if row.last_run_status else "never run"
        table.add_row(row.name, _yes_no(row.enabled), frequency, next_run, status)
    console = Console(markup=False, highlight=False, emoji=False)
    if not console.is_terminal:
        # Piped output defaults to 80 columns; never truncate cells for grep/awk.
        console = Console(markup=False, highlight=False, emoji=False, width=_PIPE_WIDTH)
    console.print(table)


@app.command()
def show(name: Annotated[str, typer.Argument(help="Job name.")]) -> None:
    """Show a job's status and configuration."""
    with _errors():
        service = _make_service()
        try:
            record = service.get_by_name(name)
        except StoredJobConfigError as exc:
            typer.echo(_stored_yaml(service, name), nl=False)
            raise fail(str(exc), 2) from exc
        tz = record.config.schedule.timezone
        typer.echo(f"name: {record.name}")
        typer.echo(f"enabled: {_yes_no(record.enabled)}")
        typer.echo(f"next run: {_fmt_next_run(record.next_run_at, tz)}")
        typer.echo()
        typer.echo(dump_yaml(record.config), nl=False)


def _stored_yaml(service: JobService, name: str) -> str:
    return yaml.safe_dump(service.stored_config(name), sort_keys=False, allow_unicode=True)


def _status_line(verb: str, name: str, record: JobRecord) -> str:
    next_run = _fmt_next_run(record.next_run_at, record.config.schedule.timezone)
    return f"{verb} job '{name}' (next run: {next_run})"


def _create_interactive(name: str | None) -> None:
    if not _is_interactive():
        raise fail(
            "Error: interactive creation needs a terminal; "
            "use 'invio job create --from-file <job.yaml>'",
            2,
        )
    service = _make_service()
    existing = set(service.names())
    if name is not None:
        check_job_name(name)  # before the first question
        if name in existing:
            raise JobExistsError(name)
    result = run_wizard(
        _make_prompter(),
        _make_checker(),
        registry=_make_registry(),
        existing_names=existing,
        name=name,
        default_timezone=_default_timezone(),
    )
    if result is None:
        raise fail("job not created", 1)
    job_name, config = result
    record = service.create(job_name, config)
    typer.echo(_status_line("created", job_name, record))


@app.command()
def create(
    from_file: Annotated[
        Path | None, typer.Option("--from-file", help="Create from this job YAML (no prompts).")
    ] = None,
    name: Annotated[str | None, typer.Option("--name", help="Job name.")] = None,
) -> None:
    """Create a job with the interactive wizard, or from a YAML file."""
    with _errors():
        if from_file is None:
            _create_interactive(name)
            return
        record = _make_service().import_yaml(from_file, name, replace=False)
        typer.echo(_status_line("created", record.name, record))


@app.command()
def edit(name: Annotated[str, typer.Argument(help="Job name.")]) -> None:
    """Edit a job's YAML in $VISUAL / $EDITOR."""
    with _errors():
        service = _make_service()
        try:
            text = dump_yaml(service.get_by_name(name).config)
        except StoredJobConfigError as exc:
            typer.echo(str(exc), err=True)
            text = _stored_yaml(service, name)
        first_round = True
        while True:
            try:
                edited = _edit_text(text)
            except editor.EditorError as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise fail("edit aborted; job unchanged", 1) from exc
            if edited is None:
                if first_round:
                    typer.echo("no changes")
                    return
                edited = text  # closed without saving: re-validate the (still invalid) text
            first_round = False
            try:
                config = loads_yaml(edited)
            except JobConfigError as exc:
                typer.echo(str(exc), err=True)
                if not typer.confirm("Re-open the editor?", default=True):
                    raise fail("edit aborted; job unchanged", 1) from exc
                text = edited
                continue
            break
        record = service.update(name, config)
        typer.echo(_status_line("updated", name, record))


def _set_enabled(name: str, enabled: bool) -> None:
    with _errors():
        service = _make_service()
        was_enabled = service.is_enabled(name)
        if enabled:
            # Always through the service: it refuses an invalid stored config (exit 2), even
            # when the job is already enabled.
            record = service.set_enabled(name, True)
            if was_enabled:
                typer.echo(f"job '{name}' is already enabled")
            else:
                typer.echo(_status_line("enabled", name, record))
            return
        if not was_enabled:
            typer.echo(f"job '{name}' is already disabled")
            return
        try:
            service.set_enabled(name, False)
        except StoredJobConfigError:
            # The disable is committed; only the record could not be built.
            typer.echo(f"disabled job '{name}'")
            typer.echo(
                f"warning: stored configuration of job '{name}' is invalid; "
                f"fix it with 'invio job edit {name}'",
                err=True,
            )
            return
        typer.echo(f"disabled job '{name}'")


@app.command()
def enable(name: Annotated[str, typer.Argument(help="Job name.")]) -> None:
    """Enable a job (it is scheduled again)."""
    _set_enabled(name, True)


@app.command()
def disable(name: Annotated[str, typer.Argument(help="Job name.")]) -> None:
    """Disable a job (it keeps its configuration and history)."""
    _set_enabled(name, False)


@app.command()
def delete(
    name: Annotated[str, typer.Argument(help="Job name.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
) -> None:
    """Delete a job and its run history."""
    with _errors():
        service = _make_service()
        if name not in service.names():
            raise JobNotFoundError(name)
        if not yes:
            if not _is_interactive():
                raise fail("Error: refusing to delete without confirmation; pass --yes", 2)
            if not typer.confirm(f"Delete job '{name}' and its run history?", default=False):
                raise fail("job not deleted", 1)
        service.delete(name)
        typer.echo(f"deleted job '{name}'")


@app.command()
def export(
    name: Annotated[str, typer.Argument(help="Job name.")],
    output: Annotated[
        Path | None, typer.Option("--output", "-o", help="Write to this file instead of stdout.")
    ] = None,
) -> None:
    """Export a job as YAML."""
    with _errors():
        service = _make_service()
        try:
            text = service.export_yaml(name, output)
        except OSError as exc:
            raise fail(f"Error: cannot write {output}: {exc}", 1) from exc
        if output is None:
            typer.echo(text, nl=False)
        else:
            typer.echo(f"exported job '{name}' to {output}")


@app.command("import")
def import_job(
    file: Annotated[Path, typer.Argument(help="Job YAML file.")],
    name: Annotated[
        str | None, typer.Option("--name", help="Job name (default: file stem).")
    ] = None,
    replace: Annotated[
        bool, typer.Option("--replace", help="Replace an existing job of that name.")
    ] = False,
) -> None:
    """Import a job from a YAML file (no prompts)."""
    with _errors():
        service = _make_service()
        target = name if name is not None else file.stem
        existed = target in service.names()
        record = service.import_yaml(file, name, replace=replace)
        verb = "replaced" if existed and replace else "imported"
        typer.echo(f"{verb} job '{record.name}'")
