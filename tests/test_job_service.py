"""Tests for ``JobService`` (job CRUD, YAML import/export, logging).

``JobService`` opens its own sessions, so every direct setup write and read-back here goes
through a committed ``session_scope`` rather than the ``db_session`` fixture.
"""

import copy
import dataclasses
import json
import logging
import pickle
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import Engine, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from invio.config.job import (
    JobConfig,
    JobConfigError,
    JobYamlLoader,
    ScheduleConfig,
    load_yaml,
    write_yaml,
)
from invio.config.settings import MissingSettingError
from invio.db.models import Digest, Item, Job, LlmUsage, Notification, Run
from invio.db.repositories import JobRepository
from invio.db.session import session_factory, session_scope
from invio.log import configure_logging
from invio.scheduling.next_run import compute_next_run
from invio.services.jobs import (
    JobExistsError,
    JobNameError,
    JobNotFoundError,
    JobRecord,
    JobService,
    StoredJobConfigError,
)
from tests.conftest import FakeClock
from tests.db_helpers import (
    make_digest,
    make_item,
    make_llm_usage,
    make_notification,
    make_run,
)
from tests.job_helpers import BAD_JOB, EXAMPLE

pytestmark = pytest.mark.db

NextRunCalls = list[tuple[ScheduleConfig, datetime]]
HISTORY_TABLES = (Run, Item, Digest, Notification, LlmUsage)


@pytest.fixture
def store(db_engine: Engine, clean_jobs: None) -> Callable[[], Any]:
    """Return a factory for committed sessions (setup writes and read-backs)."""

    @contextmanager
    def _scope() -> Iterator[Session]:
        with session_scope(session_factory(db_engine)) as session:
            yield session

    return _scope


def _service_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "invio.services.jobs"]


def _stored_job(store: Callable[[], Any], name: str) -> dict[str, Any]:
    with store() as s:
        job = s.scalars(select(Job).where(Job.name == name)).one()
        return {
            "name": job.name,
            "enabled": job.enabled,
            "config": job.config,
            "next_run_at": job.next_run_at,
            "updated_at": job.updated_at,
        }


def _job_count(store: Callable[[], Any]) -> int:
    with store() as s:
        return int(s.scalar(select(func.count()).select_from(Job)) or 0)


def _add_history(session: Session, job: Job) -> None:
    run = make_run(session, job)
    make_item(session, job, run_id=run.id)
    make_digest(session, job, run_id=run.id)
    make_notification(session, job, run_id=run.id)
    make_llm_usage(session, job, run_id=run.id)


def _history_counts(session: Session, job_id: int) -> dict[str, int]:
    return {
        table.__tablename__: int(
            session.scalar(select(func.count()).select_from(table).where(table.job_id == job_id))
            or 0
        )
        for table in HISTORY_TABLES
    }


# --- US1: create / get / list ----------------------------------------------------------------


def test_create_and_get(
    job_service: JobService,
    job_data: dict[str, Any],
    fake_clock: FakeClock,
    store: Callable[[], Any],
) -> None:
    record = job_service.create("ai-news", job_data)
    config = JobConfig.model_validate(job_data)
    assert record.name == "ai-news"
    assert record.enabled is True
    assert record.config == config
    assert record.next_run_at == fake_clock.now + timedelta(hours=1)
    assert record.created_at.utcoffset() == timedelta(0)
    assert record.updated_at.utcoffset() == timedelta(0)
    assert _stored_job(store, "ai-news")["config"] == config.model_dump(mode="json")
    assert job_service.get_by_name("ai-news") == record


def test_create_accepts_job_config_instance(
    job_service: JobService, job_data: dict[str, Any]
) -> None:
    config = JobConfig.model_validate(job_data)
    assert job_service.create("x", config).config == config


def test_create_duplicate_name(job_service: JobService, job_data: dict[str, Any]) -> None:
    first = job_service.create("ai-news", job_data)
    with pytest.raises(JobExistsError) as info:
        job_service.create("ai-news", job_data)
    assert info.value.name == "ai-news"
    assert str(info.value) == "job 'ai-news' already exists"
    assert job_service.get_by_name("ai-news") == first


def test_create_race_integrity_error(
    job_service: JobService, job_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    job_service.create("ai-news", job_data)
    monkeypatch.setattr(JobRepository, "get_by_name", lambda self, name: None)
    with pytest.raises(JobExistsError) as info:
        job_service.create("ai-news", job_data)
    assert info.value.name == "ai-news"


def test_create_invalid_config(
    job_service: JobService, job_data: dict[str, Any], store: Callable[[], Any]
) -> None:
    job_data["schedule"]["timezone"] = "Europe/Atlantis"
    job_data["bogus"] = 1
    with pytest.raises(JobConfigError) as info:
        job_service.create("bad", job_data)
    assert "schedule.timezone: unknown timezone 'Europe/Atlantis'" in info.value.errors
    assert any(line.startswith("bogus:") for line in info.value.errors)
    assert _job_count(store) == 0


def test_create_rejects_model_construct_with_invalid_content(
    job_service: JobService, job_data: dict[str, Any], store: Callable[[], Any]
) -> None:
    valid = JobConfig.model_validate(job_data)
    broken = JobConfig.model_construct(**{**valid.__dict__, "sources": []})
    with pytest.raises(JobConfigError):
        job_service.create("bad", broken)
    assert _job_count(store) == 0


def test_list_order_and_filter(
    job_service: JobService, job_data: dict[str, Any], store: Callable[[], Any]
) -> None:
    for name in ("b", "a", "c"):
        job_service.create(name, job_data)
    with store() as s:
        JobRepository(s).get_by_name("c").enabled = False  # type: ignore[union-attr]
    assert [r.name for r in job_service.list()] == ["a", "b", "c"]
    assert [r.name for r in job_service.list(enabled_only=True)] == ["a", "b"]


def test_names_compare_exactly(job_service: JobService, job_data: dict[str, Any]) -> None:
    # MariaDB's default collation would treat these as equal; jobs.name is binary (0002).
    for name in ("ai-news", "AI-News", "café", "cafe"):
        job_service.create(name, job_data)
    assert [r.name for r in job_service.list()] == ["AI-News", "ai-news", "cafe", "café"]
    assert job_service.get_by_name("AI-News").name == "AI-News"
    with pytest.raises(JobNotFoundError):
        job_service.get_by_name("AI-NEWS")


def test_get_missing(job_service: JobService) -> None:
    with pytest.raises(JobNotFoundError) as info:
        job_service.get_by_name("missing")
    assert isinstance(info.value, LookupError)
    assert str(info.value) == "job 'missing' not found"
    assert info.value.name == "missing"


@pytest.mark.parametrize("name", ["", " ", " lead", "trail ", "x" * 201])
def test_invalid_names(
    job_service: JobService, job_data: dict[str, Any], store: Callable[[], Any], name: str
) -> None:
    with pytest.raises(JobNameError) as info:
        job_service.create(name, job_data)
    assert isinstance(info.value, ValueError)
    assert info.value.name == name
    assert _job_count(store) == 0


def test_max_length_name_accepted(job_service: JobService, job_data: dict[str, Any]) -> None:
    assert job_service.create("x" * 200, job_data).name == "x" * 200


def test_invalid_stored_config(
    job_service: JobService,
    job_data: dict[str, Any],
    store: Callable[[], Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    job_service.create("ok", job_data)
    with store() as s:
        s.add(Job(name="broken", config={"bogus": 1}))
    with pytest.raises(StoredJobConfigError) as info:
        job_service.get_by_name("broken")
    assert isinstance(info.value, JobConfigError)
    assert info.value.name == "broken"
    assert str(info.value).startswith("invalid stored job 'broken':")
    assert info.value.errors
    with caplog.at_level(logging.WARNING, logger="invio.services.jobs"):
        records = job_service.list()
    assert [r.name for r in records] == ["ok"]
    warnings = [r for r in caplog.records if r.name == "invio.services.jobs"]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert warnings[0].__dict__["event"] == "job.invalid_config"
    assert warnings[0].__dict__["job_name"] == "broken"
    assert warnings[0].__dict__["error_count"] >= 1


def test_null_stored_config_is_invalid(job_service: JobService, store: Callable[[], Any]) -> None:
    with store() as s:
        s.add(Job(name="empty"))
    with pytest.raises(StoredJobConfigError):
        job_service.get_by_name("empty")


def test_error_objects_are_well_formed() -> None:
    err = StoredJobConfigError("x", ["a: bad", "b: worse"])
    assert str(err) == "invalid stored job 'x':\n  a: bad\n  b: worse"
    assert err.errors == ["a: bad", "b: worse"]
    assert err.name == "x"
    assert str(JobExistsError("x")) == "job 'x' already exists"
    assert JobExistsError("x").args == ("job 'x' already exists",)
    assert JobNotFoundError("x").args == ("job 'x' not found",)


# --- US2: update / enable / delete -----------------------------------------------------------


def test_update_replaces_and_recalculates(
    job_service: JobService,
    job_data: dict[str, Any],
    fake_clock: FakeClock,
    next_run_calls: NextRunCalls,
) -> None:
    job_data["limits"] = {"max_items_per_run": 7}
    job_service.create("j", job_data)
    fake_clock.advance(timedelta(days=1))
    new_data = {k: v for k, v in job_data.items() if k != "limits"}
    new_data["schedule"] = {**job_data["schedule"], "time": "09:15"}
    record = job_service.update("j", new_data)
    assert record.config == JobConfig.model_validate(new_data)
    assert record.name == "j"
    assert record.next_run_at == fake_clock.now + timedelta(hours=1)
    assert next_run_calls[-1][0] == record.config.schedule
    assert next_run_calls[-1][1] == fake_clock.now


def test_update_invalid_keeps_job(job_service: JobService, job_data: dict[str, Any]) -> None:
    before = job_service.create("j", job_data)
    with pytest.raises(JobConfigError):
        job_service.update("j", {**job_data, "sources": []})
    assert job_service.get_by_name("j") == before


def test_update_disabled_job_keeps_no_next_run(
    job_service: JobService, job_data: dict[str, Any], next_run_calls: NextRunCalls
) -> None:
    job_service.create("j", job_data)
    job_service.set_enabled("j", False)
    calls = len(next_run_calls)
    new_data = {**job_data, "schedule": {**job_data["schedule"], "time": "10:00"}}
    record = job_service.update("j", new_data)
    assert record.enabled is False
    assert record.next_run_at is None
    assert record.config == JobConfig.model_validate(new_data)
    assert len(next_run_calls) == calls


def test_disable_keeps_history(
    job_service: JobService, job_data: dict[str, Any], store: Callable[[], Any]
) -> None:
    job_service.create("j", job_data)
    with store() as s:
        job = JobRepository(s).get_by_name("j")
        assert job is not None
        job_id = job.id
        _add_history(s, job)
    record = job_service.set_enabled("j", False)
    assert record.enabled is False
    assert record.next_run_at is None
    with store() as s:
        assert _history_counts(s, job_id) == {t.__tablename__: 1 for t in HISTORY_TABLES}


def test_reenable_recalculates(
    job_service: JobService, job_data: dict[str, Any], fake_clock: FakeClock
) -> None:
    job_service.create("j", job_data)
    job_service.set_enabled("j", False)
    fake_clock.advance(timedelta(days=2))
    record = job_service.set_enabled("j", True)
    assert record.enabled is True
    assert record.next_run_at == fake_clock.now + timedelta(hours=1)


def test_default_next_run_uses_schedule_calculation(
    db_engine: Engine, job_data: dict[str, Any], fake_clock: FakeClock, clean_jobs: None
) -> None:
    service = JobService(session_factory(db_engine), clock=fake_clock)

    created = service.create("j", job_data)
    schedule = created.config.schedule
    assert created.next_run_at == compute_next_run(schedule, fake_clock.now)
    assert created.next_run_at == datetime(2026, 10, 5, 5, 30, tzinfo=UTC)
    assert created.next_run_at > fake_clock.now

    assert service.set_enabled("j", False).next_run_at is None
    fake_clock.advance(timedelta(days=3))
    reenabled = service.set_enabled("j", True)
    assert reenabled.next_run_at == compute_next_run(schedule, fake_clock.now)
    assert reenabled.next_run_at is not None
    assert reenabled.next_run_at > fake_clock.now

    fake_clock.advance(timedelta(days=1))
    new_data = {**job_data, "schedule": {**job_data["schedule"], "time": "10:00"}}
    updated = service.update("j", new_data)
    assert updated.next_run_at == compute_next_run(updated.config.schedule, fake_clock.now)


@pytest.mark.parametrize("enabled", [True, False])
def test_set_enabled_noop(
    job_service: JobService,
    job_data: dict[str, Any],
    store: Callable[[], Any],
    next_run_calls: NextRunCalls,
    caplog: pytest.LogCaptureFixture,
    enabled: bool,
) -> None:
    job_service.create("j", job_data)
    if not enabled:
        job_service.set_enabled("j", False)
    before = _stored_job(store, "j")
    calls = len(next_run_calls)
    with caplog.at_level(logging.INFO, logger="invio.services.jobs"):
        job_service.set_enabled("j", enabled)
    assert _stored_job(store, "j") == before
    assert len(next_run_calls) == calls
    assert [r for r in caplog.records if r.name == "invio.services.jobs"] == []


def test_set_enabled_with_invalid_stored_config(
    job_service: JobService, store: Callable[[], Any]
) -> None:
    with store() as s:
        s.add(Job(name="broken", config={"bogus": 1}, enabled=False))
    with pytest.raises(StoredJobConfigError):
        job_service.set_enabled("broken", True)
    assert _stored_job(store, "broken")["enabled"] is False


def test_delete_cascades_only_own_history(
    job_service: JobService, job_data: dict[str, Any], store: Callable[[], Any]
) -> None:
    ids: dict[str, int] = {}
    for name in ("a", "b"):
        job_service.create(name, job_data)
    with store() as s:
        for name in ("a", "b"):
            job = JobRepository(s).get_by_name(name)
            assert job is not None
            ids[name] = job.id
            _add_history(s, job)
    job_service.delete("a")
    with pytest.raises(JobNotFoundError):
        job_service.get_by_name("a")
    with store() as s:
        assert set(_history_counts(s, ids["a"]).values()) == {0}
        assert set(_history_counts(s, ids["b"]).values()) == {1}
    assert job_service.get_by_name("b").name == "b"


@pytest.mark.parametrize(
    "call",
    [
        lambda s, data: s.update("x", data),
        lambda s, data: s.set_enabled("x", True),
        lambda s, data: s.set_enabled("x", False),
        lambda s, data: s.delete("x"),
    ],
    ids=["update", "enable", "disable", "delete"],
)
def test_not_found_operations(
    job_service: JobService, job_data: dict[str, Any], call: Callable[..., Any]
) -> None:
    with pytest.raises(JobNotFoundError) as info:
        call(job_service, job_data)
    assert info.value.name == "x"


# --- US3: YAML import / export ---------------------------------------------------------------


def _variants(job_data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    daily = {**job_data, "schedule": {"frequency": "daily", "time": "06:00", "timezone": "UTC"}}
    monthly = {
        **job_data,
        "schedule": {
            "frequency": "monthly",
            "time": "23:59",
            "day_of_month": 31,
            "timezone": "America/New_York",
        },
    }
    all_sources = {
        **job_data,
        "sources": [
            {"type": "rss", "url": "https://example.com/feed.xml"},
            {"type": "web", "url": "https://example.com/news"},
            {"type": "sitemap", "url": "https://example.com/sitemap.xml"},
            {"type": "youtube_channel", "channel_id": "UC123"},
            {"type": "youtube_playlist", "playlist_id": "PL123"},
        ],
    }
    unicode = {
        **job_data,
        "notification": {**job_data["notification"], "subject": "Recherche: Übersicht 🚀"},
        "search": {"semantic_description": "KI-Agenten für Ärzte"},
    }
    return {
        "mini": job_data,
        "daily": daily,
        "monthly": monthly,
        "all_sources": all_sources,
        "unicode": unicode,
    }


@pytest.mark.parametrize("case", ["example", "mini", "daily", "monthly", "all_sources", "unicode"])
def test_import_export_roundtrip(
    job_service: JobService, job_data: dict[str, Any], tmp_path: Path, case: str
) -> None:
    if case == "example":
        path = EXAMPLE
    else:
        path = tmp_path / f"{case}.yaml"
        write_yaml(JobConfig.model_validate(_variants(job_data)[case]), path)
    expected = load_yaml(path)
    record = job_service.import_yaml(path)
    assert record.name == path.stem
    assert record.config == expected
    exported = job_service.export_yaml(path.stem)
    assert isinstance(exported, str)
    assert JobConfig.model_validate(yaml.load(exported, Loader=JobYamlLoader)) == expected
    out = tmp_path / "out.yaml"
    assert job_service.export_yaml(path.stem, out) == exported
    assert out.read_text(encoding="utf-8") == exported
    assert load_yaml(out) == expected


def test_import_explicit_name(
    job_service: JobService, job_data: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "file.yaml"
    write_yaml(JobConfig.model_validate(job_data), path)
    assert job_service.import_yaml(path, "custom").name == "custom"
    assert job_service.get_by_name("custom").name == "custom"


def test_import_invalid_file(
    job_service: JobService, tmp_path: Path, store: Callable[[], Any]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(BAD_JOB, encoding="utf-8")
    with pytest.raises(JobConfigError) as expected:
        load_yaml(bad)
    with pytest.raises(JobConfigError) as info:
        job_service.import_yaml(bad)
    assert info.value.errors == expected.value.errors
    with pytest.raises(JobConfigError, match="cannot read job file"):
        job_service.import_yaml(tmp_path / "missing.yaml")
    assert _job_count(store) == 0


def test_import_invalid_name(
    job_service: JobService, job_data: dict[str, Any], tmp_path: Path
) -> None:
    path = tmp_path / "ok.yaml"
    write_yaml(JobConfig.model_validate(job_data), path)
    with pytest.raises(JobNameError):
        job_service.import_yaml(path, " padded ")


def test_import_existing_requires_replace(
    job_service: JobService,
    job_data: dict[str, Any],
    tmp_path: Path,
    fake_clock: FakeClock,
    store: Callable[[], Any],
) -> None:
    path = tmp_path / "j.yaml"
    write_yaml(JobConfig.model_validate(job_data), path)
    job_service.import_yaml(path)
    with store() as s:
        job = JobRepository(s).get_by_name("j")
        assert job is not None
        job_id = job.id
        make_run(s, job)
    with pytest.raises(JobExistsError):
        job_service.import_yaml(path)
    changed = {**job_data, "schedule": {**job_data["schedule"], "time": "05:00"}}
    write_yaml(JobConfig.model_validate(changed), path)
    fake_clock.advance(timedelta(days=1))
    record = job_service.import_yaml(path, replace=True)
    assert record.config == load_yaml(path)
    assert record.next_run_at == fake_clock.now + timedelta(hours=1)
    with store() as s:
        assert _history_counts(s, job_id)["runs"] == 1


def test_export_missing(job_service: JobService) -> None:
    with pytest.raises(JobNotFoundError):
        job_service.export_yaml("missing")


def test_export_unwritable_path(
    job_service: JobService, job_data: dict[str, Any], tmp_path: Path
) -> None:
    before = job_service.create("j", job_data)
    with pytest.raises(OSError):
        job_service.export_yaml("j", tmp_path / "no-dir" / "x.yaml")
    assert job_service.get_by_name("j") == before


# --- Polish: logging, atomicity, boundary ----------------------------------------------------


def test_change_logging(
    job_service: JobService,
    job_data: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "imp.yaml"
    write_yaml(JobConfig.model_validate(job_data), path)
    caplog.set_level(logging.INFO, logger="invio.services.jobs")
    steps: list[tuple[str, str, Callable[[], Any]]] = [
        ("job.created", "j", lambda: job_service.create("j", job_data)),
        ("job.updated", "j", lambda: job_service.update("j", job_data)),
        ("job.disabled", "j", lambda: job_service.set_enabled("j", False)),
        ("job.enabled", "j", lambda: job_service.set_enabled("j", True)),
        ("job.imported", "imp", lambda: job_service.import_yaml(path)),
        ("job.deleted", "j", lambda: job_service.delete("j")),
    ]
    for event, name, action in steps:
        caplog.clear()
        action()
        [record] = _service_records(caplog)
        assert record.levelno == logging.INFO
        assert record.getMessage() == "job changed"
        assert (record.__dict__["event"], record.__dict__["job_name"]) == (event, name)

    configure_logging("INFO")
    capsys.readouterr()
    job_service.create("json", job_data)
    lines = [json.loads(line) for line in capsys.readouterr().err.splitlines() if line]
    line = next(entry for entry in lines if entry.get("event") == "job.created")
    assert line["job_name"] == "json"
    assert "extra_job" not in line
    assert "research@example.com" not in json.dumps(line)


def test_failed_operations_leave_store_unchanged(
    job_service: JobService, job_data: dict[str, Any], tmp_path: Path, store: Callable[[], Any]
) -> None:
    job_service.create("j", job_data)

    def snapshot() -> list[dict[str, Any]]:
        with store() as s:
            names = list(s.scalars(select(Job.name).order_by(Job.name)))
        return [_stored_job(store, n) for n in names]

    bad = tmp_path / "bad.yaml"
    bad.write_text(BAD_JOB, encoding="utf-8")
    failing: list[Callable[[], Any]] = [
        lambda: job_service.create("j", job_data),
        lambda: job_service.update("j", {**job_data, "sources": []}),
        lambda: job_service.import_yaml(bad),
        lambda: job_service.delete("unknown"),
    ]
    for call in failing:
        before = snapshot()
        with pytest.raises((JobExistsError, JobConfigError, JobNotFoundError)):
            call()
        assert snapshot() == before


def test_no_sqlalchemy_in_records(
    job_service: JobService, job_data: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    record = job_service.create("j", job_data)
    assert isinstance(record, JobRecord)
    for value in vars_of(record):
        assert not type(value).__module__.startswith("sqlalchemy")


def vars_of(record: JobRecord) -> list[Any]:
    return [
        record.name,
        record.enabled,
        record.config,
        record.next_run_at,
        record.created_at,
        record.updated_at,
    ]


def test_from_settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    with pytest.raises(MissingSettingError):
        JobService.from_settings()
    monkeypatch.setenv("INVIO_DATABASE_URL", f"sqlite:///{tmp_path}/s.sqlite")
    from invio.config.settings import get_settings

    get_settings.cache_clear()
    service = JobService.from_settings()
    assert isinstance(service, JobService)
    service._session_factory.kw["bind"].dispose()


def test_default_clock_and_next_run(
    db_engine: Engine, job_data: dict[str, Any], clean_jobs: None
) -> None:
    service = JobService(session_factory(db_engine))
    before = datetime.now(UTC)
    record = service.create("j", job_data)
    assert record.next_run_at is not None
    assert before < record.next_run_at <= before + timedelta(days=8)


# --- QA edge cases ---------------------------------------------------------------------------


def test_create_race_leaves_existing_job_unchanged(
    job_service: JobService,
    job_data: dict[str, Any],
    store: Callable[[], Any],
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = job_service.create("ai-news", job_data)
    before = _stored_job(store, "ai-news")
    fake_clock.advance(timedelta(days=1))
    other = {**job_data, "schedule": {**job_data["schedule"], "time": "01:00"}}
    with monkeypatch.context() as m:
        m.setattr(JobRepository, "get_by_name", lambda self, name: None)
        with pytest.raises(JobExistsError) as info:
            job_service.create("ai-news", other)
    # FR-020: the storage error is only the chained cause, never the raised type.
    assert type(info.value) is JobExistsError
    assert _stored_job(store, "ai-news") == before
    assert _job_count(store) == 1
    assert job_service.get_by_name("ai-news") == first


def test_create_invalid_config_matches_load_yaml_messages(
    job_service: JobService, tmp_path: Path, store: Callable[[], Any]
) -> None:
    path = tmp_path / "bad.yaml"
    path.write_text(BAD_JOB, encoding="utf-8")
    with pytest.raises(JobConfigError) as from_file:
        load_yaml(path)
    with pytest.raises(JobConfigError) as from_service:
        job_service.create("bad", yaml.load(BAD_JOB, Loader=JobYamlLoader))
    assert from_service.value.errors == from_file.value.errors
    assert _job_count(store) == 0


@pytest.mark.parametrize(
    "name",
    ["ä" * 200, "🚀" * 200, "日本" * 100, "a" + "é" * 198 + "z"],
    ids=["latin1-200", "emoji-200", "cjk-200", "mixed-200"],
)
def test_multibyte_name_at_limit_accepted(
    job_service: JobService, job_data: dict[str, Any], store: Callable[[], Any], name: str
) -> None:
    assert len(name) == 200
    assert job_service.create(name, job_data).name == name
    assert _stored_job(store, name)["name"] == name
    assert job_service.get_by_name(name).name == name


@pytest.mark.parametrize(
    "name",
    ["ä" * 201, "🚀" * 201, "日本" * 100 + "x", "\u3000name", "name\u00a0", "\tname", "name\n"],
    ids=["latin1-201", "emoji-201", "cjk-201", "ideographic-space", "nbsp", "tab", "newline"],
)
def test_invalid_unicode_names_rejected(
    job_service: JobService, job_data: dict[str, Any], store: Callable[[], Any], name: str
) -> None:
    with pytest.raises(JobNameError):
        job_service.create(name, job_data)
    assert _job_count(store) == 0


def test_import_rejects_too_long_file_stem(
    job_service: JobService, job_data: dict[str, Any], tmp_path: Path, store: Callable[[], Any]
) -> None:
    path = tmp_path / ("x" * 201 + ".yaml")
    write_yaml(JobConfig.model_validate(job_data), path)
    with pytest.raises(JobNameError):
        job_service.import_yaml(path)
    assert _job_count(store) == 0
    assert job_service.import_yaml(path, "x" * 200).name == "x" * 200


def _add_broken(store: Callable[[], Any], name: str = "broken", *, enabled: bool = True) -> None:
    with store() as s:
        s.add(Job(name=name, config={"bogus": 1}, enabled=enabled, next_run_at=None))


def test_export_broken_stored_config_raises_and_writes_nothing(
    job_service: JobService, store: Callable[[], Any], tmp_path: Path
) -> None:
    _add_broken(store)
    out = tmp_path / "out.yaml"
    with pytest.raises(StoredJobConfigError) as info:
        job_service.export_yaml("broken", out)
    assert info.value.name == "broken"
    assert not out.exists()


def test_disable_broken_stored_config_is_persisted(
    job_service: JobService, store: Callable[[], Any], caplog: pytest.LogCaptureFixture
) -> None:
    _add_broken(store, enabled=True)
    caplog.set_level(logging.INFO, logger="invio.services.jobs")
    with pytest.raises(StoredJobConfigError):
        job_service.set_enabled("broken", False)
    stored = _stored_job(store, "broken")
    assert stored["enabled"] is False
    assert stored["next_run_at"] is None
    [record] = _service_records(caplog)  # the disable took effect, so it is logged
    assert record.__dict__["event"] == "job.disabled"

    caplog.clear()
    with pytest.raises(StoredJobConfigError):
        job_service.set_enabled("broken", False)  # already disabled: no write, no log
    assert _stored_job(store, "broken") == stored
    assert _service_records(caplog) == []


@pytest.mark.parametrize(
    "repair",
    [
        lambda svc, data, path: svc.update("broken", data),
        lambda svc, data, path: svc.import_yaml(path, "broken", replace=True),
    ],
    ids=["update", "import-replace"],
)
def test_full_replacement_repairs_broken_stored_config(
    job_service: JobService,
    job_data: dict[str, Any],
    store: Callable[[], Any],
    tmp_path: Path,
    repair: Callable[..., JobRecord],
) -> None:
    _add_broken(store)
    path = tmp_path / "fix.yaml"
    write_yaml(JobConfig.model_validate(job_data), path)
    record = repair(job_service, job_data, path)
    assert record.config == JobConfig.model_validate(job_data)
    assert job_service.get_by_name("broken") == record


def test_delete_broken_stored_config(job_service: JobService, store: Callable[[], Any]) -> None:
    _add_broken(store)
    job_service.delete("broken")
    assert _job_count(store) == 0


def test_import_replace_on_disabled_job_stays_disabled(
    job_service: JobService,
    job_data: dict[str, Any],
    tmp_path: Path,
    store: Callable[[], Any],
    next_run_calls: NextRunCalls,
) -> None:
    job_service.create("j", job_data)
    job_service.set_enabled("j", False)
    with store() as s:
        job = JobRepository(s).get_by_name("j")
        assert job is not None
        job_id = job.id
        _add_history(s, job)
    calls = len(next_run_calls)
    changed = {**job_data, "schedule": {**job_data["schedule"], "time": "05:00"}}
    path = tmp_path / "j.yaml"
    write_yaml(JobConfig.model_validate(changed), path)
    record = job_service.import_yaml(path, replace=True)
    assert record.enabled is False
    assert record.next_run_at is None
    assert record.config == load_yaml(path)
    assert len(next_run_calls) == calls
    stored = _stored_job(store, "j")
    assert (stored["enabled"], stored["next_run_at"]) == (False, None)
    with store() as s:
        assert _history_counts(s, job_id) == {t.__tablename__: 1 for t in HISTORY_TABLES}


def test_job_record_is_immutable(job_service: JobService, job_data: dict[str, Any]) -> None:
    record = job_service.create("j", job_data)
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.enabled = False  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.next_run_at = None  # type: ignore[misc]
    with pytest.raises((AttributeError, TypeError)):
        record.extra = 1  # type: ignore[attr-defined]
    assert not hasattr(record, "__dict__")
    with pytest.raises(ValidationError):
        record.config.schedule = record.config.schedule  # type: ignore[misc]
    # Mutable lists inside the frozen config are copies: mutating them never reaches the store.
    record.config.notification.to.append("evil@example.com")
    assert job_service.get_by_name("j").config.notification.to == ["research@example.com"]
    assert dataclasses.replace(record, enabled=False).enabled is False
    assert job_service.get_by_name("j").enabled is True


SECRETS = (
    "secret-recipient@example.org",
    "https://secret.example.org/feed",
    "SECRET-SUBJECT",
    "SECRET-DESCRIPTION",
    "SECRET-KEYWORD",
    "SECRET-MODEL",
)


def _secret_config(job_data: dict[str, Any]) -> dict[str, Any]:
    return {
        **job_data,
        "notification": {"to": [SECRETS[0]], "subject": SECRETS[2]},
        "sources": [{"type": "rss", "url": SECRETS[1]}],
        "search": {
            "semantic_description": SECRETS[3],
            "keywords": {"any": [SECRETS[4]], "all": [SECRETS[4]], "exclude": [SECRETS[4]]},
        },
        "llm": {"provider": "openai", "models": {"fast": SECRETS[5], "smart": SECRETS[5]}},
    }


def test_logging_never_contains_config_contents(
    job_service: JobService,
    job_data: dict[str, Any],
    tmp_path: Path,
    store: Callable[[], Any],
    caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    configure_logging("DEBUG")
    caplog.set_level(logging.DEBUG)
    data = _secret_config(job_data)
    path = tmp_path / "imp.yaml"
    write_yaml(JobConfig.model_validate(data), path)
    with store() as s:
        s.add(Job(name="broken", config={**data, "bogus": SECRETS[2]}))
    job_service.create("j", data)
    job_service.update("j", data)
    job_service.set_enabled("j", False)
    job_service.set_enabled("j", True)
    job_service.import_yaml(path)
    job_service.list()
    job_service.export_yaml("j")
    job_service.delete("j")
    assert len(_service_records(caplog)) == 7  # 6 changes + 1 invalid-config warning
    for record in caplog.records:
        text = " ".join(str(v) for v in (record.getMessage(), *record.__dict__.values()))
        for secret in SECRETS:
            assert secret not in text, (record.name, secret)
    err = capsys.readouterr().err
    assert "job.invalid_config" in err
    for secret in SECRETS:
        assert secret not in err


def test_failed_operations_emit_no_change_line(
    job_service: JobService,
    job_data: dict[str, Any],
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    job_service.create("j", job_data)
    path = tmp_path / "j.yaml"
    write_yaml(JobConfig.model_validate(job_data), path)
    caplog.set_level(logging.INFO, logger="invio.services.jobs")
    caplog.clear()
    failing: list[Callable[[], Any]] = [
        lambda: job_service.update("j", {**job_data, "sources": []}),
        lambda: job_service.update("missing", job_data),
        lambda: job_service.set_enabled("missing", False),
        lambda: job_service.import_yaml(path),
        lambda: job_service.import_yaml(path, " bad "),
        lambda: job_service.export_yaml("missing"),
        lambda: job_service.create("", job_data),
        lambda: job_service.create("j", job_data),
        lambda: job_service.create("other", {**job_data, "sources": []}),
        lambda: job_service.delete("missing"),
    ]
    for call in failing:
        with pytest.raises((JobConfigError, JobNotFoundError, JobExistsError, JobNameError)):
            call()
    assert _service_records(caplog) == []


def _failing_commit(self: Session) -> None:
    raise OperationalError("COMMIT", {}, Exception("disk I/O error"))


@pytest.mark.parametrize(
    "call",
    [
        lambda svc, data: svc.create("new", data),
        lambda svc, data: svc.update("j", {**data, "limits": {"max_items_per_run": 3}}),
        lambda svc, data: svc.set_enabled("j", False),
        lambda svc, data: svc.delete("j"),
    ],
    ids=["create", "update", "disable", "delete"],
)
def test_storage_failure_on_commit_leaves_store_unchanged(
    job_service: JobService,
    job_data: dict[str, Any],
    store: Callable[[], Any],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    call: Callable[..., Any],
) -> None:
    job_service.create("j", job_data)
    before = _stored_job(store, "j")
    caplog.set_level(logging.INFO, logger="invio.services.jobs")
    caplog.clear()
    with monkeypatch.context() as m:
        m.setattr(Session, "commit", _failing_commit)
        with pytest.raises(OperationalError):
            call(job_service, job_data)
    assert _service_records(caplog) == []
    assert _job_count(store) == 1
    assert _stored_job(store, "j") == before


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(JobExistsError("x"), id="exists"),
        pytest.param(JobNotFoundError("x"), id="not-found"),
        pytest.param(JobNameError("x", "must not be empty"), id="name"),
        pytest.param(StoredJobConfigError("x", ["a: bad"]), id="stored-config"),
    ],
)
def test_errors_survive_pickle_and_copy(error: Exception) -> None:
    for clone in (pickle.loads(pickle.dumps(error)), copy.copy(error)):
        assert type(clone) is type(error)
        assert str(clone) == str(error)
        assert clone.args == error.args
        assert clone.name == error.name  # type: ignore[attr-defined]


def test_error_args_and_hierarchy() -> None:
    name_error = JobNameError("  x", "must not have leading or trailing whitespace")
    assert name_error.args == ("invalid job name: must not have leading or trailing whitespace",)
    assert name_error.name == "  x"
    stored = StoredJobConfigError("x", ["a: bad"])
    assert stored.path is None
    assert stored.args == (None, ["a: bad"])
    assert not issubclass(JobExistsError, (LookupError, ValueError, JobConfigError))
    assert not issubclass(JobNotFoundError, (ValueError, JobConfigError))
