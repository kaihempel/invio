# Feature Specification: Unattended systemd Deployment with Hardening and Ansible Provisioning

**Feature Branch**: `gh-issue-24`

**Created**: 2026-10-07

**Status**: Draft

**Input**: User description: "GitHub issue #24 — [CHORE] Add systemd units, hardening and Ansible role snippet. Context: The software (CLI `scout`) runs autonomously on the user's Debian server under systemd, without Docker. Deployment must be reproducible with the existing Ansible/DebOps setup. Depends on #23. Requirements: (1) deploy/systemd/scout-run-due.service: Type=oneshot, dedicated user `scout`, EnvironmentFile=/etc/scout/scout.env, ExecStart=/opt/scout/.venv/bin/scout run-due. (2) deploy/systemd/scout-run-due.timer: OnCalendar=*:0/15, Persistent=true, RandomizedDelaySec=60. (3) scout-notify-retry.service/.timer running hourly to retry failed notifications. (4) Hardening on services: NoNewPrivileges=yes, ProtectSystem=strict, ProtectHome=yes, PrivateTmp=yes, ReadWritePaths=/var/lib/scout, MemoryMax, TimeoutStartSec=3h. (5) Ansible role snippet: create user, install venv, env file with mode 0600, install units, systemctl enable --now, create MariaDB database and user. (6) Document the update procedure (uv sync, scout db upgrade, restart timer). Acceptance criteria: systemd-analyze verify passes for all units; timer fires every 15 minutes and survives reboot (Persistent=true); env file readable only by root and the scout user; the Ansible snippet provisions a working installation on a fresh Debian host; journalctl -u scout-run-due shows JSON log lines."

## Clarifications

### Session 2026-10-07

- Q: Should the deployment use the repository's `invio` names or the `scout` names from the issue? → A: Use `invio` everywhere (units, service account, paths); `scout` is the issue's working name.
- Q: Should the Ansible role install MariaDB itself, or expect an existing MariaDB server? → A: Install a local MariaDB server only when a role variable enables it (on by default); turn it off when DebOps or a remote server provides one.
- Q: How should the Ansible role get the invio code onto the server? → A: Check out a configurable Git ref (tag or commit, default: a release tag) into `/opt/invio` on the host, then `uv sync --locked --no-dev`.
- Q: Should updates be done by re-running the Ansible role with a new Git ref, or by a manual procedure only? → A: The role also handles updates (on a ref change it stops both timers, waits for running services, syncs dependencies, runs `invio db upgrade`, starts the timers again); the manual procedure is documented as well.
- Q: How should the deployment acceptance criteria be checked automatically? → A: Static checks in CI on every PR (unit verification, Ansible lint, tests that inspect unit settings), plus a separate opt-in CI job (manual trigger or `deploy/` path filter) that applies the role to a Debian container with systemd and checks timers, env file permissions, database and JSON journal output; reboot and timer timing are checked manually.

## User Scenarios & Testing *(mandatory)*

The "user" of this feature is the **operator**: the person who runs invio on their own Debian
server and manages that server with their existing Ansible/DebOps setup. The operator expects
research jobs to run on schedule without anyone watching, failed mails to be resent without
manual work, and the installation to be reproducible and locked down.

### User Story 1 - Due jobs run on schedule without supervision (Priority: P1)

The operator installs the scheduled run units on the server. From then on the system checks for
due research jobs every 15 minutes and runs them as a dedicated, unprivileged service account,
with no one logged in and no container runtime. If the server was off or rebooting when a check
was due, the check is made up once after boot.

**Why this priority**: This is the reason for the feature. Without a scheduled trigger, the
scheduling logic from #23 never runs on the server and no digests are produced.

**Independent Test**: Install the run-due service and timer units on a Debian host that already
has invio installed and configured. Check that the timer is active, that its next trigger falls
on a 15-minute boundary (plus at most 60 seconds of random delay), that one triggered run executes
`run-due` as the service account, and that the timer is still scheduled after a reboot.

**Acceptance Scenarios**:

1. **Given** the run-due units are installed and enabled, **When** the operator lists the
   system's timers, **Then** the run-due timer is active and its next trigger is at a quarter
   hour (:00, :15, :30, :45), delayed by no more than 60 seconds.
2. **Given** the timer fires, **When** the triggered service runs, **Then** it runs `run-due`
   once as the dedicated service account (not root), finishes, and the service returns to the
   inactive state until the next trigger.
3. **Given** the host was powered off across one or more scheduled triggers, **When** it boots
   again, **Then** the missed trigger is made up once soon after boot, and the regular 15-minute
   schedule then continues.
4. **Given** a run-due execution is still in progress when the next trigger is due, **When** the
   timer elapses, **Then** no second, overlapping execution of the service is started.
5. **Given** `run-due` exits non-zero, **When** the operator checks the service status, **Then**
   the service is shown as failed with the exit code, and the timer still fires at the next
   quarter hour.

---

### User Story 2 - Run logs are readable in the system journal (Priority: P1)

The operator troubleshoots unattended runs after the fact by reading the system journal for the
run-due service. Every log line of a run is one structured JSON object that carries the job and
run identifiers, and no secret ever shows up there.

**Why this priority**: Unattended runs can only be debugged from their logs (constitution
principle V). Without readable journal output, the operator cannot see why a run failed.

**Independent Test**: Trigger the run-due service once, then read its journal entries; check that
every message line written by invio parses as a JSON object, and that run lines carry `job` and
`run_id`.

**Acceptance Scenarios**:

1. **Given** the run-due service has run at least once, **When** the operator reads the journal
   for the run-due unit, **Then** the log lines written by invio's logging are JSON objects, one
   per line. Command result lines on stdout (for example `nothing to retry`) stay plain text.
2. **Given** a job ran during the service execution, **When** the operator filters its log lines,
   **Then** each line from that run carries the job name and the run id.
3. **Given** the env file holds database, SMTP and LLM credentials, **When** the operator reads
   the journal for either service, **Then** none of those credential values appear in it.

---

### User Story 3 - Failed notifications are retried automatically (Priority: P2)

The operator installs a second service and timer that, once per hour, resend digest mails that
failed earlier (for example because the mail server was down), using the existing notification
retry command.

**Why this priority**: Digests are still stored when delivery fails, so nothing is lost. But
without an automatic retry the operator must notice the failure and resend by hand, which defeats
unattended operation.

**Independent Test**: Install the notify-retry units, mark a stored notification as failed, start
the retry service once, and check that the notification is resent and that the timer's next
trigger is within the next hour.

**Acceptance Scenarios**:

1. **Given** the notify-retry units are installed and enabled, **When** the operator lists the
   timers, **Then** the notify-retry timer is active and fires once per hour.
2. **Given** a failed notification exists and the mail server is reachable, **When** the
   notify-retry service runs, **Then** the notification is resent and its state becomes `sent`.
3. **Given** there is nothing to retry, **When** the notify-retry service runs, **Then** it ends
   successfully without sending mail.
4. **Given** the host was off when an hourly retry was due, **When** it boots, **Then** the missed
   retry is made up once.

---

### User Story 4 - Services run locked down (Priority: P2)

The operator wants a compromise or bug in invio, which fetches untrusted web content, to be
contained. Both services therefore run with a hardened sandbox: no privilege escalation, a
read-only system, no access to home directories, a private temporary directory, write access only
to the invio state directory, a memory ceiling and an upper bound on run time.

**Why this priority**: invio processes content from the open web on the operator's own server.
Hardening limits the damage of a fault, but the feature still works without it, so it ranks below
scheduling and logging.

**Independent Test**: Inspect the effective sandbox settings of both services, and run a
diagnostic command under the same sandbox to check that writing outside the state directory and
reading home directories both fail, and that writing inside the state directory succeeds.

**Acceptance Scenarios**:

1. **Given** the hardened services, **When** a process in the service tries to write outside the
   state directory (for example under `/opt` or `/etc`), **Then** the write is denied.
2. **Given** the hardened services, **When** a process in the service tries to read a user's home
   directory, **Then** the home directory is not accessible.
3. **Given** the hardened services, **When** a process in the service writes into the state
   directory, **Then** the write succeeds.
4. **Given** a run-due execution exceeds its memory ceiling, **When** the limit is hit, **Then**
   the service is stopped and marked failed, and the next timer trigger runs normally.
5. **Given** a run-due execution still runs after 3 hours, **When** the start timeout elapses,
   **Then** the service is stopped and marked failed, and the next timer trigger runs normally.
6. **Given** any process in the service, **When** it tries to gain more privileges (for example
   through a setuid binary), **Then** this is refused.

---

### User Story 5 - One Ansible run provisions a working installation (Priority: P1)

The operator adds the provided Ansible role snippet to their existing Ansible/DebOps inventory
and runs it against a fresh Debian host. The run creates the service account, installs invio into
its own virtual environment, writes the protected env file, creates the MariaDB database and its
dedicated database user, applies the database schema, installs the units and enables and starts
the timers. Running it a second time changes nothing.

**Why this priority**: The deployment must be reproducible (issue context). A manual setup that
cannot be repeated breaks the operator's infrastructure-as-code workflow, and every other story
depends on a working installation.

**Independent Test**: Apply the role to a fresh Debian host (VM or container with systemd) with a
minimal set of variables, then check that both timers are active, the env file has the required
owner and mode, the database and user exist, the schema is current, and a manual start of the
run-due service succeeds. Apply the role again and check that it reports no changes.

**Acceptance Scenarios**:

1. **Given** a fresh, supported Debian host without MariaDB and the documented role variables
   (database password, SMTP and LLM settings) with default MariaDB handling, **When** the operator
   applies the role, **Then** a local MariaDB server is installed, the run succeeds, and both
   timers are enabled and active.
2. **Given** a host whose MariaDB server is managed by DebOps and the "manage MariaDB server"
   variable disabled, **When** the operator applies the role, **Then** the role leaves the server
   package and configuration untouched and only creates the invio database and user.
3. **Given** the role has been applied, **When** the operator inspects the env file, **Then** it
   is owned by the service account, has mode `0600`, and can be read only by root and the service
   account.
4. **Given** the role has been applied, **When** the operator connects to MariaDB with the
   provisioned credentials, **Then** the dedicated database exists, the dedicated user can access
   only that database, and the schema is at the latest revision.
5. **Given** the role has been applied once, **When** it is applied again with the same
   variables, **Then** it reports no changes and the timers keep running.
6. **Given** a required role variable (for example the database password) is missing, **When**
   the operator applies the role, **Then** it stops early with a message naming the missing
   variable and changes nothing on the host.
7. **Given** secret values in the role variables, **When** the role runs, **Then** the secrets do
   not appear in the Ansible output.

---

### User Story 6 - Updating an installation is safe and repeatable (Priority: P3)

The operator updates invio to a new version by changing the Git ref in the role variables and
re-running the role. The role stops both timers, waits for running services to finish, updates
the code and its locked dependencies, applies database migrations, and starts the timers again.
The same steps are documented as a manual procedure for hosts without Ansible. No run is cut off
halfway and no run starts against a half-migrated schema.

**Why this priority**: Updates happen rarely, and provisioning (User Story 5) already gives a
working host. But a missed migration step or an update during a running job breaks unattended
operation.

**Independent Test**: On a provisioned host, re-run the role with a newer release tag and check
that the schema is upgraded, the timers are active again, and the next scheduled run succeeds;
repeat the same update on a second host by following the manual procedure.

**Acceptance Scenarios**:

1. **Given** a provisioned host and a newer invio release tag, **When** the operator re-runs the
   role with that tag, **Then** dependencies are installed exactly as locked, the database is
   migrated to the latest revision, and both timers are active afterwards.
2. **Given** the same situation on a host without Ansible, **When** the operator follows the
   documented manual procedure (stop both timers, check out the new ref,
   `uv sync --locked --no-dev`, `invio db upgrade`, start the timers), **Then** the outcome is the
   same as in scenario 1.
3. **Given** a run-due or notify-retry execution is in progress, **When** an update starts (role or
   manual), **Then** both timers are stopped first and the update waits for the running execution
   to finish before code or schema change.
4. **Given** the migration step fails during a role run, **When** the role stops, **Then** the
   timers stay stopped, the role reports the failed step, and the documentation tells the operator
   how to check the state and roll back.
5. **Given** the role is re-run with an unchanged Git ref, **When** it finishes, **Then** the timers
   were not stopped and no migration step reported a change.

---

### Edge Cases

- The env file is missing or unreadable: the service fails at start with a clear status, and no
  run starts with partial configuration.
- The env file contains an unknown or misspelled `INVIO_*` key: invio logs the existing "unknown
  setting ignored" warning to the journal; the run is not blocked.
- The database is unreachable when the timer fires: `run-due` exits non-zero, the service is
  shown as failed, and the next trigger tries again; the timer is not disabled.
- The host clock jumps (NTP correction, suspend/resume): at most one made-up trigger, never a
  burst of back-to-back runs.
- A run is still in progress when the next 15-minute trigger is due: the timer does not start a
  second instance, and overlapping job runs are also prevented by the per-job run lock from #23.
- The state directory does not exist (for example after manual cleanup): systemd recreates it
  with the correct owner before the service starts, so the service does not fail on a read-only
  system.
- The configured Git ref does not exist: the role fails before it stops anything, and the timers
  keep running the installed version.
- The lock file of the new ref doesn't match its project: the role fails after it has stopped the
  timers but before the existing virtual environment is changed. The timers stay stopped (FR-021)
  until the operator fixes the ref and runs the role again.
- The "manage MariaDB server" variable is disabled but no server is reachable: the role stops
  with a message naming the configured database host, before it writes units or enables timers.
- MariaDB is installed but the database or user already exist (re-provisioning): the role keeps
  them, updates the password only if it changed, and never drops data.
- Optional browser rendering is installed (the `render` extra): it must work inside the sandbox
  (writable cache under the state directory) or the role must not install it by default.
- An update runs while the notify-retry service is active: the procedure stops both timers, not
  only run-due.

## Requirements *(mandatory)*

### Functional Requirements

**Naming**

- **FR-020**: All shipped units, the service account and all paths MUST use the `invio` name:
  `invio-run-due.service/.timer`, `invio-notify-retry.service/.timer`, account `invio`,
  `/etc/invio/invio.env`, `/opt/invio`, `/var/lib/invio`.

**Scheduled execution**

- **FR-001**: The repository MUST ship a run-due service unit that runs the `run-due` command of
  the installed invio CLI once per activation (one-shot), as the dedicated service account, and
  loads its configuration from the protected env file.
- **FR-002**: The repository MUST ship a run-due timer unit that activates the run-due service
  every 15 minutes on the quarter hour, with a random delay of up to 60 seconds, and that makes up
  a missed activation after downtime (persistent timer).
- **FR-003**: The repository MUST ship a notify-retry service unit that runs the existing
  notification retry command once per activation, as the same service account with the same
  env file, and a notify-retry timer that activates it once per hour and makes up a missed
  activation after downtime.
- **FR-004**: The timer units MUST be enabled for the system's normal boot target so that they are
  active again after every reboot without manual action.
- **FR-005**: A service's exit status MUST be reported unchanged to systemd, so a failed `run-due`
  or retry shows as a failed service; a failed activation MUST NOT stop later timer activations.

**Hardening**

- **FR-006**: Both services MUST run with privilege escalation disabled, the system directories
  read-only, home directories inaccessible, and a private temporary directory.
- **FR-007**: Both services MUST have write access only to the invio state directory
  (`/var/lib/invio`); systemd MUST create and own that directory for the service account.
- **FR-008**: Both services MUST have a memory ceiling. The run-due service MUST have a start
  timeout of 3 hours; the notify-retry service MUST have a shorter start timeout suited to an
  hourly job.
- **FR-009**: Both services MUST keep network access (sources, LLM providers, SMTP, database);
  hardening MUST NOT break any of the features they run.
- **FR-010**: All shipped unit files MUST pass systemd's offline unit verification without errors
  or warnings.

**Configuration and secrets**

- **FR-011**: The env file MUST be owned by the service account, have mode `0600`, and lie in a
  configuration directory that is not world-readable, so that only root and the service account
  can read it.
- **FR-012**: The repository MUST ship an example env file for the server that lists every
  setting the services need, with placeholder values and no real secrets.

**Logging**

- **FR-013**: Both services MUST send invio's log output to the system journal, tagged with the
  unit name, so that it can be read per unit; each invio log line MUST appear in the journal as
  one JSON object.

**Provisioning**

- **FR-014**: The repository MUST ship an Ansible role snippet that, on a fresh supported Debian
  host:
  - creates the dedicated system account (no login shell, no home directory content, system UID);
  - installs the Python toolchain, `git` and `uv`, checks out the configured Git ref (tag or
    commit; default: a release tag) of the invio repository into `/opt/invio`, and installs invio
    with exactly its locked runtime dependencies (no development dependencies) into the virtual
    environment `/opt/invio/.venv`; the checkout is owned by root and not writable by the service
    account;
  - writes the env file from role variables with the ownership and mode from FR-011;
  - installs and starts a local MariaDB server when the role's "manage MariaDB server" variable
    is enabled (the default);
  - creates the MariaDB database (UTF-8, `utf8mb4`) and a dedicated database user whose
    privileges are limited to that database, on the local server or on the configured existing
    server;
  - applies the database schema migrations;
  - installs the four unit files, reloads systemd, and enables and starts both timers.
- **FR-015**: The role MUST be idempotent: a second run with the same variables reports no
  changes.
- **FR-016**: The role MUST check that required variables are set before it changes anything, and
  stop with a message naming any missing one.
- **FR-017**: The role MUST keep secret variables out of its output (no logging of tasks that
  handle them).
- **FR-018**: The role MUST fit the operator's existing Ansible/DebOps setup: it is a
  self-contained role with documented default variables. With the "manage MariaDB server"
  variable disabled, it MUST NOT install, configure or restart a MariaDB server and only manages
  the invio database and user on the configured (local or remote) server.
- **FR-021**: When the configured Git ref differs from the installed one, the role MUST perform an
  update in this order: stop both timers, wait until neither service is active, check out the new
  ref, sync the locked dependencies, run `invio db upgrade`, start both timers. If any step fails,
  the role MUST stop and leave the timers stopped. With an unchanged ref, the role MUST NOT stop
  the timers or run these steps.

**Verification**

- **FR-022**: Every pull request MUST run static deployment checks in CI: offline verification of
  all four unit files, linting of the Ansible role, and automated tests that assert the required
  unit settings (schedule, persistence, random delay, service account, env file, hardening options,
  timeouts, memory ceilings) are present.
- **FR-023**: The repository MUST provide a separate CI job, started manually or by changes under
  `deploy/`, that applies the role to a Debian container running systemd and checks: both timers
  enabled and active, env file owner and mode, database and user present with the schema at the
  latest revision, a successful manual run-due start, JSON lines in the run-due journal, and a
  second role run with zero changes. This job MUST NOT be required for normal pull requests.
- **FR-024**: The container job (FR-023) MUST also check the timer schedule (the next run-due
  trigger is on a quarter hour plus at most 60 s), catch-up after a restart of the container's
  systemd with a missed trigger (exactly one catch-up start), and the sandbox probes (writes
  outside `/var/lib/invio` denied, home directories hidden, privilege escalation refused). Only
  checks that need real wall-clock hours or a physical host (four on-time activations over one
  hour, and the timed manual update) MAY remain on a manual verification checklist in the
  deployment documentation.

**Documentation**

- **FR-019**: The documentation MUST describe installation with the role, manual installation
  without Ansible (copying the units), how to check timers and read logs, updating with the role
  (changing the Git ref), and the manual update procedure: stop both timers and wait for running
  services, update the code and locked
  dependencies, apply database migrations, verify, start the timers again, plus what to do when a
  migration fails.

### Key Entities

- **Service account**: The unprivileged system user and group that both services run as; owns
  the state directory and the env file.
- **Env file**: The server-side configuration file with all `INVIO_*` settings, including
  secrets (database URL, SMTP and LLM credentials); readable only by root and the service account.
- **Run-due service / timer**: The pair that triggers the scheduler from #23 every 15 minutes.
- **Notify-retry service / timer**: The pair that resends failed notifications once per hour.
- **State directory**: The only writable location for the services (`/var/lib/invio`).
- **Role variables**: The operator-provided inputs to the Ansible role (database name, user and
  password, SMTP and LLM settings, Git repository URL and ref, "manage MariaDB server" switch,
  memory limit), with defaults where safe.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All four shipped unit files pass offline unit verification with zero errors and
  zero warnings.
- **SC-002**: On a provisioned host, the run-due service is activated within 60 seconds after
  each quarter hour, over at least one observed hour (4 of 4 activations).
- **SC-003**: After a reboot that skipped at least one scheduled activation, exactly one made-up
  run-due activation happens within 2 minutes of boot, and the regular schedule continues.
- **SC-004**: Applying the role to a fresh supported Debian host completes with zero failed tasks,
  and a manual start of the run-due service right after it succeeds (exit code 0 with no due jobs).
- **SC-005**: A second application of the role reports 0 changed tasks.
- **SC-006**: The env file can be read by root and the service account and by no other local
  account (checked with an unprivileged test user).
- **SC-007**: 100% of the lines invio's logging writes to the journal for the run-due unit parse
  as JSON objects (stdout result lines are excluded), and no journal line contains a configured
  secret value.
- **SC-008**: Inside the service sandbox, 100% of write attempts outside the state directory and
  of read attempts on home directories fail, and writes inside the state directory succeed.
- **SC-009**: Re-running the role with a newer release tag updates the host with zero failed tasks,
  and no service execution is interrupted by the update; an operator who knows systemd but not
  invio can do the same update with the manual procedure in under 10 minutes, without extra steps.

## Assumptions

- **Naming**: The issue uses the working name `scout`. This repository's CLI, package and
  settings prefix are `invio` (`invio` console script, `INVIO_*` variables), and earlier specs
  (e.g. #21) already mapped `scout/` to `src/invio/`. This feature therefore uses `invio`
  throughout: units `invio-run-due.service/.timer` and `invio-notify-retry.service/.timer` under
  `deploy/systemd/`, service account `invio`, env file `/etc/invio/invio.env`, install directory
  `/opt/invio` with `/opt/invio/.venv/bin/invio`, state directory `/var/lib/invio`. The journal
  check from the acceptance criteria becomes `journalctl -u invio-run-due`. These names are fixed
  (confirmed in clarification); no `scout` names or aliases are shipped.
- **Commands**: The `run-due` command comes from #23 (open dependency) and is invoked as
  `invio run-due`. Notification retry uses the existing `invio notify retry`; the schema update
  uses the existing `invio db upgrade`. The issue's "scout db upgrade" maps to `invio db upgrade`.
- **Configuration loading**: systemd puts the env file's variables into the process environment.
  invio reads real environment variables first, so `INVIO_ENV_FILE` is not needed and no `.env`
  is read from the working directory.
- **Logging**: invio already writes one JSON object per line to stderr (constitution V); the
  units only need to route stderr to the journal (the systemd default).
- **Timer overlap**: systemd does not start a one-shot service that is still active, so the
  timer cannot cause overlapping run-due executions; the per-job lock from #23 is the second line
  of defence.
- **Supported platform**: Debian 12 (bookworm) and Debian 13 (trixie) with systemd and a local or
  reachable MariaDB server. Docker is out of scope.
- **Memory ceiling**: The default memory ceiling is 1 GiB for run-due and 256 MiB for
  notify-retry, adjustable through a role variable; the notify-retry start timeout defaults to
  15 minutes.
- **Install source**: The role checks out a configurable Git ref (default: a release tag; a
  branch name is allowed but not recommended) onto the host and runs `uv sync --locked --no-dev`
  (confirmed in clarification), matching the CI rule that dependencies are locked. The host needs
  outbound access to the Git remote and the package index; a private remote needs a deploy key
  supplied by the operator. Wheel builds on the controller and copying the working tree are not
  used.
- **MariaDB server**: The role installs a local MariaDB server by default (confirmed in
  clarification) so a fresh host works with one role run; DebOps users or setups with a remote
  server disable this and the role then only manages the invio database and user. Server tuning,
  backups and TLS for the database connection stay out of scope.
- **Verification split** (confirmed in clarification, narrowed in analysis): SC-001 is checked in
  every CI run. The opt-in container job checks SC-003 (catch-up after a systemd restart), SC-004
  to SC-008, the schedule part of SC-002 (next trigger on the quarter hour) and the role part of
  SC-009. Only the hour-long observation in SC-002 and the timed manual update in SC-009 stay on
  the manual checklist, recorded as a justified deviation in the plan.
- **Out of scope**: Packaging (`.deb`), log shipping, monitoring and alerting on failed units,
  Playwright/browser rendering inside the sandbox (not installed by default), and multi-host
  setups.
- **Dependencies**: Requires #23 (`run-due` with locking and missed-run handling). The existing
  notification retry (#20) and database migrations are reused without changes.
