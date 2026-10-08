"""``invio job``: create, inspect, edit and manage research jobs.

Results go to stdout; errors, warnings and logs go to stderr. Exit codes: 0 success, 1 not
found / already exists / invalid name / aborted / declined, 2 invalid configuration or usage
(see ``specs/005-gh-issue-9/contracts/cli-job.md``). The module-level ``_make_*``,
``_is_interactive`` and ``_edit_text`` functions are test seams.
"""

import asyncio
import contextlib
import os
import sys
from collections.abc import Coroutine, Iterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Annotated, Any

import typer
import yaml
from pydantic import ValidationError
from rich.table import Table

from invio.cli import editor
from invio.cli.console import stderr_console, stdout_console
from invio.cli.errors import fail, mapped_errors
from invio.cli.progress import RunProgressView
from invio.cli.prompts import Prompter, QuestionaryPrompter, WizardAborted
from invio.cli.run_output import DASH, format_time, stats_block
from invio.cli.runtime import settings_error, setup_runtime
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
from invio.config.settings import get_settings
from invio.domain import RunStatus
from invio.llm.base import ModelRegistryError
from invio.llm.registry import ModelRegistry, default_registry
from invio.notify import DatabaseConfigError
from invio.pipeline import RunDeps
from invio.pipeline.deps import default_deps
from invio.pipeline.run import JobBusyError, JobDisabledError, RunResult, run_job_by_name
from invio.services.jobs import (
    JobExistsError,
    JobNameError,
    JobNotFoundError,
    JobRecord,
    JobService,
    StoredJobConfigError,
    check_job_name,
)
from invio.textsafe import strip_control

app = typer.Typer(help="Manage research jobs.", no_args_is_help=True)

_RUN = "run"  # the name of ``invio job run``; the group callback's exit code depends on it


@app.callback()
def _setup(ctx: typer.Context) -> None:
    """Manage research jobs."""
    # The root callback leaves the runtime setup to this group (``SELF_CONFIGURING_GROUPS``):
    # a configuration error is "could not start" (1) for ``job run``, where 2 means partial.
    setup_runtime(config_exit=1 if ctx.invoked_subcommand == _RUN else 2)


# --- test seams ------------------------------------------------------------------------------


def _make_service() -> JobService:
    return JobService.from_settings()


def _run_deps() -> AbstractAsyncContextManager[RunDeps]:
    """The production run dependencies (HTTP client, providers, notifier); tests replace this."""
    return default_deps(get_settings())


def _execute(coro: Coroutine[Any, Any, RunResult]) -> RunResult:
    """Run the coroutine to completion on a fresh event loop (the Ctrl-C test replaces this)."""
    return asyncio.run(coro)


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
        with mapped_errors(config_exit=2):
            yield
    except JobExistsError as exc:
        raise fail(f"Error: {exc}", 1) from exc
    except JobNameError as exc:
        raise fail(f"Error: invalid job name '{exc.name}': {exc.rule}", 1) from exc
    except (WizardAborted, KeyboardInterrupt, EOFError) as exc:
        raise fail("aborted; nothing saved", 1) from exc


def _fmt_frequency(schedule: ScheduleConfig) -> str:
    when = f"{schedule.time} {schedule.timezone}"
    if schedule.frequency is Frequency.WEEKLY and schedule.weekday is not None:
        return f"weekly {schedule.weekday.value[:3]} {when}"
    if schedule.frequency is Frequency.MONTHLY:
        return f"monthly {schedule.day_of_month} {when}"
    return f"daily {when}"


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
            frequency, next_run, status = DASH, DASH, "invalid config"
        else:
            frequency = _fmt_frequency(config.schedule)
            next_run = format_time(row.next_run_at, config.schedule.timezone)
            status = row.last_run_status.value if row.last_run_status else "never run"
        table.add_row(row.name, _yes_no(row.enabled), frequency, next_run, status)
    stdout_console().print(table)


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
        typer.echo(f"next run: {format_time(record.next_run_at, tz)}")
        typer.echo()
        typer.echo(dump_yaml(record.config), nl=False)


def _stored_yaml(service: JobService, name: str) -> str:
    return yaml.safe_dump(service.stored_config(name), sort_keys=False, allow_unicode=True)


def _status_line(verb: str, name: str, record: JobRecord) -> str:
    next_run = format_time(record.next_run_at, record.config.schedule.timezone)
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


# --- run -------------------------------------------------------------------------------------


@contextlib.contextmanager
def _run_errors() -> Iterator[None]:
    """Everything that can stop ``job run`` from starting: one stderr line, exit 1.

    Only named types are caught (``typer.Exit`` is a ``RuntimeError``). A configuration problem
    exits 1 here, not 2: "could not start" is a single exit code for scripts.
    """
    try:
        with mapped_errors(config_exit=1):
            yield
    except (DatabaseConfigError, ModelRegistryError) as exc:
        raise fail(f"Configuration error: {exc}", 1) from exc
    except ValidationError as exc:
        raise fail(f"Configuration error: {settings_error(exc)}", 1) from exc
    except KeyboardInterrupt as exc:
        raise fail("interrupted; if a run had started, it is recorded as failed", 1) from exc


async def _run_once(
    name: str, *, dry_run: bool, max_items: int | None, view: RunProgressView
) -> RunResult:
    async with _run_deps() as deps:
        return await run_job_by_name(
            name, dry_run=dry_run, max_items=max_items, observer=view, deps=deps
        )


def _disabled(name: str) -> typer.Exit:
    return fail(f"Error: job '{name}' is disabled; enable it with 'invio job enable {name}'", 1)


def _print_result(result: RunResult, *, show_digest: bool) -> None:
    """Digest (if asked), the stats table and the usage line, separated by blank lines."""
    if show_digest:
        digest = result.digest
        if digest is None or not digest.item_ids:
            reason = (
                "the run failed"
                if result.status is RunStatus.FAILED
                else "nothing relevant was found"
            )
            typer.echo(f"No digest: {reason}.")
        else:
            typer.echo(f"# {strip_control(digest.title)}\n")
            typer.echo(strip_control(digest.body, multiline=True))
        typer.echo()
    notifications = (
        None if result.dry_run else (result.notifications_sent, result.notifications_failed)
    )
    stdout_console().print(
        stats_block(
            run_id=result.run_id,
            status=result.status,
            dry_run=result.dry_run,
            stats=result.stats,
            started_at=result.started_at,
            finished_at=result.finished_at,
            notifications=notifications,
        )
    )


@app.command(_RUN)
def run(
    name: Annotated[str, typer.Argument(help="Job name.")],
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Send nothing and keep only the run row; print the digest."),
    ] = False,
    max_items: Annotated[
        int | None,
        typer.Option(
            "--max-items", help="Process at most N items (never more than the job limit)."
        ),
    ] = None,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="One stderr line per item; print the digest too."),
    ] = False,
) -> None:
    """Run a job once, now.

    Progress goes to stderr; the digest (dry run or --verbose), the statistics and the token
    and cost line go to stdout.

    Exit codes: 0 succeeded, 1 failed or could not start, 2 partial.

    Usage errors (an unknown option, a non-numeric --max-items) also exit 2.
    """
    if max_items is not None and max_items < 1:
        raise fail("Error: --max-items must be at least 1", 1)
    with _run_errors():
        record = _make_service().get_by_name(name)
        if not record.enabled:
            raise _disabled(name)
        limit = record.config.limits.max_items_per_run
        if max_items is not None and max_items > limit:
            typer.echo(
                f"note: --max-items {max_items} exceeds the job limit {limit}; using {limit}",
                err=True,
            )
        view = RunProgressView(stderr_console(), verbose=verbose)
        try:
            with view:  # stopped before anything else is printed, also on Ctrl-C
                result = _execute(_run_once(name, dry_run=dry_run, max_items=max_items, view=view))
        except JobDisabledError as exc:  # disabled between the check above and the claim
            raise _disabled(name) from exc
        except JobBusyError as exc:
            when = format_time(exc.locked_until, record.config.schedule.timezone)
            raise fail(f"Error: job '{name}' is running (locked until {when})", 1) from exc
    _print_result(result, show_digest=result.dry_run or verbose)
    if result.status is RunStatus.FAILED:
        reason = result.error or (result.errors[0].error_class if result.errors else "run failed")
        raise fail(f"run {result.run_id} failed: {strip_control(reason)}", 1)
    if result.status is RunStatus.PARTIAL:
        raise fail(
            f"run {result.run_id} finished partial: {len(result.errors)} error(s); "
            f"see 'invio run show {result.run_id}'",
            2,
        )
