# Contract: CLI commands `invio job run`, `invio run list`, `invio run show`

Every command needs `INVIO_DATABASE_URL`. Results go to **stdout**. Progress, notes and errors go
to **stderr**. Times are shown as `YYYY-MM-DD HH:MM <TZ abbr>` in the job's timezone, falling
back to UTC.

---

## `invio job run <name> [--dry-run] [--max-items N] [--verbose]`

Lives in `src/invio/cli/commands/job.py`, which is the existing `job` group.

| Option | Type | Default | Meaning |
|---|---|---|---|
| `name` | str (argument) | — | job name |
| `--dry-run` | flag | off | send nothing, keep only the run row, print the digest |
| `--max-items N` | int | job limit | cap on the items processed in this run; effective cap = `min(N, limits.max_items_per_run)` |
| `--verbose` / `-v` | flag | off | per-item outcome lines on stderr; digest printed for real runs too |

### Sequence

1. `--max-items < 1` → `Error: --max-items must be at least 1`, exit 1.
2. `JobService.get_by_name(name)`. This step fails without creating a run row in these cases:
   - not found → `Error: job '<name>' not found`, exit 1
   - stored config invalid → the existing config error text, exit **1**
   - disabled → `Error: job '<name>' is disabled; enable it with 'invio job enable <name>'`,
     exit 1
3. If `N > limit`: stderr note `note: --max-items <N> exceeds the job limit <limit>; using <limit>`.
4. `asyncio.run(run_job_by_name(name, dry_run=…, max_items=…, observer=view, deps=<_run_deps()>))`.
5. Progress on stderr while the run is going (see the Progress section).
6. Output on stdout (see the Output section).
7. Exit code from `RunResult.status`.

### Progress (stderr)

- **TTY**: a live panel with the current stage, `found N · new N · after filter N · relevant N · failed N`,
  and a `processed i/n` bar during item processing.
- **Non-TTY**: one line per stage event, for example:
  ```
  progress: deduplicate found=12 new=5
  progress: keyword_prefilter after_keyword_filter=4
  progress: items processed=4/4 relevant=2 failed=1
  progress: persist
  ```
- **`--verbose`**: additionally one line per item, `item: relevant  <title>`,
  `item: irrelevant  <title>` or `item: failed  <title>  <message>`.

### Output (stdout)

The blocks are separated by one blank line, in this order:

1. **Digest** (dry run, or `--verbose`): the digest title as `# <title>` followed by the body,
   verbatim as stored and mailed except that control characters and ANSI escapes are stripped (newlines and tabs are kept), with no rendering. When there is no digest:
   `No digest: nothing relevant was found.`
2. **Stats table** (always), two columns `Metric | Value` with these rows, in order: `Run`
   (id), `Status` (plus ` (dry run)`), `Found`, `New`, `After keyword filter`, `Processed`
   (`stats.processed`: items rated or failed; legacy runs fall back to
   `after_keyword_filter − skipped_budget`), `Relevant`, `Summarized`, `Failed`, `Skipped (budget)`
   (only if > 0), `Sources failed` (`n/m`, only if > 0), `Duration` (`12.3 s`). Real runs add
   `Notifications sent`, `Notifications failed`.
3. **Usage line** (always):
   `Tokens: in 48,211 · out 5,120 · total 53,331 · cost $0.0103`. The cost follows research
   R10 (`$0.00`, an exact sum, `≥ $x (n calls without price)` or `unknown`).

When stdout is not a terminal, the table uses the console width `_PIPE_WIDTH` (1000), the
same as `invio job list`, so rows are never wrapped.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | run `succeeded` |
| 1 | run `failed`, or the run could not start (unknown, disabled or busy job, invalid stored config, missing or invalid settings including `INVIO_LOG_LEVEL` and `INVIO_ENV_FILE`, database error, `--max-items < 1`, Ctrl-C) |
| 2 | run `partial` (also Click parse errors such as an unknown option, before anything runs) |

**Status messages on stderr**:
- `failed` → `run <id> failed: <RunResult.error (runs.error), else the first error class, else "run failed">`
- `partial` → `run <id> finished partial: <n> error(s); see 'invio run show <id>'`
- busy job → `Error: job '<name>' is running (locked until <time>)`
- Ctrl-C → `interrupted; run recorded as failed`

The help text ends with:
`Exit codes: 0 succeeded, 1 failed or could not start, 2 partial.`

---

## `invio run list [--job NAME] [--limit N]`

A new module, `src/invio/cli/commands/run.py`, which is auto-discovered as the `run` group.

| Option | Default | Rule |
|---|---|---|
| `--job NAME` | all jobs | an unknown job gives `Error: job '<name>' not found`, exit 1 |
| `--limit N` | 20 | `N >= 1`, else `Error: --limit must be at least 1`, exit 1 |

**Output (stdout)**: a table, newest first. Columns: `ID`, `Job`, `Started`, `Duration`,
`Status` (with a ` (dry)` suffix for dry runs), `Found`, `New`, `Relevant`. A missing count is
shown as `—`. A running run shows `—` for duration.

When there are no runs, the output is `No runs.` (or `No runs for job '<name>'.`) and the exit
code is 0.

---

## `invio run show <id>`

**Output (stdout)**:

```
Run 42 · job daily-ai · partial (dry run)
Started   2026-10-07 06:00 CEST   Finished 2026-10-07 06:01 CEST   Duration 63.2 s
Error     —

<stats table: same rows as `job run`, without notification rows>

Tokens: in 48,211 · out 5,120 · total 53,331 · cost ≥ $0.0103 (2 calls without price)

Errors (3)
  score_relevance  LLMInvalidOutputError: the model returned an invalid answer
                   "Some article title" — https://example.org/a
  summarize_item   LLMUnavailableError: call failed (provider fakeco, model fakeco-priced)
                   "Other title" — https://example.org/b
  fetch_sources    FetchError: source failed  (source #2, rss)
```

Rules:
- `Errors` lists every stored error, sorted by stage order and then item id.
  - Item errors show the title and URL.
  - Source errors show `source #<index+1>, <type>`, parsed from the key.
  - When `errors_omitted > 0`, a final line `… and <n> more not stored`.
- No stored list (legacy run): `Errors: item errors are not available for this run`. The run's
  `Error` line still shows `runs.error`.
- An empty list: `Errors: none`.
- A running run: status `running`, finish and duration shown as `—`, and the stats table
  omitted. The exit code is 0.
- An unknown id: `Error: run <id> not found`, exit 1.
- A non-integer id: Click usage error, exit 2.

## Common error mapping (history commands)

The mapping lives in `invio.cli.errors.mapped_errors(config_exit=...)`, shared by the `job`
commands (2), `run list` / `run show` (2) and `job run` (1). It catches named exception types
only. It follows the existing `job` command mapping:
- database error → exit 1
- missing setting / config error → exit 2 (the CLI-wide convention, unchanged for read-only
  commands)
