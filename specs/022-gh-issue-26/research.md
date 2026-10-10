# Research: End-to-End Integration Test Suite (gh-issue-26)

All open points from the spec and the Technical Context are resolved below. Each entry names
the decision, the reason, and the alternatives considered. Paths are relative to the repository
root.

## R1 — How the in-process scenarios drive the real system

**Decision**: Scenarios A–D call the real Typer app (`invio.cli.main.app`) through
`typer.testing.CliRunner`, using the same commands an operator types: `invio db upgrade`,
`invio job create --from-file <yaml> --name <job>` and `invio run-due`. Only one seam is
replaced: `invio.cli.commands.run_due._run_due`, the documented test seam ("tests replace
this"). The replacement calls the real `invio.pipeline.due.run_due(limit=…, parallel=…,
deps=…)` with deps built by the **real** `invio.pipeline.deps.default_deps(settings, …)`. The
only adjustments to those deps are:

- `allow_networks=(127.0.0.0/8,)`, so the guarded HTTP client may reach the loopback fixture
  server. `default_deps` already takes this parameter "for tests".
- `clock` is replaced (`dataclasses.replace(deps, clock=fake_clock)`), so due times, locks,
  archive file names and run timestamps are deterministic (see R6).

Everything else is production code: the SQLAlchemy engine from `INVIO_DATABASE_URL`,
`SafeHttpClient` (robots.txt, rate limit, size limits), RSS parsing, page fetch and text
extraction, the LangGraph pipeline, `deliver_digest` (real SMTP client and archive writer), the
real `compute_next_run`, `retry_delay` and lock TTL.

**Rationale**: This is the closest in-process equivalent of the systemd invocation. Exit codes,
stdout report and settings loading all go through the real CLI. The two adjustments are exactly
the ones `default_deps` exposes for tests.

**Alternatives considered**:
- Calling `run_due()` directly, without the CLI. Rejected: the spec (FR-004, FR-010) requires
  CLI invocation and exit-code assertions.
- Reusing `tests/run_cli_helpers.py` (`build_env` with fake sources and a recording notifier).
  Rejected: it fakes the HTTP sources and the notifier, which are exactly the integration points
  this suite must cover.

## R2 — Fake language-model provider (in-process)

**Decision**: Reuse `tests.pipeline_helpers.RoutedFakeProvider`. It answers by purpose
(`relevance`, `summarize`, `synthesize`), routes per item title, and records every call
(`calls`, `titles_for(purpose)`). It is registered under the provider name `ollama` with the
existing `patched_providers` fixture (`tests/conftest.py`). The job YAML uses
`llm.provider: ollama` with the packaged Ollama models (`llama3.2:3b` fast, `qwen2.5:14b`
smart). These models are in the packaged registry (`src/invio/llm/models.d/ollama.yaml`), need
no API key and are priced at 0, so the real `default_registry()` and budget code run unchanged.

Per-title routes give deterministic scores:
- relevant articles score 0.9;
- one "off-topic" article scores 0.1, below the default `min_relevance` 0.6;
- Scenario D routes one title to `RuntimeError` at relevance (this is what the existing
  `RunCli.fail_item_at_relevance` uses, and the pipeline records it as a failed item and a
  `partial` run).

**Rationale**: The fake already exists, is deterministic under concurrency (routing by title,
not by call order) and is proven by the pipeline tests. Using `ollama` avoids adding a test-only
value to the `LLMProvider` enum, which would change the job contract.

**Alternatives considered**:
- `invio.llm.fake.FakeProvider`. Rejected: its script is consumed in call order, so it is not
  deterministic with `max_parallel_items > 1`.
- Adding a `fake` provider to the product enum. Rejected: this changes the job contract and the
  JSON Schema for a test concern (constitution I, simplicity).

## R3 — Local web fixture (feed and articles)

**Decision**: Reuse `tests.http_helpers.LoopbackServer`. It serves GET on 127.0.0.1 with an OS
port, has mutable `routes` and a thread-safe request log. An e2e helper (`E2EFeed`, see
data-model) owns a list of articles and re-renders `/feed.xml` (RSS 2.0) plus one
`/articles/<slug>.html` route per article whenever an article is added. Article pages come from
`tests.pipeline_helpers.article_html(title)`, so they pass text extraction. `/robots.txt` is
served as allow-all, so the real robots check runs. `pubDate` values are relative to the fake
clock.

Settings for speed: `INVIO_HTTP_HOST_INTERVAL_SECONDS=0.01` (default 1 s per host; with one host
and about 5 requests per run, the default alone would cost about 5 s per run).

**Alternatives considered**: a new aiohttp or `http.server` fixture. Rejected because it
duplicates `LoopbackServer`.

## R4 — Local mail sink

**Decision**: Reuse `tests.smtp_helpers` (`aiosmtpd` `Controller` + `RecordingHandler`) without
AUTH. Settings: `INVIO_SMTP_HOST=127.0.0.1`, `INVIO_SMTP_PORT=<port>`,
`INVIO_SMTP_SECURITY=none`, `INVIO_SMTP_FROM=invio@example.test`. The sink stores a message in
`handle_DATA` before it answers `250`. So when `invio run-due` returns, every mail it sent is
already recorded, and no waiting or sleeping is needed (FR-012). The separate-process test (R8)
can still wait, with a bounded poll on the message count (deadline 5 s, no fixed sleeps).

**Alternatives considered**: a recording notifier (as in `run_cli_helpers`). Rejected: it skips
the SMTP client, MIME rendering and the archive link in the footer.

## R5 — Database per scenario (SQLite and MariaDB)

**Decision**:
- **SQLite (default)**: a fresh file database `tmp_path/e2e.sqlite` per scenario. The CLI opens
  its own engine from `INVIO_DATABASE_URL`, so in-memory SQLite is not possible. The schema is
  created through the CLI: `invio db upgrade` (Alembic to head). This also covers the migration
  chain end to end.
- **MariaDB (nightly)**: when `INVIO_TEST_DATABASE_URL` is set, the existing session fixture
  `_server_engine` brings the schema to head once. The scenario sets
  `INVIO_DATABASE_URL=<that URL>`, skips `db upgrade`, and the existing `clean_jobs` fixture
  deletes every job (and, by cascade, its history) at teardown. Each scenario uses a unique job
  name, so leftovers from an aborted earlier run cannot collide.
- Assertions read rows through a separate engine on the same URL (`tests` side), never through
  the CLI's engine.

**Rationale**: This is the same switch (`INVIO_TEST_DATABASE_URL`, `uses_sqlite()`) the
persistence tests already use, so the suite runs on both databases without modification
(FR-014).

## R6 — Time control

**Decision**: A `FakeClock` (from `tests/conftest.py`), started at a fixed UTC instant, is
injected as `RunDeps.clock` (R1). "Making a job due" (spec assumption: "bumping `next_run_at`")
is a helper that sets `jobs.next_run_at = clock() - 1 minute` and clears any lock, through the
assertion engine. Between invocations the clock advances by one day, so archive page names
(UTC minute) differ and lock expiry never interferes. Job creation through the CLI computes
`next_run_at` with the real clock. The scenario overwrites it with the helper before the first
`run-due`, so Scenario A does not depend on the wall clock either.

The **"not due" check** (US2 acceptance 4): after a run, the stored `next_run_at` (computed by
the real `compute_next_run` from the fake clock) lies after `clock()`. A further `run-due`
without the helper must print `nothing due` and exit 0.

**Alternatives considered**: freezegun or time-machine. Rejected: this adds a dependency, and
`RunDeps.clock` is already the seam.

## R7 — Network guard for in-process scenarios (FR-011)

**Decision**: An e2e-only fixture, `no_external_network`, monkeypatches `socket.socket.connect`,
`socket.socket.connect_ex` and `socket.getaddrinfo`. Any destination that is not a loopback
address (`127.0.0.0/8`, `::1`) or `localhost` raises `ExternalNetworkError`, an
`AssertionError` subclass. This fails the test immediately instead of hanging. The guard also
removes provider API key variables (`*_API_KEY`) and `INVIO_*` overrides (the latter is already
done by `isolated_settings`). Because the provider is replaced (R2) and the SMTP host is
127.0.0.1, a misconfiguration surfaces as a loud failure. A dedicated self-test in the suite
checks that the guard fires for a public address (SC-006 style: the guard itself is verified).

**Alternatives considered**: `pytest-socket`. Rejected: a new dev dependency for about 30 lines,
and its allow-host handling is per-test rather than per-fixture.

## R8 — Separate-process smoke test (FR-019)

**Problem**: The real `invio` process cannot be patched from the test. Two seams are needed:

1. **Loopback fetches**: `SafeHttpClient` refuses non-public addresses unless `allow_networks`
   is passed, and no setting enables that (by design: SSRF protection).
2. **LLM**: the in-process `RoutedFakeProvider` does not exist in another process.

**Decision**:
- **LLM**: a local **Ollama-compatible stub server** (`FakeOllamaServer`, an `http.server` on
  127.0.0.1 that handles `POST /api/chat`). The job uses `provider: ollama`, and the subprocess
  gets `INVIO_OLLAMA_BASE_URL=http://127.0.0.1:<port>`. The real Ollama adapter (httpx2, not
  guarded, no SDK, no key) talks to it. The stub picks its reply from the request: a `format`
  schema containing `score` gets a relevance answer, one containing `headline` gets a summary
  answer, and a request without `format` gets the digest text. It returns Ollama's
  non-streaming response shape (`message.content`, `prompt_eval_count`, `eval_count`,
  `done: true`). The exact request and response shape is taken from the existing fixtures in
  `tests/fixtures/ollama/`.
- **Loopback fetches**: a test-only bootstrap directory is put first on the subprocess's
  `PYTHONPATH`. It contains a `sitecustomize.py` that wraps
  `invio.pipeline.deps.default_deps`, so `allow_networks` defaults to `127.0.0.0/8`. The
  installed `invio` console script (`<venv>/bin/invio`) is then started unchanged. The
  bootstrap lives under `tests/e2e_bootstrap/` and is never packaged, so no product code changes.
- **Environment**: built from scratch, not inherited. It contains `PATH`, `HOME=tmp`,
  `PYTHONPATH=<bootstrap>`, the `INVIO_*` settings for the database, SMTP, archive, Ollama URL and
  HTTP interval, and `INVIO_LOG_LEVEL=WARNING`. No `*_API_KEY` and no mail credentials (FR-019).
  If `COVERAGE_PROCESS_START` is set, it is passed through, so the subprocess shows up in coverage
  when pytest-cov enables it.
- **Time**: the subprocess uses the real clock. Before `run-due`, `next_run_at` is set in the
  past relative to real UTC now. Assertions count rows, mails and pages, never exact names.
- **Database**: on SQLite, the subprocess runs its own `invio db upgrade` against its file. On
  MariaDB it uses the shared, already migrated server database.

**Rationale**: The real installed entry point, settings-from-env and logging setup are
exercised, and so is the real Ollama adapter, as a bonus. The SSRF guard stays closed in
production; the loopback opening exists only in the test bootstrap.

**Alternatives considered**:
- A product setting like `INVIO_HTTP_ALLOW_NETWORKS`. Rejected: it widens a security control in
  production for a test (constitution V; spec FR-018 only allows small product *fixes*).
- `python -c "…; app()"` instead of the console script. Rejected: FR-019 asks for the installed
  command.
- Serving the feed from a non-loopback interface address. Rejected: those are private addresses
  too, so they are blocked as well.

## R9 — Test selection and markers (FR-013, FR-017)

**Decision**: Register a new marker,
`e2e: end-to-end scenarios over local fixtures (SQLite; MariaDB with INVIO_TEST_DATABASE_URL)`,
in `pyproject.toml`. Every e2e test module sets `pytestmark = pytest.mark.e2e` and does **not**
carry `db`, so the push-time MariaDB job (`pytest -m db`) stays unchanged (FR-017). Select the
suite with `pytest -m e2e`.

## R10 — CI wiring (FR-015, FR-016, SC-003)

**Decision**:
- **`.github/workflows/ci.yml`, job `checks`**: the existing step `Tests (pytest)` becomes
  `uv run pytest --cov -m "not e2e"`. A new step follows it,
  `E2E suite (SQLite, 60 s budget)`:
  `timeout 60 uv run pytest -m e2e --cov --cov-append`. The shell `timeout`
  enforces the 60 s budget (SC-003), including the subprocess smoke test. The step blocks, like
  every step in `checks` (FR-015).
- **New workflow `.github/workflows/nightly.yml`**: `on: schedule` (cron `17 2 * * *`, UTC) and
  `workflow_dispatch`. One job, `mariadb-nightly`, uses the same `mariadb:11.4` service and
  health check as the push job, and has **no** `continue-on-error`. Steps: checkout, set up uv,
  `uv sync --locked`, then `uv run pytest -m "db or e2e" -rs` with
  `INVIO_TEST_DATABASE_URL` and `INVIO_REQUIRE_TEST_DATABASE=1`.
- **`INVIO_REQUIRE_TEST_DATABASE=1`** is checked by a `pytest_sessionstart` hook in
  `tests/conftest.py`, next to the existing `INVIO_REQUIRE_DEPLOY_TOOLS` handling. If it is set
  while `uses_sqlite()` is true (the URL is missing or empty), the hook calls `pytest.exit(…,
  returncode=1)` before collection, so neither the `db` nor the `e2e` tests run on SQLite. A
  fixture would only fail when the first e2e test requests it, after the `db` tests had already
  passed on SQLite (analysis finding I1). If MariaDB is unreachable, `_server_engine` raises during setup, and the tests
  error. Either way the job turns red, never silently green on SQLite (FR-016, edge case
  "MariaDB unavailable").
- The push-time `mariadb` job in `ci.yml` is not touched (FR-017).

**Alternatives considered**: adding `schedule:` to `ci.yml`. Rejected: it would also run the
deploy jobs nightly and mix the blocking semantics of the two MariaDB jobs.

## R11 — Product bugs uncovered (FR-018)

**Decision**: Bugs found while implementing are handled per FR-018. A small fix lands in this
branch, and the failing scenario is its regression test. A larger fix gets its own issue, and
this feature stays open until that fix lands. The tasks phase reserves one "triage findings"
task. Nothing is marked `xfail` or `skip`.

## R12 — Performance budget estimate

| Part | Estimate (SQLite, CI runner) |
|---|---|
| Fixture start (HTTP, SMTP, Ollama stub) | < 0.5 s per test |
| In-process `db upgrade` + `job create` + one `run-due` (4 articles) | ≈ 1–2 s |
| Scenarios A–D (A: 1 run, B: 2 runs + "not due" check, C: 2 runs, D: 1 run) | ≈ 8–12 s |
| Separate-process smoke test (3 interpreter starts: `db upgrade`, `job create`, `run-due`) | ≈ 6–10 s |
| Guard self-test | < 0.1 s |
| **Total** | **≈ 15–25 s**, well under 60 s |

The main risk is the HTTP per-host interval, which R3 mitigates. B and C repeat Scenario A's
setup inside their own test (scenario isolation, FR-005) through a shared helper.
