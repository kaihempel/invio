"""``RunService``: the read model behind ``invio run list`` and ``invio run show``."""

import pickle
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Engine

from invio.config.settings import MissingSettingError, Settings
from invio.db.models import Base, Job
from invio.db.repositories import JobRepository
from invio.db.session import create_db_engine, session_factory, session_scope
from invio.domain import RunStatus
from invio.services.jobs import JobNotFoundError
from invio.services.runs import RunNotFoundError, RunService
from tests.pipeline_helpers import add_run, make_job_config

pytestmark = pytest.mark.usefixtures("clean_jobs")

T0 = datetime(2026, 10, 7, 4, 0, tzinfo=UTC)


def _add_job(engine: Engine, name: str, config: dict[str, Any] | None = None) -> int:
    with session_scope(session_factory(engine)) as session:
        job = Job(name=name, enabled=True, config=config or make_job_config())
        return JobRepository(session).add(job).id


def _add_run(
    engine: Engine,
    job_id: int,
    *,
    minutes: int = 0,
    status: RunStatus = RunStatus.SUCCEEDED,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> int:
    return add_run(
        session_factory(engine),
        job_id,
        status,
        stats=stats,
        error=error,
        started_at=T0 + timedelta(minutes=minutes),
        duration=timedelta(seconds=3),
    )


def _service(engine: Engine) -> RunService:
    return RunService(session_factory(engine))


def test_list_returns_all_jobs_newest_first(db_engine: Engine) -> None:
    a, b = _add_job(db_engine, "a"), _add_job(db_engine, "b")
    first = _add_run(db_engine, a, minutes=0)
    second = _add_run(db_engine, b, minutes=5)
    third = _add_run(db_engine, a, minutes=5)  # same start as ``second``: higher id first

    runs = _service(db_engine).list()

    assert [r.id for r in runs] == [third, second, first]
    assert [r.job_name for r in runs] == ["a", "b", "a"]


def test_list_filters_by_job_and_caps(db_engine: Engine) -> None:
    a, b = _add_job(db_engine, "a"), _add_job(db_engine, "b")
    for minute in range(3):
        _add_run(db_engine, a, minutes=minute)
    _add_run(db_engine, b, minutes=9)
    service = _service(db_engine)

    assert {r.job_name for r in service.list(job="a")} == {"a"}
    assert len(service.list(job="a", limit=2)) == 2
    assert len(service.list(limit=1)) == 1


def test_list_of_an_unknown_job_raises(db_engine: Engine) -> None:
    with pytest.raises(JobNotFoundError):
        _service(db_engine).list(job="nope")


def test_list_of_an_empty_database_is_empty(db_engine: Engine) -> None:
    assert _service(db_engine).list() == []


def test_summary_fields_come_from_the_stats(db_engine: Engine) -> None:
    job = _add_job(db_engine, "a")
    _add_run(
        db_engine,
        job,
        stats={"version": 1, "dry_run": True, "found": 9, "new": 4, "relevant": 2},
    )
    _add_run(db_engine, job, minutes=1, stats=None)

    newer, older = _service(db_engine).list()

    assert (older.dry_run, older.found, older.new, older.relevant) == (True, 9, 4, 2)
    assert (newer.dry_run, newer.found, newer.new, newer.relevant) == (False, None, None, None)
    assert older.started_at == T0
    assert older.finished_at == T0 + timedelta(seconds=3)


def test_list_carries_the_job_timezone_or_utc(db_engine: Engine) -> None:
    good = _add_job(db_engine, "good")
    bad = _add_job(db_engine, "bad", {"schedule": {"timezone": "Europe/Atlantis"}})
    _add_run(db_engine, good)
    _add_run(db_engine, bad, minutes=1)

    by_name = {r.job_name: r.timezone for r in _service(db_engine).list()}

    assert by_name == {"good": "Europe/Berlin", "bad": "UTC"}


def test_get_a_legacy_run_has_unavailable_errors(db_engine: Engine) -> None:
    run_id = _add_run(db_engine, _add_job(db_engine, "a"), stats={"version": 1, "found": 1})

    detail = _service(db_engine).get(run_id)

    assert detail.errors is None
    assert detail.errors_omitted == 0
    assert detail.stats["found"] == 1
    assert detail.timezone == "Europe/Berlin"


def test_get_an_empty_list_is_an_empty_tuple(db_engine: Engine) -> None:
    run_id = _add_run(db_engine, _add_job(db_engine, "a"), stats={"version": 1, "errors": []})

    assert _service(db_engine).get(run_id).errors == ()


def test_get_parses_entries_leniently(db_engine: Engine) -> None:
    entry = {
        "stage": "score_relevance",
        "error_class": "KeyError",
        "message": "KeyError",
        "item_id": 3,
        "title": "T",
        "url": "https://example.org/a",
    }
    stats = {"version": 1, "errors": [entry, {"nonsense": 1}, "text"], "errors_omitted": 4}
    run_id = _add_run(db_engine, _add_job(db_engine, "a"), stats=stats)

    detail = _service(db_engine).get(run_id)

    assert detail.errors is not None
    good, broken, text = detail.errors
    assert (good.stage, good.item_id, good.title, good.url) == (
        "score_relevance",
        3,
        "T",
        "https://example.org/a",
    )
    assert broken.message == text.message == "<unreadable error entry>"
    assert detail.errors_omitted == 4


def test_get_a_running_run(db_engine: Engine) -> None:
    run_id = _add_run(db_engine, _add_job(db_engine, "a"), status=RunStatus.RUNNING)

    detail = _service(db_engine).get(run_id)

    assert detail.status is RunStatus.RUNNING
    assert detail.finished_at is None
    assert detail.errors is None


def test_get_with_an_invalid_job_config_uses_utc(db_engine: Engine) -> None:
    job = _add_job(db_engine, "bad", {"broken": True})
    run_id = _add_run(db_engine, job, error="boom")

    detail = _service(db_engine).get(run_id)

    assert detail.timezone == "UTC"
    assert detail.error == "boom"


def test_get_an_unknown_run_raises(db_engine: Engine) -> None:
    with pytest.raises(RunNotFoundError) as caught:
        _service(db_engine).get(999)

    assert caught.value.run_id == 999


def test_run_not_found_survives_pickling() -> None:
    error = pickle.loads(pickle.dumps(RunNotFoundError(7)))

    assert (error.run_id, str(error)) == (7, "run 7 not found")


def test_from_settings_reads_the_configured_database(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path}/history.sqlite"
    setup = create_db_engine(url)
    Base.metadata.create_all(setup)
    setup.dispose()

    service = RunService.from_settings(Settings(database_url=SecretStr(url)))

    assert service.list() == []
    service._session_factory.kw["bind"].dispose()


def test_from_settings_without_a_database_url_raises() -> None:
    with pytest.raises(MissingSettingError):
        RunService.from_settings(Settings(database_url=None))
