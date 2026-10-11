# Context: GitHub issue #38 — README, configuration reference and operations runbook

**Branch**: `gh-issue-38` · **Labels**: docs, round-4, track:integration · **Depends on**: #24

## Problem

invio runs unattended (systemd timers `invio-run-due`, `invio-notify-retry`). Operating
knowledge (install, config, troubleshooting, backup, security) must be written down so a new
developer/operator can install and run a sample job using only the docs.

## Deliverables

1. `README.md`: purpose, architecture diagram (Mermaid), quick start with a sample job
   (`docs/job.example.yaml`). Keep the existing developer reference sections (a test asserts
   the README mentions `invio db upgrade`, `INVIO_DATABASE_URL`, `INVIO_TEST_DATABASE_URL`).
2. `docs/configuration.md`: every `JobConfig` field (source of truth `src/invio/config/job.py`,
   generated `docs/job.schema.json`) with defaults and examples; also the `INVIO_*` settings
   (`src/invio/config/settings.py`) and the model registry (`src/invio/llm/models.d/`).
3. `docs/operations.md`: systemd install, updating, `invio db upgrade`, `journalctl`, stuck
   locks, retrying notifications (`invio notify retry`), adding a provider key, provider outage.
4. `docs/troubleshooting.md`: YouTube blocks, SMTP errors, rate limits, budget exceeded,
   empty digests.
5. `docs/backup.md`: `mariadb-dump` schedule and restore test; what to keep (DB, model
   registry, job YAML exports via `invio job export`).
6. `docs/security.md`: secret handling, prompt-injection mitigations, SSRF guard, robots.txt.

## Issue text vs. reality (docs must follow the code)

- The issue says `scout db upgrade` → the CLI is `invio` (`invio db upgrade`).
- The issue says `models.yaml` → the model registry is `src/invio/llm/models.d/<provider>.yaml`.
- Database is MariaDB via `mysql+pymysql` (SQLite used in tests/CI).
- `docs/deployment.md` already covers Ansible/manual install, timers, logs, updating; new
  docs should link to it rather than duplicate it.
- There is no dedicated "unlock" CLI command; lock handling must be documented as it
  actually works (lock expiry `INVIO_RUN_LOCK_SECONDS`, DB columns) — verify in code.

## CLI commands (must all appear in docs with an example)

`invio --version`, `invio db upgrade`, `invio job list|show|create|edit|enable|disable|delete|
export|import|suggest|run`, `invio llm test`, `invio notify retry`, `invio run list|show`,
`invio run-due`, `invio source discover`, `invio usage`.

## Acceptance criteria

- [ ] A new developer can install and run the sample job following only the README
- [ ] Every config field in the schema is documented
- [ ] Runbook covers lock reset, failed notifications and provider outage
- [ ] All CLI commands appear in the docs with an example

## Verification

Add `tests/test_docs.py`: every property in `docs/job.schema.json` appears in
`docs/configuration.md`; every CLI command (introspected from the Typer app) appears in the
docs as `invio <cmd>`; links between docs resolve. Run ruff, mypy, pytest.
