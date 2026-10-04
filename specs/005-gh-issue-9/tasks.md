---

description: "Task list for CLI job management with interactive creation wizard"
---

# Tasks: CLI Job Management with Interactive Creation Wizard

**Input**: Design documents from `/specs/005-gh-issue-9/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/cli-job.md,
contracts/python-api.md, quickstart.md

**Tests**: Required. Spec FR-036, constitution principle III and the issue's acceptance
criterion ("CLI commands are covered by Typer CliRunner tests") all ask for them. Within each
story, write the tests first and confirm they fail before implementing.

**Organization**: Tasks are grouped by user story (spec.md US1–US7). The three P1 stories are
ordered by dependency: US3 (list/show) → US2 (source checks) → US1 (wizard). The wizard's
acceptance check ("appears in `invio job list`") needs `list`, and the wizard calls the source
checker.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: Which user story this task belongs to (US1–US7)

## Path Conventions

Single project: `src/invio/`, `tests/` (flat `test_<area>.py` files). Run everything with
`uv run`.

## Shared conventions for all tasks

- **Command module**: `src/invio/cli/commands/job.py` exposes a module-level
  `app = typer.Typer(help="Manage research jobs.", no_args_is_help=True)`. It is auto-discovered
  as `invio job`; do **not** edit `src/invio/cli/main.py`.
- **Test seams** (contracts/python-api.md). All are module-level functions in `job.py`, and
  tests replace them with `monkeypatch.setattr(job_module, "<name>", ...)`:
  - `_make_service() -> JobService` (`JobService.from_settings()`)
  - `_make_prompter() -> Prompter`
  - `_make_checker() -> SourceChecker`
  - `_make_registry() -> ModelRegistry | None` and `_default_timezone() -> str` (added seams)
  - `_is_interactive() -> bool` (`sys.stdin.isatty() and sys.stdout.isatty()`)
  - `_edit_text(text: str) -> str | None` (delegates to the stdlib `invio.cli.editor.edit_text`;
    `click` is not a direct dependency and Typer's vendored click has no `edit`)
- **Output** (contracts/cli-job.md):
  - Results go to stdout via `typer.echo`.
  - Errors and warnings go to stderr via `typer.echo(..., err=True)`.
  - Message wording must match the contract exactly, e.g. `Error: job 'NAME' not found`,
    `created job 'NAME' (next run: …)`.
- **Exit codes** (research R8):
  - 0: success, a no-op enable/disable, or an unchanged edit.
  - 1: `JobNotFoundError`, `JobExistsError`, `JobNameError`, `WizardAborted`, a declined
    preview or delete, or an aborted edit.
  - 2: `JobConfigError` / `StoredJobConfigError`, `MissingSettingError`, or an interactive
    command without a TTY.
  - Expected errors never print a traceback. Raise `typer.Exit(code=...)` after echoing.
- **Times**: `next_run_at` is rendered as `YYYY-MM-DD HH:MM <TZ abbr>` in the job's
  `schedule.timezone` (`dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")`). Use `—`
  when it is `None` or the config is invalid.
- **Tests**:
  - Use `typer.testing.CliRunner()` and `runner.invoke(app, ["job", ...], input=...)`, with
    `app` from `invio.cli.main`.
  - The service is wired to an in-memory SQLite engine via the existing `conftest.py` fixtures.
    Reuse the `job_data` fixture and the engine/session fixtures that `tests/test_job_service.py`
    uses.
  - No real network, editor or terminal. The only exception is
    `tests/test_source_check.py`, which uses a loopback `http.server` on `127.0.0.1`.
  - Style: ruff and mypy strict. `# type: ignore[...]` only when it is narrow and has a
    justification comment.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Branch and dependencies

- [X] T001 Create the feature branch `gh-issue-9` as the constitution's Development Workflow requires: `git switch -c gh-issue-9` from the worktree branch `worktree-track-cli-sched` (it holds the spec commits). If that branch is behind `origin/main`, rebase it first. All later commits go to `gh-issue-9`.
- [X] T002 Add the runtime dependencies `questionary` and `rich` (rich is already locked via Typer; declare it explicitly) with `uv add questionary rich`. This updates `pyproject.toml` `[project].dependencies` and `uv.lock`. Verify with `uv sync --locked` and `uv run python -c "import questionary, rich"`. If mypy reports missing stubs for questionary, add a narrow `[[tool.mypy.overrides]] module = ["questionary.*"]  ignore_missing_imports = true` in `pyproject.toml` with a comment.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Contract additions and test infrastructure that every story uses

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T003 [P] Add tests for `loads_yaml(text, *, source=None)` in `tests/test_job_yaml.py`:
  - A valid text returns the same `JobConfig` as `load_yaml` on the same content.
  - Malformed YAML raises `JobConfigError` whose single error starts with `invalid YAML` and has line/column.
  - A non-mapping top level raises the "must contain a mapping at the top level" error.
  - Field errors match `load_yaml` messages exactly.
  - Duplicate keys are rejected.
  - `source=Path("x.yaml")` is reflected in `JobConfigError.path`.
- [X] T004 Implement `loads_yaml(text: str, *, source: Path | None = None) -> JobConfig` in `src/invio/config/job.py`:
  - Parse with `yaml.load(text, Loader=JobYamlLoader)`, apply the top-level mapping check, call `validate_job`, and re-raise errors with `JobConfigError(source, errors)`.
  - Refactor `load_yaml` to read the file (`utf-8-sig`) and delegate to `loads_yaml(text, source=file)`, keeping every existing message unchanged.
  - Add `"loads_yaml"` to `__all__`.
  - Run `uv run pytest tests/test_job_yaml.py tests/test_job_config.py -q`.
- [X] T005 [P] Add tests for `ModelRegistry.models_for(provider)` in `tests/test_llm_registry.py`: returns the sorted ids registered for that provider only; returns `[]` for a provider with no models; ids of other providers are excluded.
- [X] T006 Implement `ModelRegistry.models_for(self, provider: str) -> list[str]` in `src/invio/llm/registry.py`: `sorted(m.model_id for m in self._models.values() if m.provider == provider)`, with a docstring.
- [X] T007 [P] Add tests for `RunRepository.latest_status_by_job()` in `tests/test_repositories.py`:
  - It returns `{job_id: RunStatus}` for the newest run per job (ordered by `started_at` desc, then `id` desc; a tie on `started_at` is broken by the higher id).
  - Jobs without runs are absent.
  - It issues a single SELECT (count statements with a SQLAlchemy `before_cursor_execute` listener).
- [X] T008 Implement `RunRepository.latest_status_by_job(self) -> dict[int, RunStatus]` in `src/invio/db/repositories.py` as one query, e.g. a `row_number()` window over `partition_by=Run.job_id, order_by=(Run.started_at.desc(), Run.id.desc())`, filtered to `rn == 1`. It must work on SQLite and MariaDB.
- [X] T009 [P] Add tests for `JobService.overview()` and `JobService.stored_config()` in `tests/test_job_service.py`:
  - `overview()` returns every job ordered by name.
  - A job whose stored `config` is corrupted (write invalid JSON content directly through a session, e.g. `schedule.timezone = "Europe/Atlantis"`) appears with `config is None` and `config_errors == ["schedule.timezone: unknown timezone 'Europe/Atlantis'"]`.
  - `last_run_status` is the newest run's status, or `None` when the job has never run.
  - `next_run_at` is passed through.
  - `stored_config(name)` returns the raw dict even when it is invalid.
  - `stored_config("missing")` raises `JobNotFoundError`.
  - Existing `list()` still skips invalid jobs (regression guard).
- [X] T010 Implement `JobSummary` and the two methods in `src/invio/services/jobs.py`:
  - `JobSummary` is `@dataclass(frozen=True, slots=True, kw_only=True)` with the fields `name: str`, `enabled: bool`, `next_run_at: datetime | None`, `config: JobConfig | None`, `config_errors: list[str]` and `last_run_status: RunStatus | None`. The rule is "`config` is `None` exactly when `config_errors` is non-empty".
  - `overview()` runs one `session_scope` with `JobRepository(session).list()`, plus `RunRepository(session).latest_status_by_job()`, plus per-job `validate_job(job.config or {})`. It catches `JobConfigError` to fill `config_errors`.
  - `stored_config(name)` returns `dict(_require(repo, name).config or {})`.
  - Add `"JobSummary"` to `__all__`.
- [X] T011 [P] Create `src/invio/cli/prompts.py`:
  - `Validator = Callable[[str], bool | str]`.
  - `class WizardAborted(Exception)`.
  - `class Prompter(Protocol)` with `text(message, *, default="", validate=None, multiline=False) -> str`, `select(message, choices, *, default=None) -> str`, `confirm(message, *, default=True) -> bool` and `autocomplete(message, choices, *, default="", validate=None) -> str`.
  - `class QuestionaryPrompter` implements them via `questionary.text(..., validate=..., multiline=...)`, `questionary.select`, `questionary.confirm` and `questionary.autocomplete(..., match_middle=True)`. Each call uses `.ask()`. A `None` result (Ctrl+C / EOF) raises `WizardAborted`.
  - Module docstring explaining the seam (research R1/R2).
- [X] T012 [P] Create `src/invio/cli/source_check.py` with only the types (behaviour comes in US2):
  - `@dataclass(frozen=True, slots=True) class CheckResult: reachable: bool; status: int | None; reason: str | None; is_feed: bool | None`.
  - `class SourceChecker(Protocol): def check(self, url: str, *, expect_feed: bool) -> CheckResult`.
- [X] T013 Create the test helpers in `tests/cli_helpers.py` (needs T011 and T012):
  - `FakePrompter(answers: Iterable[str | bool])` implements the `Prompter` protocol. It records every asked message in `asked: list[str]`. For each call it pops the next answer; if `validate` returns a `str`, it appends `(message, error)` to `errors: list[tuple[str, str]]` and pops the next answer for the same question. An empty answer string means "accept `default`". Running out of answers raises `WizardAborted`.
  - `FakeChecker(results: Mapping[str, CheckResult] | None = None, default=CheckResult(True, 200, None, True))` records `calls: list[tuple[str, bool]]`.
  - `fake_editor(*texts: str | None)` returns a callable that returns successive texts and records the input it received.
  - A pytest fixture `job_cli(monkeypatch, db_engine, clean_jobs, fake_clock)` (fixtures from `tests/conftest.py`). It builds `JobService(session_factory(db_engine), clock=fake_clock)` with the **real** `compute_next_run`, unlike the `job_service` fixture, which uses a fake +1 h next run, so that next-run and time-zone output is realistic. It patches `invio.cli.commands.job._make_service` to return that service, sets `_is_interactive` to `True` by default, and returns a small object holding the service, a `CliRunner` and a `set_interactive(bool)` helper.
  - Register the fixture for the CLI test modules via `pytest_plugins = ["tests.cli_helpers"]` at the top of each, or import it explicitly.
- [X] T014 Create the command skeleton in `src/invio/cli/commands/job.py`:
  - The `app` and the five test seams from "Shared conventions". `_make_prompter` and `_make_checker` can return `QuestionaryPrompter()` and `HttpSourceChecker()` once those exist; until then a lazy import is fine.
  - A private context manager `_errors()` that maps exceptions to messages and exit codes:
    - `JobNotFoundError`, `JobExistsError`, `JobNameError` → `Error: <str(exc)>`, exit 1.
    - `JobConfigError` / `StoredJobConfigError` → `str(exc)` to stderr, exit 2.
    - `MissingSettingError` → `Configuration error: <exc>`, exit 2.
    - `WizardAborted`, `KeyboardInterrupt` and `EOFError` → `aborted; nothing saved`, exit 1. `KeyboardInterrupt` covers Ctrl+C pressed outside a prompt, e.g. during a 10 s reachability check, where questionary is not in control.
  - A helper `_fmt_next_run(next_run_at, tz) -> str` per "Times".
  - Add `tests/test_cli_job.py` with a test that `invio job --help` lists all nine commands. Commands may be stubs that raise `NotImplementedError` until their story.

**Checkpoint**: `uv run pytest -q` and `uv run mypy` are green. Story phases can start.

---

## Phase 3: User Story 3 - List and inspect jobs (Priority: P1) 🎯 MVP part 1

**Goal**: `invio job list` and `invio job show NAME` (spec US3, FR-020–FR-023, FR-032, FR-033).

**Independent Test**: Store two jobs with `JobService`, one valid and one with a corrupted config,
plus a run for the valid one. Then check `list` and `show` output and exit codes.

### Tests for User Story 3 ⚠️ (write first, ensure they fail)

- [X] T015 [P] [US3] Add `list` tests in `tests/test_cli_job.py`:
  - With no jobs, stdout contains `no jobs yet — create one with 'invio job create'` and the exit code is 0.
  - Two jobs (`b-job` disabled, `a-job` enabled weekly `monday 07:30 Europe/Berlin`) print rows in name order, with the header `NAME`, `ENABLED`, `FREQUENCY`, `NEXT RUN`, `LAST STATUS`. `a-job` shows `yes`, `weekly mon 07:30 Europe/Berlin`, a next run ending in `CET` or `CEST`, and `never run`. `b-job` shows `no` and `—` for the next run.
  - A job with a `failed` run shows `failed`.
  - A corrupted job shows `invalid config` and `—` for frequency and next run.
  - Missing `INVIO_DATABASE_URL` (do not patch `_make_service`) gives exit 2 with `Configuration error:`.
- [X] T016 [P] [US3] Add `show` tests in `tests/test_cli_job.py`:
  - `show a-job` prints `name: a-job`, `enabled: yes` and a `next run:` line, then YAML equal to `dump_yaml(config)`, with exit 0. The `next run:` value matches `\d{4}-\d{2}-\d{2} \d{2}:\d{2} (CET|CEST)` for the Europe/Berlin job (FR-021).
  - `show missing` prints `Error: job 'missing' not found` on stderr with exit 1.
  - `show broken` prints the stored YAML on stdout and `invalid stored job 'broken':` plus the indented errors on stderr, with exit 2.

### Implementation for User Story 3

- [X] T017 [US3] Implement `list` in `src/invio/cli/commands/job.py`:
  - Use `service.overview()` and `rich.table.Table` with the columns `NAME`, `ENABLED`, `FREQUENCY`, `NEXT RUN` and `LAST STATUS`, printed through `rich.console.Console()` on stdout. Keep CliRunner compatibility: create the `Console` inside the command, not at import time.
  - Frequency text: `daily HH:MM <tz>`, `weekly <weekday[:3]> HH:MM <tz>`, `monthly <day> HH:MM <tz>`.
  - Status precedence: `invalid config` → `last_run_status.value` → `never run`.
  - Print the empty-store message when there are no jobs.
- [X] T018 [US3] Implement `show NAME` in `src/invio/cli/commands/job.py`:
  - Call `service.get_by_name`. On success, print the header lines and `dump_yaml(record.config)`.
  - On `StoredJobConfigError`, print `yaml.safe_dump(service.stored_config(name), sort_keys=False, allow_unicode=True)` to stdout and the error to stderr, then exit 2.
  - Run `uv run pytest tests/test_cli_job.py -q`.

**Checkpoint**: Jobs created via the Python API are visible from the CLI.

---

## Phase 4: User Story 2 - Source checks during the wizard (Priority: P1)

**Goal**: A real `HttpSourceChecker` plus feed detection (spec US2, FR-007–FR-010; research R3).
The wizard's use of the results is covered in US1.

**Independent Test**: Run `tests/test_source_check.py` against a loopback HTTP server.

### Tests for User Story 2 ⚠️

- [X] T019 [P] [US2] Add `is_feed_document` tests in `tests/test_source_check.py`. These are `True` cases:
  - `<?xml …?><rss version="2.0">`
  - `<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">`
  - `<feed xmlns="http://www.w3.org/2005/Atom">`
  - A leading BOM, comments or whitespace before the root.

  These are `False` cases:
  - `<html>`
  - `<feed>` without the Atom namespace
  - Plain text, empty bytes, truncated XML before the root, binary garbage
- [X] T020 [P] [US2] Add `HttpSourceChecker` tests in `tests/test_source_check.py`. Use a `ThreadingHTTPServer` on `127.0.0.1:0` in a fixture, with a handler that serves the routes `/feed` (200 RSS), `/page` (200 HTML), `/missing` (404), `/nohead` (HEAD returns 405, GET returns 200), `/redirect` (302 to `/feed`) and `/slow` (sleeps longer than the timeout). Assertions:
  - `/feed` with `expect_feed=True` gives `reachable=True, is_feed=True`.
  - `/page` with `expect_feed=True` gives `reachable=True, is_feed=False`.
  - `/page` with `expect_feed=False` gives `is_feed is None`.
  - `/missing` gives `reachable=False, status=404, reason="HTTP 404"`.
  - `/nohead` gives `reachable=True`.
  - `/redirect` gives `reachable=True, is_feed=True`.
  - `/slow` with `timeout=0.2` gives `reachable=False` and a reason starting with `timeout`. To stay deterministic and fast (constitution III), the `/slow` handler must not `sleep()`. It blocks on a `threading.Event` that the fixture sets at teardown, before `server.shutdown()`, so no test waits on a sleeping thread.
  - Port 1 on loopback (connection refused) gives `reachable=False` and does not raise.
  - `HttpSourceChecker()` defaults to `timeout == 10.0` and `max_bytes == 65_536` (SC-005; assert the attributes).
  - Only `127.0.0.1` is contacted; no test touches an external host.
  - The `User-Agent` header starts with `invio/`.

### Implementation for User Story 2

- [X] T021 [US2] Implement `is_feed_document(head: bytes) -> bool` in `src/invio/cli/source_check.py`:
  - Feed `head` into `xml.etree.ElementTree.XMLPullParser(events=("start",))` and return on the first start event.
  - The tag must be `rss`, `{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF` or `{http://www.w3.org/2005/Atom}feed`.
  - `ParseError`, or no start event, gives `False`.
- [X] T022 [US2] Implement `HttpSourceChecker(timeout: float = 10.0, max_bytes: int = 65_536)` in `src/invio/cli/source_check.py`:
  - `check(url, *, expect_feed)` uses `urllib.request` with the header `User-Agent: invio/<invio.__version__>` and follows redirects (the default opener does).
  - Send HEAD first. Send GET instead when `expect_feed` is set, or when HEAD returns 405/501. GET reads at most `max_bytes`.
  - Map exceptions to `reason`, never raising:
    - `HTTPError` → `HTTP <code>`
    - `URLError` with a timeout, or `TimeoutError`/`socket.timeout` → `timeout after <n>s`
    - `URLError` with `gaierror` → `DNS lookup failed`
    - `ssl.SSLError` → `TLS error: …`
    - other `OSError` → `connection failed: <msg>`
  - `is_feed` is set only when `expect_feed` is true and the GET succeeded.
  - Run `uv run pytest tests/test_source_check.py -q`.

**Checkpoint**: Source checks work stand-alone.

---

## Phase 5: User Story 1 - Create a job interactively with the wizard (Priority: P1) 🎯 MVP part 2

**Goal**: `invio job create` without `--from-file` (spec US1, FR-004–FR-016; data-model
"WizardSession"; Clarifications Q2, Q3).

**Independent Test**: Run `invio job create` with a `FakePrompter` script and a `FakeChecker`,
confirm the preview, then run `invio job list` and see the job.

### Tests for User Story 1 ⚠️

- [X] T023 [P] [US1] Add wizard unit tests in `tests/test_wizard.py`. Call `run_wizard(FakePrompter([...]), FakeChecker(), registry=..., existing_names={"taken"}, default_timezone="UTC")` directly.
  - Happy path, weekly: it returns `(name, JobConfig)` with the expected schedule, recipients, sources, keywords, description, LLM and default limits.
  - Daily asks neither weekday nor day of month; monthly asks a day of month in 1–31 and not a weekday (assert on `prompter.asked`).
  - Inline rejections each record an error in `prompter.errors` and the wizard continues:
    - name `taken` → "already exists"
    - name `" x"` → whitespace rule
    - name longer than 200 chars
    - time `25:00` and `7:30`
    - timezone `Mars/Olympus`
    - e-mail `not-an-email`
    - URL `htp:/example`
    - empty semantic description
    - limit `0` and `abc`
  - At least one recipient and one source are enforced: the "add another?" question is not offered before the first one.
  - Duplicate recipient or source → warning, not added twice.
  - Keywords: `"a, b,, "` becomes `["a", "b"]`; empty input becomes `[]`.
  - A multi-line description keeps its `\n`.
  - Registry choices (Q2): `select` choices equal `registry.models_for(provider) + ["Other…"]`. Picking "Other…" plus an unknown id shows the warning containing `not registered for provider` and `runs will fail`. Declining asks again; confirming accepts. An empty registry gives free text with the same warning. `registry=None` gives free text.
  - Limits (Q3): "keep the default limits?" yes gives `LimitsConfig()` and no limit questions are asked; no asks the four values, pre-filled with defaults.
  - Sources (US2 scenarios 1–5):
    - An unreachable URL gives a warning containing the reason and "Keep this source anyway?". No asks the URL again; yes keeps it.
    - `rss` with `is_feed=False` gives a "no RSS/Atom feed detected" warning plus a keep question.
    - `youtube_channel` asks `channel_id` and `youtube_playlist` asks `playlist_id`, with no checker call (assert `checker.calls`).
  - Preview: the YAML shown equals `dump_yaml(config)`; declining returns `None`.
  - Going back to a step (FR-014): monkeypatch `invio.cli.wizard.validate_job` so the first call raises `JobConfigError(None, ["schedule.timezone: unknown timezone 'X'"])` and later calls delegate to the real function. The error is echoed, the time-zone question is asked again (it appears twice in `prompter.asked`), and later steps are **not** asked again. The wizard then completes.
  - Monthly with day of month 29, 30 or 31 echoes the note `months shorter than <n> days run on their last day`; day 28 or lower shows no note.
  - Running out of answers raises `WizardAborted`.
- [X] T024 [P] [US1] Add CLI tests in `tests/test_cli_job_create.py`. Patch `_make_prompter` and `_make_checker` with fakes and use the `job_cli` fixture.
  - A full scripted run gives exit 0, stdout containing the YAML preview and `created job 'ai-news' (next run: `. A following `invio job list` contains `ai-news` (issue acceptance criterion 1).
  - A bad e-mail and then a good one give exit 0, `prompter.errors` non-empty, and the job created (acceptance criterion 2).
  - A declined preview prints `job not created`, exits 1, and leaves the store empty.
  - Abort (answers exhausted) prints `aborted; nothing saved`, exits 1, leaves the store empty, and shows no `Traceback` in the output.
  - `_is_interactive` False and no `--from-file` gives exit 2 with stderr containing `interactive creation needs a terminal` and `--from-file`, and the prompter is never constructed.
  - Ctrl+C outside a prompt: a `FakeChecker` whose `check` raises `KeyboardInterrupt` gives `aborted; nothing saved`, exit 1, an empty store and no `Traceback`.
  - `--name ai-news` pre-fills the name and the name question is not asked. An invalid `--name` (`" x"`, or an existing name) is rejected **before** the wizard starts: it exits 1 with `Error: invalid job name ...` or `Error: job 'x' already exists`, and the prompter is never asked anything (`prompter.asked == []`).
  - A name taken between the wizard and saving (monkeypatch `service.create` to raise `JobExistsError`) prints `Error: job 'ai-news' already exists` and the preview YAML stays on stdout so the answers are not lost.

### Implementation for User Story 1

- [X] T025 [US1] Implement the step validators in `src/invio/cli/wizard.py`, each returning `True` or an error string, reusing the contract rules:
  - **name**: non-empty, no leading or trailing whitespace, ≤ `MAX_NAME_LENGTH` (200), not in `existing_names` → `job 'x' already exists`.
  - **time**: `^([01][0-9]|2[0-3]):[0-5][0-9]$` → `time must be HH:MM (00:00–23:59)`.
  - **timezone**: `in zoneinfo.available_timezones()` → `unknown timezone 'x'`.
  - **e-mail**: validate through `pydantic.TypeAdapter(EmailStr)` → `not a valid e-mail address`.
  - **URL**: `TypeAdapter(HttpUrl)` → `not a valid http(s) URL`.
  - **non-empty text**.
  - **positive int** (`must be a whole number ≥ 1`).
  - **day of month** (1–31).
- [X] T026 [US1] Implement `run_wizard(prompter, checker, *, registry, existing_names, name=None, default_timezone="UTC") -> tuple[str, JobConfig] | None` in `src/invio/cli/wizard.py`. The steps run in FR-004 order, per the data-model "WizardSession" table:
  1. Name (skipped when `name` is given; the caller validates a pre-filled name beforehand, see T027).
  2. Frequency select (`daily`, `weekly`, `monthly`); weekday select for weekly; day of month for monthly (for 29–31, echo `months shorter than <n> days run on their last day`); time (default `07:00`); timezone autocomplete (default `default_timezone`).
  3. A recipients loop (≥ 1, duplicates warned and skipped); subject (default `invio: <name>`).
  4. A sources loop:
     - Type select over the five `SourceConfig` types.
     - URL types ask the URL, call `checker.check(url, expect_feed=type == "rss")`, print `✓`/`!` lines, and ask "Keep this source anyway?" on a warning.
     - YouTube types ask the id.
     - Duplicates are warned and skipped. "Add another source?" is asked after each one.
  5. Keywords any/all/exclude as comma-separated text.
  6. A multi-line semantic description.
  7. Provider select over `LLMProvider`, then fast and smart model selection per Q2 (catch `ModelRegistryError` when the caller passes a broken registry → warn once, free text).
  8. Limits per Q3.

  Then build the mapping and call `validate_job`. On `JobConfigError`, show the errors and repeat the step owning the first error path (FR-014). Print `dump_yaml(config)` through an injected `echo` (default `typer.echo`), then `confirm("Save this job?")`. Decline returns `None`. Warnings go through the same `echo(..., err=True)`.
- [X] T027 [US1] Implement `create` (wizard path) in `src/invio/cli/commands/job.py`:
  - Options `--from-file PATH` (handled in US4) and `--name NAME`.
  - If `--from-file` is absent and `_is_interactive()` is false, exit 2 with the contract message.
  - Otherwise build the service and `existing_names = {s.name for s in service.overview()}`. If `--name` is given, validate it with the T025 name validator **before** constructing the prompter. A rejection exits 1: `Error: invalid job name '<n>': <rule>`, or `Error: job '<n>' already exists`. Then load `default_registry()` (on `ModelRegistryError`, warn and pass `None`), and work out the default timezone: `TZ` if valid, else the `/etc/localtime` symlink target after `zoneinfo/`, else `UTC`.
  - Call `run_wizard(_make_prompter(), _make_checker(), ...)`. `None` prints `job not created` and exits 1. Otherwise call `service.create(name, config)` and print `created job 'NAME' (next run: …)`.
  - Wrap everything in `_errors()`.
  - Run `uv run pytest tests/test_wizard.py tests/test_cli_job_create.py -q`.

**Checkpoint (MVP)**: Issue acceptance criteria 1 and 2 pass. `create` → `list` works end-to-end.

---

## Phase 6: User Story 4 - Non-interactive creation from a file (Priority: P2)

**Goal**: `invio job create --from-file PATH [--name NAME]` (spec US4, FR-017–FR-019).

**Independent Test**: In CliRunner with `_is_interactive` False, create from `docs/job.example.yaml`.

### Tests for User Story 4 ⚠️

- [X] T028 [P] [US4] Add tests in `tests/test_cli_job.py`. Set `set_interactive(False)` for all of them, and patch `_make_prompter` and `_make_checker` with functions that `pytest.fail` if called.
  - `create --from-file <copy of docs/job.example.yaml as tmp/ai.yaml>` gives exit 0, `created job 'ai'`, and the job stored.
  - `--name custom` stores it under `custom`.
  - The `BAD_JOB` text from `tests/job_helpers.py` gives exit 2, `invalid job file`, field lines such as `schedule.time:`, and nothing stored.
  - A missing file gives exit 2 with `cannot read job file`.
  - An existing name gives exit 1 with `Error: job 'ai' already exists`, and the existing job is unchanged (compare `updated_at`).

  This covers issue acceptance criterion 3.

### Implementation for User Story 4

- [X] T029 [US4] Implement the `--from-file` branch of `create` in `src/invio/cli/commands/job.py`: `service.import_yaml(path, name, replace=False)`, no prompter or checker, then print `created job 'NAME' (next run: …)`. Errors go through `_errors()`.

**Checkpoint**: Provisioning without a TTY works.

---

## Phase 7: User Story 5 - Edit a job in the operator's editor (Priority: P2)

**Goal**: `invio job edit NAME` with a validate/retry/abort loop (spec US5, FR-024–FR-027, FR-034;
research R5).

**Independent Test**: Replace `_edit_text` with `fake_editor(...)` and drive the retry prompt via
`CliRunner(input=...)`.

### Tests for User Story 5 ⚠️

- [X] T030 [P] [US5] Add tests in `tests/test_cli_job_edit.py`:
  - A valid change (time `07:30` → `09:00`) gives exit 0, `updated job 'a-job' (next run: …)`, the stored config updated, and `next_run_at` recalculated. The editor received `dump_yaml(original)`.
  - The editor returns `None` → `no changes`, exit 0, `updated_at` unchanged.
  - The editor returns the identical text → `no changes`, exit 0.
  - Invalid YAML (`"schedule: [\n"`), then `input="n\n"` → errors on stderr, `Re-open the editor?`, `edit aborted; job unchanged`, exit 1, and the stored config byte-identical to before (SC-006).
  - Invalid, then retry (`input="y\n"`), then valid → exit 0. The second editor call received the *edited invalid* text, not the original.
  - A validation error (`time: "25:00"`) behaves the same as invalid YAML.
  - `_edit_text` raises `invio.cli.editor.EditorError` (editor exit ≠ 0) → exit 1 and the job unchanged.
  - `edit missing` → exit 1 with `not found`.
  - A broken stored job: stderr shows `invalid stored job 'broken':` before the editor opens. The editor receives `yaml.safe_dump(stored)`. Saving a valid version repairs it: `show broken` afterwards exits 0.
  - A `name:` key in the edited content is rejected as an unknown key, and the job name is never changed (FR-027).
  - The real editor launcher is covered in `tests/test_editor.py` with a child-process script as
    `$EDITOR` (rewrites the file / leaves it / exits 3), `$VISUAL` precedence, the `vi`/`notepad`
    fallback and temp-file cleanup (US5 scenario 5).

  This covers issue acceptance criterion 4.

### Implementation for User Story 5

- [X] T031 [US5] Implement `edit NAME` in `src/invio/cli/commands/job.py`:
  1. Load the text: `dump_yaml(service.get_by_name(name).config)`. On `StoredJobConfigError`, echo the error to stderr and use `yaml.safe_dump(service.stored_config(name), sort_keys=False, allow_unicode=True)`.
  2. Loop:
     - `edited = _edit_text(text)`.
     - If `edited is None` or `edited == text` on the first round, print `no changes` and exit 0.
     - Otherwise parse with `loads_yaml(edited)`.
     - On `JobConfigError`, echo the error and ask `typer.confirm("Re-open the editor?", default=True)`. Yes sets `text = edited` and continues; no prints `edit aborted; job unchanged` and exits 1.
     - On success, call `service.update(name, config)` and print `updated job 'NAME' (next run: …)`.
  3. Catch `invio.cli.editor.EditorError` from the editor → print the error and `edit aborted; job unchanged` and exit 1.

**Checkpoint**: Acceptance criterion 4 passes.

---

## Phase 8: User Story 6 - Enable, disable and delete jobs (Priority: P2)

**Goal**: The lifecycle commands (spec US6, FR-028, FR-029, FR-035).

**Independent Test**: Toggle a stored job and delete it with and without confirmation.

### Tests for User Story 6 ⚠️

- [X] T032 [P] [US6] Add tests in `tests/test_cli_job.py`:
  - `disable a-job` prints `disabled job 'a-job'`, and `list` then shows `no` and `—`.
  - `enable a-job` prints `enabled job 'a-job' (next run: …)`, with `next_run_at` set again.
  - Repeating gives `job 'a-job' is already enabled` / `job 'a-job' is already disabled`, exit 0.
  - An unknown name gives exit 1.
  - A broken enabled job: `disable` exits 0, prints `disabled job 'broken'` plus a stderr warning containing `invalid`, and the stored `enabled` is False. `enable broken` then exits 2 with the error block.
  - `delete a-job` with `input="y\n"` prints `deleted job 'a-job'` and the job is gone. With `input="n\n"` it prints `job not deleted`, exits 1, and the job is kept.
  - `delete a-job --yes` and `-y` work with `set_interactive(False)`.
  - `delete a-job` with `set_interactive(False)` and no `--yes` gives exit 2 with `refusing to delete without confirmation; pass --yes`, and the job is kept (SC-007).
  - Deleting a broken job with `--yes` works.

### Implementation for User Story 6

- [X] T033 [US6] Implement `enable NAME` and `disable NAME` in `src/invio/cli/commands/job.py`:
  - Detect the no-op case from the job's `JobSummary` in `service.overview()`, which works for broken jobs too. A missing entry raises `JobNotFoundError(name)` (exit 1). If `summary.enabled == flag`, print the "already …" message and exit 0.
  - Otherwise call `service.set_enabled(name, flag)`.
  - For `disable`, catch `StoredJobConfigError`: print `disabled job 'NAME'` plus the stderr warning `warning: stored configuration of job 'NAME' is invalid; fix it with 'invio job edit NAME'`, and exit 0.
  - For `enable`, `StoredJobConfigError` goes through `_errors()` (exit 2).
- [X] T034 [US6] Implement `delete NAME [--yes/-y]` in `src/invio/cli/commands/job.py`:
  - Without `--yes`: if `not _is_interactive()`, exit 2 with the contract message; otherwise ask `typer.confirm(f"Delete job '{name}' and its run history?", default=False)`. Decline prints `job not deleted` and exits 1.
  - Check existence before prompting (an unknown name gives exit 1 without a prompt).
  - Then call `service.delete(name)` and print `deleted job 'NAME'`.

**Checkpoint**: The lifecycle is complete.

---

## Phase 9: User Story 7 - Export and import job files (Priority: P3)

**Goal**: `export NAME [-o PATH]` and `import FILE [--name NAME] [--replace]` (spec US7, FR-030,
FR-031, FR-035).

**Independent Test**: Export, delete, re-import, and compare.

### Tests for User Story 7 ⚠️

- [X] T035 [P] [US7] Add tests in `tests/test_cli_job.py`:
  - `export a-job` prints stdout equal to `dump_yaml(config)`, exit 0.
  - `export a-job -o out.yaml` writes the file, prints `exported job 'a-job' to out.yaml`, and the file round-trips through `load_yaml` to an equal config.
  - `export broken` exits 2.
  - `export missing` exits 1.
  - `import out.yaml` (after deleting `a-job`) prints `imported job 'out'`.
  - `import out.yaml --name a-job` gives `imported job 'a-job'`; repeating it gives exit 1 `already exists`; with `--replace` it gives `replaced job 'a-job'` and the config is updated.
  - An invalid file gives exit 2 and nothing stored.
  - `import` never prompts (prompter seam fails if called) and works with `set_interactive(False)`.

### Implementation for User Story 7

- [X] T036 [US7] Implement `export NAME [--output/-o PATH]` and `import FILE [--name NAME] [--replace]` in `src/invio/cli/commands/job.py`:
  - `export` uses `service.export_yaml(name, path)`. Without a path, echo the text with `nl=False`; with a path, print the confirmation line.
  - `import`:
    - Check `existed` beforehand (e.g. `name in {s.name for s in service.overview()}`, where the target name is `--name` or the `Path(FILE).stem`).
    - Then call `service.import_yaml(FILE, name, replace=replace)`.
    - Print `replaced job 'NAME'` if it existed and `--replace` was given, otherwise `imported job 'NAME'`.

**Checkpoint**: All nine commands are implemented.

---

## Phase 10: Polish & Cross-Cutting Concerns

- [X] T037 [P] Add a "Managing jobs" section to `README.md` after "Job service". It lists all nine `invio job` commands with one-line descriptions and covers:
  - The wizard flow, the inline validation, and that reachability and feed warnings can be overridden.
  - That a model which isn't registered is accepted only after a warning (runs fail until `models.d/<provider>.yaml` lists it).
  - `create --from-file` for automation, `delete --yes`, and `edit` using `$VISUAL`/`$EDITOR`.
  - The exit-code table from contracts/cli-job.md.

  Also add `cli/commands/job.py`, `cli/wizard.py`, `cli/prompts.py` and `cli/source_check.py` to the "Layout" section.
- [X] T038 [P] Add a no-traceback regression test in `tests/test_cli_job.py`, parameterised over every command's main failure path (not found, already exists, invalid file, broken stored config, missing DB setting, no TTY). Assert `"Traceback" not in result.output` and that the exit code matches the contract table.
- [X] T039 Run the full gate locally: `uv run ruff check . && uv run ruff format --check . && uv run mypy && uv run pytest`. Coverage must stay at or above `fail_under = 95`; add targeted tests for any uncovered branches in `src/invio/cli/wizard.py`, `src/invio/cli/source_check.py` and `src/invio/cli/commands/job.py`. Exclude `QuestionaryPrompter` (a thin wrapper over a real terminal) from coverage only with a narrow `# pragma: no cover` on its methods and a justification comment.
- [ ] T040 (MANUAL, not run by the implementing agent: needs a real terminal and editor) Follow the manual walkthrough in `specs/005-gh-issue-9/quickstart.md` in a real terminal (steps 1–15 and the invalid-stored-config block). Record any differences from contracts/cli-job.md and fix them.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: none.
- **Foundational (Phase 2)**: depends on Phase 1 and blocks every story.
  - T004 depends on T003, T006 on T005, T008 on T007, and T010 on T008 and T009.
  - T013 depends on T011 and T012.
  - T014 depends on T011 and T012.
- **US3 (Phase 3)**: depends on Phase 2.
- **US2 (Phase 4)**: depends on Phase 2 (T012). It is independent of US3.
- **US1 (Phase 5)**: depends on Phase 2 and US2 (it uses `HttpSourceChecker` as the production checker; tests use `FakeChecker`). Its acceptance test needs US3 (`list`).
- **US4, US5, US6, US7 (Phases 6–9)**: each depends only on Phase 2. US4's success output reuses `_fmt_next_run` (T014). They do not depend on each other.
- **Polish (Phase 10)**: after all stories.

### User Story Dependencies

- US3: none beyond Foundational.
- US2: none beyond Foundational.
- US1: depends on US2 (production checker) and US3 (acceptance check via `list`).
- US4, US5, US6, US7: independent; can be done in any order or in parallel.

### Within Each User Story

- Write the tests first, confirm they fail, implement, and confirm they pass.
- All command implementations touch `src/invio/cli/commands/job.py`. Implementation tasks of different stories should therefore be serialised (or merged carefully) even when the stories themselves are independent.

### Parallel Opportunities

- Phase 2: T003 ∥ T005 ∥ T007 ∥ T009 ∥ T011 ∥ T012 (all different files). Then T004 ∥ T006 ∥ T008 ∥ T013.
- US2 tests T019 ∥ T020 (the same file, but independent test functions; split them if two agents work at once), and they run in parallel with US3 work.
- Test-writing tasks of different stories are in different files or independent sections: T015/T016 (`test_cli_job.py`), T023 (`test_wizard.py`), T024 (`test_cli_job_create.py`), T030 (`test_cli_job_edit.py`).
- Polish: T037 ∥ T038.

---

## Parallel Example: Foundational

```bash
Task: "T003 loads_yaml tests in tests/test_job_yaml.py"
Task: "T005 models_for tests in tests/test_llm_registry.py"
Task: "T007 latest_status_by_job tests in tests/test_repositories.py"
Task: "T009 overview/stored_config tests in tests/test_job_service.py"
Task: "T011 Prompter protocol + QuestionaryPrompter in src/invio/cli/prompts.py"
```

## Parallel Example: User Story 1

```bash
Task: "T023 [US1] wizard step tests in tests/test_wizard.py"
Task: "T024 [US1] CLI create tests in tests/test_cli_job_create.py"
# Then sequentially: T025 → T026 (src/invio/cli/wizard.py) → T027 (commands/job.py)
```

## Parallel Example: P2 stories (after the MVP)

```bash
Task: "T028 [US4] create --from-file tests in tests/test_cli_job.py"
Task: "T030 [US5] edit loop tests in tests/test_cli_job_edit.py"
# Implementations T029, T031, T033, T034 all edit commands/job.py → run them one after another
```

---

## Implementation Strategy

### MVP First (US3 + US2 + US1)

1. T001–T014: setup and foundations.
2. T015–T018 (US3): `list`/`show`. **Validate:** jobs from the Python API are visible.
3. T019–T022 (US2): the source checker.
4. T023–T027 (US1): the wizard. **Stop and validate:** `invio job create` → `invio job list`
   (issue acceptance criteria 1 and 2).

### Incremental Delivery

1. US4 (`--from-file`): issue acceptance criterion 3.
2. US5 (`edit`): issue acceptance criterion 4.
3. US6 (enable/disable/delete).
4. US7 (export/import).
5. Polish: README, the no-traceback guard, the coverage gate, and the manual quickstart
   (acceptance criterion 5 is met across all phases).

### Issue Acceptance Criteria → Tasks

| Acceptance criterion (issue #9) | Tasks |
|---|---|
| A job can be created interactively and appears in `invio job list` | T015, T017, T024, T026, T027 |
| Invalid input is rejected inline without aborting the wizard | T013, T023, T024, T025, T026 |
| `create --from-file` works without a TTY | T028, T029 |
| `edit` rejects invalid YAML and offers to retry or abort | T003, T004, T030, T031 |
| CLI commands are covered by Typer `CliRunner` tests | T014, T015, T016, T024, T028, T030, T032, T035, T038 |

---

## Notes

- [P] tasks touch different files and have no dependencies on incomplete tasks.
- Commit after each phase checkpoint on branch `gh-issue-9`, with commit messages referencing #9.
- The PR description must justify the new runtime dependency `questionary` (research R1), note
  that `rich` is now declared explicitly, and mention the `JobService` additions (`overview`,
  `stored_config`) introduced for Clarification Q4.
