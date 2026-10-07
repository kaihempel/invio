# Contract: Ansible role `invio`

Location: `deploy/ansible/roles/invio/`. Example playbook: `deploy/ansible/playbook.example.yml`.
Collections: `deploy/ansible/requirements.yml` (`community.mysql`).
Research: [R1–R6](../research.md), [R10](../research.md#r10--mariadb-handling-clarification-q2), [R11](../research.md#r11--units-in-the-repository-vs-units-installed-by-the-role).

## Requirements

- Target: Debian 12 or 13 with systemd, Python 3 (for Ansible), `become: true`.
- Controller: `ansible-core` (version locked in the `deploy` dependency group) plus the
  collections listed above.
- Outbound network from the target: Git remote, PyPI (uv sync), GitHub releases (uv binary,
  managed Python).

## Variables

### Required (validated first, FR-016)

| Variable | Rule |
|---|---|
| `invio_git_repo` | non-empty (URL or local path) |
| `invio_git_version` | non-empty; tag or commit recommended |
| `invio_db_password` | non-empty, no newline |
| `invio_smtp_host`, `invio_smtp_from` | non-empty |
| `invio_http_contact` | non-empty |
| `invio_llm_api_keys` | dict with at least one non-empty value; keys in `mistral`, `openai`, `anthropic`, `google` |
| `invio_db_admin_user`, `invio_db_admin_password` | required **only** when `invio_mariadb_manage_server` is `false` and `invio_db_host` isn't `localhost` |

All string variables that go into the env file must not contain newlines. Every failure message
names the variable (for example `invio_db_password is required`). Validation runs before any task
that changes the host.

### Defaults (`defaults/main.yml`)

| Variable | Default |
|---|---|
| `invio_user` / `invio_group` | `invio` (fixed by FR-020; documented as not meant to be changed) |
| `invio_mariadb_manage_server` | `true` |
| `invio_db_host` / `invio_db_port` | `localhost` / `3306` |
| `invio_db_name` / `invio_db_user` | `invio` / `invio` |
| `invio_db_user_host` | `localhost` |
| `invio_git_key_file` | unset (deploy key path on the target, for private repos) |
| `invio_uv_version` / `invio_uv_sha256` | pinned values matching CI |
| `invio_python_version` | `3.12` |
| `invio_smtp_port` / `invio_smtp_security` | `587` / `starttls` |
| `invio_smtp_user` / `invio_smtp_password` | unset |
| `invio_log_level` | `INFO` |
| `invio_healthcheck_url` | unset |
| `invio_env_extra` | `{}` |
| `invio_run_due_memory_max` / `invio_notify_retry_memory_max` | `1G` / `256M` |
| `invio_update_wait_timeout` | `11100` (seconds: 3 h + 5 min) |

## Task order (`tasks/main.yml`)

1. `validate.yml`: assert the variables (no changes).
2. `mariadb.yml`: if the server switch is on, install `mariadb-server` and start it. If it's off,
   check that the server is reachable. Then install `python3-pymysql` and create the database and
   user (`no_log`).
3. `account.yml`: system group and user `invio` (`system: true`, shell `/usr/sbin/nologin`,
   home `/var/lib/invio`, `create_home: false`).
4. `config.yml`: `/etc/invio` and `invio.env` (`no_log`; permissions per
   [env-file.md](env-file.md)). A changed env file doesn't need a restart (oneshot services read
   it on every start).
5. `install.yml`: uv binary, managed Python in `/opt/invio-python` (outside the checkout), `git` check-mode probe → (update path:
   stop timers and wait, R4) → checkout → `uv sync` → `invio db upgrade` when
   `deployed-revision` differs → write `deployed-revision`.
6. `units.yml`: copy the four units (verbatim) and the optional drop-ins, `daemon-reload`, then
   `systemctl enable --now` both timers.

## Tags

`invio`, `invio:validate`, `invio:mariadb`, `invio:config`, `invio:install`, `invio:units`.
`invio:config` lets the operator update secrets without touching the code.

## Behavioural guarantees

| # | Guarantee | Spec |
|---|---|---|
| G1 | A second run with the same variables reports `changed=0` | FR-015, SC-005 |
| G2 | A missing required variable fails in `validate.yml` with its name; nothing has changed yet | FR-016 |
| G3 | No secret appears in output, including `-v` and on failure (`no_log`) | FR-017 |
| G4 | With `invio_mariadb_manage_server: false`, no `mariadb-server` package, config or service task runs | FR-018 |
| G5 | A changed `invio_git_version` on an existing install: timers stopped → wait until both services are inactive → checkout → sync → migrate → timers started. Any failure leaves the timers stopped | FR-021 |
| G6 | An unchanged `invio_git_version`: timers aren't stopped and no migration runs | US6 scenario 5 |
| G7 | A ref that doesn't exist fails in the check-mode probe, before the timers are stopped; the timers keep running. A lock-file mismatch fails in `uv sync --locked` after the timers were stopped and before the venv changes; the timers stay stopped (R3, R4) | edge cases |
| G8 | Never drops a database, user or table | edge case |
| G9 | The units installed match `deploy/systemd/` byte-for-byte; overrides only via drop-in | R11 |
