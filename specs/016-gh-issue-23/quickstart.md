# Quickstart: validating `invio run-due`

**Contracts**: [cli-run-due.md](contracts/cli-run-due.md),
[pipeline-run-due.md](contracts/pipeline-run-due.md) · **Data model**: [data-model.md](data-model.md)

## Prerequisites

- `uv sync --locked`
- For the concurrency acceptance test: a MariaDB test database, with
  `INVIO_TEST_DATABASE_URL=mysql+pymysql://…` exported. Without it the MariaDB-only tests are
  skipped and everything else runs on in-memory SQLite.

## 1. Automated checks (the CI gates)

```bash
uv run ruff check && uv run ruff format --check
uv run mypy src
uv run pytest tests/test_backoff.py tests/test_db_job_lock.py tests/test_db_due.py \
              tests/test_run_due.py tests/test_cli_run_due.py tests/test_healthcheck.py \
              tests/test_pipeline_failures.py tests/test_pipeline_layering.py
INVIO_TEST_DATABASE_URL=mysql+pymysql://… uv run pytest -m db tests/test_run_due_concurrency.py
uv run pytest            # full suite
```

What the targeted tests prove (one or more tests per acceptance scenario):

| Scenario | Expected |
|---|---|
| Nothing due (SC-007) | one SELECT on `jobs`, no write, no run. With the monitor unreachable, the ping gives up after 3 s, so the invocation still ends in < 5 s |
| Two due jobs + one future + one disabled (US1) | exactly the two due jobs run, the oldest first; the others are untouched |
| `--limit 2` with five due jobs | the two oldest run; three stay due |
| Job pre-claimed with a valid lock (US2) | reported `busy`; others still run; lock unchanged |
| Job finished by another run between selection and claim (R1) | `skipped no longer due`; no second run row |
| Two threads × `run_due` over 10 jobs, 20 rounds, MariaDB (SC-001) | each job has exactly one run per round |
| Lock expired one second ago (US3) | job reclaimed and run; lock released afterwards |
| Daily job three days overdue, two invocations (US4) | one run; `next_run_at` in the future after the first |
| Failing provider: three consecutive failures (US5) | `next_run_at` = finish + 1 h, + 2 h, + 4 h (capped by the regular slot) |
| Failure, then success | next run back on the regular slot; streak reset |
| Manual `invio job run` failure / dry run (Q2) | manual failure uses the retry time; dry run changes nothing |
| Health-check recorder (US6) | success → `GET <url>`; failed run or abort → `GET <url>/fail`; unreachable → exit code unchanged; unset → no request |
| `--parallel 2` with four jobs (US7) | never more than two overlapping runs; each job once |

## 2. Manual end-to-end run (local SQLite)

```bash
export INVIO_DATABASE_URL=sqlite:///./invio-dev.db
export INVIO_HEALTHCHECK_URL=http://127.0.0.1:8099/ping/demo
uv run invio db upgrade
uv run invio job create --from-file job.example.yaml --name demo
python -m http.server 8099 &            # stand-in monitor; check its access log
```

1. **Nothing due yet**: `uv run invio run-due` prints `nothing due` and exits 0. The monitor log
   shows `GET /ping/demo`.
2. **Make the job due** by setting its next run time into the past, e.g.
   `sqlite3 invio-dev.db "update jobs set next_run_at='2026-01-01 00:00:00' where name='demo'"`.
   Then run `uv run invio run-due`. It prints one `ran demo run N …` line, a summary line and
   exit code 0 or 1 depending on provider access. `invio job show demo` shows a future next run
   time.
3. **Overlap**: make the job due again and start `uv run invio run-due &` twice in a row. One
   invocation runs it, the other reports `busy` or `skipped no longer due`. `invio run list --job
   demo` shows one new run.
4. **Stale lock**: run
   `update jobs set locked_until='2026-01-01 00:00:00', next_run_at='2026-01-01 00:00:00'`.
   `invio run-due` reclaims the lock and runs the job.
5. **Failure retry**: point the job at an unreachable provider (or unset its API key) and make it
   due. The run fails, the exit code is 1, the monitor log shows `GET /ping/demo/fail`, and the
   next run time shown is about one hour later and marked `(retry)`.
6. **Bad options**: `uv run invio run-due --parallel 0` exits 2 with `Error: --parallel must be at
   least 1` and sends no ping.

## 3. systemd (production smoke test)

Install the example units from the README, run `systemctl start invio-run-due.service`, then
check `journalctl -u invio-run-due` for the report and JSON log lines, and check the monitoring
service for the ping.

## 4. Walk-through record (T046)

Steps 1, 2, 5 and 6 of section 2 were run on a local SQLite database with a stand-in monitor.
Observed deviations from the text above:

- The stand-in monitor (`python -m http.server`) answers 404, so each ping is also logged as
  `healthcheck.failed` with `status 404` on stderr; the exit code is unchanged, as specified.
- With the example job and no provider key the run fails: `ran demo run 1 failed next ... (retry)`,
  exit 1, and the monitor log shows `GET /ping/demo/fail`.
- Steps 3 and 4 (overlap, stale lock) are covered by automated tests, including two concurrent
  invocations on MariaDB, not by the manual walk-through.
