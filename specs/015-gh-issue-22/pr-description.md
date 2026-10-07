# `invio job run` with dry-run output, exit codes and run history (#22)

Closes #22.

## What this adds

- `invio job run <name> [--dry-run] [--max-items N] [--verbose|-v]`: runs a job once through
  `run_job_by_name`. Progress (live panel on a terminal, plain `progress:` lines otherwise) goes
  to stderr; the digest (dry run or `--verbose`), the statistics table and the
  `Tokens: ... cost ...` line go to stdout.
- Exit codes: 0 succeeded, 1 failed or could not start, 2 partial. Every refusal (unknown,
  disabled or busy job, invalid stored config, missing settings, database error, Ctrl-C,
  `--max-items < 1`) is one stderr line and creates no run row.
- `invio run list [--job NAME] [--limit N]` and `invio run show <id>`: run history. `show` lists
  every stored error with stage, sanitized message and the item's title and URL.
- `runs.stats` (still version 1) gains additive keys: `llm_calls_unpriced`, `processed`,
  `errors` (written by `finalize`, at most 100 entries) and `errors_omitted`.
- Pipeline: `run_job(..., max_items, observer)`, `run_job_by_name`, and `RunResult.error`,
  `started_at`, `finished_at`. `invio.graph.ports` gets `RunObserver`, `ProgressEvent` and
  `ProgressSnapshot` (re-exported from `invio.pipeline`).
- `RunService` (`invio.services.runs`) is the read model; `RunRepository` gets `record_errors`
  and `list_recent`.

## No new runtime dependency

Only `rich` (already used by `job list`) and the standard library. `uv.lock` is unchanged.

## Deviations

- **Constitution II, exit codes**: the issue asks for 0/1/2 as success/failure/partial. For
  `job run` a configuration error (invalid stored job config, missing setting) therefore exits
  **1**, not 2 as in the read-only commands, so a script sees one "could not start" code. Click
  usage errors (an unknown option, `--max-items abc`) still exit 2 before anything runs.
  `mapped_errors(config_exit=...)` in `invio.cli.errors` is the one shared mapper.
- `stats["processed"]` counts items with a relevance **or** summary outcome (an item failed at
  `summarize_item` has only the latter).
- `ProgressEvent.counts` is a frozen `ProgressSnapshot`, not the mutable `ProgressCounts`.
  `emit` builds the snapshot inside its guard, catches `Exception` only, and drops the observer
  after its first failure.
- Item titles are cleaned of control characters and ANSI escapes, and stored/printed URLs have no
  query string or fragment (`ItemRef`, `invio.textsafe`).
- `RunRepository.list_recent` returns `(run, job name, job config)` so `run list` formats times
  in each job's zone with one query; `RunSummary` carries `timezone`.
- `--max-items` in baseline mode still caps the items overall (deduplication applies
  `max_items_per_run` after the per-source baseline cut), so no extra rule is needed.
