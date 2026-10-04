# Quickstart: Validate the Job Service and Repositories

**Feature**: [spec.md](./spec.md) · **API**: [contracts/python-api.md](./contracts/python-api.md)

## Prerequisites

```bash
uv sync --locked
```

No database server is needed: tests use in-memory SQLite. Optional MariaDB run:
`INVIO_TEST_DATABASE_URL=mysql+pymysql://… uv run pytest -m db`.

## 1. Automated checks (same as CI)

```bash
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest tests/test_repositories.py tests/test_job_service.py tests/test_db_session_scope.py -q
uv run pytest            # full suite stays green
```

Expected: all pass; the new files run in well under 10 s (SC-005).

## 2. Manual walkthrough (SQLite file)

```python
from invio.db import migrate
from invio.db.session import create_db_engine, session_factory
from invio.services.jobs import JobService, JobExistsError

engine = create_db_engine("sqlite:///scratch.db")
with engine.begin() as conn:
    migrate.upgrade(migrate.alembic_config(connection=conn), "head")
svc = JobService(session_factory(engine))

rec = svc.import_yaml("docs/job.example.yaml")  # name "job.example"
print(rec.name, rec.enabled, rec.next_run_at)  # job.example True <now, UTC>
svc.import_yaml("docs/job.example.yaml")  # -> JobExistsError
print(svc.export_yaml("job.example") == open("docs/job.example.yaml").read())  # see note
svc.set_enabled("job.example", False)  # next_run_at -> None
svc.delete("job.example")
```

Note: exported YAML includes every field (defaults too) and drops comments, so compare loaded
configs, not text: `load_yaml(exported) == load_yaml(original)`.

## 3. Acceptance mapping

| Acceptance criterion (issue / spec) | Covered by |
|---|---|
| Duplicate name → `JobExistsError` (US1-2, FR-002) | `test_job_service.py::test_create_duplicate_name`, `::test_create_race_integrity_error` |
| Invalid config rejected, nothing stored (US1-3, FR-004) | `::test_create_invalid_config` |
| List ordered, enabled filter (US1-4, FR-007) | `::test_list_order_and_filter` |
| Missing job → `JobNotFoundError` (US1-5, US2-6) | `::test_not_found_operations` (parametrized) |
| Update re-validates + recalculates `next_run_at` (US2-1/2, FR-003a, FR-008) | `::test_update_replaces_and_recalculates`, `::test_update_invalid_keeps_job` |
| Disable keeps history, clears next run (US2-3, FR-009) | `::test_disable_keeps_history` |
| Re-enable recalculates; no-op for same state (US2-4) | `::test_reenable_recalculates`, `::test_set_enabled_noop` |
| Delete removes job + history only (US2-5, FR-010) | `::test_delete_cascades_only_own_history` |
| Import → export equal (US3-1/2, FR-013, SC-002) | `::test_import_export_roundtrip` (example file + mini/daily/monthly/all_sources/unicode variants) |
| Invalid file import, replace semantics (US3-3/4, FR-012) | `::test_import_invalid_file`, `::test_import_existing_requires_replace` |
| Invalid stored config: get raises, list skips + warns (FR-007a) | `::test_invalid_stored_config` |
| Invalid names (FR-014) | `::test_invalid_names` (parametrized) |
| One change log line, no config contents (FR-014a) | `::test_change_logging` |
| Unit of work commit/rollback/close (FR-018/019, SC-003) | `test_db_session_scope.py` |
| Repositories on SQLite (US4, FR-015–017) | `test_repositories.py` |
