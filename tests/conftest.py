"""Shared pytest fixtures."""

import logging
import os
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from invio.config.settings import get_settings
from invio.db import migrate
from invio.db.models import Base, Job
from invio.db.session import create_db_engine, session_factory, session_scope
from tests.db_helpers import TEST_DATABASE_URL, uses_sqlite

if TYPE_CHECKING:
    from invio.config.job import ScheduleConfig
    from invio.services.jobs import JobService


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


@pytest.fixture
def clean_jobs(db_engine: Engine) -> Iterator[None]:
    """Delete every job (and, by cascade, its history) at teardown.

    For tests that commit through ``session_scope``: the MariaDB engine is shared between tests.
    """
    yield
    with session_scope(session_factory(db_engine)) as session:
        for job in session.scalars(select(Job)).all():
            session.delete(job)


class FakeClock:
    """A settable clock: call it for ``now``; ``advance`` moves it forward."""

    def __init__(self) -> None:
        self.now = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def next_run_calls() -> "list[tuple[ScheduleConfig, datetime]]":
    return []


@pytest.fixture
def recording_next_run(
    next_run_calls: "list[tuple[ScheduleConfig, datetime]]",
) -> "Callable[[ScheduleConfig, datetime], datetime]":
    def _next_run(schedule: "ScheduleConfig", after: datetime) -> datetime:
        next_run_calls.append((schedule, after))
        return after + timedelta(hours=1)

    return _next_run


@pytest.fixture
def job_service(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: "Callable[[ScheduleConfig, datetime], datetime]",
    clean_jobs: None,
) -> "JobService":
    """A ``JobService`` on the test engine; jobs are deleted at teardown by ``clean_jobs``."""
    from invio.services.jobs import JobService

    return JobService(session_factory(db_engine), next_run=recording_next_run, clock=fake_clock)


@pytest.fixture
def llm_registry_dir(tmp_path: Path, job_data: dict[str, Any]) -> Path:
    """A ``models.d`` directory declaring the ``job_data`` models as ``mistral`` models.

    Also switches ``job_data`` to the ``mistral`` provider.
    """
    from tests.llm_helpers import write_registry

    job_data["llm"]["provider"] = "mistral"
    entry: dict[str, object] = {
        "input_price_per_mtok": 1,
        "output_price_per_mtok": 2,
        "context_window": 1000,
    }
    models = {model: entry for model in job_data["llm"]["models"].values()}
    return write_registry(tmp_path / "registry", "mistral", models)


@pytest.fixture
def patched_providers(monkeypatch: pytest.MonkeyPatch) -> Callable[[str, Any], None]:
    """Isolate the provider registry; returns ``register(name, provider_instance)``.

    Discovery is marked as done, so only providers registered through the helper exist.
    """
    from invio.llm import factory

    monkeypatch.setattr(factory, "_REGISTRY", {})
    monkeypatch.setattr(factory, "_discovered", True)

    def register(name: str, provider: Any) -> None:
        class _Registered:
            @classmethod
            def from_settings(cls, settings: Any) -> Any:
                return provider

        factory.register_provider(name)(_Registered)

    return register
