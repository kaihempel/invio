"""Shared pytest fixtures."""

import logging
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invio.config.settings import get_settings
from invio.db import migrate
from invio.db.models import Base
from invio.db.session import create_db_engine
from tests.db_helpers import TEST_DATABASE_URL, uses_sqlite


@pytest.fixture(autouse=True)
def isolated_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the developer's real ``.env`` and ``INVIO_*`` variables out of every test."""
    for key in list(os.environ):
        if key.startswith("INVIO_"):
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def restore_root_logger() -> Iterator[logging.Logger]:
    """Snapshot the root logger's handlers and level and restore them after the test.

    ``configure_logging()`` (also run by every CLI sub-command) mutates the root logger.
    """
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield root
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(level)


@pytest.fixture
def job_data() -> dict[str, Any]:
    """Return a fresh, minimal valid job mapping (tests mutate it freely)."""
    return {
        "schedule": {
            "frequency": "weekly",
            "time": "07:30",
            "weekday": "monday",
            "timezone": "Europe/Berlin",
        },
        "notification": {"to": ["research@example.com"], "subject": "invio digest"},
        "sources": [{"type": "rss", "url": "https://example.com/feed.xml"}],
        "search": {"semantic_description": "LLM agent frameworks"},
        "llm": {"provider": "openai", "models": {"fast": "gpt-small", "smart": "gpt-large"}},
    }


@pytest.fixture(scope="session")
def _server_engine() -> Iterator[Engine | None]:
    """Engine for the MariaDB/MySQL test server, upgraded to head once; ``None`` on SQLite."""
    if uses_sqlite():
        yield None
        return
    assert TEST_DATABASE_URL is not None
    engine = create_db_engine(TEST_DATABASE_URL)
    # Reset stale state from an aborted earlier run before bringing the schema to head.
    with engine.begin() as conn:
        migrate.downgrade(migrate.alembic_config(connection=conn), "base")
        conn.execute(text("DROP TABLE IF EXISTS other"))
    with engine.begin() as conn:
        migrate.upgrade(migrate.alembic_config(connection=conn), "head")
    yield engine
    engine.dispose()


@pytest.fixture
def db_engine(_server_engine: Engine | None) -> Iterator[Engine]:
    """SQLite: a fresh in-memory engine per test. MariaDB: the shared server engine."""
    if _server_engine is not None:
        yield _server_engine
        return
    engine = create_db_engine(
        "sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine, _server_engine: Engine | None) -> Iterator[Session]:
    """A session isolated per test (closed on SQLite, rolled back via savepoints on MariaDB)."""
    if _server_engine is None:
        with Session(db_engine) as session:
            yield session
        return
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
