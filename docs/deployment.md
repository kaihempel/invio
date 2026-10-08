# Deployment

## Overview

This directory of the repository (`deploy/`) runs invio unattended on a Debian 12 or 13 server
with systemd. Four units do the work:

| Unit | Runs | Schedule |
|---|---|---|
| `invio-run-due.timer` / `.service` | `invio run-due` | every 15 minutes (+ up to 60 s), a missed trigger runs once after boot |
| `invio-notify-retry.timer` / `.service` | `invio notify retry` | hourly (+ up to 5 min), a missed trigger runs once after boot |

Both services run as the system account `invio`, locked down (see
[Sandbox and limits](#sandbox-and-limits)), and log to the journal. The Ansible role
`deploy/ansible/roles/invio` provisions everything; [Install manually](#install-manually)
describes the same steps by hand. Paths on the host:

| Path | Content |
|---|---|
| `/opt/invio` | Git checkout (root-owned), `/opt/invio/.venv` the virtual environment |
| `/opt/invio-python` | uv-managed Python 3.12 |
| `/usr/local/bin/uv` | pinned uv |
| `/etc/invio/invio.env` | settings and secrets (`root:invio`, mode `0640`) |
| `/etc/invio/deployed-revision` | commit that was last migrated |
| `/var/lib/invio` | state, cache and archive; the only path the services may write |
| `/etc/systemd/system/invio-*` | the four units, copied unchanged from `deploy/systemd/` |

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
| `invio_git_repo` | URL or path of the repository; no user name or token in it (use `invio_git_key_file`) |
| `invio_git_version` | tag or full 40-character commit SHA (abbreviated SHAs are rejected) |
| `invio_db_password` | non-empty, single line |
| `invio_smtp_host`, `invio_smtp_from` | non-empty |
| `invio_http_contact` | non-empty |
| `invio_llm_api_keys` | mapping provider to key, at least one non-empty; providers `mistral`, `openai`, `anthropic`, `google` |
| `invio_db_admin_user`, `invio_db_admin_password` | only when `invio_mariadb_manage_server` is `false` and `invio_db_host` is not `localhost` |

Optional (defaults in `defaults/main.yml`):

| Variable | Default |
|---|---|
| `invio_mariadb_manage_server` | `true` (requires `invio_db_host: localhost`) |
| `invio_db_host`, `invio_db_port`, `invio_db_name`, `invio_db_user`, `invio_db_user_host` | `localhost`, `3306`, `invio`, `invio`, `localhost` |
| `invio_git_key_file` | unset (deploy key on the target, for private repositories) |
| `invio_uv_version`, `invio_uv_sha256` | pinned release; the checksum is a mapping keyed by CPU architecture (`x86_64`, `aarch64`) |
| `invio_python_version` | `3.12.13` (full patch version) |
| `invio_smtp_port`, `invio_smtp_security`, `invio_smtp_user`, `invio_smtp_password` | `587`, `starttls`, unset, unset |
| `invio_log_level`, `invio_healthcheck_url` | `INFO`, unset |
| `invio_env_extra` | `{}`: more `INVIO_*` settings, key to value; keys the role writes itself are rejected |
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
changes no server configuration: for a remote `invio_db_host` it checks that
`invio_db_host:invio_db_port` is reachable, then creates the database and user through
`invio_db_admin_user`. When `invio_db_host` is `localhost`, the server is administered over the
root Unix socket (`/run/mysqld/mysqld.sock`) like a managed one, and no admin variables are
needed. Use this when another tool manages
the server, for example `debops.mariadb`. Set `invio_db_user_host: "%"` when the application host
differs from the database host. The role never drops a database, user or table.

### Tags

`invio` (everything), `invio:validate`, `invio:mariadb`, `invio:account`, `invio:config`,
`invio:install`, `invio:units`. `invio:validate` always runs. `invio:install` also runs
the unit tasks: an update stops the timers, and they must be started again in the same run.

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
   install -o root -g invio -m 0640 /opt/invio/deploy/env/invio.env.example /etc/invio/invio.env
   ```

   Edit `/etc/invio/invio.env` and replace every placeholder (see
   [Configuration](#configuration-env-file)).

7. Create the schema. This runs the migration as `invio` and reads the env file exactly like the
   services do:

   ```bash
   systemd-run --wait --pipe --collect --quiet --uid=invio --gid=invio \
     -p EnvironmentFile=/etc/invio/invio.env -p WorkingDirectory=/var/lib/invio \
     -p StateDirectory=invio -p StateDirectoryMode=0750 -p UMask=0027 \
     -E PYTHONDONTWRITEBYTECODE=1 -E HOME=/var/lib/invio -E XDG_CACHE_HOME=/var/lib/invio/cache \
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
| `/etc/invio/invio.env` | `root:invio` | `0640` |
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

### Sandbox and limits

Both services run as `invio` with `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`,
`PrivateTmp`, `ReadWritePaths=/var/lib/invio` and the further restrictions in the `# Sandbox`
block of the unit files. `/var/lib/invio` is the only writable path. Limits: `MemoryMax` 1G and
`TimeoutStartSec` 3h (run-due), 256M and 15min (notify-retry). Check them with:

```bash
systemctl show -p MemoryMax,TimeoutStartUSec,ProtectSystem,ProtectHome,NoNewPrivileges \
  invio-run-due.service
systemd-analyze security invio-run-due.service      # informational only
```

To change a memory limit with the role, set `invio_run_due_memory_max` or
`invio_notify_retry_memory_max`; the role writes
`/etc/systemd/system/<service>.d/50-invio-role.conf` and removes it again when the value is back
at the default. Without the role, use `systemctl edit`. Never edit the shipped unit files.

CI repeats the following sandbox probes automatically (Molecule). To repeat them on your own
host (optional), run each command as the service account with the unit's sandbox properties,
taken from the `# Sandbox` block plus `ReadWritePaths` and `StateDirectory`:

```bash
systemd-run --wait --pipe --collect --uid=invio --gid=invio \
  -p ReadWritePaths=/var/lib/invio -p StateDirectory=invio -p NoNewPrivileges=yes \
  -p ProtectSystem=strict -p ProtectHome=yes -p PrivateTmp=yes -p PrivateDevices=yes \
  /bin/sh -c 'touch /opt/invio/x 2>/dev/null && echo WRITTEN || echo DENIED'
```

Expected results: `touch /opt/invio/x` and `touch /etc/x` are denied, `ls -A /home` prints
nothing, `touch /var/lib/invio/probe` works (remove the file afterwards) and `sudo -n true` is
refused. The hardening does not allow Chromium's own sandbox: if the optional `render` extra is
ever deployed, the units need a reviewed relaxation.

## Digest archive

Jobs with `archive.enabled: true` write static HTML pages below `INVIO_ARCHIVE_DIR`
(`/var/lib/invio/archive`). The service creates the directory on first use (mode `0755`, files
`0644`) and may write there because `ReadWritePaths=/var/lib/invio` covers it; the Ansible role
needs no extra task. Serve it with any static web server and protect it, since digests can hold
private research. `/var/lib/invio` is `0750` (`invio:invio`), so the web server user needs to
be in the `invio` group (`usermod -aG invio www-data`, then restart the web server) or the
directory must be exposed another way (for example a bind mount or a sync job).

```nginx
server {
    listen 443 ssl;
    server_name digests.example.org;
    # ssl_certificate ...;

    location /invio/ {
        alias /var/lib/invio/archive/;
        auth_basic           "invio digests";
        auth_basic_user_file /etc/nginx/invio.htpasswd;   # htpasswd -c /etc/nginx/invio.htpasswd me
        autoindex off;
    }
}
```

Use `archive.base_url: https://digests.example.org/invio` in the job file so the mail links to
the pages. Pages carry `noindex` and a no-referrer policy but are not access controlled by invio
itself.

## Updating

### With Ansible

Set `invio_git_version` to the new tag (or full commit SHA) and run the playbook again. The role
does this, in order:

1. validates the variables and prepares the database, account and env file (no change when
   nothing differs);
2. probes the checkout in check mode. If the ref is unchanged, the timers are not touched and
   no migration runs;
3. if the checkout would change on an existing installation, it checks that the new ref
   exists (an unknown ref fails here and nothing has been stopped), stops both timers and waits
   until `invio-run-due.service` and `invio-notify-retry.service` are no longer
   `active`/`activating`/`deactivating`/`reloading` (up to `invio_update_wait_timeout`);
4. checks out the new ref and runs `uv sync --locked`;
5. runs `invio db upgrade` when `/etc/invio/deployed-revision` differs from the checked-out
   commit (this also repairs a run that failed between checkout and migration), then writes the
   new revision;
6. installs the unit files and starts both timers.

Any failing step fails the play and leaves the timers stopped. A lock-file mismatch shows up in
`uv sync --locked` after the timers were stopped and before the venv changes.

### Manually

Run as root. The `UV_*` environment is the one from [Install manually](#install-manually).

```bash
systemctl stop invio-run-due.timer invio-notify-retry.timer
# wait until neither line below says active, activating, deactivating or reloading
systemctl is-active invio-run-due.service invio-notify-retry.service
git -C /opt/invio fetch --tags && git -C /opt/invio checkout <tag>
cd /opt/invio
UV_PROJECT_ENVIRONMENT=/opt/invio/.venv UV_PYTHON_INSTALL_DIR=/opt/invio-python \
UV_PYTHON_PREFERENCE=only-managed UV_PYTHON=3.12.13 UV_CACHE_DIR=/var/cache/invio-uv \
  uv sync --locked --no-dev --no-editable --compile-bytecode
systemd-run --wait --pipe --collect --quiet --uid=invio --gid=invio \
  -p EnvironmentFile=/etc/invio/invio.env -p WorkingDirectory=/var/lib/invio \
  -p StateDirectory=invio -p StateDirectoryMode=0750 -p UMask=0027 \
  -E PYTHONDONTWRITEBYTECODE=1 -E HOME=/var/lib/invio -E XDG_CACHE_HOME=/var/lib/invio/cache \
  /opt/invio/.venv/bin/invio db upgrade
git -C /opt/invio rev-parse HEAD > /etc/invio/deployed-revision
cp /opt/invio/deploy/systemd/invio-*.service /opt/invio/deploy/systemd/invio-*.timer \
  /etc/systemd/system/                 # only needed when the units changed
systemctl daemon-reload
systemctl start invio-run-due.timer invio-notify-retry.timer
systemctl list-timers 'invio-*'
systemctl start invio-notify-retry.service     # one manual run as a smoke test
```

## Recovering from a failed update

When an update fails after the timers were stopped (role or manual), they stay stopped, so no
job runs against a half-updated installation. To recover:

1. Read the error. If the cause is fixed (for example a wrong variable), run the role again or
   continue the manual procedure: the role repeats the migration when `deployed-revision` does
   not match the checkout.
2. To go back: `git -C /opt/invio checkout <previous tag>`, then run the same `uv sync` as in
   the update.
3. MariaDB DDL is not transactional, so a failed migration may be partially applied. Check the
   Alembic revision with `systemd-run --wait --pipe --collect --uid=invio --gid=invio -p
   EnvironmentFile=/etc/invio/invio.env /opt/invio/.venv/bin/alembic -c /opt/invio/alembic.ini
   current` and repair partially applied steps by hand. Restore from a backup if needed.
4. Start the timers again (`systemctl start invio-run-due.timer invio-notify-retry.timer`) and
   check `systemctl list-timers 'invio-*'`.

## Manual verification checklist

Everything that can be automated is checked by CI (`deploy-static`, and the opt-in Molecule
workflow). Two checks need real wall-clock time or a person:

1. **Quarter-hour timing over one hour** (SC-002, observation part). On a provisioned host:

   ```bash
   journalctl -u invio-run-due -o short-iso --since -1h | grep Starting
   ```

   Expect 4 starts, each at most 60 s after :00, :15, :30 and :45.
   `systemd-analyze calendar '*:0/15'` lists the slots.
2. **Timed manual update** (SC-009, manual part). On a second host, follow
   [Updating, Manually](#manually) and time it. The goal is less than 10 minutes.

For information only: after a real host reboot `systemctl list-timers 'invio-*'` shows LAST right
after boot for a trigger that was missed during the downtime (catch-up). CI proves the same with
a container restart.

## Troubleshooting

- **The unit fails at once with "Failed to load environment files".** `/etc/invio/invio.env` is
  missing (the `EnvironmentFile=` has no `-` on purpose). Create it or run the role.
- **"unknown setting ignored" in the log.** A key in the env file is not a known setting (often a
  typo). Compare with `deploy/env/invio.env.example`.
- **The database is unreachable.** The run fails with a configuration or connection error and
  the next trigger retries. Check `INVIO_DATABASE_URL` and that MariaDB is running
  (`systemctl status mariadb`).
- **A job is skipped as locked or runs twice after a long run.** A run holds its job lock for
  `INVIO_RUN_LOCK_SECONDS` (default 7200). systemd kills a run at `TimeoutStartSec` (3 h) and
  the lock then expires on its own. Set `INVIO_RUN_LOCK_SECONDS` at least as long as your
  longest job.
- **`No such command 'run-due'`.** `invio run-due` comes from issue #23; deploy a revision that
  contains it.
- **A run right after boot cannot reach the network.** `network-online.target` only waits when
  a wait-online service is enabled on the host.
- **`systemd-run` says "Failed to connect to bus".** `dbus` is not running; the role installs it
  and starts `dbus.socket`.
