"""The ``db`` fixtures isolate tests and pick the engine from ``INVIO_TEST_DATABASE_URL``."""

import os

import pytest
from sqlalchemy import Engine, make_url
from sqlalchemy.orm import Session

from tests.db_helpers import TEST_DATABASE_URL, make_job, uses_sqlite

pytestmark = pytest.mark.db


# Runs twice: the second run fails on the unique job name if the first one's row leaked.
@pytest.mark.parametrize("_run", [1, 2])
def test_each_test_starts_without_leftover_rows(db_session: Session, _run: int) -> None:
    job = make_job(db_session, "iso")

    assert job.id is not None


def test_engine_dialect_follows_test_database_url(db_engine: Engine) -> None:
    if uses_sqlite():
        assert db_engine.dialect.name == "sqlite"
    else:
        assert TEST_DATABASE_URL is not None
        assert db_engine.dialect.name == make_url(TEST_DATABASE_URL).get_backend_name()


@pytest.mark.skipif(not uses_sqlite(), reason="server URL configured")
def test_default_engine_is_in_memory(db_engine: Engine) -> None:
    assert db_engine.url.database in (None, "", ":memory:")


def test_settings_isolation_still_hides_the_test_url() -> None:
    assert "INVIO_TEST_DATABASE_URL" not in os.environ
