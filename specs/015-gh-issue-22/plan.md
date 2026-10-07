# Implementation Plan: Manual Job Runs with Dry-Run Output and Run History

**Branch**: `gh-issue-22` | **Date**: 2026-10-07 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/015-gh-issue-22/spec.md`

## Summary

This plan adds a manual, observable front end to the #21 research pipeline. Nothing in the
pipeline logic is duplicated.

**`invio job run <name> [--dry-run] [--max-items N] [--verbose]`** behaves like this:
- **Pre-check.** It first validates the job through `JobService`, so an unknown, disabled or
  invalid job fails without creating a run row.
- **Run.** It then calls a new `run_job_by_name`.
- **Progress.** The live progress panel (found, new, relevant) appears on stderr, fed by a new
  `RunObserver` port.
- **Output.** It prints the digest Markdown, a stats table and the token/cost line to stdout.
- **Exit codes.** 0 succeeded, 1 failed or could not start, 2 partial (clarification Q2).

**`invio run list [--job NAME]`** and **`invio run show <id>`** are a new auto-discovered
`run` command group. It reads through a new `invio.services.runs.RunService`, because the CLI
must not import `invio.db`.

**Stored errors.** For `run show` to list failed items reliably (clarification Q1), every run
now stores its own error list in `runs.stats["errors"]`:
- It is written by `finalize`, which always runs, after the status is saved.
- Each entry holds the stage, a sanitized message and the item's title and URL.
- The title and URL are captured at deduplication time, so dry runs keep them too.

**Pipeline additions.**
- `--max-items` becomes an in-memory override of `limits.max_items_per_run`, applied as
  `min(N, limit)` (clarification Q4).
- The cost line uses a new `llm_calls_unpriced` stat to show a marked lower bound
  (clarification Q3).

There is no migration and no new dependency.

## Technical Context

**Language/Version**: Python 3.12+

**Primary Dependencies**: all of them already exist.
- Typer for the commands.
- `rich` (`Live`, `Table`, `Console(stderr=True)`) for progress and tables.
- `langgraph` (unchanged topology).
- SQLAlchemy 2.x and Pydantic v2.

**Storage**: the existing `runs`, `jobs`, `items` and `llm_usage` tables. New keys go into
`runs.stats` (`errors`, `errors_omitted`, `llm_calls_unpriced`), which stays at version 1 and
needs no schema change (research R1).

**Testing**: pytest with pytest-asyncio, and Typer's `CliRunner` against a temporary SQLite
database.
- The real pipeline runs with `tests.pipeline_helpers.make_deps`: a `RoutedFakeProvider`,
  `FakePorts` fixture sources and articles, a `RecordingNotifier`, a fixed clock and a no-wait
  sleep.
- The test seam is `job._run_deps`.
- There is no network.

**Target Platform**: a Linux/macOS terminal (interactive) plus non-TTY use (pipes, cron, CI).

**Project Type**: CLI tool / pipeline library (`src/invio`).

**Performance Goals**: the CLI adds negligible overhead. Observer events are O(1) each, and
there are about 10 stage events and 1 event per item. `run list` is one query (bounded by
`--limit`, default 20). `run show` is two queries.

**Constraints**:
- The CLI must not import `invio.db` (enforced by `tests/test_cli_layering.py`).
- `invio.graph` must not import `cli`, `notify` or `scheduling`.
- Results go to stdout and diagnostics to stderr.
- There must be no secrets or document text in stored errors (R3).
- The observer must never fail a run.
- SQLite has a single writer, so `record_errors` uses its own short transaction.

**Scale/Scope**: at most `max_items_per_run` (default 100) items per run. At most 100 stored
errors per run. History is paged with `--limit`.

No NEEDS CLARIFICATION remain. Research R1–R11 resolve all technical choices.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-checked after Phase 1 design.*

| Principle | Check | Status |
|---|---|---|
| I. Strict contracts at boundaries | CLI options are validated before any run (`--max-items >= 1`, `--limit >= 1`). Stored errors have one definition (`StoredError` in `invio.graph.errors`), and the read side parses it leniently. The stats layout stays versioned (`version: 1`, additive keys, documented in the contract delta). | PASS |
| II. CLI-first operation | New `job run` command in the existing group. New `run` group in `src/invio/cli/commands/run.py`, auto-discovered with no edits elsewhere. Results go to stdout, progress and errors to stderr. Exit codes are non-zero on failure. | **PASS with one justified deviation**: `job run` exits **1** (not 2) for a stored-config error, so that 2 can mean `partial` (clarification Q2; see Complexity Tracking) |
| III. Test-covered behaviour | Every acceptance scenario maps to a test (quickstart §2), including the rejection paths (unknown, disabled, busy, invalid config, `--max-items 0`, unknown run id). The tests use `FakeProvider` and fixtures, with no network and no real SMTP. They are deterministic thanks to the fixed clock and the ordered error list. | PASS |
| IV. Quality gates mirror CI | ruff, ruff format, mypy strict and pytest. The observer is typed with a `Protocol`, and no `Any` is added except for the stats mapping, which is already `dict[str, Any]`. | PASS |
| V. Secrets stay secret, runs stay observable | Stored and printed error messages reuse the #19/#21 sanitizers (`failure_message`, `item_failure_message`, `sanitized_error`). Source URLs are never stored, only the source key. The DB error message is the type name only, as before. The run context is unchanged (`run_job` already wraps the run). | PASS |
| Layering | `cli → pipeline/services → graph → db`. The CLI imports `invio.pipeline.run` and `invio.services.runs`, never `invio.db`. | PASS |
| Simplicity | No new table or dependency. The progress port is a single-method Protocol. History records reuse the `JobRecord` pattern. | PASS |
| Workflow | Branch `gh-issue-22`. The README sections "Running a job" and "Managing jobs" are updated with the new commands (user-facing change). | PASS (task) |

**Post-design re-check** (after data-model and contracts): the gates are unchanged. The design
added no table, no dependency and no CLI-to-db import. The only deviation is still the exit
code, which is justified below.

## Project Structure

### Documentation (this feature)

```text
specs/015-gh-issue-22/
├── plan.md              # This file
├── research.md          # Phase 0: R1–R11
├── data-model.md        # Phase 1: stats keys, scope/progress types, read model
├── quickstart.md        # Phase 1: validation scenarios
├── contracts/
│   ├── cli-commands.md              # job run / run list / run show: options, output, exit codes
│   └── pipeline-and-stats-delta.md  # run_job(_by_name), RunObserver, record_errors, stats keys
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
src/invio/
├── cli/
│   ├── commands/
│   │   ├── job.py            # + `run` command, `_run_deps()` seam, job-run error mapping
│   │   └── run.py            # NEW: `run list`, `run show`
│   ├── run_output.py         # NEW: pure formatters (stats table, usage/cost line, error list,
│   │                         #      durations) shared by `job run` and `run show`
│   └── progress.py           # NEW: RunProgressView (rich Live on TTY, plain lines otherwise)
├── graph/
│   ├── ports.py              # + ProgressCounts, ProgressEvent, RunObserver
│   ├── scope.py              # + max_items, observer, progress, item_refs, ItemRef
│   ├── errors.py             # NEW: StoredError, stored_errors(), MAX_STORED_ERRORS
│   ├── stages.py             # + emit(); load_job applies max_items; deduplicate fills
│   │                         #   item_refs; finalize writes stored errors
│   ├── build.py              # + item progress events in process_item; new_scope args
│   └── nodes/persist.py      # + llm_calls_unpriced; _sanitized_error → sanitized_error
├── db/repositories.py        # + RunRepository.record_errors, list_recent
├── pipeline/run.py           # + max_items, observer; run_job_by_name
└── services/runs.py          # NEW: RunService, RunSummary, RunDetail, RunErrorView,
                              #      RunNotFoundError

tests/
├── test_cli_job_run.py       # NEW: dry run / real run / exit codes / max-items / verbose /
│                             #      clean stdout / refusals (FakeProvider + fixtures)
├── test_cli_run.py           # NEW: run list / run show (errors, legacy, empty, unknown id)
├── test_run_errors.py        # NEW: stored_errors (pure) + record_errors + finalize writes
├── test_run_progress.py      # NEW: observer events order/counts, raising observer, max_items
├── test_run_service.py       # NEW: RunService list/get, filters, lenient parsing, timezone
└── test_run_output.py        # NEW: cost/usage formatting rules (R10), table rows
```

**Structure Decision**: a single project, following the existing `src/invio` layout. The CLI
formatting is split into `run_output.py` (pure, unit-tested) and `progress.py` (rendering), so
`commands/job.py` stays thin and `run show` reuses the same table and cost code.

## Phase 0: Research

See [research.md](research.md). Decisions:
- R1: errors go in `runs.stats["errors"]`, with no migration.
- R2: written by `finalize`.
- R3: sanitized messages, with title and URL captured at deduplication.
- R4: `max_items` as an in-memory `min` override.
- R5: a `RunObserver` port.
- R6: rich `Live` on a TTY stderr, plain lines otherwise, and raw Markdown on stdout.
- R7: the exit-code and error mapping.
- R8: `run_job_by_name`.
- R9: the `RunService` read model.
- R10: cost formatting with `llm_calls_unpriced`.
- R11: the test approach.

## Phase 1: Design & Contracts

- [data-model.md](data-model.md): the stats keys, `StoredError`, the scope additions, the
  progress types and the history records.
- [contracts/cli-commands.md](contracts/cli-commands.md): the user-facing CLI contract.
- [contracts/pipeline-and-stats-delta.md](contracts/pipeline-and-stats-delta.md): the internal
  API and the stats delta.
- [quickstart.md](quickstart.md): 18 validation scenarios mapped to the acceptance criteria.

### Requirement → design trace

| Spec | Design |
|---|---|
| FR-001/002 | `job run` → `run_job_by_name` → the unchanged #21 graph |
| FR-003 | existing dry-run semantics (#21); asserted via `RecordingNotifier` and `next_run_at` |
| FR-004/006 | cli-commands § Output 1 |
| FR-005 | cli-commands § Output 2 (stats table rows) |
| FR-007/008 | R5 observer + R6 renderers |
| FR-009 | R10 `format_cost`, `llm_calls_unpriced` |
| FR-010 | R4 `max_items` + CLI note |
| FR-011/012 | stats rows for notifications; `--verbose` item lines and digest |
| FR-013/014 | R7 exit-code table; pre-check via `JobService.get_by_name` |
| FR-015/016 | `run list`, `RunService.list(limit=20)` |
| FR-017/018/018a | `run show`, `stats.errors` written by `finalize` (R1–R3), `RunNotFoundError` |
| FR-019 | read-only service; exit 0 when there are no runs |
| FR-020 | R11 test approach |

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|---|---|---|
| `invio job run` exits **1** for an invalid stored job config, whereas constitution II says configuration errors exit 2 | Exit code 2 must mean `partial` for `job run` (issue #22, clarification Q2). Otherwise a script cannot tell "digest usable, some items failed" from "nothing ran" | Keeping 2 for config errors makes the run outcome ambiguous. A distinct code such as 3 was offered as an option in clarification Q2 and not chosen. The exception is scoped to `job run` and stated in its help text; every other command keeps the convention |
| Click parse errors (unknown option, non-integer value) still exit 2 in `job run` | They come from Click before the command body runs | Overriding Click's `main`/`standalone_mode` for one command adds framework-level code that no other command uses. These errors happen before any run and print a clear usage message |

Reviewed in `/speckit-analyze` (finding C1, 2026-10-07): both rows stay as recorded deviations,
which the constitution's Governance section permits. If command-specific exit codes should
become the general rule, amend constitution II in a separate PR.
