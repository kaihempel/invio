# Research: Job Management Service and Record Access Layer

**Feature**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md) · **Date**: 2026-10-04

Each entry records a decision, why it was made, and the alternatives rejected. No
NEEDS CLARIFICATION items remain.

---

## R1 — Package placement and dependency direction

- **Decision**: Repositories live in `src/invio/db/repositories.py` (adapter layer). The job
  service lives in a new package `src/invio/services/` (`__init__.py`, `jobs.py`). `services`
  sits at the orchestration level: it imports `invio.db`, `invio.config` and `invio.log` (for
  nothing but a logger) and is imported by `cli`, `scheduling` and `graph`. `invio.db` never
  imports `invio.services`.
- **Rationale**: Mirrors the issue (`db/repositories.py`, `services/jobs.py`) mapped to the
  `src/invio/` package (constitution: `cli` → orchestration → adapters → config/domain). A
  separate `services` package keeps the CLI/API-facing contract out of the DB adapter.
- **Alternatives**: Service inside `invio.db` (would make the adapter layer depend on YAML file
  handling and the CLI-facing errors); service inside `invio.scheduling` (wrong owner — the CLI
  and a later API use it too).

## R2 — Unit of work: `session_scope()`

- **Decision**: Add `session_scope(factory: sessionmaker[Session]) -> Iterator[Session]` (a
  `@contextmanager`) to `src/invio/db/session.py`: yields a new session, `commit()` on normal
  exit, `rollback()` on any `BaseException`, `close()` always. Repositories receive the session
  in their constructor and only `add`/`flush`/query — they never commit (FR-016).
- **Rationale**: `session_factory()` already sets `expire_on_commit=False`, so objects loaded in
  a scope stay readable after it ends. SQLAlchemy's `sessionmaker.begin()` would do the same,
  but an explicit helper names the issue's concept, is easy to test (commit/rollback/close) and
  is the one place to add retry or logging later.
- **Alternatives**: `with factory.begin() as s:` used directly by the service (fine, but
  scheduler/pipeline would repeat it); a thread-local `scoped_session` (global state, harder to
  test, not needed for a CLI).

## R3 — Service construction and the `next_run_at` stub

- **Decision**: `JobService(session_factory, *, next_run=…, clock=utcnow)`.
  `next_run: Callable[[ScheduleConfig, datetime], datetime]` defaults to a module-level
  `_next_run_stub(schedule, after) -> after` in `services/jobs.py`. `clock` is the time source
  passed as `after`. A `JobService.from_settings()` class method builds the engine from
  `Settings.database_url` (`require_secret`) for the CLI.
- **Rationale**: Issue #6 (parallel track `cli-sched`) will create
  `invio/scheduling/next_run.py::compute_next_run(schedule, after)` with exactly this
  signature. Creating that file here would conflict in the parallel worktree; keeping the stub
  private and the parameter injectable lets #6 (or a follow-up) switch the default with a
  one-line change. Tests inject a deterministic function/clock and can assert recalculation.
- **Alternatives**: Create `scheduling/next_run.py` as a stub now (merge conflict with #6);
  call `utcnow()` inline (untestable recalculation, not swappable).

## R4 — What the service returns: `JobRecord`

- **Decision**: A frozen, slotted dataclass `JobRecord(name, enabled, config: JobConfig,
  next_run_at: datetime | None, created_at, updated_at)` defined in `services/jobs.py`, built
  from the ORM row inside the unit of work. No database id is exposed.
- **Rationale**: FR-005/FR-020 — callers must not receive ORM objects or need SQLAlchemy.
  `JobConfig` is already frozen/immutable. It cannot live in `invio.domain` because that module
  must stay stdlib-only and `JobConfig` is a Pydantic model.
- **Alternatives**: Return ORM `Job` (leaks SQLAlchemy, lazy-load pitfalls); a Pydantic model
  (no added value, `JobConfig` is already validated).
- **Job id for history writers**: The job `name` is the public identity; the numeric id stays
  internal to the `db` layer. Later consumers that write history (pipeline #19, scheduler #23)
  work inside their own `session_scope` and resolve the id there with
  `JobRepository.get_by_name(name)` before calling `RunRepository.start(job.id)` etc. They are
  repository consumers inside the persistence boundary, not `JobService` callers, so FR-020
  still holds. Exposing `id` on `JobRecord` was rejected: it would invite callers to hold ids
  across units of work (stale after delete/re-create under the same name).

## R5 — Validation on write and read

- **Decision**: Add a public `validate_job(data: Mapping[str, Any]) -> JobConfig` to
  `invio.config.job` that wraps `JobConfig.model_validate` and raises
  `JobConfigError(None, _format_validation_error(exc))`; `load_yaml` reuses it. Service write
  methods accept `JobConfig | Mapping[str, Any]`; a `JobConfig` is re-validated through
  `validate_job(config.model_dump(mode="json"))` so even a `model_construct`-ed object is
  checked (FR-003). The stored value is `config.model_dump(mode="json")`.
  On read, the stored dict goes through `validate_job`; a failure raises
  `StoredJobConfigError(JobConfigError)` carrying the job name (`str()` →
  `invalid stored job 'x':` + error lines).
- **Rationale**: FR-004 requires the same error type and messages as loading a job file;
  reusing `_format_validation_error` via a public helper avoids duplicating it (Constitution I:
  contracts defined once). A subclass lets callers catch `JobConfigError` generically while the
  message names the job (FR-007a).
- **Alternatives**: Catch `ValidationError` in the service and format separately (duplicate
  formatting); trust stored JSON without re-validation (violates FR-007a).

## R6 — Duplicate names and races

- **Decision**: `create` first queries by name (fast, clear error) and also catches
  `IntegrityError` from the `INSERT` flush, translating both into `JobExistsError(name)`. The
  only unique constraint on `jobs` besides the primary key is `name`, so an `IntegrityError` on
  inserting a job is always a duplicate name.
- **Rationale**: The pre-check alone is racy (two CLI processes); the database unique index is
  the arbiter (spec edge case). Translating keeps SQLAlchemy errors out of callers (FR-020).
- **Alternatives**: Rely only on the `IntegrityError` (works, but the pre-check gives the same
  result without a failed statement in the common case); `INSERT … ON DUPLICATE KEY` (dialect
  specific).

## R7 — Job names

- **Decision**: `JobNameError(ValueError)` for names that are empty, have leading/trailing
  whitespace, or exceed 200 characters (column length). Checked before any DB access. No
  further character restrictions.
- **Rationale**: FR-014. Names are user-visible identifiers used on the CLI; silently stripping
  whitespace would make two "different" names collide surprisingly. 200 matches
  `jobs.name String(200)`; checking up front avoids a dialect-specific `DataError` on MariaDB vs
  silent acceptance on SQLite.
- **Alternatives**: Slug-only names (`[a-z0-9-]`) — reasonable but not requested; would reject
  names users may already plan to use. Can be tightened later.

## R8 — Disable/enable semantics and update of disabled jobs

- **Decision**: `set_enabled(name, False)` sets `enabled=False`, `next_run_at=None`.
  `set_enabled(name, True)` on a disabled job sets `enabled=True` and
  `next_run_at=next_run(config.schedule, clock())`. Either call on a job already in the target
  state is a no-op (no write, no change log line). `update` on a disabled job stores the new
  config and leaves `next_run_at=None`.
- **Rationale**: Clarifications Q2 and spec edge cases. No-op avoids moving an enabled job's
  next run.
- **Alternatives**: Recalculate on every `set_enabled(True)` (moves the schedule unexpectedly).

## R9 — Delete and history

- **Decision**: `delete` issues `session.delete(job)`; history removal relies on the existing
  `ON DELETE CASCADE` foreign keys from #4 (SQLite has `PRAGMA foreign_keys=ON` via
  `create_db_engine`). Because the models declare no ORM relationships, the ORM issues a plain
  `DELETE FROM jobs WHERE id=?` and the database cascades.
- **Rationale**: One source of truth for cascade rules (already tested in
  `tests/test_db_cascade.py`).
- **Alternatives**: Explicit per-table deletes in the repository (duplicates schema rules).

## R10 — YAML import/export

- **Decision**: `import_yaml(path, name=None, *, replace=False) -> JobRecord` uses
  `load_yaml(path)` (same errors, FR-012) and `name or Path(path).stem`; existing name →
  `JobExistsError` unless `replace=True`, which performs `update` (history kept).
  `export_yaml(name, path=None) -> str` returns `dump_yaml(record.config)` and, if `path` is
  given, also writes it with `write_yaml` (atomic). `OSError` propagates unchanged.
- **Rationale**: Reuses the #3 file contract, so import→export equality (FR-013) follows from
  the existing `load_yaml`/`dump_yaml` round-trip plus the JSON storage round-trip, which is
  tested here explicitly for the example file and the job-config test fixtures.
- **Alternatives**: Embedding the job name/enabled flag into the exported YAML (breaks
  `JobConfig`'s `extra="forbid"` on re-import).

## R11 — Repository scope

- **Decision**: One class per table, each taking a `Session`. Minimal methods (see
  [contracts/python-api.md](./contracts/python-api.md)): jobs (get/list/add/delete), runs
  (start/finish/get/list newest first), items (add-if-new with created flag, get, list by
  status, seen), digests (add/list), notifications (add/mark/list), usage (add, totals per run
  and per job). Repositories return ORM rows; only the job service converts to plain records.
  `ItemRepository.add` checks `(job_id, url_hash)` first and wraps the insert in
  `session.begin_nested()` to translate a racing `IntegrityError` into "already existed".
- **Rationale**: The issue lists all six repositories; later issues (#14, #19, #23) will extend
  them. Keeping them thin and returning ORM rows avoids a second record type per table now
  (simplicity first); the "no SQLAlchemy in callers" rule is met for job management, which is
  the only consumer in this issue.
- **Alternatives**: Generic `Repository[T]` base (speculative abstraction); dataclass DTOs for
  every table (premature until the pipeline tracks define their needs).

## R12 — Change logging

- **Decision**: `logging.getLogger("invio.services.jobs")`; one `info` line per successful
  change after commit: message `"job <event>"`, `extra={"event": "job.created" | "job.updated"
  | "job.enabled" | "job.disabled" | "job.deleted" | "job.imported", "job_name": name}`. Listing
  a job with an invalid stored config logs `warning` `"skipping job with invalid stored
  configuration"` with `event="job.invalid_config"`, `job_name`, `error_count`. No config
  contents are logged.
- **Rationale**: Clarification Q4 and Constitution V. `job` is a reserved key of the JSON
  formatter (would become `extra_job`), so `job_name` is used. Logging after commit guarantees
  failed operations emit no change line. `import` logs `job.imported` (also with replace).
- **Alternatives**: Use `run_context(job=…)` (meant for job runs, not management actions).

## R13 — Testing approach

- **Decision**: New `tests/test_repositories.py` and `tests/test_job_service.py`, marked `db`,
  using the existing `db_engine`/`db_session` fixtures (SQLite in-memory by default, MariaDB
  with `INVIO_TEST_DATABASE_URL`). Service tests build `JobService(session_factory(db_engine),
  next_run=…, clock=…)` with a fake clock and a recording `next_run`. Logs checked with
  `caplog`. "Unchanged after failure" is checked by snapshotting rows before/after.
  Service tests do all direct setup writes and read-backs through committed
  `session_scope(session_factory(db_engine))` blocks, never through `db_session`: on MariaDB
  `db_session` is an uncommitted savepoint on its own connection, invisible to the service's
  sessions (on SQLite both share one `StaticPool` connection, which would hide the bug).
  `db_session` is used only by repository tests, which never commit.
- **Rationale**: Constitution III; the service commits real transactions, so it uses the engine
  rather than the per-test session. On MariaDB the shared server engine is used, so service
  tests clean up their jobs (fixture deletes all jobs at teardown).
- **Alternatives**: Mock the repositories in service tests (would not prove FR-019/cascade).
