"""Process-wide CLI runtime setup: JSON logging and the check for ignored ``INVIO_*`` keys.

It lives outside ``invio.cli.main`` so a command group can run it itself with its own exit
code (``invio job run`` exits 1 on a configuration error, because 2 means ``partial`` there).
"""

import logging

import typer
from pydantic import ValidationError

from invio.config.settings import unknown_env_keys
from invio.log import configure_logging

__all__ = ["settings_error", "setup_runtime"]

logger = logging.getLogger("invio.cli.main")


def settings_error(exc: ValidationError) -> str:
    """The invalid settings with their reasons, never the offending input values."""
    problems = [
        f"{'.'.join(str(p) for p in error['loc']) or '(root)'}: {error['msg']}"
        for error in exc.errors(include_url=False, include_input=False, include_context=False)
    ]
    return f"invalid settings: {'; '.join(problems)}"


def setup_runtime(*, config_exit: int = 2) -> None:
    """Configure JSON logging and warn about ignored ``INVIO_*`` keys.

    Configuration errors (bad ``INVIO_LOG_LEVEL``, an invalid setting, missing
    ``INVIO_ENV_FILE``) end the CLI with ``config_exit`` and a one-line message instead of a
    traceback.
    """
    try:
        configure_logging()
        unknown = unknown_env_keys()
    except ValidationError as exc:
        typer.echo(f"Configuration error: {settings_error(exc)}", err=True)
        raise typer.Exit(code=config_exit) from exc
    except (ValueError, OSError) as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=config_exit) from exc
    for key in unknown:
        logger.warning("unknown setting ignored", extra={"key": key})
