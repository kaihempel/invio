"""Alembic environment.

The connection is resolved in this order: ``config.attributes["connection"]`` (tests),
``config.attributes["url"]`` (CLI), then ``INVIO_DATABASE_URL`` from the settings (plain
``alembic`` commands). The URL is never written into the Alembic config, and Alembic's
``fileConfig`` logging setup is skipped so invio's JSON logging stays in place.

Note for future migrations: batch mode on SQLite recreates tables; with ``foreign_keys=ON`` the
drop of the old table cascades to child rows, so such migrations must copy children first.
"""

from alembic import context
from sqlalchemy import Engine
from sqlalchemy.engine import URL, Connection

from invio.config.settings import get_settings
from invio.db.models import Base
from invio.db.session import create_db_engine, normalize_url

config = context.config
target_metadata = Base.metadata


def _attribute_connection() -> Connection | None:
    value = config.attributes.get("connection")
    return value if isinstance(value, Connection) else None


def _resolve_url() -> str | URL:
    connection = _attribute_connection()
    if connection is not None:
        return connection.engine.url
    url = config.attributes.get("url")
    if isinstance(url, str | URL):
        return url
    return get_settings().require_secret("database_url")


def _configure_and_run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_offline() -> None:
    """Render SQL to the output buffer without connecting."""
    context.configure(
        url=normalize_url(_resolve_url()),
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations on the provided connection or on a new engine."""
    connection = _attribute_connection()
    if connection is not None:
        _configure_and_run(connection)
        return
    engine: Engine = create_db_engine(_resolve_url())
    try:
        with engine.connect() as new_connection:
            _configure_and_run(new_connection)
    finally:
        engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
