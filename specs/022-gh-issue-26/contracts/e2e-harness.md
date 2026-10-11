# Contract: E2E Harness and Scenarios

This contract defines what the suite promises: how it is selected and run, what it may touch,
and exactly what each scenario asserts. Entity names refer to [data-model.md](../data-model.md).

## Selection and execution

| Item | Contract |
|---|---|
| Marker | `e2e` (registered in `pyproject.toml`); every e2e module sets `pytestmark = pytest.mark.e2e`; no e2e test carries `db` |
| Run locally | `uv run pytest -m e2e` |
| Database switch | none set → SQLite file DB per test; `INVIO_TEST_DATABASE_URL` set → that MariaDB |
| Strict DB mode | `INVIO_REQUIRE_TEST_DATABASE=1` with no MariaDB URL → `pytest_sessionstart` in `tests/conftest.py` exits the session (exit code 1) before collection, so no test (`db` or `e2e`) runs on SQLite |
| Budget | whole `-m e2e` selection ≤ 60 s on SQLite (CI enforces this with `timeout 60`) |
| Network | in-process tests: any non-loopback connect or DNS lookup raises `ExternalNetworkError` (an `AssertionError`) |
| Secrets | no `*_API_KEY`, no SMTP user/password in the test or subprocess environment |
| Flakiness | no `sleep`-based synchronisation; the only poll is a bounded wait (≤ 5 s deadline) on the mail count in the separate-process test |
| Skips | no e2e test may be `skip`/`xfail` (FR-018) |

## CLI surface used (unchanged product contract)

| Invocation | Expected |
|---|---|
| `invio db upgrade` | exit 0 (SQLite only; MariaDB is migrated by `_server_engine`) |
| `invio job create --from-file <yaml> --name <job>` | exit 0; stdout `created <job> …` |
| `invio run-due` (job due) | exit 0 for `succeeded` and `partial`; stdout has one `ran  <job>  run <id>  <status> …` line and the summary line `due 1, ran 1 (…)` |
| `invio run-due` (nothing due) | exit 0; stdout `nothing due` |

In-process seam: only `invio.cli.commands.run_due._run_due` is replaced. It calls
`run_due(limit, parallel, deps=<default_deps(settings, allow_networks=LOOPBACK)> with clock=FakeClock)`
(research R1).

## Scenario assertions

Initial feed: A1, A2, A3 relevant; X1 off-topic. Recipients R1, R2.

### Scenario A — first run (`test_e2e_first_run_delivers_digest_mail_and_archive`)

After `db upgrade` → `job create` → `make_due()` → `run-due`:

1. every CLI exit code is 0; `run-due` stdout names the job with status `succeeded`
2. exactly 1 run, status `succeeded`, `errors == []`
3. items: 4 stored; A1–A3 in the digest; X1 not in the digest
4. exactly 1 digest; its item ids are exactly A1–A3
5. mail sink: exactly 1 mail to R1 and 1 to R2; each body contains the titles A1–A3 and not X1;
   the footer contains `https://archive.example.test/digests/<job-slug>/<page>.html`
6. archive: exactly 1 page under `<archive>/<job-slug>/`; `<job-slug>/index.html` and
   `index.html` both link to it; no archive file references external resources
7. job: `locked_until` is NULL and `next_run_at == expected_next_run(env)` (see "Schedule
   check" below)

### Scenario B — rerun, unchanged feed (`test_e2e_rerun_without_new_articles_sends_nothing`)

After Scenario A's steps (inside the same test), take a snapshot S, then `clock.advance(1 day)`
→ `make_due()` → `run-due`:

1. exit 0; exactly 2 runs; the new run is `succeeded`
2. item count, digest count and notification count are equal to S
3. the provider got no new `relevance` or `summarize` call (per-title counts equal to S)
4. mail sink count equal to S; archive page count equal to S
5. job not locked, `next_run_at == expected_next_run(env)`; then `run-due` again **without** `make_due()` →
   exit 0, stdout `nothing due`, still exactly 2 runs

### Scenario C — one new article (`test_e2e_new_article_is_the_only_item_processed`)

After Scenario A's steps, `feed.add(A4, relevant)` → `clock.advance(1 day)` → `make_due()` →
`run-due`:

1. exit 0; the new run is `succeeded`
2. exactly 5 items; exactly one new item (A4)
3. provider calls since A: `relevance` and `summarize` only for A4 (each exactly once)
4. exactly 2 digests; the new one contains only A4
5. exactly 1 new mail per recipient; its body contains A4 and none of A1–A3
6. 2 archive pages; both indexes list both pages, the newest first

### Scenario D — one provider failure (`test_e2e_one_failing_item_gives_partial_run_and_still_mails`)

Fresh environment; the provider raises `RuntimeError` at `relevance` for A2. Then
`db upgrade` → `job create` → `make_due()` → `run-due`:

1. exit 0 (a partial run is not a failure); stdout shows status `partial`
2. exactly 1 run, status `partial`; its errors contain
   `{stage: "score_relevance", error_class: "RuntimeError", item_id: <A2>}`
3. A2 has item status `failed`; A1 and A3 are in the digest
4. exactly 1 mail per recipient containing A1 and A3; 1 archive page
5. job not locked, `next_run_at == expected_next_run(env)`: the regular slot, not the retry
   time `clock() + retry_delay(1)` (a partial run is not a failed run)

### Guard self-test (`test_e2e_network_guard_blocks_external_hosts`)

With the guard active, connecting to `93.184.216.34:80` and resolving `example.com` both raise
`ExternalNetworkError`; connecting to the loopback feed server works.

### Separate-process smoke test (`test_e2e_installed_command_runs_scenario_a`)

Uses the `FakeOllamaServer`, the bootstrap `PYTHONPATH` and a clean environment (research R8),
and runs `<venv>/bin/invio` for `db upgrade` (SQLite only), `job create` and `run-due`:

1. each process exits 0 within 20 s (subprocess timeout)
2. 1 run `succeeded` and 4 stored items for the job; 1 digest with exactly A1–A3 (X1 filtered)
3. 1 mail per recipient that names A1–A3 and not X1; 1 archive page; `next_run_at` in the future
4. the Ollama stub received ≥ 1 request per purpose; the feed server got `/feed.xml`
5. the subprocess environment contains no `*_API_KEY` and no `INVIO_SMTP_USER` /
   `INVIO_SMTP_PASSWORD` (asserted on the env the test builds)

A second test (`test_e2e_process_guard_blocks_external_hosts`) starts a Python process with the
same bootstrap and `INVIO_E2E_BLOCK_EXTERNAL_NETWORK=1`; a lookup of `example.com` must exit
non-zero with the refusal on stderr, so the subprocess guard is proven active.

## Schedule check (scenarios A, B, D)

`expected_next_run(env)` = `invio.scheduling.next_run.compute_next_run(<job schedule>, clock())`.
This is the same computation `release_lock` (`src/invio/graph/stages.py`) does with the finish time
`deps.clock()`, and the fake clock does not move during a run. The stored value is compared as
UTC (SQLite and MariaDB return naive datetimes). With the fake clock at 12:00 UTC and a daily
07:00 UTC schedule, the expected value is 07:00 UTC the next day. A failed run would instead get
13:00 UTC the same day (`clock() + retry_delay(1)`), so the check tells the two apart.

## Mutation checks (SC-006)

These are run manually when implementing; they are not committed. Each one must make at least
one scenario fail:

| Mutation | Must fail |
|---|---|
| `deliver_digest` sends even when the digest has no new items | B |
| archive disabled in `deliver_digest` | A |
| a single-item error aborts the run | D |
| de-duplication by `url_hash` removed | B, C |
| `run-due` ignores `next_run_at` | B (step 5) |
| `release_lock` applies the retry delay to `partial` runs | D (item 5) |
