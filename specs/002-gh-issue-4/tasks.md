---

description: "Task list for issue #4 — persistent research data store with versioned schema"
---

# Tasks: Persistent Research Data Store with Versioned Schema

**Input**: Design documents from `specs/002-gh-issue-4/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/cli-db.md,
contracts/python-api.md, quickstart.md

**Tests**: Included — required by constitution principle III (every acceptance criterion
covered, rejection paths included). Write each story's tests first and confirm they fail
before implementing.

**Organization**: Tasks are grouped by user story (spec.md) in priority order:
US1 (P1) → US2 (P1) → US3 (P2) → US4 (P2) → US6 (P2) → US5 (P3).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story from spec.md (US1–US6)

## Conventions that apply to every task

- Package is `invio` (issue's `scout/` → `src/invio/`; `scout db upgrade` → `invio db upgrade`;
  `SCOUT_TEST_DATABASE_URL` → `INVIO_TEST_DATABASE_URL`). Work on branch `gh-issue-4`.
- Research decisions are referenced as R1–R14 ([research.md](./research.md)); column specs are
  in [data-model.md](./data-model.md); public names in
  [contracts/python-api.md](./contracts/python-api.md).
- `tests/conftest.py` has an **autouse fixture that deletes every `INVIO_*` variable and
  `chdir`s into `tmp_path`**. Anything read from the environment for tests must be captured at
  module import time; repo files must be located via
  `REPO_ROOT = Path(__file__).resolve().parents[1]`.
- Every persistence test carries `@pytest.mark.db` (module-level `pytestmark = pytest.mark.db`)
  and uses the `db_session` / `db_engine` fixtures, never a hard-coded URL, so it runs on both
  SQLite and MariaDB (FR-014).
- No ORM `relationship()`s (R7). After deleting rows, call `session.expire_all()` and re-query
  to observe DB-side `CASCADE` / `SET NULL`.
- Match existing style: module docstrings, Python 3.12 typing (`X | None`, `StrEnum`), ruff line
  length 100, `mypy --strict` clean over `src/`, coverage ≥ 95 %.
- `invio.domain` must stay stdlib-only (existing fresh-interpreter import test).
- Never print, log or put into an Alembic `Config` option the database URL with its password
  (FR-012, R10, R11).

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependencies and test configuration

- [X] T001 Create branch `gh-issue-4` from `main`; add runtime deps `sqlalchemy>=2.1`, `alembic>=1.20`, `pymysql>=1.2` with `uv add` so `pyproject.toml` and `uv.lock` are updated (R1)
- [X] T002 In `pyproject.toml` `[tool.pytest.ini_options]` register marker `db: persistence tests that run on SQLite and, with INVIO_TEST_DATABASE_URL, on MariaDB` under `markers`, (superseded: the Decimal `filterwarnings` entry is applied per module via `pytest.mark.filterwarnings` in `tests/test_db_items.py` / `tests/test_db_records.py`, not globally) and originally `filterwarnings = ["ignore:Dialect sqlite\\+pysqlite does \\*not\\* support Decimal objects natively:sqlalchemy.exc.SAWarning"]` (R4 note)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Domain enums, column types, models, engine factory and the SQLite test fixtures
that every story uses

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Tests (write first, must fail)

- [X] T003 [P] Extend `tests/test_domain.py`: `ItemStatus` values exactly `new, extracted, skipped_keyword, skipped_irrelevant, relevant, summarized, failed`; `RunStatus` exactly `running, succeeded, partial, failed`; `NotificationStatus` exactly `pending, sent, failed, skipped`; each is a `StrEnum` (`ItemStatus.SKIPPED_KEYWORD == "skipped_keyword"`); `url_hash("https://example.com/ä")` is 64 lower-case hex chars equal to `hashlib.sha256(url.encode("utf-8")).hexdigest()`; existing fresh-interpreter test additionally asserts `"sqlalchemy" not in sys.modules`
- [X] T004 [P] Create `tests/test_db_types.py`: `UTCDateTime` round-trip of an aware non-UTC datetime returns the same instant with `tzinfo=UTC`; writing a naive datetime raises `ValueError` (wrapped in `StatementError` when via ORM); `None` passes through; `utcnow()` is aware UTC; `normalize_url("mysql+pymysql://u:p@h/db")` gets `charset=utf8mb4`, `…?charset=latin1` is overridden to `utf8mb4`, `mariadb+pymysql://…` likewise, `sqlite://` unchanged, garbage like `"not a url"` raises `sqlalchemy.exc.ArgumentError`; `connect_args_for(url)` for mysql contains `init_command` with `STRICT_ALL_TABLES` and `time_zone='+00:00'` and is empty for sqlite; `redact("…s3cret… mysql+pymysql://u:s3cret@h/db", url)` contains no `s3cret`
- [X] T005 [P] Create `tests/test_db_engine.py` (`pytestmark = pytest.mark.db`): an engine from `create_db_engine("sqlite://")` returns `1` for `PRAGMA foreign_keys`; `session_factory(engine)()` has `expire_on_commit=False`; `Base.metadata.tables` keys are exactly `{"jobs","runs","items","digests","notifications","llm_usage"}`; every table has `mysql_engine == "InnoDB"`, `mysql_charset == "utf8mb4"`, `mysql_collate == "utf8mb4_unicode_ci"` in `table.dialect_options["mysql"]`; compiling `CreateTable(Item.__table__)` with `sqlalchemy.dialects.mysql.dialect()` contains `CHAR(64)`, `MEDIUMTEXT`, `DECIMAL(3, 2)`, `ENUM('new','extracted',…)` and `ON DELETE CASCADE`

### Implementation

- [X] T006 [P] Add to `src/invio/domain.py` (stdlib only): `ItemStatus`, `RunStatus`, `NotificationStatus` as `enum.StrEnum` with the values from data-model.md, and `def url_hash(url: str) -> str` returning `hashlib.sha256(url.encode("utf-8")).hexdigest()`; update the module docstring
- [X] T007 [P] Create `src/invio/db/types.py`: `utcnow() -> datetime` (aware UTC) and `class UTCDateTime(TypeDecorator[datetime])` with `impl = DateTime`, `cache_ok = True`, `load_dialect_impl` returning `mysql.DATETIME(fsp=6)` for `mysql`/`mariadb` dialects; `process_bind_param` raises `ValueError("naive datetime not allowed; use an aware datetime")` for naive input and stores `value.astimezone(UTC).replace(tzinfo=None)`; `process_result_value` returns `value.replace(tzinfo=UTC)` (R6)
- [X] T008 Create `src/invio/db/models.py` per data-model.md (R4, R5, R7, R8): `Base(DeclarativeBase)` with `metadata = MetaData(naming_convention={"ix": "ix_%(table_name)s_%(column_0_N_name)s", "uq": "uq_%(table_name)s_%(column_0_N_name)s", "ck": "ck_%(table_name)s_%(constraint_name)s", "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s", "pk": "pk_%(table_name)s"})`; shared `TABLE_ARGS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}`; helpers `PK = BigInteger().with_variant(Integer, "sqlite")`, `LONG_TEXT = Text().with_variant(mysql.MEDIUMTEXT(), "mysql", "mariadb")`, `_enum(cls, name)` → `Enum(cls, name=name, native_enum=True, create_constraint=True, validate_strings=True, values_callable=lambda e: [m.value for m in e])`; models `Job`, `Run`, `Item`, `Digest`, `Notification`, `LlmUsage` with typed `Mapped[...]` columns exactly as in data-model.md — including `jobs.name String(200)` unique, `items.url_hash CHAR(64)`, `items.title String(1000)`, `items.relevance Numeric(3, 2, asdecimal=True)`, `items.status` default and `server_default` `"new"`, `items.attempts` default/`server_default` `0`, `digests.title String(500)`, `digests.item_ids JSON` default `list`, `notifications.channel String(32)`, `notifications.recipient String(320)`, `llm_usage.provider String(32)`, `llm_usage.model String(200)`, `llm_usage.purpose String(64)`, `llm_usage.cost_usd Numeric(12, 6)`; every `job_id` `ForeignKey("jobs.id", ondelete="CASCADE")` NOT NULL; every `run_id` `ForeignKey("runs.id", ondelete="SET NULL")` nullable; `notifications.digest_id` `ForeignKey("digests.id", ondelete="SET NULL")`; constraints `UniqueConstraint("job_id", "url_hash")`, `CheckConstraint("relevance >= 0 AND relevance <= 1", name="relevance_range")`, `CheckConstraint("attempts >= 0", name="attempts_non_negative")`, `CheckConstraint("input_tokens >= 0", name="input_tokens_non_negative")`, `CheckConstraint("output_tokens >= 0", name="output_tokens_non_negative")`; indexes `Index(None, "enabled", "next_run_at")` on jobs, `Index(None, "job_id", "started_at")` on runs, single-column indexes on every `run_id`/`digest_id` and on `job_id` of digests/notifications/llm_usage; item `type` uses `Enum(*get_args(ItemType), name="item_type", native_enum=True, create_constraint=True)`; timestamps use `UTCDateTime` with `default=utcnow` (and `onupdate=utcnow` for `updated_at`); no `relationship()`
- [X] T009 [P] Create `src/invio/db/session.py` (R3, R7, R11): `normalize_url(url) -> URL` (`make_url`; for backend `mysql`/`mariadb` set query `charset=utf8mb4` via `url.update_query_dict(..)`); `connect_args_for(url) -> dict[str, str]` returning `{"init_command": "SET SESSION sql_mode='STRICT_ALL_TABLES,NO_ENGINE_SUBSTITUTION', time_zone='+00:00'"}` for mysql/mariadb else `{}`; `create_db_engine(url, **kwargs) -> Engine` (mysql: `pool_pre_ping=True`, `pool_recycle=3600`; sqlite: `event.listen(engine, "connect", …)` executing `PRAGMA foreign_keys=ON`); `session_factory(engine) -> sessionmaker[Session]` with `expire_on_commit=False`; `redact(text, url) -> str` replacing the password and `str(url)` / `url.render_as_string(hide_password=False)` with `***`
- [X] T010 Create `tests/db_helpers.py` and add fixtures to `tests/conftest.py`: module constant `TEST_DATABASE_URL = os.environ.get("INVIO_TEST_DATABASE_URL")` read at import (before `isolated_settings` strips it); fixture `db_engine` — when `TEST_DATABASE_URL` is unset or starts with `sqlite`, a fresh `create_db_engine("sqlite+pysqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})` per test with `Base.metadata.create_all()` and `dispose()` afterwards (MariaDB branch added in T026); fixture `db_session` yielding a `Session` bound to `db_engine`, closed after the test; helpers in `db_helpers.py`: `make_job(session, name="job-a", **kw) -> Job`, `make_run(session, job, **kw)`, `make_item(session, job, url="https://example.com/a", **kw)` (fills `url_hash` via `domain.url_hash`, `type="article"`, `title="t"`), `make_digest`, `make_notification`, `make_llm_usage`; each adds + flushes and returns the row

**Checkpoint**: `uv run pytest tests/test_domain.py tests/test_db_types.py tests/test_db_engine.py` passes; models can be created on SQLite

---

## Phase 3: User Story 1 - Create and upgrade the database schema from the CLI (Priority: P1) 🎯 MVP

**Goal**: Versioned migrations create/remove all six tables on SQLite and MariaDB;
`invio db upgrade` applies them with the exit-code contract of contracts/cli-db.md

**Independent Test**: Upgrade an empty DB → six tables + `alembic_version`; upgrade again →
no change, exit 0; downgrade base → only `alembic_version` remains

### Tests for User Story 1 (write first, must fail)

- [X] T011 [P] [US1] Create `tests/test_db_migrations.py` (`pytestmark = pytest.mark.db`), using fixture `migration_engine` (SQLite: fresh temp-file `sqlite:///{tmp_path}/m.sqlite` via `create_db_engine`; MariaDB branch added in T027): `upgrade(alembic_config(connection=conn), "head")` creates exactly `{"jobs","runs","items","digests","notifications","llm_usage","alembic_version"}` (via `inspect(engine).get_table_names()`) and `current_revision(conn) == "0001"`; second upgrade is a no-op; `downgrade(cfg, "base")` leaves only `alembic_version`; upgrade leaves a pre-existing unrelated table `other(id INTEGER PRIMARY KEY)` untouched; drift test: after upgrade, `alembic.autogenerate.compare_metadata(MigrationContext.configure(conn), Base.metadata)` returns `[]` (on mysql/mariadb filter out type diffs where model type is `JSON`)
- [X] T012 [P] [US1] Create `tests/test_cli_db.py` using `typer.testing.CliRunner` (stderr separate) and `monkeypatch.setenv`: with `INVIO_DATABASE_URL=sqlite:///{tmp_path}/cli.sqlite` → exit 0, stdout exactly `database at revision 0001\n`, run again → exit 0 same output; unset URL → exit 2, stderr contains `INVIO_DATABASE_URL is not set`; `INVIO_DATABASE_URL=not-a-url` → exit 2, stderr starts with `Configuration error:`; `INVIO_DATABASE_URL=sqlite:////nonexistent-dir/x.sqlite` → exit 1, stderr contains `Error:`; with `INVIO_DATABASE_URL=mysql+pymysql://invio:s3cret@127.0.0.1:1/x` → exit 1 and `"s3cret"` appears in neither stdout nor stderr nor captured log records; `invio --help` lists `db`

### Implementation for User Story 1

- [X] T013 [US1] Delete the placeholder directory `alembic/` (`alembic/.gitkeep`, `alembic/README.md`) and create `alembic.ini` at repo root with `[alembic]` `script_location = invio.db:migrations`, `file_template = %%(rev)s_%%(slug)s`, `prepend_sys_path = .`, no `sqlalchemy.url`, and no logging sections (R2)
- [X] T014 [US1] Create package `src/invio/db/migrations/` with `__init__.py` (docstring only), `script.py.mako` (standard Alembic template, typed `upgrade() -> None` / `downgrade() -> None`, module docstring), `versions/__init__.py`, and `env.py` (R9, R10): resolve connection in order `config.attributes["connection"]` → `config.attributes["url"]` → `get_settings().require_secret("database_url")`; engines via `invio.db.session.create_db_engine`; `context.configure(connection=…, target_metadata=Base.metadata, render_as_batch=True, compare_type=True)`; offline mode supported with the same URL resolution; do **not** call `logging.config.fileConfig`
- [X] T015 [US1] Create `src/invio/db/migrate.py` (contracts/python-api.md): `MIGRATIONS = "invio.db:migrations"`; `alembic_config(*, url=None, connection=None) -> Config` setting `script_location` and putting `url`/`connection` into `config.attributes` only; `upgrade(config, revision="head") -> str` (runs `alembic.command.upgrade`, returns resulting revision via `current_revision`); `downgrade(config, revision="base") -> None`; `current_revision(connection) -> str | None` via `MigrationContext.configure(connection).get_current_revision()`
- [X] T016 [US1] Generate `src/invio/db/migrations/versions/0001_initial_schema.py` with `uv run alembic revision --autogenerate --rev-id 0001 -m "initial schema"` against an empty temp SQLite DB (`INVIO_DATABASE_URL=sqlite:////tmp/…`), then review by hand: `revision = "0001"`, `down_revision = None`; create tables in FK order `jobs, runs, digests, items, notifications, llm_usage`; all named constraints/indexes from T008 present; every `op.create_table` passes `mysql_engine="InnoDB", mysql_charset="utf8mb4", mysql_collate="utf8mb4_unicode_ci"`; MEDIUMTEXT/DATETIME(6) variants and `ondelete` rules preserved; `downgrade()` drops indexes/tables in reverse order; ruff-format the file
- [X] T017 [US1] Create `src/invio/cli/commands/db.py` (contracts/cli-db.md, R11): `app = typer.Typer(help="Database schema management.", no_args_is_help=True)`; command `upgrade(revision: str = typer.Argument("head"))`: get URL via `get_settings().require_secret("database_url")`; `normalize_url`; `migrate.upgrade(alembic_config(url=…), revision)`; `typer.echo(f"database at revision {rev}")`; log `migration started` / `migration finished` (with `revision`, never the URL); `MissingSettingError` and `ArgumentError` → `typer.echo(f"Configuration error: {redacted}", err=True)` + `Exit(2)`; `SQLAlchemyError`, `alembic.util.CommandError`, `OSError` → `typer.echo(f"Error: {type(exc).__name__}: {redacted}", err=True)` + `Exit(1)`, where the message is passed through `session.redact(..., url)`

**Checkpoint**: T011/T012 pass; `uv run invio db upgrade` works per quickstart §2

---

## Phase 4: User Story 2 - Store discovered items without duplicates (Priority: P1)

**Goal**: Items are unique per (job, url_hash) and carry status, attempts, last error and
relevance exactly as specified

**Independent Test**: Duplicate (job, url_hash) for the same job → `IntegrityError`; for a
different job → accepted

### Tests for User Story 2 (write first, must fail)

- [X] T018 [P] [US2] Create `tests/test_db_items.py` (`pytestmark = pytest.mark.db`): second item with same `url_hash` for the same job → `IntegrityError` on flush; same `url_hash` for two jobs → both stored; new item defaults `status == ItemStatus.NEW` and `attempts == 0` (also when inserted via raw `text("INSERT INTO items (job_id, url, url_hash, type, title, created_at, updated_at) …")`); every `ItemStatus` member round-trips; raw-SQL insert with `status='bogus'` raises `IntegrityError` or `DataError` (`pytest.raises((IntegrityError, DataError))`); ORM assignment `status="bogus"` raises `StatementError` on flush; `relevance=Decimal("0.75")` reads back `Decimal("0.75")`, `Decimal("0.00")` and `Decimal("1.00")` accepted, `Decimal("1.01")` and `Decimal("-0.01")` rejected (`IntegrityError` / `DataError`); `attempts=-1` rejected; `last_error` stores 10 000 chars; a 5 000-character URL and a 5 MB `raw_content` round-trip unchanged; `type` other than `article`/`video` rejected
- [X] T019 [P] [US2] Create `tests/test_db_records.py` (`pytestmark = pytest.mark.db`): `Run` default `status == RunStatus.RUNNING`, every `RunStatus` round-trips, raw-SQL `status='done'` rejected; `Notification` default `status == NotificationStatus.PENDING`, every member round-trips, `status='bounced'` rejected; JSON round-trip of `Job.config={"sources":[{"type":"rss","url":"https://e.x/ä"}],"n":1}`, `Run.stats`, `Digest.item_ids=[1,2]`, `Notification.payload`; `LlmUsage.cost_usd=Decimal("0.001234")` round-trips; negative `input_tokens` rejected; timestamps read back aware UTC

### Implementation for User Story 2

- [X] T020 [US2] Fix any gaps in `src/invio/db/models.py` revealed by T018/T019 (e.g. missing `server_default`, CHECK names, `validate_strings`); if a column/constraint changes, update `src/invio/db/migrations/versions/0001_initial_schema.py` to match so the T011 drift test stays green

**Checkpoint**: Item uniqueness and status/relevance rules hold on SQLite

---

## Phase 5: User Story 3 - Delete a job together with all its history (Priority: P2)

**Goal**: Deleting a job removes its runs, items, digests, notifications and LLM usage;
deleting a run/digest only clears links (FR-008, FR-008a)

**Independent Test**: Job with one of each dependent record → delete job → zero dependents;
other job untouched

### Tests for User Story 3 (write first, must fail)

- [X] T021 [P] [US3] Create `tests/test_db_cascade.py` (`pytestmark = pytest.mark.db`): build jobs A and B each with one run, item (linked to run), digest (linked to run), notification (linked to run and digest) and llm_usage (linked to run); `session.delete(job_a)`, flush, `expire_all()` → counts for job A's ids in all five tables are 0 and job B's rows all remain; deleting a run alone → its job remains and the linked item, digest, notification and llm_usage still exist with `run_id is None`; deleting a digest alone → the notification remains with `digest_id is None`; deleting an item alone leaves job and run intact; inserting an item with a non-existent `job_id` raises `IntegrityError` (proves FKs are enforced on SQLite)

### Implementation for User Story 3

- [X] T022 [US3] Fix any `ondelete` / FK gaps in `src/invio/db/models.py` and mirror them in `src/invio/db/migrations/versions/0001_initial_schema.py`; confirm `create_db_engine` registers the SQLite `PRAGMA foreign_keys=ON` listener for every connection (T009)

**Checkpoint**: Cascade and SET NULL behaviour verified on SQLite

---

## Phase 6: User Story 4 - Preserve international text and emoji (Priority: P2)

**Goal**: Titles and summaries with umlauts and 4-byte emoji round-trip byte-for-byte

**Independent Test**: Store "Größenänderung — Übersicht 🚀🇩🇪" as title/summary and read back
identical on SQLite and MariaDB

### Tests for User Story 4 (write first, must fail)

- [X] T023 [P] [US4] Create `tests/test_db_unicode.py` (`pytestmark = pytest.mark.db`), parametrized over `["Größenänderung — Übersicht", "Ärger über Öl & Süßes ß", "Launch 🚀🇩🇪👩‍💻", "混合 текст ✓ 🧪"]`: store as `Item.title` and `Item.summary`, commit, new session, read back equal (`==` and same `encode("utf-8")`); `Job.name="Fühler-🚀"` round-trips; JSON `Job.config` with emoji round-trips; on mysql/mariadb only (skip on sqlite): `SELECT @@character_set_connection` is `utf8mb4` and `information_schema.TABLES.TABLE_COLLATION` for all six tables is `utf8mb4_unicode_ci`

### Implementation for User Story 4

- [X] T024 [US4] Fix any charset gaps revealed by T023 in `src/invio/db/session.py` (URL charset, `init_command`) or table options in `src/invio/db/models.py` / `0001_initial_schema.py`

**Checkpoint**: Unicode round-trip verified on SQLite (MariaDB in US6)

---

## Phase 7: User Story 6 - Run the persistence tests against both engines (Priority: P2)

**Goal**: The same `db` tests run on in-memory SQLite by default and on MariaDB when
`INVIO_TEST_DATABASE_URL` is set; optional CI job (FR-013–FR-015)

**Independent Test**: `uv run pytest -m db` passes without a server; passes again with
`INVIO_TEST_DATABASE_URL` pointing at MariaDB (quickstart §4)

### Tests for User Story 6 (write first, must fail)

- [X] T025 [P] [US6] Create `tests/test_db_fixtures.py` (`pytestmark = pytest.mark.db`): two tests that each insert job `name="iso"` both pass (proves per-test isolation on either engine); `db_engine.dialect.name` equals `"sqlite"` when `TEST_DATABASE_URL` is unset and the URL's backend otherwise; with `TEST_DATABASE_URL` unset the engine URL is in-memory (`database in (None, "", ":memory:")`); the `isolated_settings` fixture still leaves `INVIO_TEST_DATABASE_URL` absent from `os.environ` inside tests

### Implementation for User Story 6

- [X] T026 [US6] Extend `tests/conftest.py` MariaDB branch (R12): when `TEST_DATABASE_URL` is a mysql/mariadb URL, a session-scoped `_server_engine` = `create_db_engine(TEST_DATABASE_URL)` brought to head once with `migrate.upgrade(alembic_config(connection=conn), "head")`; `db_engine` returns it; `db_session` opens `conn = engine.connect()`, `trans = conn.begin()`, yields `Session(bind=conn, join_transaction_mode="create_savepoint")`, then closes session, `trans.rollback()`, `conn.close()`
- [X] T027 [US6] Extend `migration_engine` in `tests/test_db_migrations.py` (or `tests/conftest.py`) for MariaDB: use the server engine, run `downgrade base` before each migration test and ensure `upgrade head` afterwards (`finally`) so the database is always left at head for the savepoint-isolated tests (test order then does not matter)
- [X] T028 [US6] Add job `mariadb` to `.github/workflows/ci.yml` (R13): `runs-on: ubuntu-latest`, `continue-on-error: true`, `services.mariadb` with `image: mariadb:11.4`, env `MARIADB_ALLOW_EMPTY_ROOT_PASSWORD: "1"`, `MARIADB_DATABASE: invio_test` (no password committed — constitution V), `ports: ["3306:3306"]`, `options: >- --health-cmd "healthcheck.sh --connect --innodb_initialized" --health-interval 5s --health-timeout 5s --health-retries 20`; job env `INVIO_TEST_DATABASE_URL: mysql+pymysql://root@127.0.0.1:3306/invio_test?charset=utf8mb4`; steps checkout@v7, setup-uv@v7 (python 3.12, cache), `uv sync --locked`, `uv run pytest -m db`
- [X] T029 [US6] Run the full `db` suite against a local MariaDB container per quickstart §3–4 (`docker run … mariadb:11.4`) and fix any engine-specific failures in `src/invio/db/` or tests (expected hot spots: enum rejection error type, JSON reflection in drift test, `DATETIME(6)` precision, savepoint rollback after `IntegrityError`)
  Verified in CI instead of locally (no Docker on the implementing machine): `mariadb` job on PR #42, MariaDB 11.4 — 125 passed, 2 skipped (SQLite-only), no engine-specific fixes needed.

**Checkpoint**: All `db` tests green on SQLite and on MariaDB

---

## Phase 8: User Story 5 - Find jobs that are due and coordinate their execution (Priority: P3)

**Goal**: Jobs are unique by name; `next_run_at` / `locked_until` round-trip; enabled due jobs
can be found via the `(enabled, next_run_at)` index (FR-007, FR-007a)

**Independent Test**: Mixed enabled/disabled jobs with past/future next-run times → query
returns exactly the enabled, due ones

### Tests for User Story 5 (write first, must fail)

- [X] T030 [P] [US5] Create `tests/test_db_jobs.py` (`pytestmark = pytest.mark.db`): two jobs with the same `name` → `IntegrityError`; `enabled` defaults to `True`; `next_run_at` and `locked_until` (aware, with microseconds, e.g. `datetime(2026, 10, 4, 7, 30, 0, 123456, tzinfo=ZoneInfo("Europe/Berlin"))`) read back as the same instant in UTC; `select(Job).where(Job.enabled.is_(True), Job.next_run_at <= now).order_by(Job.next_run_at)` over jobs {enabled past, enabled now, enabled future, disabled past, enabled NULL} returns exactly [enabled past, enabled now]; `inspect(engine).get_indexes("jobs")` contains `ix_jobs_enabled_next_run_at` with columns `["enabled", "next_run_at"]`; `updated_at` advances after modifying a job

### Implementation for User Story 5

- [X] T031 [US5] Fix any gaps revealed by T030 in `src/invio/db/models.py` (index name/columns, `onupdate`) and mirror them in `0001_initial_schema.py`

**Checkpoint**: All user stories independently functional

---

## Phase 9: Polish & Cross-Cutting Concerns

- [X] T032 [P] Update `README.md`: new "Database" section (supported engines, `INVIO_DATABASE_URL` format `mysql+pymysql://user:pass@host:3306/invio?charset=utf8mb4`, utf8mb4 requirement, server `max_allowed_packet` ≥ 32M for raw content up to 16 MB, `invio db upgrade` with exit codes, developer `uv run alembic downgrade base` / `revision --autogenerate`, running `db` tests against MariaDB with `INVIO_TEST_DATABASE_URL`); update the Layout block (`db/` → models, sessions, migrations; remove the `alembic/` placeholder line; add `alembic.ini`)
- [X] T033 [P] Update `.env.example` comments for `INVIO_DATABASE_URL` / `INVIO_TEST_DATABASE_URL` (SQLite test URL example `sqlite://` for in-memory; MariaDB example) and update `src/invio/db/__init__.py` docstring to describe the package
- [X] T034 Run quality gates: `uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest --cov`; measure `uv run pytest -m db --durations=10` and confirm the `db` tests take < 10 s on SQLite (SC-006); fix all findings, keep coverage ≥ 95 % (add unit tests for uncovered branches, e.g. offline mode in `env.py`, `redact` without password)
- [X] T035 (quickstart §1, §2, §5 and the wheel/outside-repo checks done; §3–4 skipped, no Docker) Walk through `specs/002-gh-issue-4/quickstart.md` §1, §2, §5 (and §3–4 if Docker is available) and confirm every expected output; verify `uv run invio db upgrade` from a directory outside the repo works (packaged migrations)
- [X] T036 Review against spec acceptance criteria and constitution (no secrets in output, domain stdlib-only, no `relationship()`s, new deps justified) and draft the PR description referencing #4 with justification for SQLAlchemy, Alembic and PyMySQL; before merging confirm the optional `mariadb` CI job is green (SC-002, spec assumption)

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)** → **Foundational (Phase 2)** → user stories → **Polish (Phase 9)**
- **US1 (Phase 3)**: depends on Phase 2 only. Owns the migration; later model fixes (T020,
  T022, T024, T031) must be mirrored in `0001_initial_schema.py`.
- **US2, US3, US4, US5**: depend on Phase 2 only (fixtures use `metadata.create_all`); can run
  in parallel with US1 and each other. Their "fix gaps" tasks touch `models.py`, so serialize
  those implementation tasks.
- **US6 (Phase 7)**: depends on US1 (MariaDB fixtures run Alembic) and should come after
  US2–US4 so the MariaDB run (T029) exercises their tests.
- **Polish**: after all stories.

### Within Each User Story

- Tests first and failing → implementation → checkpoint run
- Anything changing `models.py` also updates `0001_initial_schema.py` (drift test T011)

### Parallel Opportunities

- Phase 2 tests T003, T004, T005 in parallel; implementation T006, T007, T009 in parallel
  (T008 after T006/T007; T010 after T008/T009)
- After Phase 2: T011, T012, T018, T019, T021, T023, T030 (all test files) in parallel
- T032, T033 in parallel

---

## Parallel Example: after Phase 2

```bash
# Write all story test files at once (different files, no shared state):
Task: "T011 [US1] tests/test_db_migrations.py"
Task: "T012 [US1] tests/test_cli_db.py"
Task: "T018 [US2] tests/test_db_items.py"
Task: "T021 [US3] tests/test_db_cascade.py"
Task: "T023 [US4] tests/test_db_unicode.py"
Task: "T030 [US5] tests/test_db_jobs.py"
```

## Parallel Example: User Story 1

```bash
Task: "T011 [US1] tests/test_db_migrations.py"
Task: "T012 [US1] tests/test_cli_db.py"
# then sequentially: T013 → T014 → T015 → T016 → T017
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1 + Phase 2 (models, engine, SQLite fixtures)
2. Phase 3 (migrations + `invio db upgrade`)
3. **Stop and validate**: quickstart §2 and §5 — an operator can create the schema

### Incremental Delivery

1. MVP (US1) → schema deployable
2. US2 → duplicate-free item storage verified
3. US3 + US4 → cascade and Unicode guarantees verified
4. US6 → same guarantees proven on MariaDB, CI job added
5. US5 → scheduler-facing job fields verified
6. Polish → docs, gates, PR

---

## Notes

- [P] = different files, no dependencies on incomplete tasks
- Commit after each phase checkpoint; reference `#4` in commit messages
- Acceptance criteria coverage: upgrade/downgrade → T011/T029; duplicate integrity error →
  T018; cascade → T021; umlauts/emoji → T023; tests on both engines → T026–T029
