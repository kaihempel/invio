# Deployment

## Overview

## Requirements

## Install with Ansible

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

## Updating

### With Ansible

### Manually

## Recovering from a failed update

## Manual verification checklist

## Troubleshooting
