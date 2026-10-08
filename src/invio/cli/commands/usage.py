"""``invio usage``: token usage and cost per model, provider, job or day (specs/018-gh-issue-35).

The report goes to stdout (a table, or one JSON document with ``--json``); errors and the
unpriced-model warning go to stderr. Exit codes: 0 success (also when there is no usage),
1 unknown job / database error, 2 invalid options, missing settings or an unreadable model
registry. The module-level ``_make_service`` and ``_make_registry`` are test seams.
"""

import re
from datetime import date
from typing import Annotated, Final

import typer

from invio.cli.console import stdout_console
from invio.cli.errors import fail, mapped_errors
from invio.cli.usage_output import unpriced_warning, usage_json, usage_table
from invio.llm.base import ModelRegistryError
from invio.llm.registry import ModelRegistry, default_registry
from invio.services.usage import UsageGrouping, UsageService

app = typer.Typer(help="Report LLM token usage and cost.", no_args_is_help=False)

_DAY: Final = re.compile(r"\d{4}-\d{2}-\d{2}")


def _make_service() -> UsageService:
    return UsageService.from_settings()


def _make_registry() -> ModelRegistry:
    return default_registry()


def _parse_since(value: str | None) -> date | None:
    """``YYYY-MM-DD`` as a date; anything else is a usage error (exit 2)."""
    if value is None:
        return None
    try:
        if _DAY.fullmatch(value):
            return date.fromisoformat(value)
    except ValueError:
        pass
    raise typer.BadParameter(f"'{value}' is not a date (YYYY-MM-DD)", param_hint="'--since'")


@app.callback(invoke_without_command=True)
def usage_command(
    job: Annotated[str | None, typer.Option("--job", help="Only the usage of this job.")] = None,
    since: Annotated[
        str | None,
        typer.Option(
            "--since", help="Only usage from this UTC day on (YYYY-MM-DD).", metavar="DATE"
        ),
    ] = None,
    by: Annotated[
        UsageGrouping, typer.Option("--by", help="One row per provider, model, job or UTC day.")
    ] = UsageGrouping.MODEL,
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON document.")] = False,
) -> None:
    """Show token usage and cost, priced with the current model registry."""
    start = _parse_since(since)
    try:
        registry = _make_registry()
    except ModelRegistryError as exc:
        raise fail(f"Configuration error: {exc}", 2) from exc
    with mapped_errors(config_exit=2):
        report = _make_service().report(registry, by=by, job=job, since=start)
    warning = unpriced_warning(report)
    if warning is not None:
        typer.echo(warning, err=True)
    if as_json:
        typer.echo(usage_json(report))
    elif not report.groups:
        typer.echo("No usage.")
    else:
        stdout_console().print(usage_table(report))
