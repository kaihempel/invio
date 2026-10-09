"""``invio run-due``: run every due job (contract: specs/016-gh-issue-23).

Meant for a systemd timer: results go to stdout, structured logs to stderr. Exit codes: 0 no run
failed (including nothing due, busy and partial), 1 a run failed or the invocation aborted,
2 invalid options or configuration. The module-level ``_run_due`` and ``_print_report`` are
test seams.
"""

import asyncio
import contextlib
import logging
from collections.abc import Iterator
from typing import Annotated

import typer
from pydantic import ValidationError

from invio.cli.errors import fail, mapped_errors
from invio.cli.run_output import DASH, format_time
from invio.cli.runtime import settings_error
from invio.config.settings import get_settings
from invio.domain import RunStatus
from invio.pipeline import healthcheck
from invio.pipeline.due import DueOutcome, DueReport, run_due
from invio.textsafe import strip_control

logger = logging.getLogger("invio.cli.run_due")

app = typer.Typer(help="Run every job that is due (for a systemd timer).", no_args_is_help=False)

_KIND_WIDTH = len("skipped")
_STATUS_WIDTH = len(RunStatus.SUCCEEDED.value)


def _run_due(limit: int | None, parallel: int) -> DueReport:
    """Run the due jobs on a fresh event loop; tests replace this."""
    return asyncio.run(run_due(limit=limit, parallel=parallel))


def _ping(url: str, failed: bool) -> None:
    """Send the health-check signal (never raises); tests replace this."""
    healthcheck.ping(url, failed=failed)


def _healthcheck_url() -> str | None:
    """The configured health-check URL, or ``None`` (unset, or the settings are unreadable)."""
    try:
        secret = get_settings().healthcheck_url
    except Exception:  # no signal without readable settings; setup_runtime reports the problem
        return None
    return secret.get_secret_value() if secret is not None else None


@contextlib.contextmanager
def _due_errors() -> Iterator[None]:
    """Everything that can stop ``run-due``: one stderr line and exit 1 (aborted) or 2 (config)."""
    try:
        with mapped_errors(config_exit=2):
            yield
    except ValidationError as exc:
        raise fail(f"Configuration error: {settings_error(exc)}", 2) from exc
    except KeyboardInterrupt as exc:
        raise fail("interrupted; started runs are recorded as failed", 1) from exc


@app.callback(invoke_without_command=True)
def run_due_command(
    limit: Annotated[
        int | None,
        typer.Option("--limit", help="Start at most this many due jobs (busy ones do not count)."),
    ] = None,
    parallel: Annotated[
        int,
        typer.Option(
            "--parallel",
            help="Run up to this many jobs at the same time. Multiplies with "
            "INVIO_MAX_PARALLEL_ITEMS (concurrent items per run).",
        ),
    ] = 1,
) -> None:
    """Run every enabled job whose next run time has passed."""
    if limit is not None and limit < 1:
        raise fail("Error: --limit must be at least 1", 2)
    if parallel < 1:
        raise fail("Error: --parallel must be at least 1", 2)
    url = _healthcheck_url()
    code = 1  # an exception that escapes below is an aborted invocation
    try:
        with _due_errors():
            report = _run_due(limit, parallel)
        _print_report(report)
        code = report.exit_code
    except typer.Exit as exit_:
        code = exit_.exit_code
        raise
    except Exception as err:
        logger.error("run_due.aborted", extra={"error": type(err).__name__})
        raise
    finally:
        # Exactly one signal, after all runs have finished and the report is printed.
        if url is not None:
            _ping(url, failed=code != 0)
    if code:
        raise typer.Exit(code)


def _print_report(report: DueReport) -> None:
    """One line per due job in due order, then the summary; ``nothing due`` when empty."""
    if not report.outcomes:
        typer.echo("nothing due")
        return
    width = max(len(strip_control(o.job_name)) for o in report.outcomes)
    for outcome in report.outcomes:
        name = strip_control(outcome.job_name).ljust(width)
        typer.echo(f"{outcome.kind:<{_KIND_WIDTH}}  {name}  {_detail(outcome)}".rstrip())
    typer.echo(_summary(report))


def _detail(outcome: DueOutcome) -> str:
    match outcome.kind:
        case "ran":
            status = outcome.status.value if outcome.status is not None else DASH
            next_run = format_time(outcome.next_run_at, outcome.timezone)
            suffix = " (retry)" if outcome.retry else ""
            return f"run {outcome.run_id}  {status:<{_STATUS_WIDTH}}  next {next_run}{suffix}"
        case "busy":
            return f"locked until {format_time(outcome.locked_until, outcome.timezone)}"
        case _:  # skipped and error: the reason is a short text or only an exception class
            return strip_control(outcome.reason or "")


def _summary(report: DueReport) -> str:
    ran = [o for o in report.outcomes if o.kind == "ran"]

    def count(kind: str) -> int:
        return sum(o.kind == kind for o in report.outcomes)

    def with_status(status: RunStatus) -> int:
        return sum(o.status is status for o in ran)

    parts = [
        f"due {report.due}",
        f"ran {len(ran)} ({with_status(RunStatus.SUCCEEDED)} succeeded, "
        f"{with_status(RunStatus.PARTIAL)} partial, {with_status(RunStatus.FAILED)} failed)",
        f"busy {count('busy')}",
        f"skipped {count('skipped')}",
    ]
    if count("error"):
        parts.append(f"error {count('error')}")
    if report.deferred:
        parts.append(f"deferred {report.deferred} (--limit)")
    return " · ".join(parts)
