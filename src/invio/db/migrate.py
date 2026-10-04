"""Programmatic Alembic access used by the CLI and the tests.

Note: DDL is not transactional on MariaDB/MySQL, so a failed upgrade there can leave partially
applied changes behind that need manual cleanup.
"""

from collections.abc import Callable

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy.engine import URL, Connection

from invio.config.settings import get_settings
from invio.db.session import create_db_engine

MIGRATIONS = "invio.db:migrations"


def alembic_config(*, url: str | URL | None = None, connection: Connection | None = None) -> Config:
    """Return a Config for the packaged migrations.

    ``url`` / ``connection`` go into ``config.attributes`` only, never into the
    ``sqlalchemy.url`` option (Alembic may log options, and the URL carries the password).
    """
    config = Config()
    config.set_main_option("script_location", MIGRATIONS)
    if url is not None:
        config.attributes["url"] = url
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def current_revision(connection: Connection) -> str | None:
    """Return the revision the database is at, or ``None`` if unversioned."""
    return MigrationContext.configure(connection).get_current_revision()


def _run(config: Config, action: Callable[[], None]) -> str | None:
    """Run ``action`` on the config's connection, or on a new engine for its URL/setting."""
    connection = config.attributes.get("connection")
    if isinstance(connection, Connection):
        action()
        return current_revision(connection)
    url = config.attributes.get("url") or get_settings().require_secret("database_url")
    engine = create_db_engine(url)
    try:
        with engine.begin() as new_connection:
            config.attributes["connection"] = new_connection
            try:
                action()
            finally:
                del config.attributes["connection"]
            return current_revision(new_connection)
    finally:
        engine.dispose()


def upgrade(config: Config, revision: str = "head") -> str:
    """Upgrade to ``revision`` and return the resulting revision (``"base"`` if none)."""
    return _run(config, lambda: command.upgrade(config, revision)) or "base"


def downgrade(config: Config, revision: str = "base") -> None:
    """Downgrade to ``revision``."""
    _run(config, lambda: command.downgrade(config, revision))
