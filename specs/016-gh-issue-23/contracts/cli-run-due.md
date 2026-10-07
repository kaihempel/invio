# Contract: `invio run-due`

**Spec**: FR-001, FR-007, FR-012–FR-015 · **Research**: R5–R8

## Synopsis

```text
invio run-due [--limit N] [--parallel N]
```

| Option | Type | Default | Rule |
|---|---|---|---|
| `--limit N` | int ≥ 1 | no limit | at most N runnable due jobs are started (busy jobs do not count) |
| `--parallel N` | int ≥ 1 | 1 | at most N jobs run at the same time; 1 = strictly sequential in due order |

Invalid values (`0`, negative, non-numeric) produce a one-line message on stderr that names the
option (e.g. `Error: --parallel must be at least 1`), exit with **2**, run nothing and send no
health-check signal.

## Behaviour

1. Select the due set once: enabled jobs with `next_run_at <= now (UTC)`, ordered by
   `next_run_at`, then `id`.
2. Report jobs with an unexpired lock as `busy`. Take the first `--limit` of the others as
   candidates.
3. For each candidate (sequentially, or up to `--parallel` at once): claim the job atomically
   (free or expired lock, still due), run it, and release it with the new `next_run_at`.
   A failure of one job never stops the others.
4. Print the report (below), send the health-check signal, and exit.

## stdout

One line per due job in due order, then one summary line. Job names are passed through
`strip_control`. Times are shown in the job's schedule time zone, like `invio job run`.

```text
ran      daily-ai        run 412  succeeded  next 2026-10-08 07:00 CEST
ran      weekly-rust     run 413  failed     next 2026-10-07 15:12 CEST (retry)
busy     papers          locked until 2026-10-07 16:02 CEST
skipped  old-feed        no longer due
error    news-de         OperationalError
due 5 · ran 2 (1 succeeded, 0 partial, 1 failed) · busy 1 · skipped 1 · error 1
```

An `error` line names only the exception class: the job raised outside a run result, so there
is no run id, and the message may hold secrets. `· error N` is shown only when N > 0. When
nothing is due, the only output is `nothing due`.

`(retry)` marks a next run time that came from the retry delay rather than the regular slot.
Per-run details stay in `invio run show <id>`.

## stderr

Structured JSON log lines (`run_due.started`, `run_due.finished`, the per-run `run.*` events
inside each run context, `healthcheck.failed`). No progress display: the command is meant for
unattended use.

## Exit codes

| Code | When |
|---|---|
| 0 | no run failed and the invocation completed. Includes nothing due, all busy, partial runs |
| 1 | at least one run failed, a job raised an unexpected error, or the invocation aborted (database error, unexpected exception, Ctrl-C) |
| 2 | invalid option value or usage; configuration error (invalid settings, missing `INVIO_DATABASE_URL`) |

## Health-check signal

Only when `INVIO_HEALTHCHECK_URL` is set and readable:

| Outcome | Request |
|---|---|
| exit 0 | `GET <url>` |
| exit 1 | `GET <url>/fail` (trailing `/` of `<url>` is removed first) |
| exit 2, configuration error after the settings were loaded | `GET <url>/fail` |
| exit 2, invalid option / usage | none |

- Exactly one request per invocation, sent after all runs have finished.
- Timeout 3 s, no redirects, no retry. The short timeout keeps an invocation with nothing due
  under the 5 s of SC-007, even when the monitor is unreachable.
- A failed request logs `healthcheck.failed` with only the error class or HTTP status. It never
  changes the exit code or the stdout report. The URL is never logged or printed.

## Examples

```text
$ invio run-due
nothing due
$ echo $?
0

$ invio run-due --limit 0
Error: --limit must be at least 1
$ echo $?
2
```

## systemd (documentation only)

The README documents a `invio-run-due.service` (`Type=oneshot`, `ExecStart=invio run-due`) and an
`invio-run-due.timer` (`OnCalendar=*:0/5`, `Persistent=true`). The unit files are examples, not
shipped artifacts.
