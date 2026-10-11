# Quickstart: Validate the End-to-End Suite (gh-issue-26)

These steps show the feature is done. Scenario details are in
[contracts/e2e-harness.md](contracts/e2e-harness.md), and the CI wiring is in
[contracts/ci-workflows.md](contracts/ci-workflows.md).

## Prerequisites

- `uv sync --locked`. `aiosmtpd` is already a dev dependency, and nothing new is needed.
- No provider API keys and no SMTP credentials. Unset them to prove it:
  `env -u OPENAI_API_KEY -u ANTHROPIC_API_KEY …` (the suite also strips them).
- Optional: Docker, for the local MariaDB run.

## 1. Run the suite on SQLite (AC: all four scenarios pass; under 60 s)

```bash
time uv run pytest -m e2e -v
```

Expected:
- `test_e2e_first_run_delivers_digest_mail_and_archive` (A), `…rerun_without_new_articles…`
  (B), `…new_article_is_the_only_item_processed` (C),
  `…one_failing_item_gives_partial_run_and_still_mails` (D),
  `test_e2e_network_guard_blocks_external_hosts` and
  `test_e2e_installed_command_runs_scenario_a`, all passed, none skipped.
- Wall time under 60 s (expected ≈ 15–25 s).

## 2. Prove there is no internet (AC: no internet access)

Linux (CI-like):

```bash
unshare -rn uv run pytest -m e2e      # a network namespace with only loopback
```

macOS: turn Wi-Fi off and run the same command. In both cases all tests pass.

## 3. Check that the push MariaDB job is unchanged (FR-017)

```bash
uv run pytest -m db --collect-only -q | grep -c test_e2e    # expected: 0
```

## 4. Run on MariaDB locally (AC: nightly MariaDB job is green)

```bash
docker run -d --rm --name invio-mariadb -p 3306:3306 \
  -e MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1 -e MARIADB_DATABASE=invio_test mariadb:11.4
INVIO_TEST_DATABASE_URL='mysql+pymysql://root@127.0.0.1:3306/invio_test?charset=utf8mb4' \
INVIO_REQUIRE_TEST_DATABASE=1 uv run pytest -m "db or e2e" -rs
docker stop invio-mariadb
```

Expected: everything passes.

Negative check: `INVIO_REQUIRE_TEST_DATABASE=1 uv run pytest -m e2e` without a URL must fail
immediately.

## 5. Check determinism (SC-005)

```bash
for i in $(seq 20); do uv run pytest -m e2e -q -p no:cacheprovider || break; done
```

Expected: 20 passing runs.

## 6. Mutation spot checks (SC-006)

Apply each mutation from the table in `contracts/e2e-harness.md` locally (do not commit it), run
`uv run pytest -m e2e`, and confirm that the listed scenario fails. Revert afterwards.

## 7. CI

- Push the branch. In the `checks` job, both `Tests (pytest)` and
  `E2E suite (SQLite, 60 s budget)` are green.
- Start **Nightly** with `workflow_dispatch` (Actions → Nightly → Run workflow). The
  `mariadb-nightly` job is green.
- Full gate locally: `uv run ruff check && uv run ruff format --check && uv run mypy src &&
  uv run pytest`.
