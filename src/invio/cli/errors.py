"""Shared error output for CLI commands."""

import contextlib
from collections.abc import Iterator

import typer
from sqlalchemy.exc import SQLAlchemyError

from invio.config.job import JobConfigError
from invio.config.settings import MissingSettingError
from invio.llm.base import LLMError, ModelRegistryError
from invio.notify import DatabaseConfigError
from invio.services.jobs import JobNotFoundError
from invio.services.runs import RunNotFoundError
from invio.textsafe import strip_control

__all__ = ["fail", "format_llm_error", "mapped_errors"]


def fail(message: str, code: int) -> typer.Exit:
    """Print ``message`` to stderr and return the ``Exit`` to raise (``raise fail(...)``)."""
    typer.echo(message, err=True)
    return typer.Exit(code=code)


def format_llm_error(
    exc: LLMError, *, provider: str | None = None, model: str | None = None
) -> str:
    """Return ``Error: [<provider>/<model>: ]<ErrorClass>: <message>`` (message made printable).

    The ``<provider>/<model>`` part is only written when both are given.
    """
    where = f"{provider}/{model}: " if provider is not None and model is not None else ""
    return f"Error: {where}{type(exc).__name__}: {strip_control(str(exc))}"


@contextlib.contextmanager
def mapped_errors(*, config_exit: int) -> Iterator[None]:
    """Map the errors every job and run command shares to one stderr line and an exit code.

    Only the named types are caught (``typer.Exit`` is a ``RuntimeError``, so nothing broader
    is safe). Not found and database errors exit 1. A configuration problem (an invalid stored
    job config, a missing setting, an unusable database URL, an unreadable model registry) exits
    with ``config_exit``: 2 for the read-only commands, 1 for ``invio job run``, where "could not
    start" is one exit code (contract: cli-commands.md).
    """
    try:
        yield
    except (JobNotFoundError, RunNotFoundError) as exc:
        raise fail(f"Error: {exc}", 1) from exc
    except JobConfigError as exc:  # includes StoredJobConfigError
        raise fail(str(exc), config_exit) from exc
    except (MissingSettingError, DatabaseConfigError, ModelRegistryError) as exc:
        raise fail(f"Configuration error: {exc}", config_exit) from exc
    except SQLAlchemyError as exc:
        # Deliberately only the type name: the message may embed the database URL or SQL.
        raise fail(
            f"Error: database error ({type(exc).__name__}); "
            "is the schema current? run 'invio db upgrade'",
            1,
        ) from exc
