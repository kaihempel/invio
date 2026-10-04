"""Helpers for persistence tests: the test database URL and row factories."""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from sqlalchemy.exc import DataError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from invio.db.models import Digest, Item, Job, LlmUsage, Notification, Run
from invio.domain import url_hash

# Captured at import time: the autouse ``isolated_settings`` fixture strips INVIO_* variables.
TEST_DATABASE_URL: str | None = os.environ.get("INVIO_TEST_DATABASE_URL") or None

# MariaDB reports CHECK violations as OperationalError with these errnos
# (4025 = ER_CONSTRAINT_FAILED, 3819 = MySQL's ER_CHECK_CONSTRAINT_VIOLATED).
_CHECK_VIOLATION_ERRNOS = {4025, 3819}


@contextmanager
def assert_rejected() -> Iterator[None]:
    """Assert the block is rejected by the database (constraint, enum or range violation).

    Accepts IntegrityError/DataError and, for MariaDB CHECK violations, an OperationalError
    carrying a constraint errno; any other OperationalError (e.g. a lost connection) propagates.
    """
    try:
        yield
    except (IntegrityError, DataError):
        return
    except OperationalError as exc:
        args = getattr(exc.orig, "args", ())
        if not args or args[0] not in _CHECK_VIOLATION_ERRNOS:
            raise
        return
    pytest.fail("statement was not rejected by the database")


def uses_sqlite() -> bool:
    """Return True when persistence tests run on SQLite (no/``sqlite*`` test URL)."""
    return TEST_DATABASE_URL is None or TEST_DATABASE_URL.startswith("sqlite")


def _add(session: Session, row: Any) -> Any:
    session.add(row)
    session.flush()
    return row


def make_job(session: Session, name: str = "job-a", **kw: Any) -> Job:
    return _add(session, Job(name=name, **kw))  # type: ignore[no-any-return]


def make_run(session: Session, job: Job, **kw: Any) -> Run:
    return _add(session, Run(job_id=job.id, **kw))  # type: ignore[no-any-return]


def make_item(session: Session, job: Job, url: str = "https://example.com/a", **kw: Any) -> Item:
    kw.setdefault("url_hash", url_hash(url))
    kw.setdefault("type", "article")
    kw.setdefault("title", "t")
    return _add(session, Item(job_id=job.id, url=url, **kw))  # type: ignore[no-any-return]


def make_digest(session: Session, job: Job, **kw: Any) -> Digest:
    kw.setdefault("title", "digest")
    kw.setdefault("body", "body")
    return _add(session, Digest(job_id=job.id, **kw))  # type: ignore[no-any-return]


def make_notification(session: Session, job: Job, **kw: Any) -> Notification:
    kw.setdefault("channel", "email")
    kw.setdefault("recipient", "a@example.com")
    return _add(session, Notification(job_id=job.id, **kw))  # type: ignore[no-any-return]


def make_llm_usage(session: Session, job: Job, **kw: Any) -> LlmUsage:
    kw.setdefault("provider", "openai")
    kw.setdefault("model", "gpt-small")
    return _add(session, LlmUsage(job_id=job.id, **kw))  # type: ignore[no-any-return]
