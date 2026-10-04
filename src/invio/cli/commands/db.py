"""``invio db``: database schema management."""

import logging
from typing import Annotated

import typer
from alembic.util import CommandError
from sqlalchemy.engine import URL
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from invio.config.settings import MissingSettingError, get_settings
from invio.db import migrate
from invio.db.session import normalize_url, redact

logger = logging.getLogger(__name__)

app = typer.Typer(help="Database schema management.", no_args_is_help=True)


def _fail(message: str, code: int) -> typer.Exit:
    typer.echo(message, err=True)
    return typer.Exit(code=code)


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
        url.get_dialect()  # unknown dialect/driver -> ArgumentError (NoSuchModuleError)
    except MissingSettingError as exc:
        raise _fail(f"Configuration error: {exc}", 2) from exc
    except ArgumentError as exc:
        message = redact(str(exc), raw or "")
        raise _fail(f"Configuration error: invalid database URL: {message}", 2) from exc

    def scrub(text: str) -> str:
        return redact(redact(text, raw), url)

    try:
        logger.info("migration started", extra={"revision": revision})
        reached = migrate.upgrade(migrate.alembic_config(url=url), revision)
    except (SQLAlchemyError, CommandError, OSError) as exc:
        detail = getattr(exc, "orig", None) or exc
        raise _fail(f"Error: {type(exc).__name__}: {scrub(str(detail))}", 1) from exc
    except Exception as exc:  # last resort: never show a traceback (it may embed the URL)
        raise _fail(f"Error: {type(exc).__name__}: {scrub(str(exc))}", 1) from exc
    logger.info("migration finished", extra={"revision": reached})
    typer.echo(f"database at revision {reached}")
