"""``invio run``: inspect the history of job runs (contract: specs/015-gh-issue-22).

Results go to stdout; errors go to stderr. Exit codes: 0 success, 1 not found / database error /
invalid ``--limit``, 2 missing settings or invalid stored configuration or usage. The
module-level ``_make_service`` is a test seam.
"""

from typing import Annotated

import typer
from rich.console import Console

from invio.cli.errors import fail, mapped_errors
from invio.cli.run_output import (
    DASH,
    format_cost,
    format_duration,
    format_errors,
    format_time,
    format_tokens,
    runs_table,
    stats_rows,
    stats_table,
)
from invio.domain import RunStatus
from invio.services.runs import RunService
from invio.textsafe import strip_control

app = typer.Typer(help="Inspect job runs.", no_args_is_help=True)

_PIPE_WIDTH = 1_000


def _make_service() -> RunService:
    return RunService.from_settings()


def _console() -> Console:
    console = Console(markup=False, highlight=False, emoji=False)
    if not console.is_terminal:
        # Piped output defaults to 80 columns; never wrap a row for grep/awk.
        console = Console(markup=False, highlight=False, emoji=False, width=_PIPE_WIDTH)
    return console


@app.command("list")
def list_runs(
    job: Annotated[str | None, typer.Option("--job", help="Only the runs of this job.")] = None,
    limit: Annotated[int, typer.Option("--limit", help="Show at most this many runs.")] = 20,
) -> None:
    """List runs, newest first."""
    if limit < 1:
        raise fail("Error: --limit must be at least 1", 1)
    with mapped_errors(config_exit=2):
        summaries = _make_service().list(job=job, limit=limit)
    if not summaries:
        typer.echo("No runs." if job is None else f"No runs for job '{strip_control(job)}'.")
        return
    _console().print(runs_table(summaries))


@app.command()
def show(run_id: Annotated[int, typer.Argument(help="Run id.")]) -> None:
    """Show one run: statistics, token usage and every stored error."""
    with mapped_errors(config_exit=2):
        detail = _make_service().get(run_id)
    status = detail.status.value + (" (dry run)" if detail.dry_run else "")
    zone = detail.timezone
    typer.echo(f"Run {detail.id} · job {strip_control(detail.job_name)} · {status}")
    typer.echo(
        f"Started   {format_time(detail.started_at, zone)}   "
        f"Finished {format_time(detail.finished_at, zone)}   "
        f"Duration {format_duration(detail.started_at, detail.finished_at)}"
    )
    typer.echo(f"Error     {strip_control(detail.error) if detail.error else DASH}")
    if detail.status is not RunStatus.RUNNING:
        typer.echo()
        rows = stats_rows(
            run_id=detail.id,
            status=detail.status,
            dry_run=detail.dry_run,
            stats=detail.stats,
            started_at=detail.started_at,
            finished_at=detail.finished_at,
            notifications=None,
        )
        _console().print(stats_table(rows))
        typer.echo()
        typer.echo(f"Tokens: {format_tokens(detail.stats)} · cost {format_cost(detail.stats)}")
    typer.echo()
    for line in format_errors(detail.errors, detail.errors_omitted):
        typer.echo(line)
