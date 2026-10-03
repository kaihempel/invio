"""Sub-commands of the ``scout`` CLI.

Every module in this package that exposes a module-level ``typer.Typer`` instance named
``app`` is registered automatically as ``scout <module-name>`` (underscores in the module
name become dashes). Modules whose name starts with ``_`` are ignored.
"""
