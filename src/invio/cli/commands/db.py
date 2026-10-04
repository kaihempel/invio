"""``invio db``: database schema management."""

import logging
from typing import Annotated

import typer
from sqlalchemy.engine import URL
from sqlalchemy.exc import ArgumentError

from invio.cli.errors import fail
from invio.config.settings import MissingSettingError, get_settings
from invio.db import migrate
from invio.db.session import normalize_url, redact

logger = logging.getLogger(__name__)

app = typer.Typer(help="Database schema management.", no_args_is_help=True)


@app.command()
def upgrade(
    revision: Annotated[str, typer.Argument(help="Target revision (default: head).")] = "head",
) -> None:
    """Apply pending schema migrations to the database in INVIO_DATABASE_URL.

    DDL is not transactional on MariaDB/MySQL: a failed upgrade may leave partially applied
    changes that need manual cleanup.
    """
    raw: str | None = None
    url: URL | None = None
    try:
        raw = get_settings().require_secret("database_url")
        url = normalize_url(raw)
        # Unknown dialect/driver -> ArgumentError (NoSuchModuleError); uninstalled DBAPI module
        # -> ImportError. Both are configuration errors, so check them before connecting.
        url.get_dialect().import_dbapi()
    except MissingSettingError as exc:
        raise fail(f"Configuration error: {exc}", 2) from exc
    except ArgumentError as exc:
        message = redact(str(exc), raw or "")
        raise fail(f"Configuration error: invalid database URL: {message}", 2) from exc
    except ImportError as exc:
        message = redact(str(exc), raw or "")
        raise fail(f"Configuration error: database driver not installed: {message}", 2) from exc

    def scrub(text: str) -> str:
        return redact(redact(text, raw), url)

    try:
        logger.info("migration started", extra={"revision": revision})
        reached = migrate.upgrade(migrate.alembic_config(url=url), revision)
    except Exception as exc:  # never show a traceback (it may embed the URL)
        # SQLAlchemy wraps the driver error in ``orig``; its message is the useful part.
        detail = getattr(exc, "orig", None) or exc
        raise fail(f"Error: {type(exc).__name__}: {scrub(str(detail))}", 1) from exc
    logger.info("migration finished", extra={"revision": reached})
    typer.echo(f"database at revision {reached}")
