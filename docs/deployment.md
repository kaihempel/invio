# Deployment

## Overview

## Requirements

- Target host: Debian 12 (bookworm) or 13 (trixie) with systemd, root access (`become: true`)
  and Python 3 for Ansible. Other distributions are not supported.
- Outbound network from the target: the Git remote, PyPI (`uv sync`) and GitHub releases (the uv
  binary and the managed Python).
- Controller (for the role): the `deploy` dependency group (`uv sync --group deploy`) gives you
  `ansible-core`, `ansible-lint` and Molecule. Install the collections the role needs:

  ```bash
  uv run --group deploy ansible-galaxy collection install \
    -r deploy/ansible/requirements.yml -p deploy/ansible/.collections
  ```

  Install them before running `ansible-lint` or the pre-commit hook too: lint runs in offline
  mode and does not download collections. Molecule needs a second file,
  `deploy/ansible/molecule/requirements.yml` (it adds `community.docker`).
- The role uses `ansible.mysql` (the maintained successor that `community.mysql` now forwards
  to) for the database tasks.
- The role directory contains symlinks into `deploy/systemd/`. If you copy the role somewhere
  else, copy it with `cp -rL deploy/ansible/roles/invio <target>` so that the symlinks become
  real files.
- The hardening in the unit files (see below) would block the Chromium sandbox. If the optional
  `render` extra (Playwright) is ever deployed, the units need a reviewed relaxation.
- `network-online.target` only waits for the network when a wait-online service
  (`systemd-networkd-wait-online` or `NetworkManager-wait-online`) is enabled. Without one the
  `After=` ordering is a no-op, and a run right after boot may find the network not yet up (the
  next trigger retries).

## Install with Ansible

The role `invio` (`deploy/ansible/roles/invio`) provisions a fresh Debian host in one run:
MariaDB (database and user), the `invio` account, the env file, uv and Python, the checkout and
venv, the database migration, the unit files and both timers. A second run with the same
variables reports `changed=0`. Use `deploy/ansible/playbook.example.yml` as a starting point:

```yaml
- hosts: invio_servers
  become: true
  roles: [invio]
```

### Variables

Required (validated first; a failure names the variable and nothing on the host has changed):

| Variable | Rule |
|---|---|
| `invio_git_repo` | URL or path of the repository |
| `invio_git_version` | tag or full 40-character commit SHA (abbreviated SHAs are rejected) |
| `invio_db_password` | non-empty, single line |
| `invio_smtp_host`, `invio_smtp_from` | non-empty |
| `invio_http_contact` | non-empty |
| `invio_llm_api_keys` | mapping provider to key, at least one non-empty; providers `mistral`, `openai`, `anthropic`, `google` |
| `invio_db_admin_user`, `invio_db_admin_password` | only when `invio_mariadb_manage_server` is `false` and `invio_db_host` is not `localhost` |

Optional (defaults in `defaults/main.yml`):

| Variable | Default |
|---|---|
| `invio_mariadb_manage_server` | `true` |
| `invio_db_host`, `invio_db_port`, `invio_db_name`, `invio_db_user`, `invio_db_user_host` | `localhost`, `3306`, `invio`, `invio`, `localhost` |
| `invio_git_key_file` | unset (deploy key on the target, for private repositories) |
| `invio_uv_version`, `invio_uv_sha256` | pinned release; the checksum is a mapping keyed by CPU architecture (`x86_64`, `aarch64`) |
| `invio_python_version` | `3.12.13` (full patch version) |
| `invio_smtp_port`, `invio_smtp_security`, `invio_smtp_user`, `invio_smtp_password` | `587`, `starttls`, unset, unset |
| `invio_log_level`, `invio_healthcheck_url` | `INFO`, unset |
| `invio_env_extra` | `{}`: more `INVIO_*` settings, key to value |
| `invio_run_due_memory_max`, `invio_notify_retry_memory_max` | `1G`, `256M` (applied as a drop-in) |
| `invio_update_wait_timeout` | `11100` seconds to wait for a running service before an update |

`invio_user` and `invio_group` are fixed to `invio` by the unit files.

### Secrets with Ansible Vault

Put secrets in an encrypted vars file and reference them (`invio_db_password: "{{
vault_invio_db_password }}"`). Every task that handles a secret uses `no_log`, so nothing
appears in output, also not with `-v` or on a failure.

### Database server: managed or external

With `invio_mariadb_manage_server: true` the role installs and starts `mariadb-server` and
administers it over the root Unix socket. With `false` the role installs no server package and
changes no server configuration: it checks that `invio_db_host:invio_db_port` is reachable, then
creates the database and user through `invio_db_admin_user`. Use this when another tool manages
the server, for example `debops.mariadb`. Set `invio_db_user_host: "%"` when the application host
differs from the database host. The role never drops a database, user or table.

### Tags

`invio` (everything), `invio:validate`, `invio:mariadb`, `invio:account`, `invio:config`,
`invio:install`, `invio:units`. `invio:validate` always runs.

## Install manually

This is the same procedure the Ansible role follows, step by step. Run everything as root on
Debian 12 or 13. `<tag>` is the release tag you deploy. The `UV_*` values below are the ones the
role uses.

1. Packages:

   ```bash
   apt-get install -y git ca-certificates tar gzip mariadb-server
   ```

2. Database and database user (use your own password):

   ```sql
   CREATE DATABASE invio CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
   CREATE USER 'invio'@'localhost' IDENTIFIED BY '...';
   GRANT ALL ON invio.* TO 'invio'@'localhost';
   ```

3. Service account (its home is the state directory, which systemd creates on the first start):

   ```bash
   useradd --system --shell /usr/sbin/nologin --home-dir /var/lib/invio --no-create-home invio
   ```

4. uv and Python. Install the uv version and checksum from `invio_uv_version` and
   `invio_uv_sha256` in `deploy/ansible/roles/invio/defaults/main.yml` into `/usr/local/bin`
   (download the release tarball, check its SHA-256, copy `uv`). Then install the managed Python
   outside the checkout:

   ```bash
   UV_PYTHON_INSTALL_DIR=/opt/invio-python uv python install --no-bin 3.12.13
   ```

5. Code and dependencies:

   ```bash
   git clone <repository> /opt/invio
   git -C /opt/invio checkout <tag>
   cd /opt/invio
   UV_PROJECT_ENVIRONMENT=/opt/invio/.venv UV_PYTHON_INSTALL_DIR=/opt/invio-python \
   UV_PYTHON_PREFERENCE=only-managed UV_PYTHON=3.12.13 UV_CACHE_DIR=/var/cache/invio-uv \
     uv sync --locked --no-dev --no-editable --compile-bytecode
   ```

   Create `/var/cache/invio-uv` first (`root:root`, mode `0700`).

6. Configuration:

   ```bash
   install -d -o root -g invio -m 0750 /etc/invio
   install -o invio -g invio -m 0600 /opt/invio/deploy/env/invio.env.example /etc/invio/invio.env
   ```

   Edit `/etc/invio/invio.env` and replace every placeholder (see
   [Configuration](#configuration-env-file)).

7. Create the schema. This runs the migration as `invio` and reads the env file exactly like the
   services do:

   ```bash
   systemd-run --wait --pipe --collect --quiet --uid=invio --gid=invio \
     -p EnvironmentFile=/etc/invio/invio.env -p WorkingDirectory=/var/lib/invio \
     -p StateDirectory=invio -E PYTHONDONTWRITEBYTECODE=1 -E HOME=/var/lib/invio \
     /opt/invio/.venv/bin/invio db upgrade
   ```

8. Install and start the units (copy them unchanged; do not edit them):

   ```bash
   cp /opt/invio/deploy/systemd/invio-*.service /opt/invio/deploy/systemd/invio-*.timer \
     /etc/systemd/system/
   systemctl daemon-reload
   systemctl enable --now invio-run-due.timer
   ```

   The run-due and notify-retry units are both installed by this step; enable the retry timer
   as well with `systemctl enable --now invio-notify-retry.timer`.

`invio run-due`, the command the run-due service starts, comes from issue #23. Until that is
merged into your checkout the service fails with a usage error; the timer keeps running.

## Configuration (env file)

The services read `/etc/invio/invio.env` through systemd's `EnvironmentFile=` (without a leading
`-`, so a missing file fails the start). Owner and mode:

| Path | Owner:group | Mode |
|---|---|---|
| `/etc/invio/` | `root:invio` | `0750` |
| `/etc/invio/invio.env` | `invio:invio` | `0600` |
| `/etc/invio/deployed-revision` | `root:root` | `0644` (commit that was last migrated; not secret) |

Format: one `KEY="value"` per line, only `INVIO_*` keys, no newlines in values. Write a backslash
as `\\` and a double quote as `\"`. systemd does not expand `$VARIABLE` in these values. Do not
set `INVIO_ENV_FILE` on a server: invio's own dotenv parser would expand `${...}` in the file.
`deploy/env/invio.env.example` lists every key with placeholder values. The database password
inside `INVIO_DATABASE_URL` is URL-encoded; the role does that for you.

To update only the configuration (for example rotate a secret) without touching the code, run
the playbook with `--tags invio:config`. Oneshot services read the file on every start, so no
restart is needed.

## Operating

### Timers

```bash
systemctl list-timers 'invio-*'            # next and last trigger of each timer
systemctl status invio-run-due.service     # result of the last run
```

`invio-run-due.timer` starts the run-due service at every quarter hour (plus up to 60 s of
random delay). The service is a oneshot unit: it is `inactive (dead)` between runs. systemd never
starts a second instance while one is still running, so a long run cannot overlap with the next
trigger. A trigger missed while the host was down runs once after boot.

A failed run (non-zero exit code, `MemoryMax` or `TimeoutStartSec` hit) shows as
`failed (Result: exit-code)` with the exit code in `systemctl status`. It does not stop the
timer: the next trigger starts a fresh run.

`invio-notify-retry.timer` runs `invio notify retry` once per hour (plus up to 5 minutes of
random delay). When a notification failed again or was given up, the command exits with code 1
and the unit shows as failed until the next hourly run replaces it.

### Logs

Each service logs to the system journal under its own identifier (`invio-run-due`,
`invio-notify-retry`). invio writes one JSON object per log line to stderr; systemd sends it to
the journal.

```bash
journalctl -u invio-run-due -o cat --since today
journalctl -u invio-run-due -o cat | grep '"run_id": "<id>"'   # one run
```

Check the journal contract (every line that starts with `{` is JSON with `level` and `message`,
and no secret appears) with the checker script. Pass the secrets to look for in
`INVIO_CHECK_SECRETS` (newline-separated, each at least 8 characters; the script also looks for
URL-encoded and escaped forms). It exits 0 when the output is clean, 1 on a violation (it names
the line number only, never the content), 2 on missing input and 3 on an internal error:

```bash
journalctl --sync
journalctl -u invio-run-due -o cat | deploy/scripts/check-journal-json.py
```

Result lines that commands print to stdout (for example `nothing to retry`) are plain text by
design (stdout is the command's result, stderr is for logs). Use `--allow-no-json` for services
that may print only such a line.

## Updating

### With Ansible

### Manually

## Recovering from a failed update

## Manual verification checklist

## Troubleshooting
