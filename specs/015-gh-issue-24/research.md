# Research: Unattended systemd Deployment (gh-issue-24)

Feature: [spec.md](spec.md) · Plan: [plan.md](plan.md)

## R1 — Python runtime on Debian 12 and 13

**Decision**: The role installs a **uv-managed CPython 3.12** into `/opt/invio-python`
(`UV_PYTHON_INSTALL_DIR`, root-owned, world-readable; deliberately **outside** the Git checkout
`/opt/invio`, because `git clone` refuses a non-empty destination on a first install and an
untracked interpreter tree inside the checkout would be confusing) and builds the venv against it
(`UV_PYTHON_PREFERENCE=only-managed`, `UV_PYTHON=3.12`). It does not use the system Python.

**Rationale**: The project requires Python ≥ 3.12 (`pyproject.toml`). Debian 12 ships 3.11, so
a system interpreter fails there. Debian 13 ships 3.13, which works but differs from CI (3.12).
A managed interpreter gives the same runtime on both supported releases and matches CI. uv's
default install location is under `/root/.local/share/uv`, which `ProtectHome=yes` hides from
the service, so the venv's interpreter symlink would break. That is why the install dir is
moved to `/opt/invio-python`.

**Alternatives considered**:
- *System Python*: breaks on Debian 12.
- *deadsnakes/backports*: not available for Debian; building from source is slow and adds
  build dependencies.
- *Python 3.13 on both*: CI and mypy target 3.12; mixing versions adds risk for no gain.

## R2 — Installing `uv` on the host

**Decision**: Download a **pinned uv release tarball** (`invio_uv_version`, default the version
used in CI) from GitHub with `ansible.builtin.get_url` and a pinned SHA-256 checksum, and unpack
`uv` into `/usr/local/bin`.

**Rationale**: uv is not packaged in Debian 12. A pinned version with a checksum is reproducible
and keeps the role idempotent (`get_url` skips the download when the checksum matches). The
`curl | sh` installer is neither pinned nor checked.

**Alternatives considered**: `pip install uv` into the system Python (PEP 668 blocks this on
Debian 12+); the official install script (unpinned, piped to a shell).

## R3 — Code checkout and dependency sync

**Decision**:
1. `ansible.builtin.git` checks out `invio_git_repo` at `invio_git_version` into `/opt/invio`,
   owned by root (`0755`, not writable by `invio`). Private repositories use an optional
   `invio_git_key_file` (deploy key).
2. `uv sync --locked --no-dev --no-editable --compile-bytecode` runs as root with
   `UV_PROJECT_ENVIRONMENT=/opt/invio/.venv` and `UV_CACHE_DIR=/var/cache/invio-uv` (root-only).
   `changed_when` is true when uv's output says packages were installed or uninstalled.
3. The `render` extra (Playwright) is not installed (out of scope, spec Assumptions).

**Rationale**: `--locked` matches constitution IV (CI installs with `--locked`) and fails if
`uv.lock` and `pyproject.toml` disagree. `--no-editable` installs invio as a normal package, so
the venv does not depend on files that change during a later checkout. `--compile-bytecode`
pre-compiles `.pyc`, because the venv is read-only for the service (`ProtectSystem=strict`).
The service also sets `PYTHONDONTWRITEBYTECODE=1` so nothing tries to write there.

**Alternatives considered**: wheel built on the controller, or copying the working tree
(rejected in clarification Q3); `pip install` (bypasses the lock file).

## R4 — Update detection and safe ordering (FR-021)

**Decision**: The role determines whether an update is needed **before** touching anything:

1. Run the `git` task with `check_mode: true` and register the result. `changed` means the
   checked-out commit would change (or `/opt/invio` does not exist yet).
2. If it changed **and** the timers already exist (an update, not a first install): stop
   `invio-run-due.timer` and `invio-notify-retry.timer`, then poll
   `systemctl is-active invio-run-due.service invio-notify-retry.service` until neither is
   `active` or `activating`. The poll waits up to `TimeoutStartSec` (3 h) plus a margin.
3. Real checkout → `uv sync` → `invio db upgrade` → write the deployed commit to
   `/etc/invio/deployed-revision` → `daemon-reload` → start the timers.
4. Any failing step fails the play. The timers are started only by the last step, so after a
   failure they stay stopped (FR-021). The docs explain how to recover.

A ref that doesn't exist already fails in the check-mode probe (step 1), before anything is
stopped, so the timers keep running the installed version. A lock-file mismatch only shows up in
`uv sync --locked` (step 3), after the timers were stopped. `--locked` fails before it changes
the venv, and the timers stay stopped (spec edge cases).

`invio db upgrade` runs only when `/etc/invio/deployed-revision` differs from the target commit
or is missing. This makes the step idempotent (SC-005) and runs it again after a run that
failed between checkout and migration.

**Rationale**: The `git` module only reports a change after it has already checked out, which
is too late to stop the timers first. Check mode gives the answer without side effects. A
oneshot service is `activating` while it runs, so the wait checks both states.

**Alternatives considered**: comparing `git rev-parse HEAD` with `git ls-remote` (more code,
duplicates the module's logic); always stopping the timers (breaks "unchanged ref → no stop",
User Story 6 scenario 5).

## R5 — Running `invio db upgrade` from the role

**Decision**: Run it through
`systemd-run --wait --pipe --collect --quiet --uid=invio --gid=invio -p EnvironmentFile=/etc/invio/invio.env -p WorkingDirectory=/var/lib/invio -E PYTHONDONTWRITEBYTECODE=1 /opt/invio/.venv/bin/invio db upgrade`.

**Rationale**: This reads the env file with **exactly the same parser** as the services
(systemd's `EnvironmentFile` syntax), as the same user. Sourcing the file in a shell would parse
quotes and `$` differently and could pass a different URL than the service sees. Exit codes
(`0`/`1`/`2`) are passed through, so the task fails correctly.

**Alternatives considered**: `become_user: invio` with `environment:` built from role variables
(duplicates the env file and can drift from it); a fifth `invio-db-upgrade.service` unit (more
surface area for a step that runs only during deploys).

## R6 — Env file format, location and permissions

**Decision**:
- `/etc/invio/` is `root:invio 0750`; `/etc/invio/invio.env` is `invio:invio 0600`
  (spec FR-011, issue: "0600", "readable only by root and the scout user").
- The template writes `KEY="value"` lines. Values are escaped for systemd's `EnvironmentFile`
  rules (`\` → `\\`, `"` → `\"`; newlines are rejected by role validation). The database password
  is URL-encoded into `INVIO_DATABASE_URL` (`urlencode` filter).
- `no_log: true` on the template task and on every task that uses secret variables (FR-017).
- The repository ships `deploy/env/invio.env.example` with every key the services use and
  placeholder values (FR-012).

**Rationale**: systemd (PID 1) reads `EnvironmentFile` as root before it drops privileges, so
the service would work even with `root:root 0600`. The spec asks for the file to be readable by
the service account, so it can also be used for manual `invio` commands
(`sudo -u invio …`). An unescaped `"` or `\` in a password would silently change the value.
URL-encoding stops `@`, `:` or `/` in the password from breaking the URL.

**Alternatives considered**: `root:invio 0640` (the issue explicitly says `0600`);
`LoadCredential=` (systemd credentials; invio reads settings from the environment only, so this
would need code changes, which are out of scope).

## R7 — Unit design: scheduling

**Decision**:

| Setting | run-due | notify-retry |
|---|---|---|
| `OnCalendar` | `*:0/15` | `hourly` |
| `Persistent` | `true` | `true` |
| `RandomizedDelaySec` | `60` | `300` |
| `AccuracySec` | `1s` | `1min` (default) |
| `Unit` | `invio-run-due.service` | `invio-notify-retry.service` |
| `[Install] WantedBy` | `timers.target` | `timers.target` |

**Rationale**: The default `AccuracySec=1min` can add up to a minute on top of the random delay.
That would break SC-002 ("within 60 s after each quarter hour"), so run-due uses `1s`.
`Persistent=true` stores the last trigger time under `/var/lib/systemd/timers/` and runs a missed
trigger **once** after boot, never once per missed slot (SC-003). This matches #23's "overdue job
runs exactly once". systemd never starts a oneshot service that is still active, so a slow run
cannot overlap with the next trigger (spec Assumption "Timer overlap"). The 5-minute random delay
on notify-retry keeps it from always starting at the same time as the run-due trigger at :00.

**Alternatives considered**: `OnUnitActiveSec=15min` (drifts, and isn't persistent across
reboots in the same way); cron (the issue asks for systemd).

## R8 — Unit design: service and hardening

**Decision**: Both services share this base (values differ only where noted):

- `Type=oneshot`, `User=invio`, `Group=invio`, `EnvironmentFile=/etc/invio/invio.env`,
  `WorkingDirectory=/var/lib/invio`, `ExecStart=/opt/invio/.venv/bin/invio run-due` /
  `… notify retry`.
- `Wants=network-online.target`, `After=network-online.target mariadb.service`. Ordering after a
  unit that doesn't exist (remote DB) is a no-op.
- `Environment=PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/var/lib/invio XDG_CACHE_HOME=/var/lib/invio/cache`.
- `SyslogIdentifier=invio-run-due` / `invio-notify-retry`. stdout and stderr go to the journal
  (default).
- Required by the issue: `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome=yes`,
  `PrivateTmp=yes`, `ReadWritePaths=/var/lib/invio`, `MemoryMax=` (1G / 256M),
  `TimeoutStartSec=` (3h / 15min).
- `StateDirectory=invio` with `StateDirectoryMode=0750`. systemd creates `/var/lib/invio` owned
  by `invio` before each start (spec edge case) and makes it writable under
  `ProtectSystem=strict`. `ReadWritePaths` is kept because the issue requires it.
- Additional low-risk hardening that has no effect on a networked Python CLI:
  `PrivateDevices=yes`, `ProtectKernelTunables=yes`, `ProtectKernelModules=yes`,
  `ProtectKernelLogs=yes`, `ProtectControlGroups=yes`, `ProtectClock=yes`,
  `ProtectHostname=yes`, `RestrictSUIDSGID=yes`, `RestrictRealtime=yes`,
  `RestrictNamespaces=yes`, `LockPersonality=yes`, `SystemCallArchitectures=native`,
  `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`, `CapabilityBoundingSet=`, `UMask=0027`.
- **Not** set: `MemoryDenyWriteExecute` (libffi/lxml callbacks can need W^X exceptions; risk
  without measurable benefit), `IPAddressDeny`/`PrivateNetwork` (the services need the network),
  `SystemCallFilter` (left for later tuning with `systemd-analyze security`).

**Rationale**: The issue's list is the floor. The extras are the standard set that
`systemd-analyze security` recommends and that is safe for a process that only needs TCP/UDP
and a local socket (MariaDB over `/run/mysqld`). `AF_UNIX` stays allowed for the socket and the
journal. `HOME` and `XDG_CACHE_HOME` point at the state directory, so libraries that cache
(trafilatura, Whisper models later in #29) write somewhere writable. `INVIO_ARCHIVE_DIR` already
defaults to `/var/lib/invio/archive` in `.env.example`.

**Timeouts and locks**: #23 locks each job for `INVIO_RUN_LOCK_SECONDS` (default 7200 s). If
systemd kills a run at 3 h, `finalize` does not run and the lock expires on its own. #23 reclaims
stale locks, so the job runs again later and is not stuck. The docs say that the lock time should
be at least as long as one job's run time.

**Alternatives considered**: `DynamicUser=yes` (an ephemeral UID can't own the env file the
operator also uses manually); `Type=exec` with `Restart=` (scheduled one-shots should not
restart in a loop, and #23 handles retry delays).

## R9 — JSON lines in the journal

**Decision**: Nothing changes in the code. invio's logging already writes one JSON object per
line to stderr (README "Configuration & logging"). systemd sends stderr to the journal and tags
it with `SyslogIdentifier`. Commands print their result lines (for example `retried N, …`) to
**stdout** (constitution II), and those lines are plain text. The verification counts as "log
lines" only lines that come from the logging framework, which start with `{`, and requires every
one of them to parse as JSON with `level` and `message` keys (SC-007). It also checks that no
configured secret value appears in any journal line.

**Rationale**: Turning stdout into JSON or dropping it would change CLI behaviour (out of scope)
or lose useful summaries. journald records both streams with the same transport, so the check
tells them apart by content.

**Alternatives considered**: `StandardOutput=null` (loses the run summary);
`LogExtraFields`/`journal` native protocol (needs code changes).

## R10 — MariaDB handling (clarification Q2)

**Decision**:
- `invio_mariadb_manage_server: true` (default) installs `mariadb-server`, starts it, and
  administers it over the Unix socket as root (`login_unix_socket: /run/mysqld/mysqld.sock`; root
  uses `unix_socket` auth on Debian, so no admin password is needed).
- With `false`, the role installs nothing server-side. It connects to `invio_db_host` with
  `invio_db_admin_user` and `invio_db_admin_password` (required in that mode), and first checks
  that the server is reachable. If it isn't, the role fails before any unit or timer change (spec
  edge case).
- `community.mysql.mysql_db` creates `invio_db_name` (`encoding: utf8mb4`,
  `collation: utf8mb4_unicode_ci`). `community.mysql.mysql_user` creates
  `invio_db_user@invio_db_user_host` (default `localhost`) with `"{{ invio_db_name }}.*:ALL"`
  only. The `password_update` behaviour changes the password only when it differs (spec edge
  case). Nothing is ever dropped.
- Target-host Python dependency for the modules: Debian package `python3-pymysql`.
- Collections are listed in `deploy/ansible/requirements.yml` (`community.mysql`).

**Rationale**: This is the standard way to manage MariaDB with Ansible, it is idempotent, and it
coexists with `debops.mariadb` when the server switch is off. `ALL` on the invio schema is what
Alembic migrations need (CREATE/ALTER/INDEX/REFERENCES) and does not reach other databases (spec
User Story 5 scenario 4).

**Alternatives considered**: raw `mysql -e` commands (not idempotent, password in the process
list); taking over `debops.mariadb` variables (couples the role to DebOps internals).

## R11 — Units in the repository vs. units installed by the role

**Decision**: The canonical units live in `deploy/systemd/` and are installed **byte-for-byte**
(manual install and the role use the same files). Role variables that change unit values
(memory ceilings) are applied as a drop-in, `/etc/systemd/system/<unit>.d/50-invio-role.conf`,
which the role writes only when a value differs from the default. The role's `files/` directory
holds relative symlinks to `../../../../systemd/*`, and a test checks that they resolve to the
canonical files.

**Rationale**: One source of truth. `systemd-analyze verify` runs on exactly the files that
ship. Drop-ins are systemd's standard override mechanism and show up in `systemctl cat`.

**Alternatives considered**: templating the units in the role (two divergent copies, and the
templated output isn't covered by the static verify); hard-coding the memory limits (the spec
requires them to be adjustable).

## R12 — Static verification in CI (FR-022)

**Decision**: Add a new CI job `deploy-static` (runs on every push and PR). It:
1. runs `deploy/scripts/verify-units.sh`, which builds a temporary root with a stub executable at
   `/opt/invio/.venv/bin/invio`, copies the four units into `<root>/etc/systemd/system/`, runs
   `systemd-analyze verify --root=<root> <units>`, and **fails on any output** (zero errors and
   zero warnings, SC-001);
2. runs `ansible-lint deploy/ansible` (production profile);
3. runs the pytest module `tests/test_deploy_units.py`, which parses the unit files (INI-style,
   repeated keys allowed) and asserts every required directive and value from
   [contracts/systemd-units.md](contracts/systemd-units.md). It also checks that the role's
   symlinks resolve to the canonical units and that every key in `deploy/env/invio.env.example`
   is a known setting (`unknown_env_keys`). This module runs in the normal `checks` job as well.

The linting tools (`ansible-core`, `ansible-lint`, `molecule`, `molecule-plugins[docker]`) go in
a new **`deploy` dependency group** in `pyproject.toml`, locked in `uv.lock`. It is not one of the
default groups, so `uv sync --locked` for developers stays unchanged. The deploy jobs install it
with `uv sync --locked --only-group deploy`.

**Pre-commit parity (constitution IV)**: `.pre-commit-config.yaml` gets two local hooks with the
same configuration as CI:
- `ansible-lint` (`uv run --group deploy ansible-lint`, run in `deploy/ansible`,
  `files: ^deploy/ansible/`);
- `verify-units` (`deploy/scripts/verify-units.sh`, `files: ^deploy/systemd/`).

`systemd-analyze` doesn't exist on macOS, so `verify-units.sh --if-available` exits 0 with a
visible "skipped: systemd-analyze not found" notice there. CI calls it without the flag, so it
stays strict. This platform limit is recorded in the plan's Complexity Tracking. The contract
pytest already runs through the existing pytest gate.

**ansible-lint rules**: `no-log-password` is an opt-in rule, so `.ansible-lint` enables it
explicitly (`enable_list: [no-log-password]`).

**Rationale**: `systemd-analyze verify` checks that `ExecStart` binaries exist. Without the stub
root it would fail in CI, where `/opt/invio` doesn't exist. The pytest module documents the
contract in code and runs on macOS too, where `systemd-analyze` is missing. A dedicated group
keeps constitution IV ("dependencies locked, CI `--locked`") without adding these tools to every
developer setup.

**Alternatives considered**: `uvx ansible-lint@x.y` (version pinned in the workflow but not in
the lock file); running `systemd-analyze` from pytest with a skip (CI would silently pass if it
were skipped).

## R13 — Opt-in provisioning test (FR-023)

**Decision**: Use **Molecule with the Docker driver**, scenario `deploy/ansible/molecule/default`,
platform: a Debian 12 image with systemd as PID 1 (built from
`deploy/ansible/molecule/default/Dockerfile.j2`: `debian:12` + `systemd systemd-sysv`, run with
`privileged`, `cgroupns_mode: host`, `/sys/fs/cgroup` mounted rw, `command: /lib/systemd/systemd`).
A second platform for Debian 13 runs in the same scenario.

- The repository is mounted read-only into the container at `/src`. The scenario sets
  `invio_git_repo: /src` and `invio_git_version: <commit under test>` (CI passes
  `${{ github.sha }}`; the checkout uses `fetch-depth: 0`). The image sets
  `git config --system safe.directory /src`.
- Molecule sequence: `dependency → create → prepare → converge → idempotence → side_effect →
  verify → destroy`. `idempotence` enforces SC-005. Molecule runs only **one** `side_effect`
  playbook per scenario, so all post-converge mutations (memory drop-in, update path, wait for a
  running service, reboot catch-up) live in one `side_effect.yml`, in that order. Each section
  restores the default state at its end.
- The Molecule Docker driver needs the `community.docker` collection. It is listed in
  `deploy/ansible/molecule/requirements.yml` (test-only) and installed by Molecule's `dependency`
  step. The role's `deploy/ansible/requirements.yml` only lists what production needs.
- **Schedule and catch-up** (SC-002 schedule part, SC-003): `verify.yml` reads
  `systemctl show invio-run-due.timer -p NextElapseUSecRealtime` and asserts that the minute is
  in {0, 15, 30, 45} and the second offset is at most 60 s. `side_effect.yml` does the catch-up
  check:
  1. stop the timer;
  2. set the stamp file `/var/lib/systemd/timers/stamp-invio-run-due.timer` to 1 h ago
     (`touch -d`);
  3. restart the container (Molecule `docker restart`, which restarts PID 1 systemd, the
     container equivalent of a reboot);
  4. assert that `invio-run-due.service` started exactly once within 2 min of boot
     (`journalctl -b -u invio-run-due.service | grep -c Started`).
- **Sandbox probes** (SC-008, US4): `verify.yml` runs `systemd-run --wait --pipe --collect` with
  every `[Service]` sandbox property copied from the unit (parsed with `systemctl show`) and the
  probe commands `touch /opt/invio/x`, `touch /etc/x` (expect failure), `ls -A /home`
  (expect empty), `touch /var/lib/invio/probe` (expect success) and `sudo -n true` (expect
  failure: the setuid bit is ignored under `NoNewPrivileges`, and the probe user `invio` has no
  sudo rule). The container runs privileged with systemd as PID 1, so mount namespaces and
  `NoNewPrivileges` behave like on a host.
- `verify.yml` asserts: both timers enabled and active, env file `invio:invio 0600`, an
  unprivileged test user can't read it, the database and user exist with access limited to the
  invio schema, the schema is at head (`invio db upgrade` prints the head revision and no
  migration runs), `systemctl start invio-run-due.service` exits 0, and the JSON and secret
  checks from R9 on `journalctl -u invio-run-due -o cat`.
- A second converge with a changed `invio_git_version` (the parent commit) followed by the
  original covers the update path (User Story 6, scenarios 1 and 5).
- A second scenario, `external-db`, runs a MariaDB service container next to the target with
  `invio_mariadb_manage_server: false`. It covers the DebOps or remote-server case (spec FR-018,
  User Story 5 scenario 2) and, with a wrong host, the "server unreachable" edge case.
- New workflow `.github/workflows/deploy.yml` runs it on `workflow_dispatch` and on `pull_request`
  with `paths: ["deploy/**", ".github/workflows/deploy.yml"]`. It is not a required check (FR-023).

**Rationale**: Molecule is the standard harness for role tests and gives idempotence checking for
free. The container is only a test fixture; production stays Docker-free (spec Out of scope).
Mounting the checkout tests the PR's own code instead of a published tag.

**Alternatives considered**: a hand-written script around `docker run` (would re-implement
idempotence checks); Vagrant/VM (not available on GitHub-hosted runners without nested
virtualisation; slow).

## R14 — Dependency on #23 (`run-due`)

**Decision**: The units call `invio run-due` as #23 defines it. Until #23 is merged, the Molecule
verify step "start `invio-run-due.service`" is guarded: if `invio run-due --help` fails, the
step is reported as skipped with the reason "requires #23". The rest of the verification still
runs. After #23 is merged, the guard is removed in that PR or in this one, whichever lands later.

**Rationale**: This lets the deployment work proceed in parallel (track `integration`, round 4)
without faking the command. The notify-retry and migration paths already work today.

**Alternatives considered**: a temporary stub command (could ship by accident and violates "no
speculative abstractions").

## R15 — Manual verification (FR-024)

**Decision** (narrowed after `/speckit-analyze`): Schedule, catch-up and sandbox checks are
automated in Molecule (R13). `docs/deployment.md` "Manual verification" keeps only what needs
real wall-clock hours or a person:
- four on-time activations over one real hour (`journalctl -u invio-run-due -o short-iso
  --since -1h`, 4 starts each within 60 s of :00/:15/:30/:45; SC-002 observation part);
- a timed manual update by an operator (SC-009 manual part);
- for information only: a real host reboot (`systemctl list-timers`, LAST right after boot)
  and `systemd-analyze security invio-run-due.service`.

**Rationale**: A one-hour observation window is impractical in CI, and the 10-minute update
goal measures a person. Everything else is automated, so the deviation from constitution III is
as small as possible (plan Complexity Tracking).
