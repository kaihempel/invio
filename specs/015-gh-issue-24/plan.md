# Implementation Plan: Unattended systemd Deployment with Hardening and Ansible Provisioning

**Branch**: `gh-issue-24` | **Date**: 2026-10-07 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `specs/015-gh-issue-24/spec.md`

## Summary

This feature ships deployment artefacts only. No application code, tables or CLI commands
change.

- **Units** in `deploy/systemd/` (the canonical copies, installed verbatim):
  - `invio-run-due.service` + `.timer`: oneshot `invio run-due` (#23) at `*:0/15`, with
    `Persistent=true`, `RandomizedDelaySec=60` and `AccuracySec=1s`.
  - `invio-notify-retry.service` + `.timer`: oneshot `invio notify retry` every hour, with
    `Persistent=true` and `RandomizedDelaySec=300`.
  - Both run as the system account `invio` with `EnvironmentFile=/etc/invio/invio.env`.
- **Hardening** (research R8): the issue's set (`NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`, `PrivateTmp`, `ReadWritePaths=/var/lib/invio`, `MemoryMax` 1G/256M,
  `TimeoutStartSec` 3h/15min) plus `StateDirectory=invio` and a vetted set of kernel, device and
  namespace protections. Network access stays open.
- **Ansible role** in `deploy/ansible/roles/invio/`:
  1. validates the variables;
  2. MariaDB: installs a local server by default (clarification Q2), then creates the database
     and user with `community.mysql`;
  3. creates the account;
  4. writes the env file (`invio:invio 0600`, `no_log`);
  5. installs pinned `uv` and a uv-managed CPython 3.12 in `/opt/invio-python` (R1: Debian 12
     only has 3.11);
  6. checks out the Git ref into `/opt/invio` (clarification Q3) and runs
     `uv sync --locked --no-dev`;
  7. runs `invio db upgrade` through `systemd-run` with the same env file and user (R5);
  8. installs the units, then `enable --now` both timers.
  - **Updates** (clarification Q4, R4): a check-mode Git probe detects a ref change *before*
    anything is touched. The role then stops both timers, waits for running services, deploys,
    migrates and restarts. A failure leaves the timers stopped.
- **Verification** (clarification Q5):
  - A `deploy-static` CI job on every PR: `systemd-analyze verify` in a stub root with zero
    output allowed, `ansible-lint`, and a pytest contract test of the unit directives.
  - An opt-in `deploy.yml` workflow running Molecule on Debian 12 and 13 containers with
    systemd: converge, idempotence, verify, update path, the external-DB scenario, the timer
    schedule, catch-up after a systemd restart, and sandbox probes.
  - Pre-commit hooks for `ansible-lint` and `verify-units.sh`, matching CI (constitution IV).
  - A manual checklist only for the one-hour observation and the timed manual update.
- **Docs**: a new `docs/deployment.md` (install with the role or by hand, operations, update
  procedure, recovery, manual checklist) and a README pointer.

## Technical Context

**Language/Version**: systemd unit files (systemd ≥ 252, Debian 12; 257, Debian 13); Ansible
(ansible-core 2.17+), YAML/Jinja2; Bash for `verify-units.sh`; Python 3.12 for the contract
test. The application itself is unchanged (Python 3.12, uv).

**Primary Dependencies**: Runtime on the host: systemd, MariaDB (`mariadb-server` when managed),
`git`, `uv` (pinned binary), `python3-pymysql` (for the Ansible MySQL modules). Controller and CI:
`ansible-core`, `ansible-lint`, `molecule`, `molecule-plugins[docker]` in a new non-default
`deploy` dependency group (locked); collection `community.mysql`. No new **runtime** dependency
of invio.

**Storage**: MariaDB database `invio` (existing schema, migrated with the existing
`invio db upgrade`); host files described in [data-model.md](data-model.md).

**Testing**: pytest (`tests/test_deploy_units.py`, runs everywhere); `systemd-analyze verify`
and `ansible-lint` in CI; Molecule (Docker driver, systemd-in-container) in an opt-in workflow;
a manual checklist only for the one-hour observation and the timed manual update.

**Target Platform**: Debian 12 (bookworm) and 13 (trixie) servers with systemd, no Docker in
production; amd64 (arm64 works if uv and the managed Python are available, but it isn't tested).

**Project Type**: CLI application. This feature adds deployment and infrastructure artefacts.

**Performance Goals**: run-due starts ≤ 60 s after each quarter hour (SC-002); catch-up
≤ 2 min after boot (SC-003); a role run on a fresh host succeeds in one pass (SC-004).

**Constraints**: The only writable path for the services is `/var/lib/invio`. The env file is
`0600` and readable only by root and `invio`. No secrets in Ansible output or the journal. The
role is idempotent. Updates never interrupt a running service. Unit files pass
`systemd-analyze verify` with zero output.

**Scale/Scope**: one host, one installation, four units, one role (~6 task files), two Molecule
scenarios, one doc page.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Check | Status |
|---|---|---|
| I. Strict contracts | Role variables are validated before any change and every message names the field (FR-016); the env file is parsed by invio's `Settings` at each start (unknown keys warned, config errors exit 2 → failed unit); the unit directives are pinned by a contract test ([contracts/systemd-units.md](contracts/systemd-units.md)); env example keys are checked against `Settings` | PASS |
| II. CLI-first | Units only invoke existing CLI commands (`invio run-due` from #23, `invio notify retry`, `invio db upgrade`); exit codes pass through to systemd (FR-005); stdout and stderr separation kept (R9) | PASS |
| III. Test-covered | Static criteria (verify, directives, symlinks, env example, lint) run in every CI build. Provisioning, idempotence, permissions, DB, JSON journal, the update path, the timer schedule, catch-up after a systemd restart and sandbox probes run in Molecule (opt-in, triggered by `deploy/**`). Only the one-hour observation (SC-002) and the timed manual update (SC-009) are **manual** | DEVIATION (narrow; Complexity Tracking) |
| IV. Quality gates | Existing gates unchanged. New tools are locked in the `deploy` group and installed with `uv sync --locked --only-group deploy`. The new pytest module passes ruff and the default test run. The `deploy-static` job is added to CI, and `.pre-commit-config.yaml` gets matching `ansible-lint` and `verify-units` hooks (the latter skips visibly where `systemd-analyze` is missing) | PASS (platform limit in Complexity Tracking) |
| V. Secrets / observability | Env file `0600`; `no_log` on every secret task (ansible-lint `no-log-password` rule); DB password URL-encoded, never on a command line (`systemd-run -p EnvironmentFile`); JSON logs reach the journal under `SyslogIdentifier`; a journal secret scan in Molecule (SC-007) | PASS |
| Layering / tech constraints | No change to `src/`; no new runtime dependency (only the dev-only `deploy` group, justified in the PR) | PASS |
| Simplicity | No new CLI command, no fifth unit (db upgrade via `systemd-run`), no templated units (drop-in only for `MemoryMax`), no `.deb` packaging; extra hardening limited to options with no effect on behaviour | PASS |
| Workflow / docs | Branch `gh-issue-24`; new `docs/deployment.md` + README link in the same PR (user-facing operation changes) | PASS (planned) |

**Post-design re-check (after Phase 1, revised after `/speckit-analyze`)**: PASS, with one
narrow recorded deviation (III: two manual checks) and one recorded platform limit (IV: unit
verification can't run in pre-commit on macOS). The design adds no Python runtime code, no migration and no dependency to the
application. The issue's names were changed (`scout` → `invio`) by clarification Q1. The issue's
values were all kept. Additions beyond the issue are `AccuracySec=1s` (needed for SC-002), extra
hardening (R8) and `StateDirectory` (the edge case where the state directory is missing), each
justified in research.

## Project Structure

### Documentation (this feature)

```text
specs/015-gh-issue-24/
├── plan.md              # This file
├── research.md          # Phase 0 (R1–R15)
├── data-model.md        # Phase 1: host deployment entities and state transitions
├── quickstart.md        # Phase 1: validation scenarios 1–25 (+19a)
├── contracts/
│   ├── systemd-units.md # required/allowed/forbidden directives
│   ├── env-file.md      # location, permissions, format, keys
│   └── ansible-role.md  # variables, task order, tags, guarantees G1–G9
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks; not created here)
```

### Source Code (repository root)

```text
deploy/
├── systemd/
│   ├── invio-run-due.service
│   ├── invio-run-due.timer
│   ├── invio-notify-retry.service
│   └── invio-notify-retry.timer
├── env/
│   └── invio.env.example
├── scripts/
│   ├── verify-units.sh                 # systemd-analyze verify in a stub root, fails on any output;
│   │                                   #   --if-available for pre-commit on machines without systemd
│   ├── check-journal-json.py           # JSON log-line + secret check for journal output (US2)
│   └── check-no-secrets.sh             # fails if any test secret appears in a log file (FR-017)
└── ansible/
    ├── ansible.cfg
    ├── playbook.example.yml
    ├── requirements.yml                # community.mysql (production)
    ├── .ansible-lint                   # production profile + enable_list: [no-log-password]
    ├── roles/invio/
    │   ├── defaults/main.yml
    │   ├── vars/main.yml               # invio_units, invio_timers
    │   ├── meta/main.yml
    │   ├── handlers/main.yml           # daemon-reload
    │   ├── files/                      # symlinks → ../../../../systemd/*
    │   ├── templates/
    │   │   ├── invio.env.j2
    │   │   └── memory-override.conf.j2
    │   └── tasks/
    │       ├── main.yml
    │       ├── validate.yml
    │       ├── mariadb.yml
    │       ├── account.yml
    │       ├── config.yml
    │       ├── install.yml             # uv, python, git probe, update gate, checkout, sync, migrate
    │       └── units.yml
    └── molecule/
        ├── requirements.yml            # community.docker, community.mysql (test-only)
        ├── default/                    # managed MariaDB; Debian 12 + 13
        │   ├── molecule.yml
        │   ├── Dockerfile.j2
        │   ├── prepare.yml             # negative case: missing required variable
        │   ├── converge.yml
        │   ├── side_effect.yml         # the only side-effect playbook: memory drop-in, update path,
        │   │                           #   wait-for-running-service, catch-up after systemd restart
        │   └── verify.yml              # timers, schedule, permissions, DB, journal, sandbox probes
        └── external-db/                # invio_mariadb_manage_server: false + MariaDB service container
            ├── molecule.yml
            ├── prepare.yml             # negative case: unreachable DB host
            ├── converge.yml
            └── verify.yml

docs/
└── deployment.md                       # install (role / manual), operate, update, recover, manual checklist

tests/
├── test_deploy_units.py                # unit directive contract, role symlinks, env example, drop-in
└── test_deploy_journal_check.py        # check-journal-json.py behaviour

.github/workflows/
├── ci.yml                              # + job deploy-static
└── deploy.yml                          # opt-in Molecule (workflow_dispatch, paths: deploy/**)

.pre-commit-config.yaml                 # + local hooks ansible-lint, verify-units (constitution IV)
pyproject.toml / uv.lock                # + dependency group "deploy" (not default)
README.md                               # + "Deployment" pointer to docs/deployment.md
```

**Structure Decision**: Everything lives under the new top-level `deploy/` directory, which the
issue names (`deploy/systemd/`). Application packages under `src/invio/` are not touched. The
contract test lives in the existing flat `tests/` layout. The role is self-contained under
`deploy/ansible/roles/invio/` so that it can be copied into a DebOps project. Its unit files are
symlinks to the canonical `deploy/systemd/` files, and a test checks them (R11).

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Constitution III: two checks stay manual: four on-time activations observed over one real hour (SC-002 observation part) and an operator's timed manual update (SC-009 manual part) | The first needs an hour of wall-clock time; the second measures a person. The schedule itself (next trigger on the quarter hour), catch-up after a systemd restart (SC-003) and the sandbox probes (SC-008) **are** automated in Molecule (research R13) | Simulating time (`faketime`) doesn't affect systemd's timer logic. A one-hour CI job per deploy change is wasteful when the computed next trigger and `AccuracySec` are already asserted |
| Constitution IV: the `verify-units` pre-commit hook skips (visibly) on machines without `systemd-analyze` (macOS) | `systemd-analyze` only exists on Linux | Requiring Linux for every commit would block macOS developers. CI runs the check strictly on every PR, so nothing can merge unverified |
| Constitution III: provisioning tests (Molecule) are opt-in, not run on every PR | They need privileged Docker and take several minutes. Most PRs don't touch `deploy/` | Running them on every PR slows all PRs. The `paths: deploy/**` trigger runs them exactly when deployment files change |
| Unit start check depends on the unmerged #23 (R14) | `invio run-due` doesn't exist yet | A stub command could ship by accident; the guard is removed once #23 is merged |
