---

description: "Task list for gh-issue-26: end-to-end integration test suite"
---

# Tasks: End-to-End Integration Test Suite

**Input**: Design documents from `/specs/022-gh-issue-26/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/e2e-harness.md,
contracts/ci-workflows.md, quickstart.md

**Tests**: This feature *is* a test suite. The "implementation" tasks below write test code,
test fixtures and CI configuration. No product code is planned. Product changes happen only
through T030 (FR-018).

**Organization**: Tasks are grouped by user story (spec.md US1–US5 = Scenarios A–D + CI).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: US1 = Scenario A, US2 = Scenario B, US3 = Scenario C, US4 = Scenario D, US5 = CI

## Path Conventions

The flat `tests/` layout is kept (`tests/<topic>_helpers.py` + `tests/test_<topic>_*.py`). New
files:
- `tests/e2e_helpers.py`
- `tests/test_e2e_scenarios.py`
- `tests/test_e2e_process.py`
- `tests/e2e_bootstrap/sitecustomize.py`
- `.github/workflows/nightly.yml`

Changed (small additions only):
- `tests/conftest.py`: new `pytest_sessionstart` hook (T009). The existing fixtures `FakeClock`,
  `patched_providers`, `_server_engine`, `clean_jobs` and `isolated_settings` are reused
  unchanged.
- `tests/db_helpers.py`: new import-time constant for `INVIO_REQUIRE_TEST_DATABASE` (T009).

Reused, unchanged:
- `tests/http_helpers.py` (`LoopbackServer`, `Route`)
- `tests/smtp_helpers.py` (`_serve`, `SmtpServer`, `free_port`)
- `tests/pipeline_helpers.py` (`RoutedFakeProvider`, `relevance_reply`, `summary_reply`,
  `digest_reply`, `article_html`)
- `tests/db_helpers.py` (`TEST_DATABASE_URL`, `uses_sqlite`), apart from the T009 constant

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Set up the branch, register the marker and create the empty modules.

- [X] T001 Switch to the feature branch the constitution requires ("Development Workflow": work
  happens on a branch named `gh-issue-<N>`). The worktree is currently on
  `worktree-track-integration`. Create and switch to `gh-issue-26` (or rename the worktree
  branch to it), and check that the current branch is `gh-issue-26`. Commit the spec artifacts in
  `specs/022-gh-issue-26/` as the first commit.
- [X] T002 Register the pytest marker in `pyproject.toml` `[tool.pytest.ini_options].markers`:
  `"e2e: end-to-end scenarios over local fixtures (SQLite; MariaDB with INVIO_TEST_DATABASE_URL)"`.
  Do NOT add `db` to e2e tests (FR-017). Verify with `uv run pytest --markers | grep e2e`.
- [X] T003 [P] Create `tests/e2e_helpers.py` with a module docstring ("End-to-end fixtures:
  local feed server, SMTP sink, fake provider, network guard and scenario environment (not
  shipped). Contract: specs/022-gh-issue-26/contracts/e2e-harness.md.") and an `__all__` list.
  Follow the style of `tests/run_cli_helpers.py`.
- [X] T004 [P] Create `tests/test_e2e_scenarios.py` and `tests/test_e2e_process.py`, each with a
  module docstring and `pytestmark = pytest.mark.e2e`.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The scenario environment that every story uses (research R1–R7). Everything except
T009 goes in `tests/e2e_helpers.py`, so those tasks are sequential (same file). T009
(`tests/conftest.py`, `tests/db_helpers.py`) can run in parallel with them.

**⚠️ CRITICAL**: No scenario can be written before this phase is done.

- [X] T005 Implement `E2EArticle` (frozen dataclass) in `tests/e2e_helpers.py` with the fields
  `slug: str` ("unique within a feed; URL path `/articles/<slug>.html`"), `title: str` ("unique;
  the fake provider routes by it"), `published_at: datetime` (UTC, "relative to the scenario
  clock"), `relevant: bool = True` ("`True` → score 0.9, `False` → score 0.1 (below
  `min_relevance` 0.6)") and `fail_at: Literal["relevance"] | None = None`. Add
  `initial_articles(now)`, which returns A1, A2, A3 (relevant) and X1 (off-topic), published at
  `now - 1..4 hours`.
- [X] T006 Implement `E2EFeed` in `tests/e2e_helpers.py`, wrapping
  `tests.http_helpers.LoopbackServer`:
  - `articles` list and an `add(article)` method;
  - `_render()` sets `routes["/feed.xml"]` (RSS 2.0, `Content-Type: application/rss+xml`, items
    newest first, `<link>` = `f"{base_url}/articles/{slug}.html"`, `<pubDate>` in RFC 2822) and
    one `routes[f"/articles/{slug}.html"]` per article with `article_html(title)` from
    `tests/pipeline_helpers.py` (`Content-Type: text/html; charset=utf-8`);
  - a static `routes["/robots.txt"]` = `"User-agent: *\nAllow: /\n"`;
  - properties `feed_url` and `requests`.
  Escape titles with `xml.sax.saxutils.escape`.
- [X] T007 Implement `scenario_provider(articles) -> RoutedFakeProvider` in
  `tests/e2e_helpers.py`. It uses a relevance route callable `title -> step`: `RuntimeError("boom
  secret")` if the article's `fail_at == "relevance"`, `relevance_reply(0.9)` if relevant,
  otherwise `relevance_reply(0.1)`. Look the title up in a dict that the test may extend (needed
  for Scenario C's added article), and use the default summarize/synthesize replies. Unknown
  titles raise `AssertionError`.
- [X] T008 Implement the network guard in `tests/e2e_helpers.py`:
  - `class ExternalNetworkError(AssertionError)`;
  - fixture `no_external_network(monkeypatch)`: wrap `socket.socket.connect`,
    `socket.socket.connect_ex` and `socket.getaddrinfo`. Allow only hosts `127.0.0.0/8`, `::1`,
    `localhost` and AF_UNIX; anything else raises `ExternalNetworkError(f"external network
    access blocked: {host}")` (research R7);
  - in the same fixture, `monkeypatch.delenv` every variable ending in `_API_KEY` and
    `INVIO_SMTP_USER` / `INVIO_SMTP_PASSWORD` (`raising=False`).
- [X] T009 [P] Add a `pytest_sessionstart(session)` hook to `tests/conftest.py`, next to the
  existing `INVIO_REQUIRE_DEPLOY_TOOLS` handling. Read `INVIO_REQUIRE_TEST_DATABASE` at import
  time in `tests/db_helpers.py`, as `TEST_DATABASE_URL` is, because `isolated_settings` strips
  `INVIO_*` per test. If it equals `"1"` and `uses_sqlite()` is true, call
  `pytest.exit("INVIO_REQUIRE_TEST_DATABASE=1 but INVIO_TEST_DATABASE_URL is not a MariaDB URL",
  returncode=1)`. This runs before collection, so no `db` or `e2e` test runs on SQLite in the
  nightly job (contracts/ci-workflows.md, FR-016). Do not add a fixture for this.
- [X] T010 Implement `write_job_yaml(path, *, feed_url, recipients) -> Path` in
  `tests/e2e_helpers.py`, following data-model.md "Job YAML template":
  - schedule daily 07:00 `UTC`;
  - `notification.to` = `["reader-1@example.test", "reader-2@example.test"]`;
  - `sources: [{type: rss, url: feed_url}]`;
  - a fixed `search.semantic_description`;
  - `llm: {provider: ollama, models: {fast: llama3.2:3b, smart: qwen2.5:14b}}`;
  - `archive: {enabled: true, base_url: https://archive.example.test/digests}`.
  Dump with `yaml.safe_dump`. Check the key names against `src/invio/config/job.py` and
  `docs/job.example.yaml`.
- [X] T011 Implement `E2EEnv` (dataclass) in `tests/e2e_helpers.py` with the fields from
  data-model.md (`tmp`, `database_url`, `archive_dir`, `job_name`, `job_yaml`, `feed`, `mail`,
  `provider`, `clock`, `assert_engine`, `runner`) and these methods:
  - `cli(*args) -> Result`: `CliRunner().invoke(invio.cli.main.app, list(args), env={"COLUMNS":
    "200"})`.
  - `setup_job()`: on SQLite only, run `cli("db", "upgrade")`; then run `cli("job", "create",
    "--from-file", str(job_yaml), "--name", job_name)`; assert exit 0 for each and include
    `result.output` in the failure message.
  - `make_due()`: through `assert_engine`, set the job's `next_run_at = clock() - timedelta(
    minutes=1)` and `locked_until = None`.
  - `run_due() -> Result`: `cli("run-due")`.
  - `expected_next_run() -> datetime`: `compute_next_run(JobConfig.model_validate(yaml.safe_load(
    job_yaml.read_text())).schedule, clock())` from `invio.scheduling.next_run`. This is the
    regular slot `release_lock` sets (`src/invio/graph/stages.py`). With the fake clock at
    12:00 UTC and the daily 07:00 UTC schedule, it is 07:00 UTC the next day. A failed run would
    get the retry time instead (13:00 UTC the same day).
  - `job_schedule() -> tuple[datetime | None, datetime | None]`: the job's `(next_run_at,
    locked_until)`, normalised to aware UTC (SQLite and MariaDB return naive datetimes).
  - `snapshot() -> Observed`: a frozen dataclass with the runs (status, errors), items (title →
    status, id), digests (item ids), notification count, `len(mail.messages)`, sorted archive
    page paths under `archive_dir/<slug>/` excluding `index.html`, the job's
    `next_run_at`/`locked_until`, and provider `titles_for("relevance")`/`titles_for("summarize")`
    copies.
  Read rows with `sqlalchemy.select` on `invio.db.models` (`Job`, `Run`, `Item`, `Digest`,
  `Notification`), filtered by the job id.
- [X] T012 Implement the fixture `e2e_env(tmp_path, monkeypatch, patched_providers,
  no_external_network, _server_engine, clean_jobs)` in
  `tests/e2e_helpers.py`. Steps:
  1. Start `E2EFeed` (with `initial_articles(clock())`) and the SMTP sink
     (`tests.smtp_helpers._serve()`, no AUTH).
  2. Pick the DB URL: `TEST_DATABASE_URL` if not `uses_sqlite()`, else
     `f"sqlite:///{tmp_path}/e2e.sqlite"`.
  3. Set env via `monkeypatch.setenv`:
     - `INVIO_DATABASE_URL`, `INVIO_ARCHIVE_DIR=<tmp>/archive`;
     - `INVIO_SMTP_HOST=127.0.0.1`, `INVIO_SMTP_PORT`, `INVIO_SMTP_SECURITY=none`,
       `INVIO_SMTP_FROM=invio@example.test`;
     - `INVIO_HTTP_HOST_INTERVAL_SECONDS=0.01`.
     Then call `get_settings.cache_clear()`.
  4. Register the provider: `patched_providers("ollama", provider)`.
  5. Patch `invio.cli.commands.run_due._run_due` with
     `lambda limit, parallel: asyncio.run(_due(limit, parallel))`. `_due` opens
     `default_deps(get_settings(), allow_networks=(IPv4Network("127.0.0.0/8"),))`, applies
     `dataclasses.replace(deps, clock=clock)` and awaits `run_due(limit=limit,
     parallel=parallel, deps=deps)` (research R1).
  6. Set `job_name = f"e2e-{uuid4().hex[:8]}"`.
  7. Teardown: stop the feed, sink and engine.
  Re-export the conftest fixtures it needs by importing `e2e_env` into both test modules (the
  same pattern as `run_cli` in `tests/run_cli_helpers.py`).
- [X] T013 Add `test_e2e_network_guard_blocks_external_hosts` in `tests/test_e2e_scenarios.py`
  (contract "Guard self-test"). With `no_external_network` active:
  - `socket.create_connection(("93.184.216.34", 80), timeout=1)` raises `ExternalNetworkError`;
  - `socket.getaddrinfo("example.com", 80)` raises `ExternalNetworkError`;
  - a GET to a started `LoopbackServer` succeeds.

**Checkpoint**: `uv run pytest -m e2e` collects, and the guard self-test passes.

---

## Phase 3: User Story 1 - First scheduled run delivers digest, mail and archive (Scenario A, Priority: P1) 🎯 MVP

**Goal**: YAML job → `run-due` produces a stored digest, one mail per recipient and an archive
page with indexes. The run is `succeeded`, the lock is released and the next run time lies in
the future.

**Independent Test**: `uv run pytest -m e2e -k first_run`.

- [X] T014 [US1] Add `run_scenario_a(env) -> Result` in `tests/e2e_helpers.py`:
  `env.setup_job()`, `env.make_due()`, `result = env.run_due()`, assert `result.exit_code == 0`,
  return the result. US2 and US3 reuse it.
- [X] T015 [US1] Add assertion helpers in `tests/e2e_helpers.py`:
  - `mail_texts(env, recipient) -> list[str]`: the plain-text parts via
    `tests.smtp_helpers.parts`;
  - `archive_pages(env) -> list[Path]`;
  - `assert_no_external_resources(path)`: fail on `<script src=`, `<link` with
    `href="http`, and `url(http` / `@import` in the HTML (data-model "Observed outcomes").
- [X] T016 [US1] Write `test_e2e_first_run_delivers_digest_mail_and_archive(e2e_env)` in
  `tests/test_e2e_scenarios.py`, implementing contract Scenario A items 1–7 exactly:
  - stdout contains the job name and `succeeded`;
  - 1 run `succeeded` with no errors;
  - 4 items; the digest item ids equal A1–A3;
  - 1 mail each to `reader-1@example.test` and `reader-2@example.test`, each containing A1–A3
    and not X1, with a footer link starting
    `https://archive.example.test/digests/<slug>/` and ending `.html`;
  - 1 archive page, linked from `<archive>/<slug>/index.html` and `<archive>/index.html`;
  - no external resources in the archive files;
  - job `locked_until is None` and `next_run_at == env.expected_next_run()` (exact, contract
    "Schedule check").
  Get the slug via `invio.notify.archive.slugify_job_name`.
- [X] T017 [US1] Create `tests/e2e_bootstrap/sitecustomize.py` (test-only, research R8).
  Import only `invio.pipeline.deps` and replace its attribute `default_deps` with a wrapper that
  defaults `allow_networks` to `(IPv4Network("127.0.0.0/8"),)` when the caller passes none.
  `sitecustomize` runs before any other invio module is imported, so the later
  `from invio.pipeline.deps import default_deps` in `invio.pipeline.run` and
  `invio.cli.commands.job` already gets the wrapper. Do not import or patch those modules here:
  that would slow every subprocess start. Keep it import-safe (no side effects except the patch)
  and add a docstring that says it must never be packaged. T019 proves that it loaded (the feed
  server sees `/feed.xml`).
- [X] T018 [US1] Implement `FakeOllamaServer` in `tests/e2e_helpers.py`: a
  `http.server.ThreadingHTTPServer` on `127.0.0.1:0` that handles `POST /api/chat`.
  - Parse the JSON body and pick the reply: if `format` is a dict whose `properties` contain
    `score` → relevance JSON (score 0.9); if it contains `headline` → summary JSON (same shape as
    `summary_reply`); otherwise (no `format`, or `format == "json"` → decide by the system
    prompt the same way as for the dict case) → the digest text from `digest_reply`.
  - Answer with the non-streaming shape from `tests/fixtures/ollama/chat_ok.json`
    (`message.content`, `prompt_eval_count`, `eval_count`, `done: true`).
  - Record `(purpose, model)` per request in a thread-safe list.
  - Provide `start()`, `stop()` and `base_url`.
- [X] T019 [US1] Write `test_e2e_installed_command_runs_scenario_a(tmp_path, ...)` in
  `tests/test_e2e_process.py` (contract "Separate-process smoke test", FR-019):
  - Start `E2EFeed`, the SMTP sink and `FakeOllamaServer`.
  - Locate the command with `Path(sys.executable).parent / "invio"`, and assert that it exists.
  - Build the env from scratch: `PATH`, `HOME=tmp_path`,
    `PYTHONPATH=<repo>/tests/e2e_bootstrap`, the `INVIO_*` settings as in T012 plus
    `INVIO_OLLAMA_BASE_URL=<stub>` and `INVIO_LOG_LEVEL=WARNING`, and pass through
    `COVERAGE_PROCESS_START` if present. Assert that no key ends in `_API_KEY` and that neither
    `INVIO_SMTP_USER` nor `INVIO_SMTP_PASSWORD` is set.
  - Run `subprocess.run([invio, ...], env=env, capture_output=True, text=True, timeout=20)` for
    `db upgrade` (SQLite only), `job create --from-file … --name …` and `run-due`, after setting
    `next_run_at = now(UTC) - 1 min` through an assertion engine. Assert exit 0 each time
    (include stderr in the failure message).
  - Assert: 1 run `succeeded`, 1 digest, 1 archive page, and 1 mail per recipient (bounded poll:
    deadline 5 s, poll interval via `threading.Event().wait(0.05)`, no `time.sleep`). The stub
    saw relevance, summarize and digest requests, and `/feed.xml` is in `feed.requests` (this
    proves that `sitecustomize` loaded).
  - On MariaDB, request `_server_engine` and `clean_jobs`, and skip `db upgrade`.

**Checkpoint**: Scenario A passes in-process and as a separate process. This is a viable MVP.

---

## Phase 4: User Story 2 - Repeated invocation does not duplicate work or mail (Scenario B, Priority: P1)

**Goal**: A second `run-due` on an unchanged feed creates no duplicate items, digests, pages or
mails, and makes no new provider calls. A job that is not due is not run.

**Independent Test**: `uv run pytest -m e2e -k rerun`.

- [X] T020 [US2] Write `test_e2e_rerun_without_new_articles_sends_nothing(e2e_env)` in
  `tests/test_e2e_scenarios.py`, implementing contract Scenario B items 1–5:
  1. `run_scenario_a(env)`, then `before = env.snapshot()`.
  2. `env.clock.advance(timedelta(days=1))`, `env.make_due()`, `env.run_due()` exits 0.
  3. Assert:
     - exactly 2 runs, and the new one is `succeeded`;
     - item, digest and notification counts, mail count and archive pages are equal to
       `before`;
     - `titles_for("relevance")` and `titles_for("summarize")` are equal to `before`;
     - job not locked, and `next_run_at == env.expected_next_run()` (exact).
  4. Call `env.run_due()` again **without** `make_due()`. It exits 0, stdout contains `nothing
     due`, and there are still exactly 2 runs.

**Checkpoint**: US1 and US2 pass independently.

---

## Phase 5: User Story 3 - Only newly published articles are processed (Scenario C, Priority: P2)

**Goal**: One new article → only it is fetched, scored, summarized, mailed and archived.

**Independent Test**: `uv run pytest -m e2e -k new_article`.

- [X] T021 [US3] Write `test_e2e_new_article_is_the_only_item_processed(e2e_env)` in
  `tests/test_e2e_scenarios.py`, implementing contract Scenario C items 1–6:
  1. `run_scenario_a(env)`, then `before = env.snapshot()`.
  2. Add `A4` (relevant, `published_at = clock() + 1 day - 1 hour`) through `env.feed.add` and
     register it in the provider's title table (T007).
  3. Advance the clock by 1 day, `make_due()`, `run_due()` exits 0.
  4. Assert:
     - the new run is `succeeded`; there are 5 items and the only new title is A4;
     - the relevance and summarize counter differences against `before` are exactly `{A4: 1}`;
     - there are 2 digests and the new one contains only A4's id;
     - exactly 1 new mail per recipient, containing A4 and none of A1–A3;
     - there are 2 archive pages, and both indexes list both pages with the newer one first
       (compare the order of the two page hrefs in each index).

**Checkpoint**: US1–US3 pass independently.

---

## Phase 6: User Story 4 - One failing item yields a partial run that still mails (Scenario D, Priority: P2)

**Goal**: A provider failure for one item gives a `partial` run with an error entry, and the
mail and archive still go out.

**Independent Test**: `uv run pytest -m e2e -k partial`.

- [X] T022 [US4] Add a way to start an `E2EEnv` with a modified article set in
  `tests/e2e_helpers.py`. For example, `env.set_articles([...])` re-renders the feed and rebuilds
  the provider's title table before `setup_job()`. Alternatively, use an indirect parametrization
  of `e2e_env`. Pick whichever keeps `e2e_env`'s signature simple.
- [X] T023 [US4] Write `test_e2e_one_failing_item_gives_partial_run_and_still_mails(e2e_env)` in
  `tests/test_e2e_scenarios.py`, implementing contract Scenario D items 1–5:
  1. Set A2 to `fail_at="relevance"`, then `run_scenario_a`-like steps (setup, `make_due`,
     `run_due` exits 0).
  2. Assert:
     - stdout shows `partial`;
     - 1 run with status `partial`, and its errors contain the entry with `stage ==
       "score_relevance"`, `error_class == "RuntimeError"` and `item_id == <A2 id>` (the shape
       comes from `invio.domain.RunError`, see `tests/test_pipeline_failures.py`
       `test_s5b_an_unexpected_error_fails_only_that_item`);
     - A2's item status is `failed`, and the digest contains A1 and A3;
     - 1 mail per recipient containing A1 and A3, and 1 archive page;
     - the job is not locked and `next_run_at == env.expected_next_run()`: the regular
       slot, not the retry time. This is what proves a `partial` run is not treated as failed.
     - `"boom secret"` does not appear in any stored run error or mail.

**Checkpoint**: All four scenarios pass on SQLite.

---

## Phase 7: User Story 5 - Suite runs in CI on SQLite and nightly on MariaDB (Priority: P3)

**Goal**: A blocking SQLite e2e step with a 60 s budget on every push and PR, and a nightly
MariaDB workflow that turns red on any failure. The push-time MariaDB job is unchanged.

**Independent Test**: CI is green on push, and a manual `workflow_dispatch` of **Nightly** is
green (quickstart §7).

- [X] T024 [US5] Edit `.github/workflows/ci.yml`, job `checks` (contracts/ci-workflows.md):
  - change the step `Tests (pytest)` to `run: uv run pytest --cov -m "not e2e"`;
  - add a step right after it, `name: E2E suite (SQLite, 60 s budget)` with
    `run: timeout 60 uv run pytest -m e2e --cov --cov-append -rs`.
  Do NOT touch the jobs `mariadb` and `deploy-static` (FR-017).
- [X] T025 [P] [US5] Create `.github/workflows/nightly.yml` following contracts/ci-workflows.md:
  - `---` header, `name: Nightly`;
  - `on: schedule: - cron: "17 2 * * *"` and `workflow_dispatch:`;
  - `permissions: contents: read`;
  - job `mariadb-nightly` without `continue-on-error`. Copy the `mariadb:11.4` service (env,
    ports, health options) verbatim from the `mariadb` job in `ci.yml`.
  - job env: `INVIO_TEST_DATABASE_URL: mysql+pymysql://root@127.0.0.1:3306/invio_test?charset=utf8mb4`
    and `INVIO_REQUIRE_TEST_DATABASE: "1"`;
  - steps: checkout `actions/checkout@v7`, `astral-sh/setup-uv@v7` (python 3.12, cache),
    `uv sync --locked`, and `name: Persistence and e2e tests on MariaDB` with
    `run: uv run pytest -m "db or e2e" -rs`.
  Lines must be ≤ 120 characters.
- [X] T026 [US5] Validate the workflows locally:
  - `uv run --group deploy yamllint --strict .github/workflows/`;
  - `uv run pytest -m db --collect-only -q | grep -c test_e2e` prints `0` (FR-017);
  - `INVIO_REQUIRE_TEST_DATABASE=1 uv run pytest -m e2e` fails at once without a MariaDB URL
    (FR-016).
- [X] T027 [US5] Run the suite against a local MariaDB (quickstart §4, `docker run …
  mariadb:11.4`) with `INVIO_REQUIRE_TEST_DATABASE=1 uv run pytest -m "db or e2e" -rs`. Fix any
  test-side DB assumptions in `tests/e2e_helpers.py`: unique job names, `clean_jobs` teardown,
  timezone-naive datetimes returned by MariaDB (compare as UTC).

**Checkpoint**: All acceptance criteria are verifiable in CI.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [X] T028 [P] Update `README.md` near the existing test instructions (around the
  `uv run pytest -m db` section). Add a short "End-to-end tests" paragraph:
  - `uv run pytest -m e2e`: local fixtures only, no keys, no internet, under 60 s;
  - the nightly MariaDB workflow and `INVIO_REQUIRE_TEST_DATABASE`;
  - how to run it against MariaDB locally.
- [ ] T029 Measure and harden:
  - `time uv run pytest -m e2e` must be ≤ 60 s (note the measured time in the PR description);
  - run the suite 20× in a row (quickstart §5) to check determinism (SC-005);
  - run it offline (`unshare -rn` on Linux, or Wi-Fi off on macOS) to check SC-002.
- [ ] T030 Mutation spot checks and the FR-018 triage. Apply each mutation from
  contracts/e2e-harness.md "Mutation checks" locally (do not commit), confirm that the listed
  scenario fails, and revert (SC-006). For any real product bug found during T016–T027:
  - small and local → fix it in `src/invio/…`, with the scenario as its failing test;
  - larger → open a GitHub issue, link it in the PR, and keep issue #26 open until that fix
    lands.
  Never mark a scenario `skip` or `xfail`.
- [ ] T031 Run the full quality gate locally:
  `uv run ruff check && uv run ruff format --check && uv run mypy src && uv run pytest -m "not e2e" && uv run pytest -m e2e`.
  Then fix lint and type issues in the new test files (fully annotated, no bare `Any` without a
  justifying comment).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies. T001 (branch) comes first; everything else is committed
  on `gh-issue-26`.
- **Foundational (Phase 2)**: depends on Setup; blocks all stories. T005 → T006 → T007 → T008 →
  T010 → T011 → T012 → T013 (same file, sequential). T009 (session hook) is independent.
- **US1 (Phase 3)**: depends on Phase 2. T014 → T015 → T016 is the in-process MVP. T017, T018 →
  T019 is the process smoke test and can follow T016 or run alongside it.
- **US2 (Phase 4)** and **US3 (Phase 5)**: depend on T014 (`run_scenario_a`) only.
- **US4 (Phase 6)**: depends on Phase 2 (T022 extends `E2EEnv`); independent of US1–US3 tests.
- **US5 (Phase 7)**: T024/T025 depend only on T002 (marker). T026/T027 need the scenarios to
  exist.
- **Polish (Phase 8)**: after all stories.

### User Story Dependencies

- US1: foundation for B and C (they start with Scenario A's steps, but inside their own test).
- US2, US3: depend on US1's `run_scenario_a` helper; otherwise independent of each other.
- US4: independent of US1–US3.
- US5: independent of the scenario code for the workflow files. Its acceptance (green CI) needs
  US1–US4.

### Parallel Opportunities

- T003 and T004 (different files).
- T009 (`tests/conftest.py`) in parallel with T005–T013.
- T017 (`tests/e2e_bootstrap/sitecustomize.py`) in parallel with T014–T016.
- T024 (`ci.yml`) and T025 (`nightly.yml`) in parallel with any Phase 3–6 task.
- T028 (README) in parallel with T029.
- Scenario tests T020, T021 and T023 all live in `tests/test_e2e_scenarios.py`, so they are not
  [P]. Write them one after the other.

---

## Parallel Example: User Story 1 + CI

```bash
# Once Phase 2 is done, at the same time:
Task: "T016 [US1] Scenario A test in tests/test_e2e_scenarios.py"
Task: "T017 [US1] sitecustomize bootstrap in tests/e2e_bootstrap/sitecustomize.py"
Task: "T025 [US5] Nightly workflow in .github/workflows/nightly.yml"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 + Phase 2 (fixtures, guard, environment).
2. T014–T016: Scenario A in-process. **Stop and validate**: `uv run pytest -m e2e -k first_run`.
3. T017–T019: the separate-process smoke test.

### Incremental Delivery

1. MVP (Scenario A).
2. Add B (rerun idempotency), the highest remaining value because duplicate mails are the most
   visible failure.
3. Add C, then D.
4. Wire CI (T024–T027), then the polish phase (T028–T031).

---

## Notes

- Every scenario uses its own `tmp_path`, DB (SQLite) or unique job name (MariaDB), feed server,
  SMTP sink and provider (FR-005).
- Do not use `time.sleep`. The only wait is the bounded mail poll in T019.
- If `sitecustomize` is not picked up in the subprocess (T019 fails on `/feed.xml`), fall back to
  a `.pth` file in a temp dir that is added via `site.addsitedir` from a tiny `sitecustomize`.
  Alternatively, check that the venv has no own `sitecustomize` (plan.md "Risks").
- Commit after each phase on branch `gh-issue-26`.
