"""``invio notify``: e-mail notifications (contract: specs/011-gh-issue-20)."""

import asyncio

import typer
from pydantic import ValidationError

from invio.cli.errors import fail
from invio.config.settings import MissingSettingError, Settings, get_settings
from invio.domain import NotificationStatus
from invio.notify import (
    DatabaseConfigError,
    RetryOutcome,
    RetryResult,
    missing_smtp_settings,
    open_session_factory,
    retry_failed,
    scrub_error,
)

app = typer.Typer(help="E-mail notifications.", no_args_is_help=True)


def _line(result: RetryResult) -> str:
    """Format one processed notification: label, id, job, recipient and (on failure) error."""
    if result.status is NotificationStatus.SENT:
        label = "sent"
    elif result.status is NotificationStatus.SKIPPED:
        label = "given up"
    else:
        label = "failed"
    line = f"{label:<9} #{result.notification_id} {result.job_name} {result.recipient}"
    if result.status is not NotificationStatus.SENT and result.error:
        line += f"  {result.error}"
    return line


def _report(outcome: RetryOutcome) -> None:
    if outcome.retried == 0:
        typer.echo("nothing to retry")
        return
    for result in outcome.results:
        typer.echo(_line(result))
    typer.echo(
        f"retried {outcome.retried}, sent {outcome.sent}, "
        f"failed {outcome.failed}, given up {outcome.given_up}"
    )


@app.command()
def retry() -> None:
    """Re-send failed notifications and notifications interrupted more than an hour ago.

    Exit codes: 0 when nothing failed, 1 when a notification failed or was given up after five
    attempts, 2 for a configuration error (database URL, SMTP host or sender).
    """
    try:
        settings: Settings = get_settings()
    except (ValidationError, ValueError) as exc:
        raise fail(f"Configuration error: {exc}", 2) from exc
    try:
        factory = open_session_factory(settings)
    except (MissingSettingError, DatabaseConfigError) as exc:
        raise fail(f"Configuration error: {exc}", 2) from exc
    missing = missing_smtp_settings(settings)
    if missing:
        raise fail(f"Configuration error: smtp not configured: missing {missing[0]}", 2)

    try:
        outcome = asyncio.run(retry_failed(factory, settings=settings))
    except Exception as exc:  # never show a traceback (it may embed credentials)
        detail = getattr(exc, "orig", None) or exc
        raise fail(f"Error: {type(exc).__name__}: {scrub_error(str(detail), settings)}", 1) from exc
    _report(outcome)
    if outcome.failed + outcome.given_up > 0:
        raise typer.Exit(code=1)
