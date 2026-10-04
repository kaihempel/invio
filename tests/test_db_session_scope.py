"""Tests for the ``session_scope`` unit of work."""

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from invio.db.models import Item, Job
from invio.db.repositories import ItemRepository, JobRepository
from invio.db.session import session_factory, session_scope
from tests.db_helpers import make_candidate

pytestmark = pytest.mark.db


def _names(engine: Engine) -> list[str]:
    with Session(engine) as session:
        return list(session.scalars(select(Job.name)))


@pytest.mark.usefixtures("clean_jobs")
def test_commits_on_success(db_engine: Engine) -> None:
    factory = session_factory(db_engine)
    with session_scope(factory) as session:
        session.add(Job(name="kept", config={}))
        held = session
    assert _names(db_engine) == ["kept"]
    assert held.in_transaction() is False


@pytest.mark.usefixtures("clean_jobs")
def test_rolls_back_and_reraises(db_engine: Engine) -> None:
    factory = session_factory(db_engine)
    with pytest.raises(RuntimeError, match="boom"), session_scope(factory) as session:
        session.add(Job(name="lost", config={}))
        session.flush()
        raise RuntimeError("boom")
    assert _names(db_engine) == []
    assert session.in_transaction() is False


@pytest.mark.usefixtures("clean_jobs")
def test_commit_failure_rolls_back_and_closes(
    db_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = session_factory(db_engine)
    closed: list[bool] = []
    original_close = Session.close

    def failing_commit(self: Session) -> None:
        raise RuntimeError("commit failed")

    def spy_close(self: Session) -> None:
        closed.append(True)
        original_close(self)

    monkeypatch.setattr(Session, "commit", failing_commit)
    monkeypatch.setattr(Session, "close", spy_close)
    with pytest.raises(RuntimeError, match="commit failed"), session_scope(factory) as session:
        session.add(Job(name="lost", config={}))
    assert closed == [True]
    monkeypatch.undo()
    assert _names(db_engine) == []


@pytest.mark.usefixtures("clean_jobs")
def test_attributes_readable_after_scope(db_engine: Engine) -> None:
    factory = session_factory(db_engine)
    with session_scope(factory) as session:
        job = Job(name="readable", config={"a": 1})
        session.add(job)
    assert job.name == "readable"
    assert job.config == {"a": 1}


@pytest.mark.usefixtures("clean_jobs")
def test_flush_error_raised_by_commit_rolls_back_whole_unit(db_engine: Engine) -> None:
    factory = session_factory(db_engine)
    with session_scope(factory) as session:
        session.add(Job(name="dup", config={}))
    # Nothing is flushed inside the block: the duplicate only surfaces in ``commit()``.
    with pytest.raises(IntegrityError), session_scope(factory) as session:
        session.add(Job(name="other", config={}))
        session.add(Job(name="dup", config={}))
        held = session
    assert held.in_transaction() is False
    assert _names(db_engine) == ["dup"]


@pytest.mark.usefixtures("clean_jobs")
def test_base_exception_rolls_back(db_engine: Engine) -> None:
    factory = session_factory(db_engine)
    with pytest.raises(KeyboardInterrupt), session_scope(factory) as session:
        session.add(Job(name="lost", config={}))
        session.flush()
        raise KeyboardInterrupt
    assert session.in_transaction() is False
    assert _names(db_engine) == []


@pytest.mark.usefixtures("clean_jobs")
def test_savepoint_as_first_write_rolls_back(db_engine: Engine) -> None:
    # On SQLite a SAVEPOINT outside a transaction would become the transaction itself and its
    # RELEASE would commit; the engine must have begun a real transaction by then.
    factory = session_factory(db_engine)
    with session_scope(factory) as session:
        job_id = JobRepository(session).add(Job(name="a", config={})).id
    with pytest.raises(RuntimeError, match="boom"), session_scope(factory) as session:
        _, created = ItemRepository(session).add(job_id, make_candidate())
        assert created is True
        raise RuntimeError("boom")
    with Session(db_engine) as session:
        assert session.scalar(select(func.count()).select_from(Item)) == 0
