"""Delete behaviour: job deletion cascades; run/digest deletion only clears links."""

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from invio.db.models import Digest, Item, Job, LlmUsage, Notification, Run
from invio.domain import url_hash
from tests.db_helpers import (
    make_digest,
    make_item,
    make_job,
    make_llm_usage,
    make_notification,
    make_run,
)

pytestmark = pytest.mark.db

DEPENDENTS = (Run, Item, Digest, Notification, LlmUsage)


def _build(session: Session, name: str) -> dict[str, object]:
    job = make_job(session, name)
    run = make_run(session, job)
    item = make_item(session, job, run_id=run.id)
    digest = make_digest(session, job, run_id=run.id)
    notification = make_notification(session, job, run_id=run.id, digest_id=digest.id)
    usage = make_llm_usage(session, job, run_id=run.id)
    return {
        "job": job,
        "run": run,
        "item": item,
        "digest": digest,
        "notification": notification,
        "usage": usage,
    }


def _count(session: Session, model: type, job_id: int) -> int:
    return (
        session.scalar(select(func.count()).select_from(model).where(model.job_id == job_id)) or 0
    )  # type: ignore[attr-defined]


def test_deleting_a_job_removes_its_history_only(db_session: Session) -> None:
    a = _build(db_session, "a")
    b = _build(db_session, "b")
    job_a, job_b = a["job"], b["job"]

    db_session.delete(job_a)
    db_session.flush()
    db_session.expire_all()

    for model in DEPENDENTS:
        assert _count(db_session, model, job_a.id) == 0  # type: ignore[attr-defined]
        assert _count(db_session, model, job_b.id) == 1  # type: ignore[attr-defined]
    assert db_session.get(Job, job_b.id) is not None  # type: ignore[attr-defined]


def test_deleting_a_run_keeps_children_with_null_run(db_session: Session) -> None:
    built = _build(db_session, "a")
    job = built["job"]

    db_session.delete(built["run"])
    db_session.flush()
    db_session.expire_all()

    assert db_session.get(Job, job.id) is not None  # type: ignore[attr-defined]
    for key in ("item", "digest", "notification", "usage"):
        assert db_session.get(type(built[key]), built[key].id).run_id is None  # type: ignore[attr-defined]


def test_deleting_a_digest_keeps_the_notification(db_session: Session) -> None:
    built = _build(db_session, "a")

    db_session.delete(built["digest"])
    db_session.flush()
    db_session.expire_all()

    assert db_session.get(Notification, built["notification"].id).digest_id is None  # type: ignore[attr-defined]


def test_deleting_an_item_keeps_job_and_run(db_session: Session) -> None:
    built = _build(db_session, "a")

    db_session.delete(built["item"])
    db_session.flush()
    db_session.expire_all()

    assert db_session.get(Job, built["job"].id) is not None  # type: ignore[attr-defined]
    assert db_session.get(Run, built["run"].id) is not None  # type: ignore[attr-defined]


def test_item_with_missing_job_is_rejected(db_session: Session) -> None:
    db_session.add(Item(job_id=999_999, url="u", url_hash=url_hash("u"), type="article", title="t"))

    with pytest.raises(IntegrityError):
        db_session.flush()
