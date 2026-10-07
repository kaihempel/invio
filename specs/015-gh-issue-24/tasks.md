---

description: "Task list for gh-issue-24: unattended systemd deployment, hardening and Ansible provisioning"
---

# Tasks: Unattended systemd Deployment with Hardening and Ansible Provisioning

**Input**: Design documents from `specs/015-gh-issue-24/`

**Prerequisites**: [plan.md](plan.md), [spec.md](spec.md), [research.md](research.md),
[data-model.md](data-model.md), [contracts/](contracts/), [quickstart.md](quickstart.md)

**Tests**: Required. Constitution III and spec FR-022–FR-024 ask for automated checks for every
acceptance criterion that can be automated, plus a manual checklist for the rest (plan
Complexity Tracking). The test tasks come before the implementation tasks in each story. Write
them first and check that they fail.

**Organization**: Tasks are grouped by user story. Phases follow priority: US1, US2 and US5 (P1),
then US3 and US4 (P2), then US6 (P3).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: The user story the task belongs to (US1–US6)

## Path Conventions

Deployment artefacts live under `deploy/` at the repository root, tests in the flat `tests/`
directory, and docs in `docs/`. `src/invio/` is **not** changed by this feature.

Canonical names (FR-020): units `invio-run-due.service/.timer` and
`invio-notify-retry.service/.timer`, account `invio`, `/etc/invio/invio.env`, `/opt/invio`,
`/opt/invio/.venv/bin/invio`, `/var/lib/invio`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Directory layout, tool dependencies and lint configuration

- [X] T001 Create the directory skeleton from plan.md "Source Code": `deploy/systemd/`, `deploy/env/`, `deploy/scripts/`, `deploy/ansible/roles/invio/{defaults,meta,handlers,files,templates,tasks}/`, `deploy/ansible/molecule/{default,external-db}/` (add `.gitkeep` only where a directory would otherwise be empty at the end of this phase)
- [X] T002 Add a non-default dependency group `deploy` to `pyproject.toml` with `ansible-core>=2.17,<2.19`, `ansible-lint>=25`, `molecule>=25`, `molecule-plugins[docker]>=25`. Don't add it to `[tool.uv] default-groups`. Run `uv lock` and commit `uv.lock`. Check that `uv sync --locked` (dev only) still installs no Ansible packages (research R12)
- [X] T003 [P] Create `deploy/ansible/requirements.yml` listing the collection `community.mysql` with a pinned minimum version (production, research R10). Create `deploy/ansible/molecule/requirements.yml` listing `community.docker` and `community.mysql` (test-only; the Molecule Docker driver needs `community.docker`, research R13)
- [X] T004 [P] Create `deploy/ansible/.ansible-lint` with `profile: production`, `enable_list: [no-log-password]` (opt-in rule, research R12), `exclude_paths: [molecule/*/.cache, .collections]` and no skipped rules. Create `deploy/ansible/ansible.cfg` with `roles_path = roles` and `collections_path = ./.collections`, and add `deploy/ansible/.collections/` to `.gitignore`
- [X] T005 [P] Create `deploy/ansible/roles/invio/meta/main.yml`: role name `invio`, platforms Debian `bookworm` and `trixie`, `min_ansible_version: "2.17"`, dependencies `[]`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The verification harness that every story's tests use, the shared env example, and
the docs skeleton

**⚠️ CRITICAL**: No user story work can start until this phase is complete

- [X] T006 Create the test module `tests/test_deploy_units.py` with a helper `parse_unit(path) -> dict[str, dict[str, list[str]]]`: section → key → list of values. It must support repeated keys, `#`/`;` comments, and treat an empty assignment (`Key=`) as one empty value. Add a helper `assert_directives(unit, section, expected: dict[str, str | set[str]])`. A `set` compares whitespace-split values ignoring order. Add a constant `REPO = Path(__file__).resolve().parents[1]` and fixtures that load each unit from `deploy/systemd/`. Unit tests of the parser come first (repeated keys, comments, empty assignment)
- [X] T007 Create `deploy/scripts/verify-units.sh` (bash, `set -euo pipefail`). It creates a temporary root with `mktemp -d` and a `trap` cleanup, writes an executable stub `/opt/invio/.venv/bin/invio` (`#!/bin/sh\nexit 0`) into it, and copies every `deploy/systemd/*.service` and `*.timer` into `<root>/etc/systemd/system/`. It then runs `systemd-analyze verify --root=<root> <root>/etc/systemd/system/invio-*` and captures stdout and stderr. It exits 1 if the command fails **or prints anything** (SC-001), and exits 2 with a message if `systemd-analyze` is missing or there are no units. With the flag `--if-available`, a missing `systemd-analyze` instead prints `skipped: systemd-analyze not found` to stderr and exits 0 (for pre-commit on macOS; CI never passes the flag). Make it executable (research R12)
- [X] T008 Create `deploy/env/invio.env.example` with every key from contracts/env-file.md "Keys written by the role": `INVIO_DATABASE_URL`, `INVIO_SMTP_HOST`, `INVIO_SMTP_PORT`, `INVIO_SMTP_SECURITY`, `INVIO_SMTP_FROM`, `INVIO_SMTP_USER`, `INVIO_SMTP_PASSWORD`, `INVIO_MISTRAL_API_KEY`, `INVIO_OPENAI_API_KEY`, `INVIO_ANTHROPIC_API_KEY`, `INVIO_GOOGLE_API_KEY`, `INVIO_LOG_LEVEL`, `INVIO_HTTP_CONTACT`, `INVIO_ARCHIVE_DIR="/var/lib/invio/archive"`, `INVIO_HEALTHCHECK_URL`. Use the format `KEY="value"` with placeholders only (`change-me`, `example.com`), a header comment with the required permissions ("`invio:invio 0600` in `/etc/invio/`"), and no `INVIO_ENV_FILE`
- [X] T009 Add test `test_env_example_keys_are_known_settings` to `tests/test_deploy_units.py`. It monkeypatches out every `INVIO_*` variable from `os.environ`, calls `invio.config.settings.unknown_env_keys(REPO / "deploy/env/invio.env.example")` and asserts `[]`. Add test `test_env_example_has_only_placeholders`: every value is empty or contains `change-me`, `example.`, `/var/lib/invio` or is one of `587`, `starttls`, `INFO` (FR-012)
- [X] T010 Add the job `deploy-static` to `.github/workflows/ci.yml` (ubuntu-latest, on push and pull_request like `checks`). Steps: checkout; setup-uv (python 3.12); `uv sync --locked --group deploy`; `deploy/scripts/verify-units.sh`; `uv run ansible-galaxy collection install -r deploy/ansible/requirements.yml -p deploy/ansible/.collections`; `cd deploy/ansible && uv run ansible-lint`; `uv run pytest tests/test_deploy_units.py`. Leave the existing `checks` job unchanged: it already runs `tests/test_deploy_units.py` through `pytest --cov`. In the same task, add two local hooks to `.pre-commit-config.yaml` with the same configuration as CI (constitution IV):
  - `ansible-lint`: `entry: bash -c 'cd deploy/ansible && uv run --group deploy ansible-lint'`, `language: system`, `files: ^deploy/ansible/`, `pass_filenames: false`;
  - `verify-units`: `entry: deploy/scripts/verify-units.sh --if-available`, `language: system`, `files: ^deploy/systemd/`, `pass_filenames: false`.
- [X] T011 Create `docs/deployment.md` with headings only, to be filled by the stories: Overview, Requirements, Install with Ansible, Install manually, Configuration (env file), Operating (timers, logs), Updating (role / manual), Recovering from a failed update, Manual verification checklist, Troubleshooting

**Checkpoint**: `uv run pytest tests/test_deploy_units.py` runs (env tests pass; unit fixtures fail with "file not found" until US1), and `verify-units.sh` exits 2 ("no units")

---

## Phase 3: User Story 1 - Due jobs run on schedule without supervision (Priority: P1) 🎯 MVP

**Goal**: `invio-run-due.timer` triggers `invio-run-due.service` (oneshot `invio run-due` as
`invio`) every 15 minutes, catches up once after downtime, and never overlaps.

**Independent Test**: Copy the two units to a Debian host with invio at `/opt/invio` and
`/etc/invio/invio.env`. `systemctl enable --now invio-run-due.timer` → `systemctl list-timers`
shows the next quarter hour; `systemctl start invio-run-due.service` runs `run-due` as `invio`;
after a reboot the timer is still active (quickstart #1, #2, #10, #19, #20).

### Tests for User Story 1 ⚠️

- [ ] T012 [P] [US1] Add `test_run_due_service_contract` to `tests/test_deploy_units.py`. It asserts on `deploy/systemd/invio-run-due.service`, exactly as in contracts/systemd-units.md "Services (both)" and the run-due column:
  - `[Unit]`: `Description` non-empty, `Documentation=file:///opt/invio/docs/deployment.md`, `Wants=network-online.target`, `After={network-online.target, mariadb.service}`;
  - `[Service]`: `Type=oneshot`, `User=invio`, `Group=invio`, `EnvironmentFile=/etc/invio/invio.env` (no leading `-`), `WorkingDirectory=/var/lib/invio`, `ExecStart=/opt/invio/.venv/bin/invio run-due`, `StateDirectory=invio`, `StateDirectoryMode=0750`, `ReadWritePaths=/var/lib/invio`, `MemoryMax=1G`, `TimeoutStartSec=3h`, `UMask=0027`;
  - no `Restart`, no `SuccessExitStatus`, no `[Install]` section (FR-001, FR-004, FR-005, FR-007, FR-008, FR-020).
- [ ] T013 [P] [US1] Add `test_run_due_timer_contract` to `tests/test_deploy_units.py`: `OnCalendar=*:0/15`, `Persistent=true`, `RandomizedDelaySec=60`, `AccuracySec=1s`, `Unit=invio-run-due.service`, `[Install] WantedBy=timers.target` (FR-002, FR-004, SC-002; research R7)

### Implementation for User Story 1

- [ ] T014 [P] [US1] Create `deploy/systemd/invio-run-due.service` with the `[Unit]` and `[Service]` directives listed in T012 (the hardening directives come in US4, the journal directives in US2). Add a short comment block at the top: purpose, "installed verbatim by the role", and "override `MemoryMax` only via a drop-in"
- [ ] T015 [P] [US1] Create `deploy/systemd/invio-run-due.timer` with the directives from T013 and a comment: "missed triggers run once after boot (Persistent); AccuracySec=1s keeps starts within 60 s of the quarter hour"
- [ ] T016 [US1] Run `deploy/scripts/verify-units.sh` locally (Linux or CI) and fix any warning until it exits 0 with no output. Check that T012 and T013 pass
- [ ] T017 [US1] Fill `docs/deployment.md` "Install manually" with a complete installation without Ansible (FR-019), mirroring the role's steps:
  1. `apt install git ca-certificates mariadb-server`;
  2. create the database and user in SQL (`CREATE DATABASE invio CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; CREATE USER 'invio'@'localhost' IDENTIFIED BY '…'; GRANT ALL ON invio.* TO 'invio'@'localhost';`);
  3. `useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/invio --no-create-home invio`;
  4. install the pinned uv to `/usr/local/bin`; `UV_PYTHON_INSTALL_DIR=/opt/invio-python uv python install 3.12`;
  5. `git clone` + checkout of a tag into `/opt/invio`; `uv sync --locked --no-dev --no-editable --compile-bytecode` with the same `UV_*` environment as the role (research R1, R3);
  6. `/etc/invio` (`root:invio 0750`) and `invio.env` from `deploy/env/invio.env.example` (`invio:invio 0600`);
  7. `invio db upgrade` through `systemd-run` (research R5);
  8. copy the units to `/etc/systemd/system/`, `systemctl daemon-reload`, `systemctl enable --now invio-run-due.timer`. Fill "Operating" with `systemctl list-timers 'invio-*'`, `systemctl status invio-run-due.service`, and what a failed run looks like (exit code shown, next trigger still fires; FR-005). Add a note that `run-due` comes from #23

**Checkpoint**: The run-due units exist, pass verify and the contract tests, and can be installed by hand

---

## Phase 4: User Story 2 - Run logs are readable in the system journal (Priority: P1)

**Goal**: Each service's output is in the journal under its own identifier. Every invio log
line is one JSON object, and no secrets appear.

**Independent Test**: After one run-due start,
`journalctl -u invio-run-due -o cat | deploy/scripts/check-journal-json.py` exits 0
(quickstart #11).

### Tests for User Story 2 ⚠️

- [ ] T018 [P] [US2] Extend `test_run_due_service_contract` in `tests/test_deploy_units.py`: `SyslogIdentifier=invio-run-due`, and `Environment` (all values merged) equals the set `{PYTHONDONTWRITEBYTECODE=1, PYTHONUNBUFFERED=1, HOME=/var/lib/invio, XDG_CACHE_HOME=/var/lib/invio/cache}`. Assert there is no `StandardOutput` or `StandardError` override (journal is the default; research R9)
- [ ] T019 [P] [US2] Create `tests/test_deploy_journal_check.py`. It runs `deploy/scripts/check-journal-json.py` as a subprocess (`sys.executable`) on fixture input and covers four cases. (a) Mixed JSON log lines and plain stdout lines (`nothing to retry`) → exit 0. (b) A line starting with `{` that isn't valid JSON → exit 1, and the message names the line number. (c) A JSON line without `level` or `message` → exit 1. (d) A secret passed via env `INVIO_CHECK_SECRETS="pw1\npw2"` that appears in any line (JSON or not) → exit 1, and the output does **not** print the secret. Also: empty input → exit 1 ("no log lines")

### Implementation for User Story 2

- [ ] T020 [US2] Add `SyslogIdentifier=invio-run-due` and the `Environment=` line(s) from T018 to `deploy/systemd/invio-run-due.service`
- [ ] T021 [US2] Create `deploy/scripts/check-journal-json.py` (stdlib only, Python ≥ 3.11, executable, `#!/usr/bin/env python3`). It reads stdin. Lines starting with `{` must parse as JSON objects with the keys `level` and `message`. Other lines are allowed (stdout summaries, R9). It fails if no JSON line was seen, or if any secret from the newline-separated `INVIO_CHECK_SECRETS` env appears in any line. When it reports a secret hit, it prints only the line number, never the secret. Exit 0 or 1. It must pass ruff and the existing ruff config (add `deploy/scripts` to ruff's `src` only if needed for import sorting)
- [ ] T022 [US2] Fill `docs/deployment.md` "Operating → Logs": `journalctl -u invio-run-due -o cat`, filtering by run with `| grep '"run_id": "…"'`, the JSON check script, and the note that stdout summary lines are plain text by design (constitution II)

**Checkpoint**: The journal contract and its checker are in place and tested

---

## Phase 5: User Story 5 - One Ansible run provisions a working installation (Priority: P1)

**Goal**: The role `invio` provisions a fresh Debian 12 or 13 host in one run (MariaDB, account,
env file, uv, Python, checkout, sync, migration, units, timers). It is idempotent, validates its
inputs first and never shows secrets.

**Independent Test**: `cd deploy/ansible && uv run molecule test` (scenario `default`, Debian 12
and 13) passes converge, idempotence and verify; `uv run molecule test -s external-db` passes
(quickstart #5–#9, #13, #14, #17, #18).

### Tests for User Story 5 ⚠️

- [ ] T023 [P] [US5] Add `test_role_unit_files_are_symlinks_to_canonical_units` to `tests/test_deploy_units.py`. For every file in `deploy/systemd/`, `deploy/ansible/roles/invio/files/<name>` exists, is a **relative** symlink, and `resolve()` equals the canonical file. The role's `files/` directory has no extra unit files (research R11, G9)
- [ ] T024 [P] [US5] Create `deploy/ansible/molecule/default/molecule.yml`:
  - driver `docker`;
  - platforms `debian12` and `debian13`, built from `Dockerfile.j2` with `privileged: true`, `cgroupns_mode: host`, volumes `/sys/fs/cgroup:/sys/fs/cgroup:rw` and `${MOLECULE_PROJECT_DIRECTORY}/../..:/src:ro`, `command: /lib/systemd/systemd`, `pre_build_image: false`;
  - provisioner `ansible` with `env: ANSIBLE_ROLES_PATH: ../../roles` and inventory group_vars that set test values for every required variable (`invio_git_repo: /src`, `invio_git_version: "{{ lookup('env', 'INVIO_TEST_REF') }}"`, dummy SMTP host `127.0.0.1`, `invio_http_contact: ci@example.invalid`, `invio_llm_api_keys: {mistral: ci-secret-mistral}`, `invio_db_password: ci-secret-db`);
  - `dependency: {name: galaxy, options: {requirements-file: ../requirements.yml}}`;
  - test sequence `dependency, create, prepare, converge, idempotence, side_effect, verify, destroy`, with `provisioner.playbooks.side_effect: side_effect.yml`.

  Molecule runs only one side-effect playbook per scenario, so create `deploy/ansible/molecule/default/side_effect.yml` here. Its first section is the **catch-up check** (US1 scenario 3, SC-003, research R13):
  1. `systemctl stop invio-run-due.timer`;
  2. `touch -d '-1 hour' /var/lib/systemd/timers/stamp-invio-run-due.timer`;
  3. restart the container from the controller (`delegate_to: localhost`, `community.docker.docker_container name={{ inventory_hostname }} state=started restart=true`), then `wait_for_connection`;
  4. assert that `journalctl -b -u invio-run-due.service` shows exactly one start within 120 s of boot, and that both timers are `active`.

  Later stories (US4, US6) append their own sections to this file.
- [ ] T025 [P] [US5] Create `deploy/ansible/molecule/default/Dockerfile.j2`: `FROM {{ item.image }}` (debian:12 / debian:13), `apt-get install -y systemd systemd-sysv python3 sudo ca-certificates`, `git config --system --add safe.directory /src`, remove `/lib/systemd/system/multi-user.target.wants/*` getty units, `STOPSIGNAL SIGRTMIN+3`
- [ ] T026 [US5] Create `deploy/ansible/molecule/default/verify.yml`. It asserts:
  1. `systemctl is-enabled` and `is-active` are `enabled` and `active` for both timers present at this point;
  2. `stat /etc/invio/invio.env` gives owner `invio`, group `invio`, mode `0600`, and `/etc/invio` gives `root:invio 0750`;
  3. `sudo -u nobody cat /etc/invio/invio.env` fails;
  4. `mysql -N -e "SELECT DEFAULT_CHARACTER_SET_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME='invio'"` returns `utf8mb4`;
  5. `SHOW GRANTS FOR 'invio'@'localhost'` contains only `invio`.* grants (plus USAGE);
  6. `systemd-run --wait --pipe --collect --uid=invio --gid=invio -p EnvironmentFile=/etc/invio/invio.env /opt/invio/.venv/bin/invio db upgrade` exits 0 and prints `database at revision <R>`, where `<R>` is the newest revision file under `/opt/invio/src/invio/db/migrations/versions/` (or the head reported by `alembic heads` via the same `systemd-run`); a second call prints the same revision;
  7. the guarded run-due start (only if `invio run-due --help` succeeds, otherwise `debug: msg="skipped: requires #23"`, research R14): `systemctl start invio-run-due.service` succeeds and `systemctl show -p Result` is `success`;
  8. `journalctl -u invio-run-due -o cat` piped to the copied `check-journal-json.py` with `INVIO_CHECK_SECRETS` set to the test secrets exits 0;
  9. timer schedule: `systemctl show invio-run-due.timer -p NextElapseUSecRealtime --value`, parsed with `date -d`, has minute in {0, 15, 30, 45} and seconds ≤ 60 past that minute (SC-002 schedule part; quickstart #8–#11, #19).
- [ ] T027 [US5] Create `deploy/ansible/molecule/default/converge.yml` (hosts all, become, `roles: [invio]`). Also add a negative check to `deploy/ansible/molecule/default/prepare.yml`: run the role with `invio_db_password` undefined via `ansible.builtin.include_role` inside `block/rescue`, then assert that the rescue happened, that the failure message contains `invio_db_password`, and that `/etc/invio` does not exist (FR-016, quickstart #13)
- [ ] T028 [P] [US5] Create `deploy/scripts/check-no-secrets.sh` (bash, `set -euo pipefail`, executable). Usage: `check-no-secrets.sh <logfile> <secret>...`. It exits 1 if any secret occurs in the log file (`grep -F`), and prints only the index of the matching secret, never the secret itself. Otherwise it exits 0. T042 runs it on a verbose converge log with the Molecule test secrets `ci-secret-db`, `ci-secret-mistral` and `ci-secret-admin` (FR-017, quickstart #14)
- [ ] T029 [P] [US5] Create `deploy/ansible/molecule/external-db/molecule.yml`:
  - platforms: a Debian 12 target (same image settings as T024) and a `mariadb:11.4` container `dbserver` on a shared Docker network `molecule-invio`, with `MARIADB_ROOT_PASSWORD=ci-secret-admin`;
  - group_vars: `invio_mariadb_manage_server: false`, `invio_db_host: dbserver`, `invio_db_user_host: "%"`, `invio_db_admin_user: root`, `invio_db_admin_password: ci-secret-admin`;
  - a `converge.yml` that applies the role to the target only.

  Add `verify.yml`: no `mariadb-server` package installed on the target (`dpkg -s` fails), and the database and user exist on `dbserver`. Add a negative block in `prepare.yml`: the role with `invio_db_host: no-such-host` fails in `mariadb.yml` with a message containing `no-such-host`, and `/etc/systemd/system/invio-run-due.timer` does not exist (quickstart #17, #18)

### Implementation for User Story 5

- [ ] T030 [P] [US5] Create `deploy/ansible/roles/invio/defaults/main.yml` with every default from contracts/ansible-role.md "Defaults":
  - `invio_user: invio`, `invio_group: invio`, `invio_mariadb_manage_server: true`;
  - `invio_db_host: localhost`, `invio_db_port: 3306`, `invio_db_name: invio`, `invio_db_user: invio`, `invio_db_user_host: localhost`;
  - `invio_uv_version` and `invio_uv_sha256` (pinned to the version used by `astral-sh/setup-uv` in CI; fill in the real checksum of `uv-x86_64-unknown-linux-gnu.tar.gz`), `invio_python_version: "3.12"`;
  - `invio_smtp_port: 587`, `invio_smtp_security: starttls`, `invio_log_level: INFO`, `invio_env_extra: {}`;
  - `invio_run_due_memory_max: 1G`, `invio_notify_retry_memory_max: 256M`, `invio_update_wait_timeout: 11100`.

  Optional variables (`invio_git_key_file`, `invio_smtp_user`, `invio_smtp_password`, `invio_healthcheck_url`, `invio_db_admin_user`, `invio_db_admin_password`) are documented in comments and left undefined. Add a comment that `invio_user`/`invio_group` are fixed by the unit files and are not meant to be changed
- [ ] T031 [P] [US5] Create `deploy/ansible/roles/invio/tasks/validate.yml` (tag `invio:validate`, no changes). Use `ansible.builtin.assert` with `fail_msg` naming each variable:
  - `invio_git_repo`, `invio_git_version`, `invio_db_password`, `invio_smtp_host`, `invio_smtp_from` and `invio_http_contact` are defined and non-empty;
  - `invio_llm_api_keys` is a mapping with at least one non-empty value, and its keys are a subset of `mistral, openai, anthropic, google`;
  - every `invio_env_extra` key matches `^INVIO_[A-Z0-9_]+$`;
  - `invio_db_admin_user` and `invio_db_admin_password` are set when `not invio_mariadb_manage_server and invio_db_host != 'localhost'`;
  - no env-bound string contains `\n`;
  - `ansible_distribution == 'Debian'` and `ansible_distribution_major_version in ['12', '13']`.

  Mark tasks that reference secret variables `no_log: true` (FR-016, FR-017)
- [ ] T032 [P] [US5] Create `deploy/ansible/roles/invio/tasks/mariadb.yml` (tag `invio:mariadb`, all DB tasks `no_log: true`).
  - When `invio_mariadb_manage_server`: `apt` install `mariadb-server` and `python3-pymysql`, then `service mariadb started enabled`. Set login facts for `login_unix_socket: /run/mysqld/mysqld.sock`.
  - Otherwise: `apt` install `python3-pymysql` only; `wait_for host={{ invio_db_host }} port={{ invio_db_port }} timeout=10` with `fail_msg` "MariaDB server {{ invio_db_host }}:{{ invio_db_port }} is not reachable"; login via `invio_db_admin_user`/`invio_db_admin_password`.
  - Then `community.mysql.mysql_db name={{ invio_db_name }} encoding=utf8mb4 collation=utf8mb4_unicode_ci state=present`, and `community.mysql.mysql_user name={{ invio_db_user }} host={{ invio_db_user_host }} password={{ invio_db_password }} priv="{{ invio_db_name }}.*:ALL" update_password=always state=present` (idempotent: it changes only when the password differs). Never use `state: absent` (research R10, G4, G8)
- [ ] T033 [P] [US5] Create `deploy/ansible/roles/invio/tasks/account.yml`: `group name=invio system=true`; `user name=invio group=invio system=true shell=/usr/sbin/nologin home=/var/lib/invio create_home=false` (data-model "Service account")
- [ ] T034 [P] [US5] Create `deploy/ansible/roles/invio/templates/invio.env.j2`. It writes the `KEY="value"` lines from contracts/env-file.md using a macro `q(v)` that escapes `\` → `\\` and `"` → `\"`. `INVIO_DATABASE_URL` is built as `mysql+pymysql://{{ invio_db_user | urlencode }}:{{ invio_db_password | urlencode }}@{{ invio_db_host }}:{{ invio_db_port }}/{{ invio_db_name }}?charset=utf8mb4`; when `invio_db_host == 'localhost'` and the server is managed, append `&unix_socket=/run/mysqld/mysqld.sock`. Write one key per entry in `invio_llm_api_keys` (`INVIO_<PROVIDER>_API_KEY`), the optional keys only when defined, `INVIO_ARCHIVE_DIR="/var/lib/invio/archive"`, then the `invio_env_extra` entries sorted by key. Add a header comment "Managed by Ansible role invio — do not edit"
- [ ] T035 [US5] Create `deploy/ansible/roles/invio/tasks/config.yml` (tag `invio:config`): `file path=/etc/invio state=directory owner=root group=invio mode=0750`; `template src=invio.env.j2 dest=/etc/invio/invio.env owner=invio group=invio mode=0600` with `no_log: true` (contracts/env-file.md "Location and permissions"; depends on T033, T034)
- [ ] T036 [US5] Create `deploy/ansible/roles/invio/tasks/install.yml` (tag `invio:install`) for the first-install path:
  1. `apt` install `git ca-certificates`; `file path=/var/cache/invio-uv state=directory owner=root group=root mode=0700` (data-model "Installation tree");
  2. `get_url` the uv tarball pinned by `invio_uv_version` with `checksum: sha256:{{ invio_uv_sha256 }}` to `/var/cache/invio-uv/`, then `unarchive` `uv` to `/usr/local/bin` (`creates` guard keyed by version);
  3. `command: uv python install {{ invio_python_version }}` with env `UV_PYTHON_INSTALL_DIR=/opt/invio-python`, and `changed_when` on "Installed" in stderr;
  4. `git repo={{ invio_git_repo }} dest=/opt/invio version={{ invio_git_version }} key_file={{ invio_git_key_file | default(omit) }}` (owned by root, directory `0755`), registered as `invio_checkout`;
  5. `command: uv sync --locked --no-dev --no-editable --compile-bytecode` with `chdir=/opt/invio` and env `UV_PROJECT_ENVIRONMENT=/opt/invio/.venv UV_PYTHON_INSTALL_DIR=/opt/invio-python UV_PYTHON_PREFERENCE=only-managed UV_PYTHON={{ invio_python_version }} UV_CACHE_DIR=/var/cache/invio-uv`, and `changed_when: "'Installed' in r.stderr or 'Uninstalled' in r.stderr"`;
  6. `slurp` `/etc/invio/deployed-revision` (`failed_when: false`) and compare it with `invio_checkout.after`;
  7. when they differ: `command: systemd-run --wait --pipe --collect --quiet --uid=invio --gid=invio -p EnvironmentFile=/etc/invio/invio.env -p WorkingDirectory=/var/lib/invio -p StateDirectory=invio -E PYTHONDONTWRITEBYTECODE=1 /opt/invio/.venv/bin/invio db upgrade` (`no_log: false` is fine: invio scrubs the URL), then `copy content="{{ invio_checkout.after }}\n" dest=/etc/invio/deployed-revision owner=root mode=0644`.

  Research R1–R5
- [ ] T037 [US5] Create the role's unit symlinks `deploy/ansible/roles/invio/files/invio-run-due.service` → `../../../../systemd/invio-run-due.service` and `files/invio-run-due.timer` → `../../../../systemd/invio-run-due.timer` (relative, `ln -s`). Create `deploy/ansible/roles/invio/vars/main.yml` with `invio_units: [invio-run-due.service, invio-run-due.timer]` and `invio_timers: [invio-run-due.timer]` (US3 extends both lists)
- [ ] T038 [US5] Create `deploy/ansible/roles/invio/handlers/main.yml` (`systemd daemon_reload: true`, listen `invio daemon-reload`) and `deploy/ansible/roles/invio/tasks/units.yml` (tag `invio:units`): `copy src={{ item }} dest=/etc/systemd/system/{{ item }} owner=root group=root mode=0644` over `invio_units`, notifying the handler; `meta: flush_handlers`; `systemd name={{ item }} enabled=true state=started` over `invio_timers` (FR-004, G9)
- [ ] T039 [US5] Create `deploy/ansible/roles/invio/tasks/main.yml` that imports, in order, `validate.yml`, `mariadb.yml`, `account.yml`, `config.yml`, `install.yml`, `units.yml`, each tagged `invio` plus its own tag (contracts/ansible-role.md "Task order", "Tags")
- [ ] T040 [P] [US5] Create `deploy/ansible/playbook.example.yml`: hosts `invio_servers`, `become: true`, `roles: [invio]`, with commented example vars. Secrets are referenced from `vault_*` variables (Ansible Vault), with a comment on running it with DebOps (`invio_mariadb_manage_server: false` when `debops.mariadb` manages the server)
- [ ] T041 [US5] Run `cd deploy/ansible && uv run ansible-lint` until it's clean. Then `INVIO_TEST_REF=$(git rev-parse HEAD) uv run molecule test` for `default` and `external-db` until converge, idempotence (`changed=0`, FR-015/SC-005) and verify pass on Debian 12 and 13. Fix the role, not the tests
- [ ] T042 [US5] Create `.github/workflows/deploy.yml`:
  - triggers: `workflow_dispatch`, and `pull_request` + `push` with `paths: ["deploy/**", ".github/workflows/deploy.yml", "tests/test_deploy_*.py"]`;
  - job `molecule` (ubuntu-latest) with a matrix over scenario `[default, external-db]`;
  - steps: checkout with `fetch-depth: 0`; setup-uv; `uv sync --locked --only-group deploy`; install the collections; `INVIO_TEST_REF=${{ github.sha }}`; a secret-leak converge (`uv run molecule converge -s ${{ matrix.scenario }} -- -v 2>&1 | tee converge.log`, then `deploy/scripts/check-no-secrets.sh converge.log ci-secret-db ci-secret-mistral ci-secret-admin`, then `uv run molecule destroy -s ${{ matrix.scenario }}`); `uv run molecule test -s ${{ matrix.scenario }}`.

  Don't add it to required checks (FR-023)
- [ ] T043 [US5] Fill `docs/deployment.md` "Requirements", "Install with Ansible" (requirements.yml, inventory variables table from contracts/ansible-role.md, Vault for secrets, DebOps notes, the server switch) and "Configuration (env file)" (path, permissions, format and escaping, `invio:config` tag to update secrets only, no restart needed for oneshot services)

**Checkpoint**: A fresh Debian host is provisioned by one role run, with run-due scheduled. This plus US1 and US2 is the MVP

---

## Phase 6: User Story 3 - Failed notifications are retried automatically (Priority: P2)

**Goal**: `invio-notify-retry.timer` runs `invio notify retry` once per hour as `invio`, and
catches up once after downtime.

**Independent Test**: Install the units, mark a notification `failed`, run
`systemctl start invio-notify-retry.service` and check that the row is `sent`.
`systemctl list-timers` shows an hourly trigger (quickstart #12).

### Tests for User Story 3 ⚠️

- [ ] T044 [P] [US3] Add `test_notify_retry_service_contract` to `tests/test_deploy_units.py`. It has the same assertions as T012 and T018 for `deploy/systemd/invio-notify-retry.service`, with `ExecStart=/opt/invio/.venv/bin/invio notify retry`, `SyslogIdentifier=invio-notify-retry`, `MemoryMax=256M`, `TimeoutStartSec=15min`. Factor the shared base expectations into one dict used by both service tests
- [ ] T045 [P] [US3] Add `test_notify_retry_timer_contract`: `OnCalendar=hourly`, `Persistent=true`, `RandomizedDelaySec=300`, no `AccuracySec`, `Unit=invio-notify-retry.service`, `WantedBy=timers.target` (FR-003, research R7)
- [ ] T046 [US3] Extend `deploy/ansible/molecule/default/verify.yml`. `invio-notify-retry.timer` must be enabled and active. `systemctl start invio-notify-retry.service` succeeds, and `journalctl -u invio-notify-retry -o cat` contains `nothing to retry` and passes `check-journal-json.py` (quickstart #12)

### Implementation for User Story 3

- [ ] T047 [P] [US3] Create `deploy/systemd/invio-notify-retry.service`, mirroring `invio-run-due.service` (same `[Unit]`, same base `[Service]` keys, `Environment`), with the values from T044
- [ ] T048 [P] [US3] Create `deploy/systemd/invio-notify-retry.timer` with the values from T045
- [ ] T049 [US3] Add relative symlinks `deploy/ansible/roles/invio/files/invio-notify-retry.service` and `.timer` → `../../../../systemd/…`. Extend `invio_units` and `invio_timers` in `deploy/ansible/roles/invio/vars/main.yml` with the two units and the timer
- [ ] T050 [US3] Run `deploy/scripts/verify-units.sh` (must print nothing), `uv run pytest tests/test_deploy_units.py`, and `molecule test` (default). Document the notify-retry timer in `docs/deployment.md` "Install manually" and "Operating" (exit code 1 when a notification failed or was given up shows as a failed unit; the next hour retries)

**Checkpoint**: Both timers are installed by hand or by the role; failed mails are resent hourly

---

## Phase 7: User Story 4 - Services run locked down (Priority: P2)

**Goal**: Both services run with the issue's hardening plus the vetted extra set. Memory
ceilings and timeouts are enforced and can be changed through a role drop-in.

**Independent Test**: `systemctl show invio-run-due.service -p ProtectSystem,ProtectHome,NoNewPrivileges,MemoryMax,TimeoutStartUSec` shows the contract values. The sandbox probes from `docs/deployment.md` "Manual verification" behave as specified (quickstart #21, #22).

### Tests for User Story 4 ⚠️

- [ ] T051 [P] [US4] Extend the shared service base in `tests/test_deploy_units.py` with the hardening directives from contracts/systemd-units.md:
  - required: `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`, `PrivateTmp=yes`;
  - additional: `PrivateDevices=yes`, `ProtectKernelTunables=yes`, `ProtectKernelModules=yes`, `ProtectKernelLogs=yes`, `ProtectControlGroups=yes`, `ProtectClock=yes`, `ProtectHostname=yes`, `RestrictSUIDSGID=yes`, `RestrictRealtime=yes`, `RestrictNamespaces=yes`, `LockPersonality=yes`, `SystemCallArchitectures=native`, `RestrictAddressFamilies={AF_UNIX, AF_INET, AF_INET6}`, `CapabilityBoundingSet=` (empty).

  Add `test_services_have_no_unlisted_directives`: every `[Service]` key in both services is in the allowlist (base + journal + hardening). None of the forbidden keys `MemoryDenyWriteExecute`, `PrivateNetwork`, `IPAddressDeny`, `DynamicUser` appears, `User` isn't `root`, and no `ExecStart*` value starts with `+` or `!` (contracts/systemd-units.md "Forbidden")
- [ ] T052 [P] [US4] Add `test_memory_override_template` to `tests/test_deploy_units.py`. It renders `deploy/ansible/roles/invio/templates/memory-override.conf.j2` with Jinja2 (already a runtime dependency) for `memory_max="2G"`, parses the result with `parse_unit`, and asserts that the only content is `[Service] MemoryMax=2G` (contracts/systemd-units.md "Role overrides")
- [ ] T053 [US4] Extend `deploy/ansible/molecule/default/verify.yml` and `side_effect.yml`:
  - verify: `systemctl show -p MemoryMax,TimeoutStartUSec,NoNewPrivileges,ProtectSystem,ProtectHome,PrivateTmp` for both services, compared with the contract values (`MemoryMax=1073741824` / `268435456`, `TimeoutStartUSec=3h` / `15min`).
  - verify, **sandbox probes** (SC-008, US4 scenarios 1–3 and 6, research R13): run `systemd-run --wait --pipe --collect --uid=invio --gid=invio` with every sandbox property of `invio-run-due.service` passed as `-p` (`NoNewPrivileges`, `ProtectSystem`, `ProtectHome`, `PrivateTmp`, `ReadWritePaths`, `StateDirectory`, `PrivateDevices`, `RestrictSUIDSGID`, …, read from the unit file with the same key list as the contract test). Probes: `touch /opt/invio/x` and `touch /etc/x` must fail; `ls -A /home` must print nothing; `touch /var/lib/invio/probe` must succeed (remove it afterwards); `sudo -n true` must fail.
  - `side_effect.yml`, appended section: converge with `invio_run_due_memory_max: 2G`; assert that `/etc/systemd/system/invio-run-due.service.d/50-invio-role.conf` exists and `MemoryMax=2147483648`; converge with the default and assert that the drop-in is removed.

### Implementation for User Story 4

- [ ] T054 [P] [US4] Add the hardening directives from T051 to `deploy/systemd/invio-run-due.service`, grouped under a comment `# Sandbox (issue #24 + research R8)`
- [ ] T055 [P] [US4] Add the same hardening block to `deploy/systemd/invio-notify-retry.service`
- [ ] T056 [P] [US4] Create `deploy/ansible/roles/invio/templates/memory-override.conf.j2`: a comment "Managed by Ansible role invio", then `[Service]` and `MemoryMax={{ memory_max }}`
- [ ] T057 [US4] Extend `deploy/ansible/roles/invio/tasks/units.yml`. For each of `(invio-run-due.service, invio_run_due_memory_max, 1G)` and `(invio-notify-retry.service, invio_notify_retry_memory_max, 256M)`: when the value differs from the default, create `/etc/systemd/system/<svc>.d/` (`0755`) and template `50-invio-role.conf`; otherwise `file state=absent` on that drop-in. Both notify `invio daemon-reload` (research R11)
- [ ] T058 [US4] Run `deploy/scripts/verify-units.sh` (no output), the pytest module and `molecule test` (default). In `docs/deployment.md` "Operating", document how an operator can repeat the automated sandbox probes on their own host (the same `systemd-run` command as T053) and `systemd-analyze security invio-run-due.service` for information. These are optional for operators; CI already covers them (quickstart #21, #22)

**Checkpoint**: Both services are sandboxed, the limits are enforced, and the overrides work

---

## Phase 8: User Story 6 - Updating an installation is safe and repeatable (Priority: P3)

**Goal**: Re-running the role with a new `invio_git_version` stops both timers, waits for
running services, deploys, migrates and restarts. An unchanged ref doesn't touch the timers,
and any failure leaves them stopped. The manual procedure is documented.

**Independent Test**: Molecule `side_effect` converges the parent commit and then the original
commit. `deployed-revision` follows, the timers are active again, and the idempotence run shows
the timers' `ActiveEnterTimestamp` unchanged (quickstart #15, #16, #23, #24).

### Tests for User Story 6 ⚠️

- [ ] T059 [US6] Append an **update** section to `deploy/ansible/molecule/default/side_effect.yml` (the single side-effect playbook from T024):
  1. record `ActiveEnterTimestamp` of both timers;
  2. converge with `invio_git_version: "{{ lookup('env','INVIO_TEST_REF') }}~1"` (resolve it to a SHA with `git -C /src rev-parse`);
  3. assert the timers' `ActiveEnterTimestamp` changed (they were restarted), `/etc/invio/deployed-revision` equals the parent SHA, and both timers are active;
  4. converge back to `INVIO_TEST_REF` and assert `deployed-revision` equals that SHA;
  5. converge again unchanged and assert the timers' `ActiveEnterTimestamp` is unchanged and the play has `changed=0` (FR-021, US6 scenarios 1 and 5).

  Also add a failure case: converge with `invio_git_version: does-not-exist` inside `block/rescue`. Assert that it failed in the check-mode probe, that both timers are still `active` with an unchanged `ActiveEnterTimestamp`, and that `deployed-revision` is unchanged (spec edge case, contract G7, quickstart #25)
- [ ] T060 [US6] Append a "wait for running service" section to `deploy/ansible/molecule/default/side_effect.yml`. Start a transient long-running unit masquerading as the service: a drop-in that replaces `ExecStart` with `/bin/sleep 20`, applied with `systemctl start --no-block invio-run-due.service`. Then converge with the parent ref and assert that the converge's checkout happened after the service became inactive (compare `ExecMainExitTimestamp` with the mtime of `/etc/invio/deployed-revision`). Remove the drop-in afterwards (US6 scenario 3)

### Implementation for User Story 6

- [ ] T061 [US6] Extend `deploy/ansible/roles/invio/tasks/install.yml` with the update gate before the real checkout (research R4, contracts G5–G7):
  1. a `git` task with the same arguments and `check_mode: true`, registered as `invio_checkout_probe`;
  2. `stat /etc/systemd/system/invio-run-due.timer`, registered as `invio_installed`;
  3. when `invio_checkout_probe.changed and invio_installed.stat.exists`: `systemd name={{ item }} state=stopped` over `invio_timers`, then a `command: systemctl is-active {{ invio_units | select('match', '.*\\.service$') | join(' ') }}` loop with `register`, `until: r.stdout_lines | intersect(['active', 'activating']) | length == 0`, `retries: "{{ (invio_update_wait_timeout / 10) | int }}"`, `delay: 10`, `failed_when: false`, `changed_when: false`, followed by an assert that the wait succeeded ("services still running after {{ invio_update_wait_timeout }} s").

  The existing `units.yml` starts the timers at the end, so a failure in any step between them leaves the timers stopped (FR-021)
- [ ] T062 [US6] Make the `uv sync` and migration steps in `deploy/ansible/roles/invio/tasks/install.yml` run on unchanged refs **without** reporting changes. `uv sync` keeps its `changed_when`. The migration runs only when `deployed-revision` differs from `invio_checkout.after`. With an unchanged ref, `units.yml` `state: started` on already-started timers reports `ok` (G6)
- [ ] T063 [US6] Fill `docs/deployment.md`:
  - "Updating → With Ansible": set `invio_git_version` to the new tag and run the playbook; what the role does, in order.
  - "Updating → Manually": `systemctl stop invio-run-due.timer invio-notify-retry.timer`; wait until `systemctl is-active invio-run-due.service invio-notify-retry.service` shows no `active`/`activating`; `git -C /opt/invio fetch --tags && git -C /opt/invio checkout <tag>`; `uv sync --locked --no-dev --no-editable --compile-bytecode` with the same `UV_*` environment as the role; `systemd-run … invio db upgrade` (the same command as the role); `systemctl daemon-reload`; copy the units again if they changed; `systemctl start` both timers; verify with `systemctl list-timers` and one manual start.
  - "Recovering from a failed update": the timers stay stopped; `git -C /opt/invio checkout <previous tag>` + `uv sync`; MariaDB DDL isn't transactional, so check the Alembic revision (`alembic current` via `systemd-run`) and fix partially applied steps by hand; restore from backup if needed; restart the timers (FR-019, US6 scenario 4)

**Checkpoint**: Updates are safe through the role and documented for manual use

---

## Phase 9: Polish & Cross-Cutting Concerns

- [ ] T064 [P] Add a "Deployment" section to `README.md` (two or three sentences plus a link to `docs/deployment.md` and `deploy/`), and list `deploy/` in the README "Layout" section (constitution: docs in the same PR)
- [ ] T065 [P] Fill `docs/deployment.md` "Overview" (the four units, the account, paths from data-model.md "Installation tree"), "Troubleshooting" (env file missing → unit fails at start; unknown `INVIO_*` key warning; DB unreachable → failed run, next trigger retries; lock vs `TimeoutStartSec`: set `INVIO_RUN_LOCK_SECONDS` ≥ the longest job, research R8) and "Manual verification", which holds **only** the two checks that aren't automated (plan Complexity Tracking): quarter-hour timing observed over 1 h (`journalctl -u invio-run-due -o short-iso --since -1h | grep Starting`, 4 starts each ≤ 60 s after the slot; SC-002 observation part) and the timed manual update (SC-009 manual part). Add an informational note on checking catch-up after a real host reboot (`systemctl list-timers`, LAST right after boot)
- [ ] T066 Check the #23 dependency. If `invio run-due` exists on `main` by now, remove the R14 guard in `deploy/ansible/molecule/default/verify.yml` and make the run-due start mandatory. Otherwise leave the guard and note in the PR description that the guard must be removed once #23 is merged
- [ ] T067 Run all gates: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy src`, `uv run pytest`, `deploy/scripts/verify-units.sh`, `cd deploy/ansible && uv run ansible-lint`, `molecule test` for both scenarios. Fix all findings
- [ ] T068 Walk through specs/015-gh-issue-24/quickstart.md scenarios 1–25 (including 19a) and tick each one against its CI, Molecule or manual source. Write `specs/015-gh-issue-24/pr-description.md`: summary, link to issue #24, the `scout` → `invio` naming (clarification Q1), the justification for the new dev-only dependency group `deploy` (constitution: new dependencies justified), the manual-verification deviation (plan Complexity Tracking), and the #23 guard status

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies
- **Foundational (Phase 2)**: depends on Setup and blocks all stories (parser, verify script, env example, CI job, doc skeleton)
- **US1 (Phase 3)**: after Foundational
- **US2 (Phase 4)**: after US1 (it extends `invio-run-due.service` and its test)
- **US5 (Phase 5)**: after US1. It installs the run-due units. The Molecule journal check (T026, step 8) uses US2's `check-journal-json.py`, so finish US2 first or run T026 step 8 after T021
- **US3 (Phase 6)**: after Foundational for the unit files (T044–T048). T046 and T049 need US5's role and Molecule scenario
- **US4 (Phase 7)**: after US1 and US3 (it hardens both services). T053, T056 and T057 need US5
- **US6 (Phase 8)**: after US5 (the role's `install.yml`, `units.yml`, Molecule `default`)
- **Polish (Phase 9)**: after all stories

### Story completion order

```text
Setup → Foundational → US1 → US2 → US5 ─┬─▶ US3 ─▶ US4 ─┐
                                         └─▶ US6 ────────┴─▶ Polish
```

### Within each story

Tests first (and failing) → unit or role files → verify/lint/Molecule run → docs.

## Parallel Opportunities

- Setup: T003, T004 and T005 in parallel after T001/T002.
- US1: T012 ∥ T013 (tests), then T014 ∥ T015 (unit files).
- US2: T018 ∥ T019.
- US5: tests T023, T024, T025, T028 and T029 in parallel; implementation T030, T031, T032, T033, T034 and T040 in parallel (separate files), then T035 → T036 → T037 → T038 → T039 in sequence.
- US3: T044 ∥ T045, T047 ∥ T048. The unit files can be written in parallel with US5.
- US4: T051 ∥ T052; T054 ∥ T055 ∥ T056.
- Polish: T064 ∥ T065.

### Parallel example: User Story 5

```text
Task: "T030 defaults/main.yml"      Task: "T031 tasks/validate.yml"
Task: "T032 tasks/mariadb.yml"      Task: "T033 tasks/account.yml"
Task: "T034 templates/invio.env.j2" Task: "T040 playbook.example.yml"
```

## Implementation Strategy

### MVP (US1 + US2 + US5)

1. Setup and Foundational (T001–T011).
2. US1: the run-due units pass verify and the contract tests, and can be installed by hand.
3. US2: JSON journal contract and checker.
4. US5: the role provisions a fresh host; Molecule green on Debian 12 and 13.
5. **Stop and validate**: quickstart #1–#11, #13, #14, #17, #18. A server now runs due jobs every 15 minutes, unattended.

### Incremental delivery

6. US3: hourly notification retry.
7. US4: sandbox hardening and memory overrides.
8. US6: safe updates through the role, plus the documented manual procedure.
9. Polish: README, the (two-item) manual checklist, #23 guard, gates, PR description.

## Notes

- `src/invio/` isn't changed. If a task seems to need application code, stop: that is out of
  scope (spec Out of scope).
- Unit files stay byte-identical between `deploy/systemd/` and the installed copies. Overrides
  are only possible through drop-ins (R11).
- Never commit real secrets. The Molecule secrets are the `ci-secret-*` placeholders.
- Commit after each task or logical group, on branch `gh-issue-24`.
