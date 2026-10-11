# Backup and restore

**Last Updated:** 2026-10-11

What to back up on an invio host, how to schedule the database dump, how to test a restore, and
how to recover. The host layout is in [deployment.md](deployment.md#overview); the commands used
here (`invio-svc`, `invio db upgrade`, `invio job export`) are in [operations.md](operations.md).

The examples assume the Ansible layout: MariaDB on the same host, database and user `invio`,
the root account able to reach MariaDB over the Unix socket.

## What to keep

| Item | Where | Keep | Why |
|---|---|---|---|
| Database | MariaDB database `invio` (`INVIO_DATABASE_URL`) | Yes, daily dump, off host | All jobs, job configuration, runs, digests, items, notifications, LLM usage. The only copy of run history. |
| Env file | `/etc/invio/invio.env` | Yes, encrypted | Database password, SMTP password, provider keys, healthcheck URL. Secret. |
| Job YAML exports | `invio job export <name>` | Yes | Human-readable, versionable copy of each job's configuration. Redundant with the dump, but the only copy you can read and diff. |
| Digest archive | `INVIO_ARCHIVE_DIR` (default `/var/lib/invio/archive`) | Yes, if archive is enabled | Published pages are files; no command rebuilds them from the database. |
| Deployed revision | `/etc/invio/deployed-revision` | Yes (tiny) | Tells you which code version the schema was migrated with. |
| YouTube cookies file | `INVIO_YOUTUBE_COOKIES_FILE` (if set) | Yes, encrypted | A login session; treat it like a secret. |
| Model registry | `src/invio/llm/models.d/*.yaml` in the checkout | No separate copy | Part of the code. Restore it by checking out the same tag (see below). |
| Code checkout | `/opt/invio` | No | Re-cloned from the Git remote at the deployed tag. Keep the deploy key if the repository is private (`invio_git_key_file`). |
| Whisper model cache | `/var/lib/invio/cache/huggingface` | No | Downloaded again on the first transcription (about 0.5 GB for `small`). |
| Virtual environment, uv, Python | `/opt/invio/.venv`, `/opt/invio-python`, `/var/cache/invio-uv` | No | Recreated by `uv sync --locked` and the role. |

Of `/var/lib/invio`, only the archive (and a cookies file, if you keep one there) is needed for a
restore. If you back up the directory as a whole, exclude `cache/`.

## Database dump: `mariadb-dump`

Run as root over the Unix socket, so no password is needed on the command line. Use
`--single-transaction` so the dump is one consistent snapshot without locking the tables while
runs are writing.

```bash
install -d -m 0700 /var/backups/invio
mariadb-dump --single-transaction --default-character-set=utf8mb4 invio \
  | gzip -9 > /var/backups/invio/invio-$(date -u +%Y%m%dT%H%M%SZ).sql.gz
gzip -t /var/backups/invio/invio-*.sql.gz && echo "archive ok"
```

Before every `invio db upgrade` (and before an update, see [operations.md](operations.md#installation-and-updating)),
take a dump by hand with the same command. MariaDB DDL is not transactional, so a dump is the way
back from a half-applied migration.

### Schedule: systemd timer (example)

This is documentation only. The units below are not part of `deploy/`; put them in
`/etc/systemd/system/` on the host if you want them. In unit files `%` is written as `%%` and a
literal `$` as `$$`.

```ini
# /etc/systemd/system/invio-db-backup.service
[Unit]
Description=invio: dump the database

[Service]
Type=oneshot
UMask=0077
ExecStart=/bin/sh -c 'install -d -m 0700 /var/backups/invio && mariadb-dump --single-transaction --default-character-set=utf8mb4 invio | gzip -9 > /var/backups/invio/invio-$$(date -u +%%Y%%m%%dT%%H%%M%%SZ).sql.gz'
ExecStartPost=/usr/bin/find /var/backups/invio -name 'invio-*.sql.gz' -mtime +30 -delete
```

```ini
# /etc/systemd/system/invio-db-backup.timer
[Unit]
Description=invio: daily database dump

[Timer]
OnCalendar=*-*-* 02:40:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
```

```bash
systemctl daemon-reload
systemctl enable --now invio-db-backup.timer
systemctl list-timers 'invio-db-backup*'
systemctl start invio-db-backup.service            # one run now; check the file appeared
```

Starting at 02:40 keeps the dump away from the quarter-hour starts of `invio-run-due`. Thirty days
of dumps are kept by the `find` line; adjust it to your policy.

A cron equivalent (`%` must be escaped as `\%` in crontab):

```cron
40 2 * * * root sh -c 'install -d -m 0700 /var/backups/invio && mariadb-dump --single-transaction --default-character-set=utf8mb4 invio | gzip -9 > /var/backups/invio/invio-$(date -u +\%Y\%m\%dT\%H\%M\%SZ).sql.gz'
```

### Copy off the host

Dumps on the same disk do not survive a lost host. Copy them to another machine or storage after
each run, encrypted. Example with `gpg` to a recipient key (the key must exist in your keyring):

```bash
latest=$(ls -1t /var/backups/invio/invio-*.sql.gz | head -1)
gpg --encrypt --recipient backup@example.org \
  --output "/srv/offsite/$(basename "$latest").gpg" "$latest"
```

Use any tool you already run for backups (restic, borg, a sync job); the requirement is an off-host,
encrypted copy with a tested restore.

## Restore test

Do this regularly (monthly is a reasonable start) and after every change to the backup job. The
test restores the newest dump into a scratch database, migrates it with the deployed code and
reads it back. It never starts a timer and never sends mail.

### 1. Create the scratch database

```bash
sudo mariadb -e "CREATE DATABASE invio_restore_test CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
GRANT ALL ON invio_restore_test.* TO 'invio'@'localhost';"
```

Use the same host part as `invio_db_user_host` if it differs from `localhost`.

### 2. Load the dump

```bash
gunzip -c /var/backups/invio/invio-20261011T024000Z.sql.gz | sudo mariadb invio_restore_test
sudo mariadb invio_restore_test -e "SELECT COUNT(*) AS jobs FROM jobs; SELECT version_num FROM alembic_version;"
```

### 3. Point a copy of the env file at the scratch database

Copy the env file as root and remove the mail and healthcheck settings, so a mistake in the test
cannot send a mail or ping the healthcheck. systemd reads `EnvironmentFile=` as root, so a
root-only copy is enough.

```bash
sudo install -m 0600 /etc/invio/invio.env /root/invio-restore.env
sudo sed -i -e 's|/invio?|/invio_restore_test?|' \
           -e '/^INVIO_SMTP_/d' -e '/^INVIO_HEALTHCHECK_URL=/d' /root/invio-restore.env
sudo grep -c '^INVIO_DATABASE_URL=.*invio_restore_test' /root/invio-restore.env   # must print 1
```

### 4. Migrate and read back

Use the code revision the dump was taken with, or the currently deployed one. A dump older than
the code is migrated forward; a dump newer than the code cannot be read back reliably.

```bash
systemd-run --wait --pipe --collect --quiet --uid=invio --gid=invio \
  -p EnvironmentFile=/root/invio-restore.env -p WorkingDirectory=/var/lib/invio \
  -E HOME=/var/lib/invio -E XDG_CACHE_HOME=/var/lib/invio/cache \
  /opt/invio/.venv/bin/invio db upgrade
# database at revision 0004      (the last revision of the code)

systemd-run --wait --pipe --collect --quiet --uid=invio --gid=invio \
  -p EnvironmentFile=/root/invio-restore.env -p WorkingDirectory=/var/lib/invio \
  -E HOME=/var/lib/invio -E XDG_CACHE_HOME=/var/lib/invio/cache \
  /opt/invio/.venv/bin/invio job list

systemd-run --wait --pipe --collect --quiet --uid=invio --gid=invio \
  -p EnvironmentFile=/root/invio-restore.env -p WorkingDirectory=/var/lib/invio \
  -E HOME=/var/lib/invio -E XDG_CACHE_HOME=/var/lib/invio/cache \
  /opt/invio/.venv/bin/invio run list --limit 5
```

Expected: `db upgrade` ends with `database at revision <head>`, `job list` shows the same jobs as
production, and `run list` shows recent runs. Compare the job count with the production database.

Do not run `invio run-due`, `invio job run` or `invio notify retry` against the scratch copy. The
copy has no mail settings, but a run would still call the LLM providers and write rows.

### 5. Clean up

```bash
sudo rm -f /root/invio-restore.env
sudo mariadb -e "DROP DATABASE invio_restore_test;"
```

Note the date, the dump file, the code revision and the result somewhere you will find it again.
A restore test that has not been run is not a backup.

## Recovery

### Database lost or corrupted

1. Stop the timers and wait for the services to become inactive (see
   [operations.md](operations.md#scheduled-runs-and-timers)):

   ```bash
   systemctl stop invio-run-due.timer invio-notify-retry.timer
   systemctl is-active invio-run-due.service invio-notify-retry.service   # wait for inactive
   ```

2. Recreate the database and load the dump. This drops the current database; do it only when the
   current one is lost or corrupted.

   ```bash
   sudo mariadb -e "DROP DATABASE invio; CREATE DATABASE invio CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"
   gunzip -c /var/backups/invio/invio-20261011T024000Z.sql.gz | sudo mariadb invio
   ```

3. Check out the code revision that was deployed and migrate:

   ```bash
   cat /etc/invio/deployed-revision                     # the commit that matches the dump
   git -C /opt/invio checkout "$(cat /etc/invio/deployed-revision)"
   # then the uv sync from deployment.md, and:
   systemd-run --wait --pipe --collect --quiet --uid=invio --gid=invio \
     -p EnvironmentFile=/etc/invio/invio.env -p WorkingDirectory=/var/lib/invio \
     -E HOME=/var/lib/invio -E XDG_CACHE_HOME=/var/lib/invio/cache \
     /opt/invio/.venv/bin/invio db upgrade
   ```

4. Clear locks and runs that were running at the time of the dump. Nothing runs now, so every
   lock is stale:

   ```sql
   -- sudo mariadb invio
   UPDATE jobs SET locked_until = NULL WHERE locked_until IS NOT NULL;
   ```

5. Start the timers and check: `systemctl start invio-run-due.timer invio-notify-retry.timer`,
   then `systemctl list-timers 'invio-*'`.

Notifications that were `pending` at the time of the dump may be sent again by the retry, or
were already sent. Check the recipients before re-sending (see
[operations.md](operations.md#failed-notifications)).

### Host lost (new host)

1. Install the host as in [deployment.md](deployment.md#install-with-ansible), with the same
   `invio_git_version` as in `/etc/invio/deployed-revision` of the old host (if you have it).
   The role writes a new env file from its variables.
2. Restore the database as in steps 1 to 4 above, into the new host's `invio` database. Use
   `invio db upgrade` only after the dump is loaded.
3. Put back the archive directory, if used:

   ```bash
   sudo tar -xzf /var/backups/invio/archive-20261011.tar.gz -C /var/lib/invio
   sudo chown -R invio:invio /var/lib/invio/archive
   ```

4. Restore the env file from the encrypted copy or from the vault variables, and run the role again
   with `--tags invio:config`. Check with `invio-svc llm test <provider>`. `invio-svc notify retry`
   prints `nothing to retry` unless the dump held failed notification rows.
5. Start the timers and check `systemctl list-timers 'invio-*'`.

### A single job or its configuration

Import a job from its export. The service account reads the file, so it must be in a path the
`invio` account can read: stage it under `/var/lib/invio`, not under `/root`:

```bash
install -d -o invio -g invio -m 0750 /var/lib/invio/restore
install -o invio -g invio -m 0600 /root/exports/daily-digest.yaml /var/lib/invio/restore/
invio-svc job import /var/lib/invio/restore/daily-digest.yaml --replace
rm -f /var/lib/invio/restore/daily-digest.yaml
```

`--replace` overwrites the job of that name; its run history is kept. Without `--replace` an
existing name is an error. A job that was deleted is created again by the import, but its run
history is gone with the delete.

Export every job, for example before a host change (run in the root shell where the `invio-svc`
function from [operations.md](operations.md#running-commands-as-the-service-account) is defined):

```bash
install -d -m 0700 /root/exports
sudo mariadb -N -B invio -e 'SELECT name FROM jobs ORDER BY name' | \
while IFS= read -r job; do
  invio-svc job export "$job" > "/root/exports/$job.yaml"
done
ls -l /root/exports
```

`job export` writes to stdout, which `systemd-run` passes to the root shell. The exports hold
job configuration only (no keys), but treat them as internal: they name your sources and recipients.

## Env file and secrets

- The env file is the only place the secrets live on the host, and it is root-owned `0640`.
  A backup of it must be encrypted and kept off the host, like the database dump.
- If the host is deployed with Ansible and Vault, the vault is already the source of truth for the
  env file: a new host gets the file back from the role. Keep an encrypted copy of the rendered
  file as well only if the vault is not under your control in the same way.
- Encrypt the copy on the host before it leaves the machine:

  ```bash
  sudo gpg --encrypt --recipient backup@example.org \
    --output /srv/offsite/invio-env-$(date -u +%F).gpg /etc/invio/invio.env
  ```

- Rotating a secret means changing it in the vault or the env file, not only in the backup. Old
  copies must be removed or re-encrypted when a key is revoked.
- Never paste the env file or a dump into a ticket or a chat. The dump contains recipients and the
  digests (private research), and the env file contains every key.

## Archive and model registry details

- **Digest archive** (`INVIO_ARCHIVE_DIR`): back it up as files. The digest text is in the
  `digests` table, but the HTML pages and index files exist only on disk.

  ```bash
  tar -czf /var/backups/invio/archive-$(date -u +%F).tar.gz -C /var/lib/invio archive
  ```

- **Model registry** (`src/invio/llm/models.d/*.yaml`): the prices and context windows used for
  the cost columns of `invio usage` and the `run show` statistics. The files are part of the code
  and are read from the installed package, and no setting points to another directory. To restore
  the registry, check out the same revision (the deployed one is in `/etc/invio/deployed-revision`).
  Copying yaml files to another place on the host has no effect: they would not be read.

## Related pages

- [operations.md](operations.md): the commands and the lock and notification procedures used above.
- [deployment.md](deployment.md): installation, update and the failed-update recovery.
- [troubleshooting.md](troubleshooting.md): symptoms after a restore, such as stale locks.
- [security.md](security.md): secret handling in more detail.
