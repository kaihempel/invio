# Research: Persistent Research Data Store with Versioned Schema

**Feature**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md) · **Date**: 2026-10-04

Each entry records a decision, why it was made, and the alternatives rejected. No
NEEDS CLARIFICATION items remain.

---

## R1 — Libraries and versions

- **Decision**: Runtime: `sqlalchemy>=2.1` (latest 2.1.3), `alembic>=1.20` (latest 1.20.0),
  `pymysql>=1.2` (latest 1.2.3). No new dev dependencies (SQLAlchemy and Alembic ship inline
  type hints; PyMySQL is only loaded by SQLAlchemy through the URL, never imported by invio code,
  so no `types-PyMySQL` is needed).
- **Rationale**: Mandated by the issue (SQLAlchemy 2.x typed `Mapped[...]`, Alembic, driver
  `mysql+pymysql`). PyMySQL is pure Python, so `uv sync --locked` works without a C toolchain or
  MariaDB client libraries.
- **Alternatives**: `mysqlclient` (faster, needs C build + libmariadb, rejected for install
  friction); `mariadb` connector (needs MariaDB Connector/C, same issue); SQLModel (extra layer
  over SQLAlchemy, not needed).

## R2 — Where the migrations live

- **Decision**: Alembic environment and versions live **inside the package** at
  `src/invio/db/migrations/` (`env.py`, `script.py.mako`, `versions/`). A root `alembic.ini`
  with `script_location = invio.db:migrations` lets developers run `uv run alembic …` from the
  repo root. The `alembic/` placeholder directory is removed. `invio db upgrade` builds the
  Alembic `Config` in code (no ini file needed).
- **Rationale**: invio runs under cron/systemd with an arbitrary working directory and may be
  installed as a wheel; migrations must be found via the package, not via a path relative to the
  CWD (constitution II). Alembic supports the `package:directory` form for `script_location`.
- **Alternatives**: Keep root `alembic/` (as the spec assumption originally said) — breaks
  `invio db upgrade` outside the repo checkout and in installed wheels. Spec assumption updated.

## R3 — MariaDB/MySQL table options and connection charset

- **Decision**: Every table gets
  `{"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}`
  through a shared `__table_args__` (SQLAlchemy's MariaDB dialect honours `mysql_*` options).
  The engine factory forces `charset=utf8mb4` on `mysql*`/`mariadb*` URLs (adds it if missing,
  overrides any other value) and sets a session `init_command` of
  `SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION', time_zone='+00:00'`.
- **Rationale**: Explicit per-table charset keeps 4-byte emoji working even when the server or
  database default is `latin1`/`utf8mb3` (spec edge case). The connection charset must also be
  `utf8mb4` or emoji are mangled on the wire. Strict mode guarantees invalid ENUM values and
  out-of-range numbers are rejected rather than silently truncated, whatever the server config.
  UTC session time zone makes server-side time functions consistent with R6.
- **Alternatives**: Rely on server defaults (fails the edge case); `utf8mb4_0900_ai_ci`
  (MySQL 8-only, unknown to MariaDB); `utf8mb4_bin` (case-sensitive job names, surprising for
  operators).

## R4 — Column types per engine

- **Decision**:
  | Need | Type |
  |---|---|
  | Primary keys | `BigInteger().with_variant(Integer, "sqlite")`, autoincrement |
  | `url_hash`, `content_hash` | `CHAR(64)` |
  | Full URL | `Text` (not indexed; lookups use `url_hash`) |
  | `raw_content`, digest `body` | `Text().with_variant(mysql.MEDIUMTEXT(), "mysql", "mariadb")` (16 MB) |
  | `title` | `String(1000)` |
  | `relevance` | `Numeric(3, 2, asdecimal=True)` → `Decimal` |
  | `cost_usd` | `Numeric(12, 6, asdecimal=True)` |
  | JSON fields | `sqlalchemy.JSON` |
  | Timestamps | `UTCDateTime` (R6) over `DateTime().with_variant(mysql.DATETIME(fsp=6), …)` |
- **Rationale**: SQLite only autoincrements `INTEGER PRIMARY KEY`, so the BigInteger variant is
  needed. A unique index over a long `TEXT` URL is impossible in InnoDB (3072-byte key limit),
  hence `CHAR(64)` SHA-256 hex. MEDIUMTEXT meets FR-005's 16 MB. Microsecond `DATETIME(6)`
  avoids lock-time rounding on MariaDB. `Numeric` returns exact `Decimal` values (spec US2-5).
- **Alternatives**: `LONGTEXT` (4 GB, unnecessary); `BINARY(32)` hash (smaller but unreadable
  and harder to debug; the issue mandates `CHAR(64)`); `Float` relevance (inexact round-trip).
- **Note**: SQLite stores `Numeric` as a float internally and SQLAlchemy emits a one-time
  "Decimal not natively supported" warning; two-decimal values still round-trip exactly
  through SQLAlchemy's conversion. The warning is filtered in pytest config.

## R5 — Status enumerations

- **Decision**: Define `ItemStatus`, `RunStatus`, `NotificationStatus` as `enum.StrEnum` in
  `src/invio/domain.py` (stdlib only). Columns use
  `sqlalchemy.Enum(<StrEnum>, name=…, native_enum=True, create_constraint=True,
  validate_strings=True, values_callable=lambda e: [m.value for m in e])`; item `type` uses the
  same pattern with values from `typing.get_args(ItemType)`.
- **Rationale**: Statuses are shared contracts consumed by graph, scheduling and notify; the
  constitution requires them to be defined once in the dependency-free domain module. On MariaDB
  this yields a native `ENUM(...)` (rejected under strict mode, R3); on SQLite a `VARCHAR` plus a
  named `CHECK` constraint. `values_callable` stores the lower-case values (`skipped_keyword`),
  not the member names.
- **Alternatives**: Plain `String` (no DB-level rejection, fails FR-006/006a); lookup tables
  (overkill for fixed vocabularies).

## R6 — Time handling

- **Decision**: A small `UTCDateTime` `TypeDecorator` stores naive UTC and returns aware UTC
  `datetime`s; writing a naive `datetime` raises `ValueError`. Defaults (`created_at`,
  `updated_at`, `started_at`) are Python-side (`default=utcnow`, `onupdate=utcnow`).
- **Rationale**: FR-016 requires UTC instants on both engines; neither MariaDB `DATETIME` nor
  SQLite has a time zone. Rejecting naive inputs catches local-time bugs at the boundary
  (constitution I). Python-side defaults behave identically on both engines.
- **Alternatives**: `TIMESTAMP` on MariaDB (2038 limit, session-tz conversion surprises);
  `DateTime(timezone=True)` (ignored by both engines).

## R7 — Foreign keys and delete behaviour

- **Decision**: `job_id` columns: `ForeignKey("jobs.id", ondelete="CASCADE")`, `NOT NULL`.
  Optional `run_id` (items, digests, notifications, llm_usage) and `digest_id` (notifications):
  `ondelete="SET NULL"`, nullable. The engine factory registers a `connect` listener issuing
  `PRAGMA foreign_keys=ON` for SQLite connections (production and tests alike). Models declare
  **no** ORM `relationship()`s in this issue; deletes rely on the database.
- **Rationale**: Implements FR-008/008a and the clarified run/digest behaviour. SQLite ignores
  FKs unless the pragma is set per connection (spec edge case). Without relationships the ORM
  never tries to null or load children itself, so the database is the single enforcer and the
  behaviour is identical on both engines. Relationships can be added when a consumer needs them.
- **Alternatives**: ORM-level `cascade="all, delete-orphan"` (would load all children into
  memory and could disagree with DB rules); relationships with `passive_deletes=True` (fine but
  speculative now).

## R8 — Constraint naming and indexes

- **Decision**: `MetaData(naming_convention=…)` with `ix_%(table_name)s_%(column_0_N_name)s`,
  `uq_%(table_name)s_%(column_0_N_name)s`, `ck_%(table_name)s_%(constraint_name)s`,
  `fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s`, `pk_%(table_name)s`.
  Indexes: `uq_items_job_id_url_hash`, `uq_jobs_name`, `ix_jobs_enabled_next_run_at`
  (composite), `ix_runs_job_id_started_at`, plus FK indexes on every `run_id`/`digest_id` and
  on `job_id` where not covered by a composite index starting with it.
- **Note**: `ix_%(column_0_N_label)s` was rejected: labels include the table name per column
  (`ix_jobs_enabled_jobs_next_run_at`); verified with SQLAlchemy 2.1.
- **Rationale**: Deterministic names make migrations reproducible and downgrades/alterations
  possible on SQLite batch mode. InnoDB requires an index on every FK column (it would create
  unnamed ones otherwise, causing autogenerate drift).
- **Alternatives**: Unnamed constraints (engine-generated names, drift between engines).

## R9 — Initial migration

- **Decision**: One hand-reviewed revision `0001_initial_schema` (revision id `"0001"`,
  `down_revision=None`) generated with `alembic revision --autogenerate` against an empty
  SQLite DB, then reviewed: creates `jobs → runs → digests → items → notifications → llm_usage`
  in FK order; `downgrade()` drops them in reverse. `env.py` uses `render_as_batch=True`
  (future SQLite ALTERs), `compare_type=True`, and does **not** call `logging.config.fileConfig`.
  A drift test runs Alembic's `compare_metadata` after `upgrade head` and asserts no
  differences (SQLite; on MariaDB JSON-vs-LONGTEXT reflection noise is ignored).
- **Rationale**: Readable sequential ids; drift test proves models and migration agree, which
  lets the per-test SQLite fixture use the faster `metadata.create_all()` safely. Skipping
  `fileConfig` keeps invio's structured JSON logging intact (constitution V).
- **Alternatives**: Using `create_all` in production (no versioning — violates FR-010).

## R10 — `env.py` connection resolution

- **Decision**: Priority: (1) an existing `Connection` passed via
  `config.attributes["connection"]` (tests, in-memory SQLite); (2) `config.attributes["url"]`
  set by the CLI; (3) `get_settings().require_secret("database_url")` for plain
  `uv run alembic …`. Engines always come from `invio.db.session.create_db_engine` so the R3/R7
  connection rules apply to migrations too.
- **Rationale**: In-memory SQLite exists only on one connection, so tests must hand it in. The
  URL stays a `SecretStr` until the engine is built and is never written into the `Config`
  `sqlalchemy.url` option (which Alembic may log).

## R11 — `invio db upgrade` command and error handling

- **Decision**: `src/invio/cli/commands/db.py` exposes `app = typer.Typer(help=…)` with
  command `upgrade [REVISION]` (default `head`). Exit codes: 0 success (also when already at
  head); 2 on `MissingSettingError` or an unparsable URL (`sqlalchemy.exc.ArgumentError`);
  1 on any other `SQLAlchemyError`/`alembic.util.CommandError`. stdout: one line
  `database at revision <rev>`; stderr: structured logs and a one-line error. Error text is
  built from the exception type and `str(exc.orig)`/`str(exc)` passed through `redact()`, which
  replaces the URL password (and the full URL rendered via
  `URL.render_as_string(hide_password=True)`) with `***`.
- **Rationale**: Constitution II (exit codes, stdout/stderr split) and V/FR-012 (no secrets).
  PyMySQL error messages contain user and host but redaction protects against drivers or
  future code that echo the URL.
- **Alternatives**: Also exposing `downgrade`/`current` (not required; spec assumption keeps
  downgrade a developer action via `uv run alembic`).

## R12 — Test database fixtures

- **Decision**: `tests/conftest.py` reads `INVIO_TEST_DATABASE_URL` **at import time** into a
  module constant (the autouse `isolated_settings` fixture deletes all `INVIO_*` variables
  before each test). Fixtures in `tests/db_helpers.py` / `conftest.py`:
  - `db_engine`: SQLite → a fresh `sqlite+pysqlite://` engine with `StaticPool` per test,
    schema via `metadata.create_all()`. MariaDB → session-scoped engine; schema brought to head
    once via Alembic.
  - `db_session`: SQLite → plain `Session`. MariaDB → connection-level outer transaction with
    `Session(join_transaction_mode="create_savepoint")`, rolled back after each test (isolation
    without re-creating tables; integrity-error tests roll back only their savepoint).
  - `migration_url` / migration tests: SQLite → fresh temp-file DB per test; MariaDB → the test
    DB, `downgrade base` then `upgrade head` (leaves it at head for the remaining tests).
  - All persistence tests carry the `db` marker (registered in `pyproject.toml`); the MariaDB CI
    job runs `pytest -m db`.
- **Rationale**: Satisfies FR-013/014 and SC-006 (no server needed locally; in-memory is fast).
  Savepoint isolation is the documented SQLAlchemy 2 recipe and keeps MariaDB runs fast.
- **Alternatives**: Recreating tables per test on MariaDB (slow DDL, implicit commits);
  `pytest-mysql`/testcontainers dependency (CI service container is simpler).

## R13 — CI job

- **Decision**: New job `mariadb` in `.github/workflows/ci.yml`, `continue-on-error: true`,
  `services.mariadb: image mariadb:11.4` with `MARIADB_DATABASE=invio_test` and
  `MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1` (no credential in the committed workflow — constitution V),
  health check `healthcheck.sh --connect --innodb_initialized`, port 3306.
  Env `INVIO_TEST_DATABASE_URL=mysql+pymysql://root@127.0.0.1:3306/invio_test?charset=utf8mb4`.
  Steps: checkout, setup-uv, `uv sync --locked`, `uv run pytest -m db`. A container starts fresh
  per CI run, satisfying "fresh MariaDB".
- **Rationale**: FR-015; non-blocking per spec assumption, but must be green before merging
  DB changes. A password-less root on an ephemeral, localhost-only service container avoids
  committing any credential.
- **Alternatives**: Matrix over MySQL 8 too (not required now; easy to add later).

## R14 — Coverage and typing

- **Decision**: The engine-level MariaDB branches (URL normalisation, `init_command`) are
  covered by pure unit tests on `normalize_url()` / `connect_args_for()` that run without a
  server, keeping the 95 % coverage gate green on the SQLite-only required job. `migrations/env.py`
  and the revision file are executed by migration tests. mypy strict needs no plugin with
  SQLAlchemy 2.x `Mapped[...]`.
