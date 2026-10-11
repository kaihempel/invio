# Operations runbook

**Last Updated:** 2026-10-11

Day-to-day operation of a host that runs invio unattended. Installation and the Ansible role are
in [deployment.md](deployment.md); configuration keys in [configuration.md](configuration.md);
symptoms and fixes in [troubleshooting.md](troubleshooting.md); database and file backups in
[backup.md](backup.md). Security notes are in [security.md](security.md).

## Architecture

```
systemd timer ──► invio-run-due.service ──► invio run-due ──► run_job (per due job)
                                                                 │ claim job lock (jobs.locked_until)
                                                                 ▼
                                                    graph: fetch → LLM → persist → notify → finalize
systemd timer ──► invio-notify-retry.service ──► invio notify retry ──► notifications (SMTP)
                                                                 │
                                          MariaDB (INVIO_DATABASE_URL) ◄─┘
```

Both services run as the system account `invio`, read `/etc/invio/invio.env` and log JSON lines to
the journal. Every job run is bounded by its lock (see [Stuck job locks](#stuck-job-locks)).

## Command reference

Every command below is run as the service account on the host (see
[Running commands as the service account](#running-commands-as-the-service-account)), or
locally with `uv run` against a development database.

| Command | Purpose | Example |
|---|---|---|
| `invio --version` | Print the installed version | `invio --version` |
| `invio db upgrade` | Apply pending schema migrations | `invio db upgrade` |
| `invio run-due` | Run every due job (what the timer does) | `invio run-due --limit 2 --parallel 2` |
| `invio job list` | List jobs with next run and last status | `invio job list` |
| `invio job show` | Show one job's status and configuration | `invio job show daily-digest` |
| `invio job create` | Create a job (wizard, or from a file) | `invio job create --from-file daily-digest.yaml` |
| `invio job edit` | Edit a job's YAML in `$VISUAL` / `$EDITOR` | `invio job edit daily-digest` |
| `invio job enable` | Schedule a job again | `invio job enable daily-digest` |
| `invio job disable` | Stop scheduling a job (keeps config and history) | `invio job disable daily-digest` |
| `invio job delete` | Delete a job and its run history | `invio job delete daily-digest --yes` |
| `invio job export` | Write a job as YAML | `invio job export daily-digest -o daily-digest.yaml` |
| `invio job import` | Import a job from YAML (no prompts) | `invio job import daily-digest.yaml --replace` |
| `invio job suggest` | Let a model draft keywords and a description | `invio job suggest "EU AI Act enforcement" --yaml` |
| `invio job run` | Run one job now | `invio job run daily-digest --dry-run -v` |
| `invio source discover` | Find feeds, sitemaps and channels of a URL | `invio source discover https://example.org --add-to daily-digest --pick 1` |
| `invio llm test` | One tiny request to check a provider key | `invio llm test openai` |
| `invio notify retry` | Re-send failed notifications | `invio notify retry` |
| `invio run list` | List runs, newest first | `invio run list --job daily-digest --limit 10` |
| `invio run show` | One run: statistics, tokens, stored errors | `invio run show 42` |
| `invio usage` | LLM tokens and cost | `invio usage --job daily-digest --since 2026-10-01 --by day` |

Exit codes are documented per command in `invio <command> --help` and in the README
(`### Running a job`, `### Scheduled runs: invio run-due`, `### Usage and cost: invio usage`).

## Running commands as the service account

Commands that read the database or the registry must see the same environment as the timers.
A plain shell does not: invio reads `.env` from the working directory unless `INVIO_ENV_FILE`
is set, and the server file must not be used that way (see
[deployment.md](deployment.md#configuration-env-file)). Use `systemd-run` as root so the command
runs as `invio` with the units' environment:

```bash
# as root: run one invio command as the service account, with the units' environment
invio-svc() {
  systemd-run --wait --pipe --collect --quiet --uid=invio --gid=invio \
    -p EnvironmentFile=/etc/invio/invio.env -p WorkingDirectory=/var/lib/invio \
    -E HOME=/var/lib/invio -E XDG_CACHE_HOME=/var/lib/invio/cache \
    /opt/invio/.venv/bin/invio "$@"
}

invio-svc job list
```

Standard output is passed through, so `invio-svc job export daily-digest > daily-digest.yaml`
writes the file as root. The examples below use the same pattern, written as `invio-svc`.
The service account owns only `/var/lib/invio`; a command that writes to a root-owned path
(for example `job export -o /etc/...`) fails for `invio`, so export to stdout and redirect as root.

## Installation and updating

- Installation: [deployment.md, Install with Ansible](deployment.md#install-with-ansible) or
  [Install manually](deployment.md#install-manually).
- Updating (role, or the manual sequence that stops both timers, waits for running services,
  checks out the tag, runs `uv sync --locked`, `invio db upgrade` and starts the timers again):
  [deployment.md, Updating](deployment.md#updating).
- After any update, check the installed version and the schema revision:

```bash
/opt/invio/.venv/bin/invio --version           # invio 0.1.0 (example)
cat /etc/invio/deployed-revision               # commit that was last migrated
invio-svc db upgrade                           # no-op when the schema is current
```

If an update failed after the timers were stopped, the timers stay stopped. Recover as described
in [deployment.md, Recovering from a failed update](deployment.md#recovering-from-a-failed-update).

## Database upgrade: `invio db upgrade`

`invio db upgrade [REVISION]` applies pending Alembic migrations to the database in
`INVIO_DATABASE_URL`. The default target is `head`. It logs each migration as a JSON line on
stderr and ends with the revision:

```bash
invio-svc db upgrade
# {"timestamp": "...", "level": "INFO", "logger": "invio.cli.commands.db", "message": "migration finished", "revision": "0004"}
# database at revision 0004
```

Check the current revision directly in MariaDB (the revision is in `alembic_version`):

```sql
SELECT version_num FROM alembic_version;
```

Rules:

- Run the upgrade before the timers are started again after an update. The role and the manual
  procedure already do this.
- MariaDB DDL is not transactional. A failed upgrade can leave a partly applied migration. Take a
  backup first ([backup.md](backup.md)) and fix partial changes by hand before running the upgrade
  again.
- `invio db upgrade` does not run jobs and sends nothing; it is safe to repeat.

## Scheduled runs and timers

```bash
systemctl list-timers 'invio-*'                   # next and last trigger of each timer
systemctl status invio-run-due.service            # result of the last run
systemctl status invio-notify-retry.service       # result of the last notification retry
```

- `invio-run-due.timer` starts `invio run-due` every quarter hour. A failed run does not stop the
  timer; the next trigger starts a fresh run.
- `invio-notify-retry.timer` runs `invio notify retry` hourly. The unit shows `failed` while the
  last retry exited with 1 (a notification failed or was given up), until the next hourly run.
- Pause all scheduled runs: `systemctl stop invio-run-due.timer invio-notify-retry.timer`.
  Resume: `systemctl start invio-run-due.timer invio-notify-retry.timer`.
- Pause a single job: `invio-svc job disable daily-digest`. Resume it with
  `invio-svc job enable daily-digest`; the next run time is recalculated from the schedule.

`invio run-due` is what the timer runs. You can call it by hand; it honours the same locks:

```bash
invio-svc run-due --limit 2 --parallel 2
# busy     job.example  locked until 2026-10-12 01:00 CEST     (a run holds the job)
# due 1 · ran 0 (0 succeeded, 0 partial, 0 failed) · busy 1 · skipped 0
```

## Log inspection with journalctl

Both services log to the system journal under their unit names. Each log line on stderr is one
JSON object. `SyslogIdentifier` is the unit name without `.service`.

```bash
journalctl -u invio-run-due -o cat --since today          # all run-due lines today
journalctl -u invio-run-due -o cat -f                     # follow live
journalctl -u invio-notify-retry -o cat --since -24h      # retries of the last day
journalctl -t invio-run-due -o cat --since "2026-10-11 07:00"   # same, by identifier
```

`-o cat` prints the JSON only; the time is in the `timestamp` field.

Fields of every line (from `src/invio/log.py`):

| Field | Meaning |
|---|---|
| `timestamp` | UTC, ISO 8601 with offset |
| `level` | `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `logger` | Module name, for example `invio.pipeline` or `invio.notify.email` |
| `message` | Event name, for example `run.started`, `llm.fallback`, `notification failed` |
| `job` | Job name, present inside a run |
| `run_id` | Database run id as a string, present inside a run |

Other keys are the event's own fields (`event`, `job_name`, `error`, `reason`, `notification_id`,
`recipient`, ...). A field whose name clashes with a reserved one gets the `extra_` prefix.
Secrets are never logged; `error` holds the error class and a scrubbed message only.

Examples:

```bash
# one run, by its database run id
journalctl -u invio-run-due -o cat | grep '"run_id": "42"'

# warnings and errors of the last day
journalctl -u invio-run-due -o cat --since -24h | grep -E '"level": "(WARNING|ERROR)"'

# LLM provider fallbacks (the primary failed, the fallback answered or failed too)
journalctl -u invio-run-due -o cat --since -24h | grep -E '"message": "llm\.fallback(_failed)?"'

# a job's lock or busy messages
journalctl -u invio-run-due -o cat | grep -E '"message": "run\.lock_lost"'
```

`jq` is not installed by default on Debian; if you use it, `journalctl -u invio-run-due -o cat | jq -c 'select(.level == "ERROR")'`.

### Checking the journal contract

`deploy/scripts/check-journal-json.py` checks that every line starting with `{` is a JSON object
with string `level` and `message` keys, and that no configured secret appears in the output. It
reads stdin and needs `INVIO_CHECK_SECRETS` (newline-separated secret values, each at least 8
characters; URL-encoded and escaped forms are searched too):

```bash
# secrets.txt (root-only, 0600): one secret value per line, at least 8 characters each
journalctl --sync
journalctl -u invio-run-due -o cat | INVIO_CHECK_SECRETS="$(cat /root/secrets.txt)" \
  /opt/invio/deploy/scripts/check-journal-json.py --require-secrets
echo "exit=$?"    # 0 clean, 1 violation (line number only), 2 usage or no input, 3 internal error
```

Use `--allow-no-json` for a unit that may print only plain result lines such as `nothing to retry`.

## Stuck job locks

### How the lock works

- The lock is the column `jobs.locked_until` (naive UTC in the database). A run claims the job
  with one conditional UPDATE: it takes the lock only when `locked_until` is empty or `<= now`,
  and sets `locked_until = now + INVIO_RUN_LOCK_SECONDS` (default `7200`, minimum `60`). That
  value is also the run's token.
- The run releases the lock at the end (in `finalize`, or in the safety net after a crash) by
  setting `locked_until` back to `NULL`, but only when the value still equals its token. A run
  whose lock was taken over logs `run.lock_lost` and does not touch the job.
- Before each write stage and each item, a running graph checks its token against the clock
  (`LockExpiredError`). A run that outlives its lock stops at the next check and fails.
- There is no heartbeat: the lock cannot be extended while a run is alive. Set
  `INVIO_RUN_LOCK_SECONDS` above the longest job you run (see
  [troubleshooting.md](troubleshooting.md#other-symptoms)).
- A held lock is reported in two places: `invio job run` fails with

  ```
  Error: job 'daily-digest' is running (locked until 2026-10-12 01:00 CEST)
  ```

  (exit 1, no run row is created), and `invio run-due` prints `busy` for a due job.
- `invio job show` does not display the lock. Read it in MariaDB (see below).

### Is the lock really stuck?

A lock is normally released at the end of a run or taken over after expiry. A held lock with a
dead owner happens when the process is killed (OOM, `MemoryMax`, `TimeoutStartSec`, `systemctl
stop`). invio has no SIGTERM handler, so a stopped run does not reach `finalize`: it leaves the
lock and a run row with `status = running`.

Then wait. The lock expires at `locked_until` by itself, and the next timer trigger after that
time runs the job. Confirm that nothing is running and read the expiry (SQL below):

```bash
systemctl is-active invio-run-due.service invio-notify-retry.service   # both must say inactive
ps -u invio -o pid,etime,args | grep -v grep | grep 'bin/invio' || echo "no invio process"
```

### Reading the lock in MariaDB

Times in the database are naive UTC. Use the MariaDB socket as root:

```sql
-- sudo mariadb invio
SELECT id, name, enabled, next_run_at, locked_until, UTC_TIMESTAMP() AS now_utc
FROM jobs ORDER BY name;

-- runs that never finished (status stays 'running' after a kill)
SELECT id, job_id, status, started_at, finished_at
FROM runs WHERE status = 'running' ORDER BY id;
```

A `locked_until` in the past is free. A future one is held until then.

### Clearing a lock by hand

There is no unlock command. Clear the lock only when all of these hold:

1. `systemctl is-active invio-run-due.service` prints `inactive` and no `bin/invio` process of
   the service account is running (see above).
2. The job is not mid-run: its newest run in `runs` is not `running`, or that run is known to be
   dead.
3. Waiting until `locked_until` is not acceptable.

If a live run is still working when the lock is cleared, a second run can start the same job and
the digest may be sent twice. The old run only notices at its own release (`run.lock_lost`); it
does not stop early on the database change.

```sql
-- sudo mariadb invio
UPDATE jobs
SET locked_until = NULL
WHERE name = 'daily-digest' AND locked_until > UTC_TIMESTAMP();
-- must report 1 row affected; 0 means the lock had already expired or the name is wrong
```

Optional: a run row left as `running` does not block anything. The failure streak that sets the
retry delay ignores `running` rows, and `invio run show` says it has no final statistics. Marking
it failed is cosmetic, but it then counts as a failure in the streak, which lengthens the retry
delay of the job. Do it deliberately:

```sql
UPDATE runs
SET status = 'failed', finished_at = UTC_TIMESTAMP(6),
    error = 'run killed; marked failed by hand'
WHERE id = 42 AND status = 'running';
```

After clearing, the next `run-due` (the next quarter hour) starts the job if its `next_run_at` has
passed. `invio-svc run-due --limit 1` starts it at once; `invio-svc job list` shows the next run time.

## Failed notifications

### States

Each digest mail is one row in `notifications`. `status` is one of:

| `status` | Meaning | Retried by `invio notify retry` |
|---|---|---|
| `pending` | Being sent, or interrupted (no final result) | Only when the last attempt is more than 1 hour old (`STALE_PENDING_AFTER`) |
| `sent` | Delivered | No |
| `failed` | The last attempt failed (`error` holds the scrubbed message) | Yes |
| `skipped` | Given up after `MAX_ATTEMPTS` = 5 attempts, or interrupted at the limit | No |

`attempts` counts send attempts; `last_attempt_at` is the start of the latest one.

### Retrying

`invio notify retry` re-sends every `failed` row and every stale `pending` row, rebuilding the mail
from the stored digest and payload. Exit codes: `0` nothing failed, `1` a notification failed again
or was given up, `2` configuration error (database URL, `INVIO_SMTP_HOST`, `INVIO_SMTP_FROM`).

```bash
invio-svc notify retry
# sent      #17 daily-digest research@example.com
# failed    #18 daily-digest research@example.com  SMTPAuthenticationError: ...
# retried 2, sent 1, failed 1, given up 0
```

With no candidates it prints `nothing to retry` and exits 0. The hourly timer does the same; to
run it now: `systemctl start invio-notify-retry.service` (it is a oneshot, so this waits for
the result and does not overlap with a timer run).

To list rows that need attention (MariaDB, `sudo mariadb invio`):

```sql
SELECT id, job_id, run_id, status, attempts, last_attempt_at, recipient, error
FROM notifications
WHERE status IN ('failed', 'pending', 'skipped')
ORDER BY id;
```

### Giving up and re-sending by hand

A `skipped` row is never retried automatically. To send it again, reset it to `failed` and run the
retry once (attempts start from zero again):

```sql
UPDATE notifications SET status = 'failed', attempts = 0 WHERE id = 18 AND status = 'skipped';
```

```bash
invio-svc notify retry
```

Caution: a `pending` row can be a mail that was actually delivered (the SMTP send succeeded but
recording it failed; the log then shows `notification sent but not recorded`). Retrying it sends
the mail again. Check the recipient's mailbox before re-sending a stale `pending` row.

### Causes

SMTP problems (authentication, TLS, timeouts, a missing sender) are listed with their messages in
[troubleshooting.md](troubleshooting.md#smtp-errors). A digest whose stored row has been removed
fails with `digest no longer available` and cannot be rebuilt.

## Adding or rotating a provider key

Keys are the variables `INVIO_MISTRAL_API_KEY`, `INVIO_OPENAI_API_KEY`, `INVIO_ANTHROPIC_API_KEY`
and `INVIO_GOOGLE_API_KEY` in `/etc/invio/invio.env` (format and owner: see
[deployment.md](deployment.md#configuration-env-file)). A provider is used only by jobs that name
it in their `llm` section; the key of an unused provider is harmless.

1. Edit the file as root and replace the value (keep `root:invio 0640`):

   ```bash
   sudoedit /etc/invio/invio.env
   # INVIO_OPENAI_API_KEY="sk-new-..."
   ```

   With Ansible, change the variable (or the vault entry it refers to) and run the role with
   `--tags invio:config`. The role writes this file on every run, so a key changed only on the
   host is overwritten by the next full playbook run.

2. No restart is needed. The units are oneshot services that read the file on every start. A run
   that is already in progress keeps the old key until it ends.

3. Test the key with one request as the service account:

   ```bash
   invio-svc llm test openai
   # ok provider=openai model=gpt-... input_tokens=... output_tokens=... duration_ms=...
   ```

   Exit codes: `0` ok, `1` the provider call failed (for a bad key:
   `Error: LLMAuthError: ...`), `2` configuration problem (for example a missing key:
   `Configuration error: LLM provider 'openai' needs an API key: set INVIO_OPENAI_API_KEY`).
   Use `--model` to test a specific registered model.

4. Remove the old key from any place you stored it (vault, password manager, a copy of the file).

## Provider outage

Each job role can name a fallback provider and model (the `llm` section of the job; see README
`### Per-role providers and fallback`). A request goes to the role's provider first. On
`LLMUnavailableError`, `LLMRateLimitError` or `LLMQuotaError` (after the provider's own retries)
the same request goes to the fallback, and one `llm.fallback` warning is logged. Authentication
errors and rejected requests never fall back.

Procedure when a provider is down or failing:

1. Confirm the provider is the problem, not the key:

   ```bash
   invio-svc llm test anthropic
   # Error: LLMUnavailableError: ...   -> provider outage or network; the key may be fine
   # Error: LLMAuthError: ...          -> the key is wrong or revoked: see the key rotation above
   ```

2. See what the runs did:

   ```bash
   journalctl -u invio-run-due -o cat --since -6h | grep -E '"message": "llm\.fallback(_failed)?"'
   invio-svc run list --limit 10
   ```

   `llm.fallback` means the primary failed and the fallback was tried; `llm.fallback_failed`
   means the fallback failed too, and that call ended with an error. Failed runs are retried with
   backoff (1 h, doubling up to 24 h), so a job recovers without action once the provider is back.

3. Choose one:

   - Wait. Most outages are short and the retry schedule covers them.
   - Keep a job running on a fallback for the duration: add the fallback to the job's `llm`
     section (`invio-svc job edit daily-digest` opens the YAML; the README explains the fields and
     the validation rules). Export the job first (`invio-svc job export daily-digest > backup.yaml`)
     so the previous configuration is kept. The providers and models are checked before the run
     starts.
   - Pause the job until the provider recovers: `invio-svc job disable daily-digest`, then
     `invio-svc job enable daily-digest` (the next run is scheduled again).
   - Pause the job until the provider recovers: `invio-svc job disable daily-digest`, then
     `invio-svc job enable daily-digest` (the next run is scheduled again).

There is no circuit breaker. During a long outage every call runs the provider's full retry
policy before it reaches the fallback, so runs get slower but still complete.

## Jobs: inspect, change, export and import

```bash
invio-svc job list                               # name, enabled, schedule, next run, last status
invio-svc job show daily-digest                  # status, next run and the full configuration

invio-svc job export daily-digest > daily-digest.yaml     # YAML to stdout
invio-svc job import daily-digest.yaml --replace          # re-import; --name NAME for another name
invio-svc job create --from-file daily-digest.yaml        # create without prompts (refuses an existing name)

invio-svc job suggest "EU AI Act enforcement" --yaml      # print a drafted YAML block once
invio-svc source discover https://example.org --add-to daily-digest --pick 1
                                                          # add the first feed/sitemap found to a job
invio-svc job delete daily-digest --yes                   # deletes the job and all its run history
```

`job edit` opens the stored YAML in `$VISUAL` or `$EDITOR` (an interactive terminal is needed)
and validates it on save; an invalid edit can be corrected or aborted, and the job stays unchanged
on abort. Exports
contain no secrets (keys live in the env file). `job delete` cannot be undone: export the job
first, or take a database backup ([backup.md](backup.md)).

## Manual runs

```bash
invio-svc job run daily-digest --dry-run -v   # digest and one line per item; sends nothing, stores no items
invio-svc job run daily-digest                # real run: stores results, sends the mail
invio-svc job run daily-digest --max-items 5  # cap the items of this run
```

- A dry run still calls the LLM and spends tokens, but leaves no item rows, so the next real run
  is not affected.
- A manual run and the timer never overlap for the same job: the second one reports the lock
  (see [Stuck job locks](#stuck-job-locks)).
- Exit codes of `job run`: `0` succeeded, `1` failed or could not start, `2` partial.

## Inspecting runs and usage

```bash
invio-svc run list                              # newest first: id, job, start, duration, status
invio-svc run list --job daily-digest --limit 5
invio-svc run show 42                           # statistics, token usage and every stored error
```

`run show` lists each stored error with its stage, sanitized message and the item's title and URL
(or the source position for a source error). The statistics table shows `Found`, `New`,
`Relevant`, `Summarized`, `Failed`, `Sources failed` and `Skipped (budget)`. What the numbers
mean, and what to do with the common failure patterns, is in
[troubleshooting.md](troubleshooting.md).

Token usage and cost:

```bash
invio-svc usage                                            # one row per provider/model, all jobs
invio-svc usage --job daily-digest --since 2026-10-01      # one job, from a UTC day
invio-svc usage --by day --since 2026-10-01                # one row per UTC day
invio-svc usage --by job --json                            # one JSON document for scripts
```

Costs come from the current model registry (`src/invio/llm/models.d/*.yaml`) applied to the
summed tokens. A model without a price shows as `≥ $X` or `unknown`; a warning on stderr names
it.

## Health checks and monitoring

If `INVIO_HEALTHCHECK_URL` is set, every `run-due` and `notify retry` invocation pings the URL:
`<url>` on success, `<url>/fail` on exit 1 or on a configuration error (exit 2). Treat the URL
as a secret. The ping never changes the exit code, and a failing ping is only logged as
`healthcheck.failed`.

Without a healthcheck, watch for:

- `systemctl list-units --failed 'invio-*'` shows a failed run or notify retry.
- `journalctl -u invio-run-due -o cat --since -1h | grep -c '"level": "ERROR"'` counts errors.
- No `run-due` lines for an hour means the timer is stopped; see `systemctl list-timers 'invio-*'`.

## Related pages

- [deployment.md](deployment.md): installation, role variables, updating, sandbox, digest archive.
- [troubleshooting.md](troubleshooting.md): symptom, cause and fix for the common failures.
- [backup.md](backup.md): database dumps, restore test, and what else to keep.
- [configuration.md](configuration.md): every `INVIO_*` setting and job field.
- [security.md](security.md): secret handling and the source guards.
