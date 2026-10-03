"""Entry point of the ``scout`` CLI with automatic sub-command discovery."""

import importlib
import pkgutil
from types import ModuleType
from typing import Annotated

import typer

from scout import __version__
from scout.cli import commands


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


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"scout {__version__}")
        raise typer.Exit()


def create_app(package: ModuleType = commands) -> typer.Typer:
    """Build the root Typer app and register all sub-commands found in ``package``."""
    root = typer.Typer(name="scout", help="Scout: AI research system.", no_args_is_help=True)

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
        """Scout: AI research system."""

    discover_commands(root, package)
    return root


app = create_app()

if __name__ == "__main__":  # pragma: no cover
    app()
