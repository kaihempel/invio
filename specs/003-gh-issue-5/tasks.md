---

description: "Task list for issue #5 — repositories and JobService for job CRUD"
---

# Tasks: Job Management Service and Record Access Layer

**Input**: Design documents from `specs/003-gh-issue-5/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/python-api.md,
quickstart.md

**Tests**: Included — required by constitution principle III and FR-021 (every acceptance
criterion covered, rejection paths included). Write each story's tests first and confirm they
fail before implementing.

**Organization**: Tasks are grouped by user story (spec.md) in priority order:
US1 (P1) → US2 (P1) → US3 (P2) → US4 (P2).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story from spec.md (US1–US4)

## Conventions that apply to every task

- Package is `invio` (issue's `scout/db/repositories.py` → `src/invio/db/repositories.py`,
  `scout/services/jobs.py` → `src/invio/services/jobs.py`). Work on branch `gh-issue-5`.
- Research decisions are referenced as R1–R13 ([research.md](./research.md)); records, errors
  and state rules are in [data-model.md](./data-model.md); exact signatures in
  [contracts/python-api.md](./contracts/python-api.md).
- `tests/conftest.py` has an **autouse fixture that deletes every `INVIO_*` variable and
  `chdir`s into `tmp_path`**. Repo files are located via `REPO_ROOT` / `EXAMPLE` from
  `tests/job_helpers.py`. The `job_data` fixture returns a fresh minimal valid job mapping.
- Every persistence test module sets `pytestmark = pytest.mark.db` and uses the `db_engine` /
  `db_session` fixtures and `tests/db_helpers.py` factories (`make_job`, `make_run`,
  `make_item`, `make_digest`, `make_notification`, `make_llm_usage`) — never a hard-coded URL —
  so it runs on SQLite and, with `INVIO_TEST_DATABASE_URL`, on MariaDB.
- **Service tests never write through `db_session`.** `JobService` opens its own sessions; on
  MariaDB `db_session` is an uncommitted savepoint on a separate connection, so rows written
  there are invisible to the service. In `tests/test_job_service.py` every direct setup write
  (history rows, broken configs, `enabled=False`) and every direct read-back goes through
  `with session_scope(session_factory(db_engine)) as s:` (committed), using the `db_helpers`
  factories with `s`. `db_session` stays reserved for repository tests (`tests/test_repositories.py`),
  which never commit.
- No ORM `relationship()`s exist. After deletes, call `session.expire_all()` and re-query to
  observe DB-side `CASCADE`.
- All datetimes are aware UTC (`invio.db.types.utcnow`); `UTCDateTime` rejects naive values.
- Repositories flush but **never commit**; only `session_scope` commits (FR-016, FR-018).
- No SQLAlchemy type or exception may cross the `JobService` boundary (FR-020).
- Never log configuration contents (recipients, URLs, keywords) — only `event`, `job_name`,
  `error_count` (FR-014a, R12). Use `job_name`, not `job` (reserved by the JSON formatter).
- Do **not** create `src/invio/scheduling/next_run.py` (owned by parallel issue #6, R3).
- Match existing style: module docstrings, Python 3.12 typing, ruff line length 100,
  `mypy --strict` clean over `src/`, coverage ≥ 95 %.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Package skeleton

- [X] T001 Create package `src/invio/services/__init__.py` with a module docstring ("Application services used by the CLI, scheduler and pipeline; they own units of work and never expose SQLAlchemy types.") and update the docstring of `src/invio/db/__init__.py` to list the new `repositories` module and `session_scope` (R1)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared validation entry point, unit of work and the job repository needed by every
`JobService` story

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

### Tests (write first, must fail)

- [X] T002 [P] Create `tests/test_job_validate.py`: `validate_job(job_data)` returns a `JobConfig` equal to `JobConfig.model_validate(job_data)`; for `yaml.safe_load(BAD_JOB)` (from `tests/job_helpers.py`) `validate_job` raises `JobConfigError` with `path is None` and `errors` equal to the `errors` of `load_yaml` on the same text written to `tmp_path / "bad.yaml"`; an unknown top-level key yields an error line starting with `<key>:` and containing `Extra inputs are not permitted`; `str(exc)` starts with `invalid job file:` (R5)
- [X] T003 [P] Create `tests/test_db_session_scope.py` (`pytestmark = pytest.mark.db`): with `factory = session_factory(db_engine)`, a `Job` added inside `with session_scope(factory) as s:` is visible from a new session afterwards (committed); when the block raises `RuntimeError`, the error propagates and the job is absent (rolled back); the yielded session is closed after the block in both cases (spy on `Session.close` via `monkeypatch` or check `s.in_transaction() is False` and objects detached); `expire_on_commit=False` keeps attributes readable after the scope (FR-018)
- [X] T004 [P] Create `tests/test_repositories.py` (`pytestmark = pytest.mark.db`) with the `JobRepository` section: `add(Job(name=…, config={}))` flushes and assigns `id`; `get_by_name` returns the row or `None`; `list()` returns jobs ordered by `name` (insert `b`, `a`, `c`); `list(enabled_only=True)` omits `enabled=False` jobs; `add` of a duplicate name raises `sqlalchemy.exc.IntegrityError` on flush; `delete(job)` + `flush()` + `expire_all()` removes the job and cascades its runs (created with `make_run`); repository never commits (after `add` + `db_session.rollback()`, `get_by_name` returns `None`)

### Implementation

- [X] T005 [P] In `src/invio/config/job.py` add public `def validate_job(data: Mapping[str, Any]) -> JobConfig` that calls `JobConfig.model_validate(data)` and on `ValidationError` raises `JobConfigError(None, _format_validation_error(exc))` from the original; refactor `load_yaml` to call `validate_job(data)` and re-raise with the file path as `JobConfigError(file, exc.errors) from exc` so its messages stay unchanged; export in the module docstring/README snippet if one lists public names (R5)
- [X] T006 [P] In `src/invio/db/session.py` add `@contextmanager def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]`: `session = factory()`; `try: yield session; session.commit()`; `except BaseException: session.rollback(); raise`; `finally: session.close()` with docstring "Commit on success, roll back on any exception (re-raised), always close." (R2)
- [X] T007 Create `src/invio/db/repositories.py` with module docstring (thin, flush-only access per table; callers own the unit of work) and `class JobRepository` per contracts/python-api.md: `__init__(self, session: Session)`; `get_by_name(name) -> Job | None` (`select(Job).where(Job.name == name)`, `scalar_one_or_none`); `list(*, enabled_only=False) -> builtins.list[Job]` ordered by `Job.name`; `add(job) -> Job` (`session.add` + `flush`); `delete(job) -> None` (`session.delete` + `flush`). Annotate return types with `builtins.list` because the method `list` shadows the builtin in the class body (R11)

**Checkpoint**: `uv run pytest tests/test_job_validate.py tests/test_db_session_scope.py tests/test_repositories.py` green; existing `tests/test_job_yaml.py` still green

---

## Phase 3: User Story 1 - Register and look up research jobs (Priority: P1) 🎯 MVP

**Goal**: Create jobs under unique names with validated config and a next run time; fetch by
name; list (all or enabled only); dedicated errors for duplicates, unknown names, invalid names
and invalid stored configs.

**Independent Test**: Create a job with `job_data`, fetch it, list jobs; creating the same name
again raises `JobExistsError`.

### Tests for User Story 1 (write first, must fail)

- [X] T008 [US1] Add fixtures to `tests/conftest.py`: `fake_clock` returning a fixed aware datetime `datetime(2026, 10, 4, 12, 0, tzinfo=UTC)` that tests can advance (small class with `now` attribute and `__call__`); `next_run_calls` list plus `recording_next_run(schedule, after)` that appends `(schedule, after)` and returns `after + timedelta(hours=1)`; `job_service(db_engine, fake_clock, recording_next_run)` returning `JobService(session_factory(db_engine), next_run=recording_next_run, clock=fake_clock)`, and at teardown deleting all rows from `jobs` via `session_scope` (needed on the shared MariaDB engine; harmless on SQLite)
- [X] T009 [US1] Create `tests/test_job_service.py` (`pytestmark = pytest.mark.db`) with US1 tests: `test_create_and_get` — `create("ai-news", job_data)` returns `JobRecord` with `enabled is True`, `config == JobConfig.model_validate(job_data)`, `next_run_at == fake_clock.now + 1h`, aware UTC `created_at`/`updated_at`, and the stored `jobs.config` column (read back via `session_scope(session_factory(db_engine))`) equals `config.model_dump(mode="json")`; `create` also accepts a `JobConfig` instance; `get_by_name` returns an equal record; `test_create_duplicate_name` — second `create("ai-news", …)` raises `JobExistsError` with `.name == "ai-news"` and `str` `job 'ai-news' already exists`, first job unchanged; `test_create_race_integrity_error` — monkeypatch `JobRepository.get_by_name` to return `None`, create twice, second raises `JobExistsError` (not `IntegrityError`); `test_create_invalid_config` — `job_data` with `schedule.timezone="Europe/Atlantis"` and an extra key raises `JobConfigError` whose `errors` contain `schedule.timezone: unknown timezone 'Europe/Atlantis'`, and `jobs` table is empty; a `JobConfig.model_construct(...)` with invalid content is also rejected; `test_list_order_and_filter` — jobs `b`, `a`, `c` (disable `c` by writing `enabled=False` directly inside `session_scope(session_factory(db_engine))`) → `list()` names `["a","b","c"]`, `list(enabled_only=True)` names `["a","b"]`; `test_get_missing` — `get_by_name("missing")` raises `JobNotFoundError` (`isinstance(…, LookupError)`, `str` `job 'missing' not found`)
- [X] T010 [US1] In `tests/test_job_service.py` add `test_invalid_names` parametrized over `""`, `" "`, `" lead"`, `"trail "`, `"x" * 201` → `create(name, job_data)` raises `JobNameError` (a `ValueError`) and nothing is stored; `"x" * 200` succeeds (FR-014)
- [X] T011 [US1] In `tests/test_job_service.py` add `test_invalid_stored_config` — insert `Job(name="broken", config={"bogus": 1})` and a valid job `ok` directly inside `session_scope(session_factory(db_engine))` (committed); `get_by_name("broken")` raises `StoredJobConfigError` (subclass of `JobConfigError`) whose `str` starts with `invalid stored job 'broken':`; `list()` returns only `ok` and `caplog` (logger `invio.services.jobs`, level WARNING) has exactly one record with `event == "job.invalid_config"`, `job_name == "broken"`, integer `error_count >= 1` (FR-007a)

### Implementation for User Story 1

- [X] T012 [US1] Create `src/invio/services/jobs.py` foundations per data-model.md/contracts: module docstring; `logger = logging.getLogger(__name__)`; `NextRun = Callable[[ScheduleConfig, datetime], datetime]`; `def _next_run_stub(schedule: ScheduleConfig, after: datetime) -> datetime: return after` with a comment that #6's `invio.scheduling.next_run.compute_next_run` replaces it; `MAX_NAME_LENGTH = 200`; `@dataclass(frozen=True, slots=True, kw_only=True) class JobRecord` with `name: str`, `enabled: bool`, `config: JobConfig`, `next_run_at: datetime | None`, `created_at: datetime`, `updated_at: datetime`; errors `JobExistsError(Exception)` (`"job '{name}' already exists"`), `JobNotFoundError(LookupError)` (`"job '{name}' not found"`), `JobNameError(ValueError)` (message names the broken rule: empty / leading or trailing whitespace / longer than 200 characters), `StoredJobConfigError(JobConfigError)` with `__init__(self, name, errors)` calling `super().__init__(None, errors)` and `__str__` returning `invalid stored job '{name}':` + `"\n  "`-indented errors; each error stores `.name`
- [X] T013 [US1] In `src/invio/services/jobs.py` implement `class JobService` construction and helpers: `__init__(self, session_factory: sessionmaker[Session], *, next_run: NextRun = _next_run_stub, clock: Callable[[], datetime] = utcnow)`; `@classmethod from_settings(cls, settings: Settings | None = None) -> JobService` using `(settings or get_settings()).require_secret("database_url")` → `create_db_engine` → `session_factory`; private `_check_name(name)` raising `JobNameError` for `name == ""`, `name != name.strip()`, `len(name) > MAX_NAME_LENGTH`; `_validate(config: JobConfig | Mapping[str, Any]) -> JobConfig` returning `validate_job(config.model_dump(mode="json") if isinstance(config, JobConfig) else config)`; `_record(job: Job) -> JobRecord` validating `job.config or {}` via `validate_job` and converting `JobConfigError` to `StoredJobConfigError(job.name, exc.errors)`; `_require(repo, name) -> Job` raising `JobNotFoundError`; `_log_change(event, name)` → `logger.info(f"job {event.removeprefix('job.')}", extra={"event": event, "job_name": name})` (R4, R5, R7, R12)
- [X] T014 [US1] In `src/invio/services/jobs.py` implement `create(name, config) -> JobRecord`: `_check_name`; `cfg = _validate(config)`; in one `session_scope`: if `JobRepository.get_by_name(name)` exists → `JobExistsError(name)`; build `Job(name=name, enabled=True, config=cfg.model_dump(mode="json"), next_run_at=self._next_run(cfg.schedule, self._clock()))`; `repo.add(job)` wrapped in `try/except IntegrityError as exc: raise JobExistsError(name) from exc`; build the record inside the scope; after the scope `_log_change("job.created", name)` (FR-002, FR-003, FR-008, R6)
- [X] T015 [US1] In `src/invio/services/jobs.py` implement `get_by_name(name) -> JobRecord` (one scope; `_require` + `_record`) and `list(*, enabled_only: bool = False) -> builtins.list[JobRecord]` (one scope; for each repo row try `_record`, on `StoredJobConfigError` log `logger.warning("skipping job with invalid stored configuration", extra={"event": "job.invalid_config", "job_name": job.name, "error_count": len(exc.errors)})` and skip); results ordered by name (FR-005, FR-007, FR-007a)

**Checkpoint**: US1 tests green — MVP: jobs can be registered, fetched and listed

---

## Phase 4: User Story 2 - Change, pause and remove jobs (Priority: P1)

**Goal**: Full-replacement update with re-validation and next-run recalculation; disable keeps
history and clears next run; re-enable recalculates; delete removes job and history.

**Independent Test**: Create a job with history rows, update, disable, re-enable, delete —
checking stored state after each step.

### Tests for User Story 2 (write first, must fail)

- [X] T016 [US2] In `tests/test_job_service.py` add: `test_update_replaces_and_recalculates` — create, advance `fake_clock` by 1 day, `update(name, new_data)` where `new_data` has a different `schedule.time` and no `limits` key → record `config == JobConfig.model_validate(new_data)` (full replacement: defaults, not old values, for omitted sections), `name` unchanged, `next_run_at == advanced now + 1h`, last `next_run_calls` entry has the new schedule; `test_update_invalid_keeps_job` — `update` with `sources: []` raises `JobConfigError` and `get_by_name` returns the previous record unchanged (config and `next_run_at`); `test_update_disabled_job_keeps_no_next_run` — disable, update → `enabled is False`, `next_run_at is None`, no new `next_run_calls` entry (FR-003a, FR-008)
- [X] T017 [US2] In `tests/test_job_service.py` add: `test_disable_keeps_history` — create job, add one run, item, digest, notification and llm_usage row via `db_helpers` factories inside `session_scope(session_factory(db_engine))` (committed); read history back the same way; `set_enabled(name, False)` → `enabled is False`, `next_run_at is None`, and all five history rows still exist; `test_reenable_recalculates` — after disable, advance clock, `set_enabled(name, True)` → `enabled is True`, `next_run_at == new now + 1h`; `test_set_enabled_noop` — `set_enabled(name, True)` on an enabled job leaves `next_run_at` and `updated_at` unchanged, records no `next_run_calls` entry and logs no change line; same for disabling a disabled job; `test_delete_cascades_only_own_history` — two jobs each with the five history rows; `delete("a")` → `get_by_name("a")` raises `JobNotFoundError`, no rows with job `a`'s id in `runs`, `items`, `digests`, `notifications`, `llm_usage`; job `b` and all its rows untouched (FR-009, FR-010)
- [X] T018 [US2] In `tests/test_job_service.py` add `test_not_found_operations` parametrized over `update("x", job_data)`, `set_enabled("x", True)`, `set_enabled("x", False)`, `delete("x")` → each raises `JobNotFoundError` with `.name == "x"` (US2-6, FR-006)

### Implementation for User Story 2

- [X] T019 [US2] In `src/invio/services/jobs.py` implement `update(name, config) -> JobRecord`: `cfg = _validate(config)` before opening the scope; in one scope `_require`; set `job.config = cfg.model_dump(mode="json")`; if `job.enabled`: `job.next_run_at = self._next_run(cfg.schedule, self._clock())`; `session.flush()` then `session.refresh(job)` so `updated_at` (`onupdate`) is current; build record; after the scope `_log_change("job.updated", name)` (FR-003, FR-003a, FR-008, R8)
- [X] T020 [US2] In `src/invio/services/jobs.py` implement `set_enabled(name, enabled) -> JobRecord`: one scope; `_require`; if `job.enabled == enabled` return `_record(job)` without writing or logging; disabling sets `enabled=False`, `next_run_at=None`; enabling validates the stored config via `_record`, sets `enabled=True`, `next_run_at=self._next_run(record.config.schedule, self._clock())`; flush + refresh; after the scope log `job.enabled` / `job.disabled` (FR-009, R8)
- [X] T021 [US2] In `src/invio/services/jobs.py` implement `delete(name) -> None`: one scope; `_require`; `JobRepository.delete(job)` (DB `ON DELETE CASCADE` removes history, R9); after the scope `_log_change("job.deleted", name)` (FR-010)

**Checkpoint**: US1 + US2 tests green — full job CRUD works

---

## Phase 5: User Story 3 - Move jobs between YAML files and the data store (Priority: P2)

**Goal**: Import job files under a name (default: file stem) with explicit replace; export a
stored job as YAML text or to a file; import→export yields an equal config.

**Independent Test**: Import `docs/job.example.yaml`, export it, compare loaded configs.

### Tests for User Story 3 (write first, must fail)

- [X] T022 [US3] In `tests/test_job_service.py` add: `test_import_export_roundtrip` parametrized over `EXAMPLE` and temp files written with `write_yaml(JobConfig.model_validate(data), tmp_path / f"{case}.yaml")` for these variants of `job_data` (SC-002): `mini` (unchanged weekly job), `daily` (`schedule = {"frequency": "daily", "time": "06:00", "timezone": "UTC"}`), `monthly` (`schedule = {"frequency": "monthly", "time": "23:59", "day_of_month": 31, "timezone": "America/New_York"}`), `all_sources` (`sources` = one each of `{"type": "rss", "url": …}`, `{"type": "web", "url": …}`, `{"type": "sitemap", "url": …}`, `{"type": "youtube_channel", "channel_id": "UC123"}`, `{"type": "youtube_playlist", "playlist_id": "PL123"}`), and `unicode` (`notification.subject = "Recherche: Übersicht 🚀"`, `search.semantic_description = "KI-Agenten für Ärzte"`) → `import_yaml(path)` creates job named `path.stem` (`job.example` / `mini` / …) with `config == load_yaml(path)`; `exported = export_yaml(name)` is a `str` and `JobConfig.model_validate(yaml.load(exported, Loader=JobYamlLoader)) == load_yaml(path)`; `export_yaml(name, tmp_path / "out.yaml")` writes the same text and `load_yaml(tmp_path / "out.yaml") == load_yaml(path)` (FR-011, FR-013, SC-002); `test_import_explicit_name` — `import_yaml(path, "custom")` stores under `custom`
- [X] T023 [US3] In `tests/test_job_service.py` add: `test_import_invalid_file` — `BAD_JOB` written to `tmp_path / "bad.yaml"` → `import_yaml` raises `JobConfigError` with the same `errors` as `load_yaml`, `jobs` table empty; missing file → `JobConfigError` (`cannot read job file`); `test_import_existing_requires_replace` — import, add a run for the job inside `session_scope(session_factory(db_engine))`, re-import same path → `JobExistsError`; `import_yaml(path, replace=True)` on a changed file updates the config, keeps the run row and recalculates `next_run_at`; `test_export_missing` → `JobNotFoundError`; `test_export_unwritable_path` — `export_yaml(name, tmp_path / "no-dir" / "x.yaml")` raises `OSError` and the stored job is unchanged (FR-012)

### Implementation for User Story 3

- [X] T024 [US3] In `src/invio/services/jobs.py` implement `export_yaml(name, path: str | os.PathLike[str] | None = None) -> str`: `record = self.get_by_name(name)`; `text = dump_yaml(record.config)`; if `path` is given call `write_yaml(record.config, path)` (atomic, `OSError` propagates); return `text` (FR-011, R10)
- [X] T025 [US3] In `src/invio/services/jobs.py` implement `import_yaml(path, name: str | None = None, *, replace: bool = False) -> JobRecord`: `cfg = load_yaml(path)` (same errors as file loading); `job_name = name if name is not None else Path(path).stem`; `_check_name(job_name)`; one scope: if the job exists and not `replace` → `JobExistsError`; if it exists and `replace` → same steps as `update` (share a private `_apply_update(session, job, cfg)` helper extracted from T019); otherwise same steps as `create` (share a private `_insert(session, name, cfg)` helper extracted from T014, keeping the `IntegrityError` → `JobExistsError` translation); after the scope `_log_change("job.imported", job_name)` (FR-012, R10, R12)

**Checkpoint**: US1–US3 tests green — YAML import/export round-trips

---

## Phase 6: User Story 4 - Record and query run history through one access layer (Priority: P2)

**Goal**: Run, item, digest, notification and LLM-usage repositories with the basic add /
lookup / filter / update operations.

**Independent Test**: On the in-memory test DB, add and read back each record kind through its
repository; check ordering, filters and item idempotency.

### Tests for User Story 4 (write first, must fail)

- [X] T026 [US4] In `tests/test_repositories.py` add `RunRepository` tests: `start(job.id)` → `status == RunStatus.RUNNING`, aware `started_at`, explicit `started_at=` honoured; `finish(run, RunStatus.SUCCEEDED, stats={"items": 3}, error=None)` sets `finished_at` (default `utcnow()`, aware) and stats; `finish(..., RunStatus.FAILED, error="boom")` stores the error; `get(id)` / `get(999)` → row / `None`; `list_for_job` newest first by `started_at` (ties broken by `id` descending) and `limit=1` returns only the newest; runs of other jobs excluded
- [X] T027 [US4] In `tests/test_repositories.py` add `ItemRepository` tests using `invio.domain.Candidate` built with `url_hash(url)`: first `add(job.id, cand, run_id=run.id)` → `(item, True)` with `status == ItemStatus.NEW`, `attempts == 0`, fields copied (`url`, `url_hash`, `title`, `type`, `published_at`, `teaser`, `content_hash`); second `add` with same URL → `(same item id, False)` and still one row; same URL for another job → created; race: insert the duplicate directly with `make_item` after monkeypatching the pre-check to miss → `add` returns `(existing, False)` and the session remains usable (savepoint rolled back); `seen(job.id, hash)` True/False; `list_for_job(job.id)` ordered by id and `status=ItemStatus.FAILED` filter returns only matching items; `get(id)` (FR-017)
- [X] T028 [US4] In `tests/test_repositories.py` add `DigestRepository` (`add` stores `title`, `body`, `item_ids`, optional `run_id`; `list_for_job` newest first), `NotificationRepository` (`add` → `status == NotificationStatus.PENDING`; `mark(n, NotificationStatus.SENT)` sets `sent_at` to an aware time when not given; `mark(n, NotificationStatus.FAILED, error="smtp")` stores the error and leaves `sent_at` `None`; `list_for_job(status=…)` filter; `list_for_run(run.id)`), and `UsageRepository` tests (`add` rows with `cost_usd=Decimal("0.001000")`, `Decimal("0.002500")` and `None` → `totals_for_run(run.id) == UsageTotals(input_tokens=…, output_tokens=…, cost_usd=Decimal("0.003500"))`; `totals_for_job` sums across runs including `run_id=None` rows; empty → `UsageTotals(0, 0, Decimal("0"))`; other jobs excluded). Mark Decimal-using tests with `pytest.mark.filterwarnings("ignore:Dialect sqlite\\+pysqlite does \\*not\\* support Decimal objects natively:sqlalchemy.exc.SAWarning")` like `tests/test_db_records.py`

### Implementation for User Story 4

- [X] T029 [US4] In `src/invio/db/repositories.py` add `RunRepository` per contracts/python-api.md: `start(job_id, *, started_at=None)` (`Run(job_id=…, status=RunStatus.RUNNING, started_at=started_at or utcnow())`, add, flush); `finish(run, status, *, stats=None, error=None, finished_at=None)` (set fields, `finished_at or utcnow()`, flush); `get(run_id)` via `session.get(Run, run_id)`; `list_for_job(job_id, *, limit=None)` ordered `Run.started_at.desc(), Run.id.desc()`
- [X] T030 [US4] In `src/invio/db/repositories.py` add `ItemRepository`: `add(job_id, candidate: Candidate, *, run_id=None) -> tuple[Item, bool]` — look up `(job_id, candidate.url_hash)` first and return `(existing, False)`; otherwise inside `with session.begin_nested():` add `Item(job_id=…, run_id=…, url=…, url_hash=…, type=…, title=…, published_at=…, teaser=…, content_hash=…)` and flush; on `IntegrityError` (savepoint rolled back) re-query and return `(existing, False)`; else `(item, True)`; `get(item_id)`; `seen(job_id, url_hash) -> bool`; `list_for_job(job_id, *, status=None)` ordered by `Item.id` (FR-017, R11)
- [X] T031 [US4] In `src/invio/db/repositories.py` add `DigestRepository` (`add(job_id, title, body, item_ids, *, run_id=None)`, `list_for_job` ordered `created_at.desc(), id.desc()`), `NotificationRepository` (`add(job_id, channel, recipient, *, run_id=None, digest_id=None, payload=None)`; `mark(notification, status, *, error=None, sent_at=None)` setting `sent_at = sent_at or utcnow()` only when `status is NotificationStatus.SENT`; `list_for_job(job_id, *, status=None)` and `list_for_run(run_id)` ordered by id), `@dataclass(frozen=True, slots=True) class UsageTotals(input_tokens: int, output_tokens: int, cost_usd: Decimal)` and `UsageRepository` (`add(job_id, provider, model, input_tokens, output_tokens, *, run_id=None, purpose=None, cost_usd=None)`; `totals_for_run` / `totals_for_job` via one `select(func.coalesce(func.sum(...), 0), …)` each, converting results to `int` / `Decimal(str(value))`); all methods flush, none commit

**Checkpoint**: All four stories green

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Logging contract, docs, gates

- [X] T032 In `tests/test_job_service.py` add `test_change_logging`: with `caplog.set_level(logging.INFO, logger="invio.services.jobs")`, each of `create`, `update`, `set_enabled(False)`, `set_enabled(True)`, `import_yaml`, `delete` emits exactly one INFO record with the expected `event` (`job.created`, `job.updated`, `job.disabled`, `job.enabled`, `job.imported`, `job.deleted`) and `job_name`; no record's message or `__dict__` values contain the recipient `research@example.com` or the source URL from `job_data`; failed operations (`JobExistsError`, `JobConfigError`, `JobNotFoundError`) emit no INFO record; also assert a `configure_logging()` JSON line for `job.created` contains `"job_name"` and not `"extra_job"` (FR-014a, R12)
- [X] T033 [P] In `tests/test_job_service.py` add `test_failed_operations_leave_store_unchanged`: snapshot all `jobs` rows (`name, enabled, config, next_run_at, updated_at`) before each failing call (duplicate create, invalid update, invalid import, unknown delete) and assert identical afterwards; plus `test_no_sqlalchemy_in_records` asserting `JobRecord` field values are not SQLAlchemy instances and `JobService.from_settings()` without `INVIO_DATABASE_URL` raises `MissingSettingError`, with `INVIO_DATABASE_URL=sqlite:///{tmp_path}/s.sqlite` returns a `JobService` (SC-003, SC-004)
- [X] T034 [P] Update `README.md`: add a "Job service" subsection under "Database" with a short Python example (`JobService.from_settings()`, `create`, `list`, `set_enabled`, `export_yaml`, `import_yaml`), the error types, the note that `next_run_at` is a placeholder until scheduling (#6) lands, and that callers must not use SQLAlchemy directly; extend the "Layout" section with `services/` and `db/repositories.py`
- [X] T035 Run all gates and fix findings: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest --cov` (coverage ≥ 95 %); then run the manual walkthrough in [quickstart.md](./quickstart.md) section 2 against a temp SQLite file
- [X] T036 Mark completed tasks `[X]` in this file and verify every row of the acceptance mapping in quickstart.md section 3 points to an existing, passing test

---

## Phase 8: Review follow-ups (PR #43)

- [X] T037 SQLite: `create_db_engine` disables pysqlite's implicit transaction handling and emits `BEGIN` itself, so the savepoint in `ItemRepository.add` can no longer commit on its own when it is the first write; regression test `test_savepoint_as_first_write_rolls_back`
- [X] T038 MariaDB: the engine runs at `READ COMMITTED`, so `ItemRepository.add` sees a concurrent writer's committed row after the duplicate-key error (under REPEATABLE READ the plain re-query missed it, and with `innodb_snapshot_isolation` a locking read fails with error 1020); two-session test `test_item_add_concurrent_writer_returns_winner` (server only)
- [X] T039 Migration `0002`: binary collation for `jobs.name` on MariaDB, so names compare exactly as on SQLite; `test_names_compare_exactly`
- [X] T040 Contract: document the commit-then-raise behaviour of disabling a job with a broken stored config, and test that repeating it is a silent no-op
- [X] T041 Cleanups: fixed log message `"job changed"`, `_next_run_due`, no re-validation of a just-stored config, `export_yaml` serialises once (`write_yaml` returns the text), `_all` helper in repositories, `make_candidate` test helper, de-duplicated log tests, TODO(#6) on the `next_run` stub

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none
- **Foundational (Phase 2)**: after Setup — blocks all stories (`validate_job`,
  `session_scope`, `JobRepository`)
- **US1 (Phase 3)**: after Foundational
- **US2 (Phase 4)**: after US1 (reuses `JobService` construction, `_record`, `_require`,
  `_log_change` from T012–T015)
- **US3 (Phase 5)**: after US2 (T025 extracts shared helpers from T014 and T019)
- **US4 (Phase 6)**: after Foundational only — independent of US1–US3 (other classes in
  `src/invio/db/repositories.py`); can run in parallel with Phases 3–5
- **Polish (Phase 7)**: after all stories

### Within Each Story

- Tests first (must fail), then implementation in the listed order
- `tests/test_job_service.py` and `src/invio/services/jobs.py` are single files, so tasks
  touching them are sequential (no [P])

### Parallel Opportunities

- T002, T003, T004 (different test files) in parallel; T005, T006 in parallel
- US4 (T026–T031) as a whole can run in parallel with US1–US3 — different files
  (`db/repositories.py`, `tests/test_repositories.py` vs `services/jobs.py`,
  `tests/test_job_service.py`); inside US4 the tasks share files and run sequentially
- T033 and T034 in parallel

---

## Parallel Example: Foundational + US4

```bash
# Foundational tests together:
Task: "Create tests/test_job_validate.py (T002)"
Task: "Create tests/test_db_session_scope.py (T003)"
Task: "Create JobRepository section in tests/test_repositories.py (T004)"

# After Phase 2, two tracks:
Track A: T008 → T015 (US1) → T016–T021 (US2) → T022–T025 (US3)   # services/jobs.py
Track B: T026–T028 → T029–T031 (US4)                               # db/repositories.py
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 + Phase 2
2. Phase 3 (US1) → **STOP and VALIDATE**: create / get / list with all error paths

### Incremental Delivery

1. Foundation → US1 (register/look up) → US2 (change/pause/remove) → US3 (YAML) → US4
   (history repositories) → Polish
2. Each checkpoint keeps the full suite green

---

## Notes

- Commit after each phase checkpoint with messages referencing `#5`.
- When #6 merges, a follow-up switches `JobService`'s `next_run` default to
  `invio.scheduling.next_run.compute_next_run` — not part of this task list.
