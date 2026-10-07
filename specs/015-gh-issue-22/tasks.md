---

description: "Task list for `invio job run` with dry-run output and run history (#22)"
---

# Tasks: Manual Job Runs with Dry-Run Output and Run History

**Input**: Design documents from `specs/015-gh-issue-22/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/cli-commands.md](contracts/cli-commands.md),
[contracts/pipeline-and-stats-delta.md](contracts/pipeline-and-stats-delta.md),
[quickstart.md](quickstart.md)

**Tests**: Included. FR-020 and constitution III require an automated test for every
acceptance scenario, including the rejection paths. Write each story's tests first and confirm
they fail before implementing.

**Organization**: Tasks are grouped by user story in spec priority order:
- P1: US1 (dry-run preview) and US2 (exit codes)
- P2: US3 (real run), US4 (`--max-items`) and US5 (run history)
- P3: US6 (verbose)

`S<n>` refers to the scenario numbers in [quickstart.md](quickstart.md).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: the user story the task belongs to (US1–US6)

## Conventions used by every task

- Source goes in `src/invio/` and tests in `tests/`. Use strict mypy, with no new `Any` except
  the `runs.stats` dict.
- **Layering**:
  - `invio.cli` must never import `invio.db` (`tests/test_cli_layering.py`). It uses
    `invio.pipeline.run` and `invio.services.*`.
  - `invio.graph` must never import `invio.cli`, `invio.notify`, `invio.scheduling` or
    `invio.pipeline`.
- **Output streams**: results go to stdout through `rich.console.Console(markup=False, highlight=False, emoji=False)`,
  and progress, notes and errors go to stderr (`Console(stderr=True, …)` or
  `typer.echo(..., err=True)`). When stdout is not a terminal, use `width=_PIPE_WIDTH` (1000),
  as `list_jobs` in `src/invio/cli/commands/job.py` does.
- **Secrets and text**: never print or store provider, database or document text. Reuse the
  sanitizers `failure_message`, `item_failure_message` and `sanitized_error`.
- **Stats**: `runs.stats` stays `"version": 1`. New keys are additive, and readers must ignore
  unknown keys and tolerate missing ones.
- **CLI tests**: use `typer.testing.CliRunner` and read `result.stdout` and `result.stderr`
  separately. They run the real pipeline through the `run_cli` fixture (T001), with no network
  and no SMTP.

---

## Phase 1: Setup (Shared Test Infrastructure)

**Purpose**: the test harness every CLI story uses.

- [X] T001 Create `tests/run_cli_helpers.py` with a `RunCli` dataclass and a `run_cli` pytest fixture. The fixture:
  - creates a temp-file SQLite engine with the schema applied (same approach as the `engine` fixture in `tests/test_cli_notify.py`) and sets `INVIO_DATABASE_URL` to it via `monkeypatch`;
  - builds an `Env` with `tests.pipeline_helpers.build_env(engine, fake_clock)`, which stores job `"research"`;
  - monkeypatches `invio.cli.commands.job._make_service` to `JobService(env.factory)`;
  - monkeypatches `invio.cli.commands.job._run_deps` to an `@asynccontextmanager` that yields `env.deps`.

  `RunCli` exposes `env`, `invoke(args)` (runs `CliRunner().invoke(cli_main.app, args, env={"COLUMNS": "200"})`), `runs()` (all `Run` rows of the job, newest first), `job_row()` and `notifier_calls` (`env.ports.notifier`). Add helpers that reconfigure `env` for these cases:
  - one item failing at relevance (`partial`);
  - every source raising `FetchError` (`failed`);
  - no relevant items;
  - `N` candidates.

  Add `"tests/test_cli_job_run.py"` and `"tests/test_cli_run.py"` to the `F811` per-file-ignores in `pyproject.toml`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: the ports, scope fields, pipeline entry point, stats key and pure formatters that
several stories share.

**⚠️ CRITICAL**: no user story phase can start until this phase is complete.

### Tests for the foundation

- [X] T002 [P] Write `tests/test_run_output.py` for `invio.cli.run_output`.
  - `format_cost(stats)` follows research R10:
    - `llm_calls == 0` → `"$0.00"`
    - `cost_complete` true with `"0.010305"` → `"$0.0103"`
    - 2 of 5 calls unpriced, `"0.005"` → `"≥ $0.0050 (2 calls without price)"`
    - all calls unpriced → `"unknown"`
    - legacy stats without `llm_calls_unpriced` and `cost_complete` false → `"≥ $0.0050 (some calls without price)"`
  - `format_tokens(stats)` → `"in 48,211 · out 5,120 · total 53,331"`.
  - `format_duration(start, end)` → `"12.3 s"`, and `"—"` when `end` is `None`.
  - `stats_rows(...)` returns the rows of contracts/cli-commands.md § Output 2, in order:
    - `Skipped (budget)` only when > 0;
    - `Sources failed` as `"n/m"` only when > 0;
    - notification rows only for real runs;
    - `Processed` comes from `stats["processed"]`. For legacy stats without that key, it falls back to `after_keyword_filter − skipped_budget`, and to `"—"` when neither is present;
    - missing keys rendered as `"—"`.
- [X] T003 [P] Write `tests/test_run_pipeline_api.py`:
  - `run_job_by_name("research", dry_run=True, deps=env.deps)` returns a `RunResult` for the stored job.
  - `run_job_by_name("nope", ...)` raises `JobNotFoundError` and creates no run row.
  - `run_job(..., max_items=0)` raises `ValueError` before any run row or lock change.
  - A finished run's `stats["llm_calls_unpriced"]` equals the number of ledger entries with `cost_usd is None`. The fixture registry's `fakeco-free` and `fakeco-priced` models give both kinds.
  - `stats["processed"]` equals the number of items that were rated or failed.
  - When every source fails, `RunResult.error == "all sources failed"`. `RunResult.started_at` and `RunResult.finished_at` equal the run row's values, and `finished_at` is not before `started_at`.

### Implementation for the foundation

- [X] T004 [P] Add `ProgressCounts`, `ProgressEvent` and `RunObserver` to `src/invio/graph/ports.py`, exactly as in contracts/pipeline-and-stats-delta.md § `invio.graph.ports`.
  - `ProgressCounts` is a mutable `@dataclass(slots=True)` with int fields `found`, `new`, `after_keyword_filter`, `selected`, `processed`, `relevant` and `failed`, all defaulting to 0. It gets a `snapshot()` method returning a copy.
  - `ProgressEvent` is frozen and kw-only. Its `outcome` is `Literal["relevant", "irrelevant", "failed"] | None`.
  - Export all three in `__all__`.
  - Import `RunStage` from `invio.graph.state` only under `TYPE_CHECKING` if a cycle arises.
- [X] T005 Extend `RunScope` in `src/invio/graph/scope.py`. Add a frozen `ItemRef(title: str, url: str)` and these fields:
  - `max_items: int | None = None`
  - `observer: RunObserver | None = None`
  - `progress: ProgressCounts = field(default_factory=ProgressCounts)`
  - `item_refs: dict[int, ItemRef] = field(default_factory=dict)`

  Extend `new_scope(...)` in `src/invio/graph/build.py` with keyword-only `max_items: int | None = None` and `observer: RunObserver | None = None`. Depends on T004.
- [X] T006 Add `emit(scope: RunScope, event: ProgressEvent) -> None` to `src/invio/graph/stages.py`.
  - When `scope.observer` is not `None`, it calls the observer inside `try/except Exception`.
  - On an exception it logs `logger.warning("run.observer_failed", extra={"error": type(err).__name__})` and never re-raises.

  Also add the helper `stage_event(scope, stage)`, which builds a `kind="stage"` event with `scope.progress.snapshot()`. Depends on T005.
- [X] T007 In `src/invio/graph/nodes/persist.py`:
  - add `"llm_calls_unpriced": sum(1 for e in budget.ledger if e.cost_usd is None)` to `_ledger_stats`;
  - add `"processed": len({o.item_id for o in result.relevance})` to `build_stats`. These are the items that reached a relevance outcome, rated or failed; an item failed at extraction gets a synthesized `FAILED` relevance outcome. `_ledger_stats` alone (the recovery layout) does not include it;
  - rename `_sanitized_error` to public `sanitized_error`, add it to `__all__`, and update its callers in the same module.

  The key is documented in contracts/pipeline-and-stats-delta.md; the #19 contract file stays as it is.
- [X] T008 Extend `src/invio/pipeline/run.py`:
  - `run_job(..., max_items: int | None = None, observer: RunObserver | None = None)`:
    - validates `max_items >= 1` (`ValueError(f"max_items must be >= 1, got {max_items}")`) next to the `concurrency` check, before any write;
    - passes both values through the `deps is None` recursion and into `new_scope(...)`.
  - Add `run_job_by_name(name, *, dry_run=False, deps=None, concurrency=None, max_items=None, observer=None) -> RunResult`:
    - it resolves the id with `JobRepository.get_by_name` inside `session_scope(deps.session_factory)`;
    - when `deps is None`, it opens `default_deps(get_settings())` first, as `run_job` does;
    - it raises `JobNotFoundError(name)` for an unknown name, then delegates to `run_job`.
  - Add three fields to `RunResult`: `error: str | None` (`runs.error`), `started_at: datetime` and `finished_at: datetime | None`. Read them from the run row in `_run`, in the same session that reads `status` and `stats`.
  - Export both in `__all__` and re-export them from `src/invio/pipeline/__init__.py` if that module re-exports `run_job`.

  Makes T003 pass. Depends on T005.
- [X] T009 [P] Create `src/invio/cli/run_output.py` with pure functions and no I/O: `format_cost`, `format_tokens`, `format_duration`, `format_time(dt, tz)` (`YYYY-MM-DD HH:MM <abbr>`, reusing the logic of `_fmt_next_run` in `src/invio/cli/commands/job.py`), `stats_rows(...)` and `stats_table(rows) -> rich.table.Table` (two columns, `Metric` and `Value`).

  Signature: `stats_rows(*, run_id: int, status: RunStatus, dry_run: bool, stats: Mapping[str, Any], started_at: datetime | None, finished_at: datetime | None, notifications: tuple[int, int] | None) -> list[tuple[str, str]]`.

  Makes T002 pass. It must not import `invio.db`.
- [X] T010 Add the test seam `_run_deps() -> AbstractAsyncContextManager[RunDeps]` to `src/invio/cli/commands/job.py`, under "test seams". Production returns `default_deps(get_settings())`.

**Checkpoint**: `uv run pytest tests/test_run_output.py tests/test_run_pipeline_api.py` passes, and the existing suite is still green.

---

## Phase 3: User Story 1 - Preview a job's digest without sending anything (Priority: P1) 🎯 MVP

**Goal**: `invio job run <name> --dry-run` shows live progress (found, new, relevant) on
stderr, then prints the digest Markdown, the stats table and the token/cost line on stdout. It
sends nothing and leaves `next_run_at` unchanged.

**Independent Test**: S1, S2 and S11 run against the `run_cli` fixture.

### Tests for User Story 1

- [X] T011 [P] [US1] Write `tests/test_run_progress.py`, which runs `run_job(env.job_id, dry_run=True, deps=env.deps, observer=recorder)` with a recording observer. Assert:
  - the stage events arrive in the order `deduplicate`, `keyword_prefilter`, then `synthesize_digest`, `persist`, `notify` and `finalize`;
  - the `deduplicate` event has `found` and `new` equal to `stats["found"]` and `stats["new"]`;
  - one `item` event arrives per selected item, with `title` taken from the candidate and `outcome` one of `relevant`/`irrelevant`/`failed`;
  - the last event's `processed`, `relevant` and `failed` equal the stats;
  - the counts never decrease;
  - an observer that raises on every call leaves the run's status and stats identical to a run without an observer, and logs `run.observer_failed`.
- [X] T012 [P] [US1] Write `tests/test_cli_job_run.py` § dry run, using the `run_cli` fixture:
  - `job run research --dry-run` exits 0;
  - `result.stdout` contains `# <digest title>`, then the digest body verbatim (`RunResult.digest.body`), the stats-table rows `Found`, `New`, `Relevant` and `Failed`, and a line starting with `Tokens: in`;
  - `run_cli.notifier_calls == []`;
  - the job's `next_run_at` is unchanged;
  - exactly one new run row exists, with `stats["dry_run"] is True`;
  - a job with nothing relevant prints `No digest: nothing relevant was found.` and the stats, and exits 0 (S2);
  - `result.stdout` contains no `progress:` line, while `result.stderr` contains `progress: deduplicate found=` (S11, non-TTY rendering).

### Implementation for User Story 1

- [X] T013 [US1] In `src/invio/graph/stages.py`:
  - in `deduplicate`, fill `scope.item_refs[item.id] = ItemRef(title=item.title[:300], url=item.url)` for every taken item, next to the `item_types` update;
  - set `scope.progress.found/new` from `result.stats` and `emit` a stage event;
  - in `keyword_prefilter`, set `after_keyword_filter` and `selected` to `len(selected)`, then `emit`;
  - `emit` a stage event at the end of `synthesize_digest`, `persist`, `notify` and `finalize` (in `finalize` only when not already finalized).

  Depends on T006.
- [X] T014 [US1] In `src/invio/graph/build.py`, emit item progress from `process_item`:
  - after a successful item, increment `scope.progress.processed`;
  - increment `relevant` when `result.relevance.status == ItemStatus.RELEVANT`, and `failed` when either outcome is `FAILED`;
  - `emit` a `kind="item"` event with:
    - `item_id`;
    - `title` from `scope.item_refs`;
    - `outcome` (`"failed"` if any outcome failed, `"relevant"` if rated relevant, else `"irrelevant"`);
    - `message`, the failed outcome's `error`.
  - Do the same in `_fail_item`, with `outcome="failed"` and `message=item_failure_message(err)`.
  - Do not emit on `BudgetExceeded` or `_RunAborted`.

  Depends on T013.
- [X] T015 [P] [US1] Create `src/invio/cli/progress.py` with `RunProgressView`, which is callable as a `RunObserver`:
  - The constructor is `RunProgressView(console: Console, *, verbose: bool = False)`, where `console` is a stderr console.
  - On a terminal (`console.is_terminal`), `start()` and `stop()` wrap a `rich.live.Live(console=console, transient=True, refresh_per_second=8)`. The live renderable is:
    - the stage line;
    - `found N · new N · after filter N · relevant N · failed N`;
    - `processed i/n`, once `selected > 0`.
  - Otherwise it prints plain lines for stage events, e.g.:
    - `progress: deduplicate found=12 new=5`
    - `progress: keyword_prefilter after_keyword_filter=4`
    - `progress: items processed=i/n relevant=r failed=f`, printed only when an item completes the batch, or on every item when `verbose`
    - `progress: <stage>` for the later stages
  - It is usable as a context manager, so `stop()` always runs.
- [X] T016 [US1] Add the `run` command to `src/invio/cli/commands/job.py`, with this signature:

  ```python
  @app.command("run")
  def run(
      name: str,
      dry_run: bool = typer.Option(False, "--dry-run"),
      max_items: int | None = typer.Option(None, "--max-items"),
      verbose: bool = typer.Option(False, "--verbose", "-v"),
  ) -> None
  ```

  Steps:
  1. Call `service.get_by_name(name)` through `_make_service()`.
  2. Build `RunProgressView(Console(stderr=True, …), verbose=verbose)`.
  3. Run `asyncio.run(_run(...))`, where the inner coroutine is `async with _run_deps() as deps: return await run_job_by_name(name, dry_run=…, max_items=…, observer=view, deps=deps)`.
  4. Print the output blocks to stdout in this order:
     - the digest, for dry runs: `# <title>`, a blank line, then the body exactly as stored; or `No digest: nothing relevant was found.` when `result.digest` is `None` or has no item ids;
     - `stats_table(stats_rows(...))`;
     - `Tokens: <format_tokens> · cost <format_cost>`.

  The duration row uses `result.started_at` and `result.finished_at` (T008), so `job run` and `run show` compute it the same way.

  Run the coroutine through a one-line seam, `_execute(coro) -> RunResult`, which returns `asyncio.run(coro)`. US2 uses it for the Ctrl-C test. Exit-code handling is completed in US2. For now, exit with 0. Depends on T008, T009, T010, T014 and T015.

**Checkpoint**: S1, S2 and S11 pass. A dry run is fully usable as the MVP.

---

## Phase 4: User Story 2 - Scriptable outcome through exit codes (Priority: P1)

**Goal**: exit 0 for `succeeded`, 1 for `failed` or "could not start", and 2 for `partial`.
Every refusal is a one-line stderr message, and none of them creates a run row.

**Independent Test**: S3, S4, S5, S8 and S18.

### Tests for User Story 2

- [X] T017 [P] [US2] Add these tests to `tests/test_cli_job_run.py` § exit codes:
  - a succeeded run → 0;
  - one item failing at relevance → 2, with stderr containing `finished partial: 1 error(s); see 'invio run show <id>'`;
  - every source failing → 1, with stderr `run <id> failed: all sources failed`;
  - a parametrized refusal test, each case asserting exit 1, a one-line stderr message and an unchanged run-row count:
    - unknown job → `Error: job 'nope' not found`;
    - disabled job (`build_env(..., enabled=False)`) → `is disabled`;
    - invalid stored config (`JobCli.corrupt_timezone`-style edit of the stored config) → exit **1**, not 2;
    - job locked (`build_env(..., locked_until=<future>)`) → `is running (locked until`;
  - a missing `INVIO_DATABASE_URL` with `_run_deps` not patched → `Configuration error:` and exit 1;
  - Ctrl-C at CLI level: monkeypatch `job._execute` to close the coroutine and raise `KeyboardInterrupt` → exit 1, stderr `interrupted; run recorded as failed`, and no traceback. This is deterministic and needs no real signal or task cancellation.
  - Ctrl-C at pipeline level, in `tests/test_run_pipeline_api.py`: `run_job` with a `fetch_source` that raises `asyncio.CancelledError` re-raises it, the run row is `failed` and the job's `locked_until` is `None`. If an equivalent #21 safety-net test already exists in `tests/test_pipeline_*.py`, reference it instead of duplicating it.
  - `job run --help` contains `Exit codes: 0 succeeded, 1 failed or could not start, 2 partial.`

### Implementation for User Story 2

- [X] T018 [US2] In `src/invio/cli/commands/job.py`, add a context manager `_run_errors()`. It maps the following to a one-line stderr message through `fail(...)`:

  | Exception | Exit | Message |
  |---|---|---|
  | `JobNotFoundError` | 1 | `Error: job '<name>' not found` |
  | `StoredJobConfigError` / `JobConfigError` | **1** | existing config text |
  | `JobDisabledError` | 1 | `Error: job '<name>' is disabled; enable it with 'invio job enable <name>'` |
  | `JobBusyError` | 1 | `Error: job '<name>' is running (locked until <format_time(locked_until, tz)>)` |
  | `MissingSettingError` / `ValidationError` from settings | 1 | `Configuration error: …` |
  | `SQLAlchemyError` | 1 | the existing type-name-only message |
  | `KeyboardInterrupt` | 1 | `interrupted; run recorded as failed` |

  Import `JobDisabledError` and `JobBusyError` from `invio.pipeline.run`. Add the disabled pre-check after `get_by_name` (raise `JobDisabledError`-equivalent message without calling the pipeline). Depends on T016.
- [X] T019 [US2] Finish the `run` command in `src/invio/cli/commands/job.py`:
  - After printing the output, map `result.status`:
    - `SUCCEEDED` → return (0);
    - `FAILED` → stderr `run <id> failed: <message>`, then `raise typer.Exit(1)`;
    - `PARTIAL` → stderr `run <id> finished partial: <len(result.errors)> error(s); see 'invio run show <id>'`, then `raise typer.Exit(2)`.
  - `<message>` is `result.error`, the sanitized `runs.error` (for example `all sources failed`). It falls back to `result.errors[0].error_class`, then to `"run failed"`.
  - Wrap the whole body in `_run_errors()`.
  - Add `Exit codes: 0 succeeded, 1 failed or could not start, 2 partial.` to the docstring and help.

**Checkpoint**: S3, S4, S5, S8 and S18 pass. Scripts can branch on the exit code.

---

## Phase 5: User Story 3 - Real manual run (Priority: P2)

**Goal**: without `--dry-run`, the run saves its results, delivers the notification and
reports how many notifications were sent and failed. The digest is printed only with
`--verbose`.

**Independent Test**: S9.

### Tests for User Story 3

- [X] T020 [P] [US3] Add these tests to `tests/test_cli_job_run.py` § real run. `job run research` exits 0 and has these effects:
  - `run_cli.notifier_calls` has one entry;
  - stdout contains the rows `Notifications sent | 1` and `Notifications failed | 0`, and does **not** contain the digest body;
  - the run row has no `dry_run` key;
  - `next_run_at` advanced (it equals `plus_one_hour` of the clock).

### Implementation for User Story 3

- [X] T021 [US3] In `src/invio/cli/commands/job.py`'s `run`:
  - pass `notifications=(result.notifications_sent, result.notifications_failed)` to `stats_rows` when `not result.dry_run`;
  - print the digest block only when `result.dry_run` (US6 adds the `--verbose` case).

  Depends on T019.

**Checkpoint**: S9 passes.

---

## Phase 6: User Story 4 - Limit the size of a test run (Priority: P2)

**Goal**: `--max-items N` caps the items processed in this run to `min(N, job limit)`, prints
a note when `N` is above the limit, rejects `N < 1` with exit 1, and never changes the stored
config.

**Independent Test**: S6, S7 and S8.

### Tests for User Story 4

- [X] T022 [P] [US4] Add these tests to `tests/test_run_pipeline_api.py`:
  - with 10 new candidates and `max_items=3`, `stats["after_keyword_filter"] <= 3` and at most 3 items are taken;
  - with `max_items=1000` and the job's `max_items_per_run=5`, at most 5 items are taken;
  - the stored `jobs.config` is byte-identical before and after both runs.
- [X] T023 [P] [US4] Add these tests to `tests/test_cli_job_run.py` § max-items:
  - `--dry-run --max-items 2` with 10 candidates prints `Processed` ≤ 2 and exits 0;
  - `--max-items 1000` with job limit 100 shows `note: --max-items 1000 exceeds the job limit 100; using 100` on stderr, and the run proceeds;
  - `--max-items 0` → exit 1, `Error: --max-items must be at least 1`, no run row;
  - `--max-items abc` → exit 2 (Click usage error).

### Implementation for User Story 4

- [X] T024 [US4] In `load_job` in `src/invio/graph/stages.py`, after validation, when `scope.max_items is not None`:
  - set `config = config.model_copy(update={"limits": config.limits.model_copy(update={"max_items_per_run": min(scope.max_items, config.limits.max_items_per_run)})})`;
  - assign it to `scope.config`;
  - never write it to the database.

  Depends on T005. Makes T022 pass.
- [X] T025 [US4] In the `run` command in `src/invio/cli/commands/job.py`, before calling the pipeline:
  - when `max_items is not None and max_items < 1`, `raise fail("Error: --max-items must be at least 1", 1)`;
  - after `get_by_name`, when `max_items > record.config.limits.max_items_per_run`, print `note: --max-items {N} exceeds the job limit {M}; using {M}` to stderr.

  Declare `--max-items` without `min=`, so the range error exits 1 (research R7). Depends on T019.

**Checkpoint**: S6, S7 and S8 pass.

---

## Phase 7: User Story 5 - Inspect run history (Priority: P2)

**Goal**: `invio run list [--job NAME] [--limit N]` and `invio run show <id>`. `show` lists
every stored error with its stage, message and the item's title and URL. The error list is
persisted per run by `finalize` (clarification Q1), so it survives retries and dry runs.

**Independent Test**: S12–S17.

### Tests for User Story 5

- [X] T026 [P] [US5] Write `tests/test_run_errors.py`.
  - The pure `stored_errors(errors, items, refs, failure)`:
    - an item `RunError` with a matching `ItemResult` whose relevance outcome has `error="LLMInvalidOutputError: …"` gives `message` equal to that text, plus `title` and `url` from `refs`;
    - a source error gives `message="FetchError: source failed"` and `source="0:rss"`, with no URL;
    - a fatal error with `failure=ValidationError` gives `sanitized_error(failure)`;
    - entries are sorted by `STAGE_ORDER`, then item id, with exact duplicates removed;
    - 150 errors give 100 entries and `omitted=50`;
    - `message` is truncated to 500 chars and `title` to 300.
  - `RunRepository.record_errors`:
    - merges into the existing stats and leaves the other keys untouched;
    - turns `NULL` stats into `{"version": 1, "errors": [...]}`;
    - returns `False` and changes nothing for a `running` run or an unknown id;
    - never changes `status`, `error` or `finished_at`;
    - writes `errors_omitted` only when > 0.
  - End to end through `run_job`:
    - a partial run with one failing item stores `stats["errors"]` with that item's title and URL;
    - a **dry** run with a failing item stores the same, although the item rows were rolled back (S15);
    - a run whose `fetch_sources` raises stores a `fetch_sources` entry;
    - a succeeded run stores `"errors": []`;
    - if `record_errors` is monkeypatched to raise `SQLAlchemyError`, the run status is unchanged and `run.errors_not_recorded` is logged.
- [X] T027 [P] [US5] Write `tests/test_run_service.py` against seeded runs, using `JobCli.add_run`-style helpers plus direct stats writes.
  - `RunService.list()`:
    - returns all jobs newest first (`started_at DESC, id DESC`);
    - `job="a"` filters;
    - `limit=2` caps the result;
    - an unknown job raises `JobNotFoundError`;
    - an empty database gives `[]`.
  - Summary fields: `dry_run` comes from `stats["dry_run"]`, and `found`, `new` and `relevant` are `None` when absent.
  - `RunService.get()`:
    - returns `errors=None` for legacy stats without an `errors` key;
    - returns `()` for `[]`;
    - shows an unparsable entry as message `"<unreadable error entry>"`;
    - `errors_omitted` defaults to 0;
    - `timezone` is the job's `schedule.timezone`, or `"UTC"` when the stored config is invalid;
    - an unknown id raises `RunNotFoundError`.
- [X] T028 [P] [US5] Write `tests/test_cli_run.py`.
  - `run list`:
    - prints the header `ID Job Started Duration Status Found New Relevant` and one row per run, with `(dry)` on dry runs;
    - `--job research` filters;
    - `--job nope` → exit 1, `Error: job 'nope' not found`;
    - with no runs → `No runs.` and exit 0;
    - `--limit 0` → exit 1.
  - `run show <id>` of a partial run (produced with `run_cli`):
    - exits 0 and shows `Errors (1)`, the stage `score_relevance`, the sanitized message, `"<title>"` and the URL;
    - after re-running the job so the item succeeds, `run show <old id>` still shows the error (S14).
  - Other cases:
    - a legacy run → `Errors: item errors are not available for this run`;
    - a run with an empty list → `Errors: none`;
    - a `running` run → status `running`, duration `—`, exit 0;
    - an unknown id → exit 1, `Error: run 999 not found`;
    - `run show abc` → exit 2;
    - the cost line follows R10 for complete, partial and unpriced stats (S17).

### Implementation for User Story 5

- [X] T029 [P] [US5] Create `src/invio/graph/errors.py` with `MAX_STORED_ERRORS: Final = 100`, `MAX_MESSAGE_CHARS: Final = 500` and `MAX_TITLE_CHARS: Final = 300`.
  - The frozen kw-only dataclass `StoredError` has `stage: RunStage`, `error_class: str`, `message: str`, `item_id: int | None = None`, `title: str | None = None`, `url: str | None = None` and `source: str | None = None`.
    - `__post_init__` rejects a blank `error_class`, and rejects `item_id` and `source` set together.
    - `to_json()` omits `None` fields.
  - The pure `stored_errors(errors, items, refs, failure) -> tuple[list[StoredError], int]`. Its message rules come from research R3:
    - an item error takes the failed outcome's `error` (relevance or summary) for that `item_id` and stage, falling back to `"<Class>"`;
    - a fatal error is the first error in `errors` whose class equals `type(failure).__name__`, and takes `sanitized_error(failure)`;
    - a source error takes `"<Class>: source failed"`;
    - anything else takes `"<Class>"`.
  - Sort and cap as T026 requires. Depends on T007.
- [X] T030 [P] [US5] Add two methods to `RunRepository` in `src/invio/db/repositories.py`:
  - `record_errors(self, run_id: int, entries: Sequence[Mapping[str, Any]], *, omitted: int = 0) -> bool`, which works as specified in contracts/pipeline-and-stats-delta.md. It assigns a **new** dict to `run.stats` so SQLAlchemy detects the JSON change, and it flushes only.
  - `list_recent(self, *, job_id: int | None = None, limit: int = 20) -> list[tuple[Run, str]]`, a single `select(Run, Job.name).join(Job)`, ordered by `Run.started_at.desc(), Run.id.desc()`.
- [X] T031 [US5] In `finalize` in `src/invio/graph/stages.py`, after the status is known (after `record_failure` when `fatal`) and **before** `release_lock`:
  - build the entries with `stored_errors(state.get("errors", []), state.get("items", []), scope.item_refs, scope.failure)`;
  - write them in a new `session_scope(deps.session_factory)` with `RunRepository(session).record_errors(scope.run_id, [e.to_json() for e in entries], omitted=omitted)`;
  - on `Exception`, log `logger.warning("run.errors_not_recorded", extra={"error": type(err).__name__, "db_run_id": scope.run_id})` and continue.

  Depends on T013, T029 and T030. Makes the end-to-end part of T026 pass.
- [X] T032 [US5] Create `src/invio/services/runs.py` with these frozen kw-only dataclasses, following data-model §3: `RunSummary`, `RunDetail` and `RunErrorView`.
  - `RunNotFoundError(LookupError)` carries `run_id`.
  - `RunService(session_factory)` has `from_settings(settings=None)`, mirroring `JobService.from_settings`.
  - `list(*, job=None, limit=20)` resolves `job` with `JobRepository.get_by_name` (raising `JobNotFoundError(job)` when it is not found), then calls `list_recent`.
  - `get(run_id)` reads the run and its job. It parses `stats["errors"]` leniently (see T027). Its `timezone` comes from `validate_job(job.config).schedule.timezone`, with `JobConfigError` → `"UTC"`.
  - It returns no ORM objects. Depends on T030. Makes T027 pass.
- [X] T033 [P] [US5] Add the following to `src/invio/cli/run_output.py`:
  - `format_errors(errors: tuple[RunErrorView, ...] | None, omitted: int) -> list[str]`, which renders the `Errors (n)` block of contracts/cli-commands.md:
    - the stage is padded to 17 chars;
    - a second line shows `"<title>" — <url>` for item errors and `(source #<index+1>, <type>)` for source keys;
    - `… and <n> more not stored` when `omitted` > 0;
    - `Errors: none` for an empty list;
    - `Errors: item errors are not available for this run` for `None`.
  - `runs_table(summaries, tz_of) -> rich.table.Table`, with the columns `ID`, `Job`, `Started`, `Duration`, `Status` (plus ` (dry)`), `Found`, `New` and `Relevant`, and `—` for `None`.

  Depends on T009 and T032.
- [X] T034 [US5] Create `src/invio/cli/commands/run.py`, defining `app = typer.Typer(help="Inspect job runs.", no_args_is_help=True)` and a `_make_service()` seam returning `RunService.from_settings()`.
  - `list` takes `--job` and `--limit` (default 20; `< 1` → `fail("Error: --limit must be at least 1", 1)`). It prints `No runs.` or `No runs for job '<name>'.`, or `runs_table`.
  - `show <id: int>` prints:
    - the header `Run <id> · job <name> · <status>[ (dry run)]`;
    - the `Started`/`Finished`/`Duration` line in `detail.timezone`;
    - the `Error` line (`—` when `None`);
    - the stats table without notification rows, skipped for `running`;
    - the `Tokens:` line;
    - `format_errors`.
  - Error mapping:
    - `RunNotFoundError` → `Error: run <id> not found`, exit 1;
    - `JobNotFoundError` → exit 1;
    - `SQLAlchemyError` → the same type-name-only message as `job._errors`, exit 1;
    - `MissingSettingError` → exit 2.
  - Use the stdout console with the `_PIPE_WIDTH` rule.

  The command is auto-discovered, so no other file changes. Depends on T033. Makes T028 pass.

**Checkpoint**: S12–S17 pass. Failed items are identifiable from one command (SC-003).

---

## Phase 8: User Story 6 - Verbose diagnostics (Priority: P3)

**Goal**: `--verbose` prints one stderr line per item outcome and prints the digest for real
runs too.

**Independent Test**: S10.

### Tests for User Story 6

- [X] T035 [P] [US6] Add these tests to `tests/test_cli_job_run.py` § verbose:
  - a run with relevant, irrelevant and failed items using `--dry-run -v` writes stderr lines `item: relevant  <title>`, `item: irrelevant  <title>` and `item: failed  <title>  <message>` (one per item), and none of them appear on stdout;
  - `job run research -v` (a real run) prints the digest body on stdout.

### Implementation for User Story 6

- [X] T036 [US6] In `RunProgressView` in `src/invio/cli/progress.py`, when `verbose` and `event.kind == "item"`, print `item: <outcome:<10> <title>[  <message>]`. On a TTY, print through `live.console.print` so the line appears above the panel.

  Then, in the `run` command in `src/invio/cli/commands/job.py`, print the digest block when `result.dry_run or verbose`. Depends on T015 and T021.

**Checkpoint**: S10 passes. All stories are complete.

---

## Phase 9: Polish & Cross-Cutting Concerns

- [X] T037 [P] Extend `tests/test_graph_layering.py` (or the existing layering test) so that `invio.graph.errors` and `invio.graph.ports` import nothing from `invio.cli`, `invio.pipeline`, `invio.notify` or `invio.scheduling`. Also confirm that `tests/test_cli_layering.py` still passes with the new `cli/run_output.py`, `cli/progress.py` and `cli/commands/run.py`, which import no `invio.db`.
- [X] T038 [P] Update `README.md`:
  - in "Running a job", document `invio job run <name> [--dry-run] [--max-items N] [--verbose]`, the stdout/stderr split, the exit codes (0/1/2, including that a config error exits 1 here), the item cap rule `min(N, limit)`, and the cost line format;
  - add a new "Run history" subsection for `invio run list` and `invio run show`, covering the stored errors and the legacy-run note;
  - in "Runs, statistics and the token budget", list the new stats keys `llm_calls_unpriced`, `errors` and `errors_omitted`;
  - remove the sentence "The CLI command follows in #22".
- [X] T039 [P] Add a CHANGELOG/PR note in `specs/015-gh-issue-22/pr-description.md` (the same format as `specs/014-gh-issue-21/pr-description.md`). It records the constitution II exit-code deviation (plan Complexity Tracking) and confirms no new runtime dependency.
- [X] T040 Run the full gates: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src` and `uv run pytest -q`. Fix any findings in the files touched above.
- [ ] T041 Walk through [quickstart.md](quickstart.md) § 2 and tick every scenario S1–S18 against the automated tests. Do a manual TTY spot check (§ 3) of the live panel in a real terminal.

---

## Dependencies & Execution Order

### Phase dependencies

- **Setup (T001)**: no dependencies.
- **Foundational (T002–T010)**: depends on Setup. It **blocks all stories**.
- **US1 (T011–T016)**: depends on Foundational. This is the MVP.
- **US2 (T017–T019)**: depends on US1's `run` command (T016).
- **US3 (T020–T021)**: depends on US2 (T019).
- **US4 (T022–T025)**:
  - T022 and T024 (pipeline) depend only on Foundational;
  - T023 and T025 (CLI) depend on T019.
- **US5 (T026–T034)**:
  - T026–T030 and T032–T033 depend only on Foundational, so they can run in parallel with US1–US4;
  - T031 depends on T013 (`item_refs` captured in `deduplicate`).
- **US6 (T035–T036)**: depends on US1 (T015) and US3 (T021).
- **Polish (T037–T041)**: after all the desired stories.

### Story independence

- **US5's history commands** (`run list`/`run show`) work on any stored runs, including runs
  made by `run_job` directly or by the scheduler. They can therefore be delivered without the
  `job run` command.
- **US4's pipeline override** is usable by `run_job` callers without the CLI.

### Within each story

Write the tests first and see them fail. Then build in this order: pure functions/models, then
repository/service, then graph wiring, then the CLI command.

## Parallel Execution Examples

```text
# Foundational, after T001:
T002 tests/test_run_output.py    ‖ T003 tests/test_run_pipeline_api.py
T004 graph/ports.py              ‖ T007 nodes/persist.py ‖ T009 cli/run_output.py ‖ T010 job seam

# US1:
T011 tests/test_run_progress.py  ‖ T012 tests/test_cli_job_run.py (dry run) ‖ T015 cli/progress.py

# US5 (can run alongside US1–US4 once Foundational is done):
T026 tests/test_run_errors.py ‖ T027 tests/test_run_service.py ‖ T028 tests/test_cli_run.py
T029 graph/errors.py          ‖ T030 db/repositories.py

# Polish:
T037 layering tests ‖ T038 README.md ‖ T039 pr-description.md
```

## Implementation Strategy

### MVP first (US1 only)

1. Phase 1 (T001), then Phase 2 (T002–T010).
2. Phase 3 (T011–T016): `invio job run <name> --dry-run` shows progress, the digest, stats and
   cost.
3. **Stop and validate** S1, S2 and S11. This already meets the issue's core goal ("see what
   would be sent").

### Incremental delivery

1. Add US2 (exit codes). The command is now scriptable, which covers acceptance criteria 1 and
   2 of the issue.
2. Add US3 and US4 (real run, item cap).
3. Add US5 (history with stored errors), which covers the issue's acceptance criterion 3.
4. Add US6 (verbose), then Polish. The full CI gates and quickstart cover acceptance
   criterion 4 (`FakeProvider` and fixtures).

### Parallel team strategy

After Foundational, developer A takes US1 → US2 → US3 → US4 (CLI) → US6. Developer B takes US5
(errors persistence, service, `run` group) and the US4 pipeline override (T022, T024).
