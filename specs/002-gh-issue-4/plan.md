# Implementation Plan: Persistent Research Data Store with Versioned Schema

**Branch**: `gh-issue-4` | **Date**: 2026-10-04 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/002-gh-issue-4/spec.md` (GitHub issue #4)

## Summary

Add the persistence layer: SQLAlchemy 2.x typed declarative models for `jobs`, `runs`,
`items`, `digests`, `notifications` and `llm_usage` in `src/invio/db/models.py` (InnoDB +
utf8mb4 table options, `CHAR(64)` URL hash with `UNIQUE (job_id, url_hash)`, MEDIUMTEXT raw
content, JSON columns, enum statuses, `ON DELETE CASCADE` from jobs and `SET NULL` from
runs/digests); an engine factory that enforces `utf8mb4`, strict SQL mode and UTC on MariaDB
and foreign keys on SQLite; an Alembic environment packaged at `src/invio/db/migrations/` with
one initial migration; the auto-discovered `invio db upgrade` command; and pytest fixtures that
run the same persistence tests on in-memory SQLite by default and on MariaDB when
`INVIO_TEST_DATABASE_URL` is set, plus an optional MariaDB CI job.

## Technical Context

**Language/Version**: Python 3.12 (uv-managed)

**Primary Dependencies**: New runtime: SQLAlchemy ≥ 2.1, Alembic ≥ 1.20, PyMySQL ≥ 1.2
(R1). Existing: Typer, pydantic-settings.

**Storage**: MariaDB 10.6+/11.x (production, InnoDB, utf8mb4; tested in CI with 11.4). MySQL 8
should work with the same dialect options but is not tested. SQLite (tests, in-memory or temp file)

**Testing**: pytest (existing autouse settings isolation), new `db` marker; optional CI job
against a `mariadb:11.4` service container

**Target Platform**: Linux (cron/systemd) and macOS dev

**Project Type**: CLI application / library modules (single project)

**Performance Goals**: `invio db upgrade` on an empty DB < 1 min (SC-001, in practice
seconds); persistence tests add < 10 s to the local suite (SC-006)

**Constraints**: No network/DB server needed for the default suite; `mypy --strict` clean;
coverage ≥ 95 % on the SQLite-only required job; no credentials in any output (FR-012);
`invio.domain` stays stdlib-only

**Scale/Scope**: ~450 LOC source (models, types, session, migrate, migration, CLI), ~600 LOC
tests; 6 tables, 1 migration, 1 CLI command, 1 CI job

All unknowns resolved in [research.md](./research.md) (R1–R14); no NEEDS CLARIFICATION remain.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Pre-design | Post-design |
|---|---|---|---|
| I. Strict Contracts at Boundaries | Typed models; shared contracts defined once; fail fast | ✅ FR-001, FR-006/006a | ✅ status enums in `invio.domain` (R5), DB-level CHECK/ENUM + strict mode (R3), naive datetimes rejected (R6) |
| II. CLI-First Operation | Via `invio` CLI, auto-discovered, stdout/stderr split, exit codes (config → 2) | ✅ FR-011 | ✅ `commands/db.py` auto-discovered; exit 0/1/2 contract ([cli-db.md](./contracts/cli-db.md)); migrations packaged so CLI works from any CWD (R2) |
| III. Test-Covered Behaviour | Every acceptance criterion tested; unit tests without DB server | ✅ FR-013/014 | ✅ in-memory SQLite default, MariaDB opt-in (R12); acceptance mapping in [quickstart.md](./quickstart.md) |
| IV. Quality Gates Mirror CI | ruff, format, mypy strict, pytest; `uv.lock`, `--locked` | ✅ | ✅ inline-typed libs, no plugin (R14); MariaDB job uses `uv sync --locked`; required gates unchanged |
| V. Secrets / Observability | URL is `SecretStr`, never logged; structured logs | ✅ FR-012 | ✅ URL kept out of Alembic `Config` options (R10), `redact()` on errors (R11), `env.py` skips `fileConfig` so JSON logging stays (R9) |
| Architecture: dependency direction | `cli` → … → adapters (`db`) → `config`/domain | ✅ | ✅ `db` imports only `config.settings` and `domain`; CLI imports `db` |
| Architecture: new runtime deps justified | Justify in PR | ✅ SQLAlchemy/Alembic/PyMySQL mandated by issue | ✅ PyMySQL chosen for pure-Python install (R1) |
| Architecture: simplicity | No speculative abstractions | ✅ | ✅ no ORM relationships, no repository classes, upgrade-only CLI (R7, R11) |
| Workflow: branch `gh-issue-<N>`; docs updated | | ✅ branch `gh-issue-4` | ✅ README: database setup + `invio db upgrade`, layout update |

Result: **PASS** — no violations; Complexity Tracking not needed.

## Project Structure

### Documentation (this feature)

```text
specs/002-gh-issue-4/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── cli-db.md        # `invio db upgrade` behaviour, output, exit codes
│   └── python-api.md    # public names of invio.db.* and new invio.domain enums
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
src/invio/
├── domain.py                    # + ItemStatus, RunStatus, NotificationStatus, url_hash()
├── db/
│   ├── __init__.py              # existing
│   ├── types.py                 # NEW: UTCDateTime, utcnow
│   ├── models.py                # NEW: Base (naming convention), 6 models
│   ├── session.py               # NEW: normalize_url, create_db_engine, session_factory, redact
│   ├── migrate.py               # NEW: alembic_config, upgrade, downgrade, current_revision
│   └── migrations/              # NEW (package data)
│       ├── env.py
│       ├── script.py.mako
│       └── versions/
│           └── 0001_initial_schema.py
└── cli/commands/
    └── db.py                    # NEW: `invio db upgrade`

alembic.ini                      # NEW: script_location = invio.db:migrations (dev use)
alembic/                         # REMOVED (placeholder replaced by packaged migrations)

tests/
├── conftest.py                  # + capture INVIO_TEST_DATABASE_URL at import; db fixtures
├── db_helpers.py                # NEW: engine/session builders, factory helpers (make_job…)
├── test_db_types.py             # NEW: UTCDateTime, normalize_url/connect args, redact
├── test_db_engine.py            # NEW: FK pragma, session factory, table options, MySQL DDL
├── test_db_migrations.py        # NEW: upgrade/downgrade, idempotent upgrade, drift check
├── test_db_items.py             # NEW: uniqueness, item status, relevance, large content
├── test_db_records.py           # NEW: run/notification statuses, JSON, llm_usage, UTC times
├── test_db_fixtures.py          # NEW: per-test isolation, engine selection
├── test_db_cascade.py           # NEW: job cascade, run/digest SET NULL, other job untouched
├── test_db_unicode.py           # NEW: umlauts + emoji round-trip
├── test_db_jobs.py              # NEW: unique name, due-job lookup, lock time round-trip
├── test_cli_db.py               # NEW: exit 0/1/2, stdout line, no password in output
└── test_domain.py               # + status enums, url_hash; still no sqlalchemy import

pyproject.toml / uv.lock         # deps; pytest marker `db`; filter SQLite Decimal warning
.github/workflows/ci.yml         # + optional `mariadb` job (R13)
README.md                        # database setup, `invio db upgrade`, layout update
```

**Structure Decision**: Single project, existing `src/invio/` layout. The issue's `scout/`
paths map to `src/invio/db/`, `invio db upgrade`, `INVIO_TEST_DATABASE_URL`. Migrations move
into the package (R2) so the CLI works from any working directory and in installed wheels; the
spec assumption about the root `alembic/` directory is superseded accordingly.

## Implementation Notes (for /speckit-tasks)

- Order: deps + marker → domain enums/`url_hash` → `types.py` → `models.py` → `session.py`
  → migrations env + `0001` (autogenerate against empty SQLite, then review) → `migrate.py`
  → test fixtures → persistence tests → CLI command + tests → CI job → README.
- `conftest.py`: read `os.environ.get("INVIO_TEST_DATABASE_URL")` at module import, before
  the autouse fixture strips `INVIO_*`.
- Enum rejection tests insert via raw SQL (`text()`) so they hit the DB, and accept
  `IntegrityError` (SQLite CHECK) or `DataError` (MariaDB strict ENUM); a separate test checks
  ORM-level `StatementError` for invalid strings.
- Cascade/SET NULL tests must `session.expire_all()` (or use fresh queries) after deleting,
  because no ORM relationships exist and the identity map may hold stale rows.
- On MariaDB, migration tests run `downgrade base` → `upgrade head` and must leave the DB at
  head for the remaining savepoint-isolated tests; do not run `-n` in parallel there.
- Keep `invio.domain` free of SQLAlchemy; the existing fresh-interpreter import test guards it.
- PR description: justify the three new runtime dependencies (constitution).

## Complexity Tracking

No violations to justify.
