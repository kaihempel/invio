"""International text and emoji must round-trip unchanged."""

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from invio.db.models import Base, Item, Job
from tests.db_helpers import make_item, make_job, uses_sqlite

pytestmark = pytest.mark.db

TEXTS = [
    "Größenänderung — Übersicht",
    "Ärger über Öl & Süßes ß",
    "Launch 🚀🇩🇪👩‍💻",
    "混合 текст ✓ 🧪",
]


@pytest.mark.parametrize("value", TEXTS)
def test_title_and_summary_round_trip(db_session: Session, value: str) -> None:
    item = make_item(db_session, make_job(db_session), title=value, summary=value)
    item_id = item.id
    db_session.commit()
    db_session.expire_all()

    stored = db_session.get(Item, item_id)
    assert stored is not None
    assert stored.title == value
    assert stored.summary == value
    assert stored.title.encode("utf-8") == value.encode("utf-8")


def test_job_name_and_json_config_round_trip(db_session: Session) -> None:
    config = {"topic": "Fühler 🚀", "tags": ["日本語", "🧪"]}
    job = make_job(db_session, "Fühler-🚀", config=config)
    db_session.commit()
    db_session.expire_all()

    stored = db_session.get(Job, job.id)
    assert stored is not None
    assert stored.name == "Fühler-🚀"
    assert stored.config == config


@pytest.mark.skipif(uses_sqlite(), reason="MariaDB/MySQL only")
def test_connection_charset_and_table_collations(db_session: Session) -> None:  # pragma: no cover
    assert db_session.execute(text("SELECT @@character_set_connection")).scalar_one() == "utf8mb4"
    rows = db_session.execute(
        text(
            "SELECT TABLE_NAME, TABLE_COLLATION FROM information_schema.TABLES"
            " WHERE TABLE_SCHEMA = DATABASE()"
        )
    ).all()
    collations = {name: collation for name, collation in rows if name in Base.metadata.tables}
    assert collations == dict.fromkeys(Base.metadata.tables, "utf8mb4_unicode_ci")
