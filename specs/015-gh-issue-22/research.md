# Research: Manual Job Runs with Dry-Run Output and Run History (#22)

All Technical Context unknowns are resolved below. Every decision builds on the #21 pipeline
(`invio.pipeline.run.run_job`), the #19 run statistics (`runs.stats`, version 1) and the CLI
conventions of #9.

---

## R1 — Where the per-run error list is stored

**Decision**: Use a new additive key `errors` inside `runs.stats`, still version 1. It holds a
list of objects `{stage, error_class, message, item_id?, title?, url?, source?}`. At most
`MAX_STORED_ERRORS = 100` entries are kept. `errors_omitted` (int) counts any errors left out
beyond that cap. A finished run with no errors gets `"errors": []`.

**Rationale**:
- Clarification Q1 asks for a persisted per-run list. The `runs.stats` JSON column is already
  written exactly once per run, in every path: normal save, dry-run save and failure recovery.
- The #19/#21 contracts make consumers ignore unknown keys, so the addition is
  backward-compatible and needs no migration.
- One row per run keeps `run list` at one query.

**Alternatives considered**:
- **A `run_errors` table.** It needs a migration on SQLite and MariaDB, and a join for
  `run show`. Nothing queries errors across runs, so the extra table brings no benefit (this
  was clarification option C).
- **A `runs.errors` JSON column.** It also needs a migration, for the same data.
- **Deriving the errors from `items.last_error`.** Clarification Q1 rejected this: later retries
  overwrite the text, and a dry run rolls the items back.

## R2 — When and by whom the error list is written

**Decision**: The list is written by the `finalize` node, the only way out of the graph, which
always runs. It calls a new `RunRepository.record_errors(run_id, entries)` in its own short
transaction:
- **When**: after the run's status has been saved (by `persist`, or by `record_failure` on the
  fatal path) and **before** `release_lock`.
- **How**: the write merges `errors`/`errors_omitted` into the stored `stats` dict. A run whose
  stats are `NULL` gets `{"version": 1, "errors": [...]}`.
- **On failure**: if writing the list fails, the run's status is unchanged. `finalize` logs
  `run.errors_not_recorded` with the error class only and continues.

The entries are built by a pure function, `invio.graph.errors.stored_errors(...)`. Its inputs:
- `state["errors"]`, the complete merged `RunError` list. Errors that `finalize` itself raises
  never reach the list.
- The item outcomes in `state["items"]`, which give the message of each failed item.
- `scope.item_refs`, which gives each item's title and URL.
- `scope.failure`, the original exception of the first fatal error.

**Rationale**:
- `finalize` sees the full error list, including errors raised after `persist`, such as a
  `notify` failure.
- Persisting the list inside `persist_run`/`finish_dry_run` would miss those later errors.
- `run_job`'s safety net (cancellation, graph build failure) runs outside the graph. In that
  case the run keeps only its `runs.error`, and `run show` says item errors are unavailable,
  the same as for runs from before this feature.

**Alternatives considered**:
- **Writing the list in `run_job` after `ainvoke`.** This would happen after the lock is
  released. A crash in between would lose the list, and a scheduler run that bypasses the CLI
  still goes through `finalize`, so `finalize` is the better place.

## R3 — Error messages without leaking secrets or document text

**Decision**: The `message` of each entry depends on the kind of error:
- **Item errors**: the outcome's existing `error` text. `failure_message` and
  `item_failure_message` have already sanitized it to `"<Class>: <facts>"`.
- **The fatal error**: `persist._sanitized_error(scope.failure)`, the same text as
  `runs.error`. This function is made public as `sanitized_error`.
- **Source errors**: `"<Class>: source failed"`, with `source` set to the source key
  (`"<index>:<type>"`). The source URL is never included (RunError invariant from #21). The CLI
  can resolve the key to the configured source's type and position.
- **Any other error**: `"<Class>"`.

`title` and `url` are captured at deduplication time into `scope.item_refs`. A dry run rolls
back the inserted items, so reading them in `finalize` would fail. Titles are cut to 300
characters.

**Rationale**: these are the same sanitization rules as #19/#21 (constitution V). Item URLs are
already stored in `items.url` and shown in digests.

## R4 — Passing `--max-items` into the run

**Decision**: `run_job` gets a new keyword-only parameter, `max_items: int | None = None`:
- It is validated to be `>= 1` before any write, raising `ValueError` like `concurrency`.
- It is stored on `RunScope.max_items`.
- `load_job` applies it as `config.model_copy(update={"limits": limits.model_copy(update={"max_items_per_run": min(max_items, limits.max_items_per_run)})})`,
  on the in-memory `scope.config` only.
- The stored job configuration is never written.

Because the effective cap is `min(N, job limit)`, the cap can only go down (clarification Q4).

The CLI computes the same `min` beforehand from the job's validated config. It prints the note
`note: --max-items N exceeds the job limit M; using M` on stderr when `N > M`.

**Rationale**: deduplication already enforces `limits.max_items_per_run` newest-first, so no
new selection logic is needed.

## R5 — Progress reporting from the graph to the CLI

**Decision**: A new port, `RunObserver`, is defined in `invio.graph.ports` as a protocol with
one method, `def __call__(self, event: ProgressEvent) -> None`. `ProgressEvent` is a frozen
dataclass with these fields:
- `kind`: `"stage"` or `"item"`
- `stage`: a `RunStage`
- for `"stage"` events, the cumulative counts `found`, `new`, `after_keyword_filter`,
  `selected`, `processed`, `relevant`, `failed`
- for `"item"` events, `item_id`, `title` and `outcome` (`relevant` | `irrelevant` | `failed` |
  `skipped_budget`) plus `message`

Who emits what:
- `run_job(..., observer=None)` stores the observer on the scope.
- Stages emit events through `stages.emit(scope, event)`, at these points:
  - after `deduplicate` (found, new)
  - after `keyword_prefilter` (after_keyword_filter, selected)
  - after each processed item (processed, relevant, failed, plus an item event)
  - at `synthesize_digest`, `persist`, `notify` and `finalize`
- `emit` catches and logs any exception from the observer (`run.observer_failed`, class only),
  so a broken display never fails a run.
- The counts live on the scope (`scope.progress`), because items finish concurrently. They all
  run on the event-loop thread, so no lock is needed.

**Rationale**: the progress needs to be per stage, not per token. `ainvoke` stays as it is.
LangGraph's `astream(stream_mode="updates")` would also work, but it changes how `run_job`
collects the final state, and its item updates arrive as merged `RunState` fragments without
titles.

**Alternatives considered**:
- **Polling the database for counts.** A dry run commits nothing, so there is nothing to poll.
- **Log-based progress.** Logs are JSON on stderr for machines and would clash with the live
  display.

## R6 — Live display and non-TTY behaviour

**Decision**: Two renderers, both on stderr, with a small `RunProgressView` that consumes
`ProgressEvent`s:
- **Interactive stderr**: `rich.live.Live` on a `rich.console.Console(stderr=True)`. The
  display has one status line (stage), a counts row (found · new · relevant · failed) and, when
  items are being processed, a `processed / selected` bar.
- **Non-interactive stderr** (`console.is_terminal` is false: pipes, cron, CI, `CliRunner`):
  one plain line per stage event, such as `progress: deduplicate found=12 new=5`.

With `--verbose`, each item event is printed as a line: `  relevant  <title>`,
`  failed    <title>  <message>`. On a TTY, these lines go above the live display through
`live.console.print`.

Final results go to stdout through a separate `Console(stderr=False)`: the digest Markdown, the
stats table and the usage line. Redirecting stdout therefore gives a clean document (SC-006).
The digest is printed as raw Markdown text, not rendered with `rich.markdown`, so stdout is the
exact text that would be mailed and is diffable.

**Rationale**: constitution II puts results on stdout and diagnostics on stderr. `rich` is
already a dependency.

## R7 — Exit codes and error mapping in `invio job run`

**Decision**:

| Situation | Exit | Message (stderr) |
|---|---|---|
| status `succeeded` | 0 | — |
| status `failed` | 1 | `run <id> failed: <runs.error>` |
| status `partial` | 2 | `run <id> partial: <n> error(s); see 'invio run show <id>'` |
| unknown job (`JobNotFoundError`) | 1 | `Error: job '<name>' not found` |
| stored config invalid (`StoredJobConfigError`) | **1** | the existing multi-line config error |
| job disabled (`JobDisabledError`, or the pre-check) | 1 | `Error: job '<name>' is disabled` |
| job busy (`JobBusyError`) | 1 | `Error: job '<name>' is running (locked until <time>)` |
| missing setting / invalid settings | 1 | `Configuration error: …` |
| database error (`SQLAlchemyError`) | 1 | the existing type-name-only DB message |
| Ctrl-C (`KeyboardInterrupt`) | 1 | `interrupted; run recorded as failed` |
| `--max-items` < 1 | 1 | `Error: --max-items must be at least 1` (checked in the command, before anything runs) |
| unknown option / non-integer `--max-items` | 2 | Click usage error (parsing, before anything runs) |

The pre-check before calling the pipeline is `JobService.get_by_name(name)`. It validates the
stored config and reads `enabled`. As a result, an invalid config, an unknown job and a disabled
job all fail without creating a run row, which matches the spec's edge case "without starting
a run".

The command's help text states: "exit codes: 0 succeeded, 1 failed or could not start, 2
partial (a configuration error exits 1 here)".

**Note on usage errors**: `--max-items` is declared without `min=` and range-checked in the
command, so a value below 1 exits 1 like every other refusal. Click still exits 2 for parse
errors, such as an unknown option or a non-integer value. These happen before any run, and the
message makes the cause obvious. Changing that would mean overriding Click's `main`, which none
of the other commands do. This residual overlap is recorded in Complexity Tracking.

## R8 — Resolving a job name to a run

**Decision**: Add `run_job_by_name(name, *, dry_run=False, deps=None, concurrency=None, max_items=None, observer=None) -> RunResult`
to `invio.pipeline.run`.
- It resolves the id with `JobRepository.get_by_name` inside `session_scope` and then delegates
  to `run_job`.
- An unknown name raises `JobNotFoundError(name)`.
- The CLI uses this function because the CLI must not import `invio.db` (enforced by
  `tests/test_cli_layering.py`), and `JobRecord` deliberately has no id.

## R9 — Run history read model

**Decision**: A new `invio.services.runs` module provides `RunService(session_factory)`, with
`from_settings()`. It has two methods:
- `list(job: str | None = None, limit: int = 20) -> list[RunSummary]`, newest first:
  `started_at DESC, id DESC`, joined with `jobs` for the name.
- `get(run_id: int) -> RunDetail`, which raises `RunNotFoundError`.

An unknown `job` filter raises `JobNotFoundError`.

The returned records are frozen dataclasses with no ORM objects, the same pattern as
`JobRecord`. Usage and cost come from `runs.stats`, because the ledger keys exist for normal,
dry and recovered runs alike.

`RunDetail.timezone` is the job's `schedule.timezone` when its stored config validates, and
`UTC` otherwise. The CLI formats times in that zone, as `invio job show` does.

The repository gains `RunRepository.list_recent(job_id: int | None, limit: int) -> list[tuple[Run, str]]`.

## R10 — Cost display (clarification Q3)

**Decision**: Add a new additive stats key, `llm_calls_unpriced` (int), to `_ledger_stats`,
computed as `sum(1 for e in ledger if e.cost_usd is None)`. One pure formatter,
`format_cost(stats) -> str`, lives in `invio.cli.run_output`. Its rules, in order:

1. `llm_calls == 0` → `$0.00`.
2. `cost_complete` → `$<estimated_cost_usd>`, at 4 significant decimals with a minimum of 2
   places, e.g. `$0.0123`.
3. Otherwise, if `llm_calls_unpriced` equals `llm_calls` → `unknown`.
4. Otherwise, if `llm_calls_unpriced` is present → `≥ $0.0123 (2 calls without price)`.
5. For older runs that lack the key → `≥ $0.0123 (some calls without price)`.

Tokens are shown as `in 48,211 · out 5,120 · total 53,331`.

## R11 — Testing approach

**Decision**:
- **CLI tests** (`tests/test_cli_job_run.py`, `tests/test_cli_run.py`) use Typer's
  `CliRunner` with a temporary SQLite database. The pipeline is the real one, run with
  `tests.pipeline_helpers.make_deps`: a purpose-routed `FakeProvider`, fake source and page
  ports, a `RecordingNotifier`, a fixed clock and a no-wait sleep. There is no network.
- **Test seam**: `job._run_deps()` is an async context manager that yields `RunDeps`.
  Production uses `default_deps(get_settings())`, and tests monkeypatch the seam. This follows
  the existing `_make_service` pattern.
- **Graph tests** cover `stored_errors`, `record_errors`, the observer events (a recording
  observer), an observer that raises, and the `max_items` override.
- **Service tests** cover `RunService.list`/`get` against seeded runs, including old-style
  stats without `errors` or `llm_calls_unpriced`.
- **Exit codes** are covered by fixture jobs that succeed, end `partial` (one item's relevance
  reply is invalid) and end `failed` (every source fetch raises `FetchError`).
- **"No mail sent"** is asserted with `RecordingNotifier.calls == []`, plus an unchanged
  `next_run_at`.
