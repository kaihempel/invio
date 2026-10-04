"""Entry point of the ``invio`` CLI with automatic sub-command discovery."""

import importlib
import logging
import pkgutil
from types import ModuleType
from typing import Annotated

import typer

from invio import __version__
from invio.cli import commands
from invio.config.settings import unknown_env_keys
from invio.log import configure_logging

logger = logging.getLogger(__name__)


def discover_commands(app: typer.Typer, package: ModuleType) -> list[str]:
    """Register every sub-module of ``package`` that exposes a Typer ``app``.

    Modules whose name starts with ``_`` and modules without a ``typer.Typer`` named ``app``
    are skipped. The command name is the module name with ``_`` replaced by ``-``.

    Returns the names of the registered commands.
    """
    registered: list[str] = []
    for module_info in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
        if module_info.name.startswith("_"):
            continue
        module = importlib.import_module(f"{package.__name__}.{module_info.name}")
        sub_app = getattr(module, "app", None)
        if not isinstance(sub_app, typer.Typer):
            continue
        command_name = module_info.name.replace("_", "-")
        app.add_typer(sub_app, name=command_name)
        registered.append(command_name)
    return registered


def _setup_runtime() -> None:
    """Configure JSON logging and warn about ignored ``INVIO_*`` keys.

    Configuration errors (bad ``INVIO_LOG_LEVEL``, missing ``INVIO_ENV_FILE``) end the CLI
    with exit code 2 and a one-line message instead of a traceback.
    """
    try:
        configure_logging()
        unknown = unknown_env_keys()
    except (ValueError, OSError) as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=2) from exc
    for key in unknown:
        logger.warning("unknown setting ignored", extra={"key": key})


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"invio {__version__}")
        raise typer.Exit()


def create_app(package: ModuleType = commands) -> typer.Typer:
    """Build the root Typer app and register all sub-commands found in ``package``."""
    root = typer.Typer(
        name="invio",
        help="Invio: AI research system.",
        no_args_is_help=True,
        pretty_exceptions_show_locals=False,  # locals may hold the database URL
    )

    @root.callback()
    def main(
        version: Annotated[
            bool,
            typer.Option(
                "--version",
                "-V",
                help="Show the version and exit.",
                callback=_version_callback,
                is_eager=True,
            ),
        ] = False,
    ) -> None:
        """Invio: AI research system."""
        _setup_runtime()

    discover_commands(root, package)
    return root


app = create_app()

if __name__ == "__main__":  # pragma: no cover
    app()
