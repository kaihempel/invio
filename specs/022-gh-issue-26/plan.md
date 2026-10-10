# Implementation Plan: End-to-End Integration Test Suite

**Branch**: `gh-issue-26` | **Date**: 2026-10-10 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/022-gh-issue-26/spec.md`

## Summary

Add an `e2e`-marked pytest suite that drives invio the way an operator does: YAML job file →
`invio job create` → `invio run-due` (CLI, in-process). It asserts the externally visible
results: database rows, mails received by a local SMTP sink, and static archive files.

The real production wiring runs (`default_deps`: guarded HTTP client, RSS parsing, page
extraction, pipeline, SMTP delivery, archive writer, scheduler). Only these parts are replaced:
- the LLM provider, by the existing title-routed `RoutedFakeProvider` registered as `ollama`;
- the clock, by `FakeClock`;
- the HTTP guard's allowlist, which is opened to loopback through the existing `allow_networks`
  test seam.

The suite has:
- four scenarios (A first run, B rerun without changes, C one new article, D one provider
  failure → `partial`);
- a self-test for the network guard;
- one smoke test that runs the installed `invio` command as a separate process against an
  Ollama-compatible stub.

CI runs the suite on SQLite in the blocking `checks` job with a hard 60 s budget. A new nightly
workflow runs `db` and `e2e` tests against MariaDB, and its failures are not ignored.

## Technical Context

**Language/Version**: Python 3.12 (uv-managed)

**Primary Dependencies**:
- Typer (`CliRunner`), pytest, pytest-asyncio
- aiosmtpd (already a dev dependency)
- httpx2, SQLAlchemy/Alembic (product)

No new dependencies.

**Storage**: SQLite file DB per test (default); MariaDB 11.4 via `INVIO_TEST_DATABASE_URL`
(nightly)

**Testing**: pytest; new marker `e2e`; existing helpers: `LoopbackServer`, `smtp_helpers`,
`RoutedFakeProvider`, `FakeClock`, `patched_providers`, `_server_engine`/`clean_jobs`

**Target Platform**: GitHub Actions `ubuntu-latest`; developer macOS/Linux

**Project Type**: CLI application (test suite + CI workflow changes only)

**Performance Goals**: `pytest -m e2e` ≤ 60 s on SQLite in CI (estimate 15–25 s, research R12)

**Constraints**:
- no internet, no API keys, no mail credentials;
- deterministic (no sleeps, fake clock);
- push-time MariaDB job unchanged;
- no `skip`/`xfail`.

**Scale/Scope**: 6 tests, about 4 feed articles plus 1 added, 2 recipients; about 600–800 lines
of test code, 1 new workflow file, 1 edited workflow, a `pyproject.toml` marker and a README
testing note

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Assessment | Status |
|---|---|---|
| I. Strict contracts at boundaries | Jobs enter only through the real YAML import (`job create --from-file`), so validation runs. No new config keys and no test-only provider enum value (research R2). | ✅ |
| II. CLI-first | Every scenario goes through the `invio` CLI. Exit codes and stdout are asserted (FR-010). | ✅ |
| III. Test-covered behaviour | This feature *is* tests. No network, no real providers or production DB (R7, R2, R5). Deterministic, with no retries (R6, R4). | ✅ |
| IV. Quality gates mirror CI | Test code passes ruff and ruff format. mypy covers `src/` only, but the new helpers are fully typed anyway. CI keeps `uv sync --locked`. The new gate (`e2e` step) runs in CI and with the same command locally. | ✅ |
| V. Secrets / observability | No secrets are used. The subprocess environment is built without keys (R8). Logs stay structured; nothing is changed. | ✅ |
| Architecture constraints | No product code changes are planned. The loopback opening for the subprocess lives in a test-only `sitecustomize` bootstrap, not in settings (R8). | ✅ |
| Workflow | Branch `gh-issue-26`; specs in `specs/022-gh-issue-26`. The README gets a short "Tests" note on `-m e2e` and the nightly job (developer-facing behaviour). | ✅ |

No violations, so Complexity Tracking stays empty.

**Post-design re-check (after Phase 1)**: still ✅. The design touches only `tests/` (including one hook in `tests/conftest.py`),
`pyproject.toml` (marker), `.github/workflows/` and `README.md`. Product changes can come only
from FR-018 bug fixes, which are reviewed one by one.

## Project Structure

### Documentation (this feature)

```text
specs/022-gh-issue-26/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── e2e-harness.md   # selection, CLI surface, per-scenario assertions
│   └── ci-workflows.md  # ci.yml changes, new nightly.yml
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
tests/
├── e2e_helpers.py              # NEW: E2EArticle, E2EFeed, E2EEnv, FakeOllamaServer,
│                               #      network guard, make_due/snapshot/expected_next_run,
│                               #      e2e_env fixture
├── e2e_bootstrap/
│   └── sitecustomize.py        # NEW: subprocess-only; default allow_networks = loopback
├── test_e2e_scenarios.py       # NEW: scenarios A–D + guard self-test (pytestmark = e2e)
├── test_e2e_process.py         # NEW: installed-command smoke test (pytestmark = e2e)
├── conftest.py                 # + pytest_sessionstart: INVIO_REQUIRE_TEST_DATABASE check;
│                               #   existing fixtures reused
├── http_helpers.py             # reused: LoopbackServer
├── smtp_helpers.py             # reused: aiosmtpd sink
└── pipeline_helpers.py         # reused: RoutedFakeProvider, article_html, replies

pyproject.toml                  # + marker "e2e"
.github/workflows/ci.yml        # checks: split pytest step, add 60 s e2e step
.github/workflows/nightly.yml   # NEW: schedule + workflow_dispatch, MariaDB, db or e2e
README.md                       # + Tests: how to run -m e2e, nightly MariaDB
```

**Structure Decision**: The flat `tests/` layout is kept (`<topic>_helpers.py` +
`test_<topic>_*.py`), as in the rest of the suite, so the shared `tests/conftest.py` autouse
fixtures (`isolated_settings`, `restore_root_logger`, yt-dlp guard) apply unchanged. The
subprocess bootstrap needs its own directory, because it is put on `PYTHONPATH` by itself.

## Phase 0 / Phase 1 Outputs

- [research.md](research.md) covers R1–R12: CLI seam, fake provider, fixtures, DB per
  scenario, time control, network guard, subprocess strategy, markers, CI, bug policy, budget.
- [data-model.md](data-model.md): fixture entities, observed outcomes, state transitions.
- [contracts/e2e-harness.md](contracts/e2e-harness.md): exact assertions per scenario and the
  mutation table (SC-006).
- [contracts/ci-workflows.md](contracts/ci-workflows.md): CI changes.
- [quickstart.md](quickstart.md): validation steps for every acceptance criterion.

## Risks

| Risk | Mitigation |
|---|---|
| Per-host HTTP interval (1 s default) blows the budget | `INVIO_HTTP_HOST_INTERVAL_SECONDS=0.01` in the e2e settings (R3) |
| `sitecustomize` not picked up (e.g. the venv has its own) | the smoke test asserts that the feed server saw `/feed.xml`; if it fails, the fallback is a `.pth` file in a temp `site` dir on `PYTHONPATH` |
| Ollama stub drifts from the real API shape | the stub's reply shape comes from `tests/fixtures/ollama/` (used by the adapter's own tests) |
| MariaDB leftovers between nightly tests | unique job names + `clean_jobs` teardown (R5) |
| The suite exposes a real product bug | FR-018: a small fix lands here, a large fix gets its own issue that blocks closing this one |
| Coverage instrumentation slows the e2e step | measured in the first CI run; the budget covers the instrumented run |

## Complexity Tracking

No constitution violations, so this section is empty.
