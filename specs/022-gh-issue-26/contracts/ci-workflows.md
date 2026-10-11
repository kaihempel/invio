# Contract: CI Wiring for the E2E Suite

## `.github/workflows/ci.yml` (on push and pull_request)

| Job | Change | Blocking |
|---|---|---|
| `checks` | step `Tests (pytest)` becomes `uv run pytest --cov -m "not e2e"` | yes (unchanged) |
| `checks` | new step `E2E suite (SQLite, 60 s budget)` after it: `timeout 60 uv run pytest -m e2e --cov --cov-append` | yes |
| `deploy-static` | none | — |
| `mariadb` | **none**; it still runs `uv run pytest -m db` with `continue-on-error: true` (FR-017) | no (unchanged) |

Rules:
- The e2e step runs on SQLite (no `INVIO_TEST_DATABASE_URL` in `checks`).
- `timeout` exits 124 when the 60 s budget is exceeded, and that fails the step (SC-003).
- Chromium is already installed in `checks`. The suite does not need it.

## `.github/workflows/nightly.yml` (new)

```yaml
name: Nightly
on:
  schedule:
    - cron: "17 2 * * *"   # 02:17 UTC daily
  workflow_dispatch:
permissions:
  contents: read
jobs:
  mariadb-nightly:
    runs-on: ubuntu-latest
    # no continue-on-error: a failure turns the workflow red (FR-016)
    services:
      mariadb: { image: mariadb:11.4, … same env, port and health check as ci.yml … }
    env:
      INVIO_TEST_DATABASE_URL: mysql+pymysql://root@127.0.0.1:3306/invio_test?charset=utf8mb4
      INVIO_REQUIRE_TEST_DATABASE: "1"
    steps:
      - checkout, setup-uv (python 3.12, cache), uv sync --locked
      - name: Persistence and e2e tests on MariaDB
        run: uv run pytest -m "db or e2e" -rs
```

Rules:
- It runs on the default branch only (that is how GitHub runs `schedule`). `workflow_dispatch`
  allows a manual run on any branch.
- MariaDB is unreachable → `_server_engine` errors → the tests error → the job is red.
- `INVIO_TEST_DATABASE_URL` is missing or empty → with `INVIO_REQUIRE_TEST_DATABASE=1`,
  `pytest_sessionstart` (`tests/conftest.py`) exits with code 1 before collection → the job is
  red. No test runs on SQLite, so it never passes silently there.
- `yamllint --strict .` (in `deploy-static`) must accept the new file. It follows the same style
  as `ci.yml` (`---` document start, unquoted `on:`, lines ≤ 120 characters).
