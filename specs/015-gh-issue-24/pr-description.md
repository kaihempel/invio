# Unattended systemd deployment, hardening and Ansible role (#24)

Closes #24.

## Summary

invio can now run unattended on a Debian 12 or 13 server.

- `deploy/systemd/` holds four units: `invio-run-due.timer/.service` (every 15 minutes) and
  `invio-notify-retry.timer/.service` (hourly). Both services run `Type=oneshot` as the system
  account `invio` (the issue calls it `scout`; clarification Q1 renamed it to `invio`), read
  `/etc/invio/invio.env`, log to the journal under their own `SyslogIdentifier`, and are locked
  down (`NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`,
  `ReadWritePaths=/var/lib/invio`, `MemoryMax`, `TimeoutStartSec`, plus the extra set from
  research R8). Both timers are `Persistent=true`, so a missed trigger runs once after boot.
- `deploy/ansible/roles/invio` provisions a fresh host in one run: MariaDB (or an external
  server), the account, the env file, uv and a managed Python, the checkout, `uv sync --locked`,
  the migration, the units and the timers. A second run reports `changed=0`. Updates stop the
  timers, wait for running services, deploy, migrate and restart; any failure leaves the timers
  stopped.
- `deploy/env/invio.env.example`, `deploy/scripts/` (`verify-units.sh`, `check-journal-json.py`,
  `check-no-secrets.sh`), `docs/deployment.md` (install by hand, operate, update, recover,
  troubleshoot) and a README section.
- Tests: `tests/test_deploy_units.py` (unit contract, symlinks, env example, env template
  rendering and escaping, memory drop-in template), `tests/test_deploy_journal_check.py`, a
  `deploy-static` CI job (unit verification, ansible-lint, contract tests), pre-commit hooks, and
  the opt-in `deploy.yml` workflow (Molecule scenarios `default` on Debian 12 and 13, and
  `external-db`).

`src/invio/` is not changed.

## New dependency group (constitution: justify new dependencies)

`pyproject.toml` gets a non-default dependency group `deploy` (`ansible-core>=2.17,<2.19`,
`ansible-lint>=25`, `molecule>=25`, `molecule-plugins[docker]>=25`), locked in `uv.lock`. It is
development and CI only, is not in `default-groups`, and `uv sync --locked` for developers still
installs no Ansible package (checked). The deploy jobs use `--group deploy` /
`--only-group deploy` with `--locked`. No runtime dependency is added.

## Deviations from tasks.md and the contracts

| Item | What changed and why |
|---|---|
| D2 | `molecule-plugins[docker]>=25` resolves (26.9.28), so the lower bound `>=23.7` from the blueprint was not needed. The group stays as written in T002. |
| Collection | The role and the tests use `ansible.mysql` (`>=4.2.1`), not `community.mysql`. `community.mysql` 5.x only forwards to it and `ansible-lint` (production profile, `fqcn`) demands the canonical name. `requirements.yml` lists `ansible.mysql`. |
| D14 | `invio_uv_sha256` is a mapping keyed by `ansible_architecture` (`x86_64`, `aarch64`) instead of one string, so the role also works on arm64 hosts and arm64 CI/dev machines. Pinned to uv 0.12.23 (same `version:` in `setup-uv` in `deploy.yml`). Contract table "pinned values matching CI" still holds. |
| Python pin | `invio_python_version` is the full patch version `3.12.13`, and `uv python install --no-bin` keeps uv from writing into `/root/.local/bin` (reviewer M6). The change report comes from parsing uv's output. |
| D3 | The check-mode git probe does not fail for an unknown ref, so on the update path the role first runs `git fetch --tags origin` and `git rev-parse --verify --quiet <ref>^{commit}` (falling back to `origin/<ref>`), and fails with "does not exist" before it stops any timer. Verified by Molecule. |
| D4 | `validate.yml` rejects abbreviated SHAs (`^[0-9a-f]{7,39}$`): use a tag or a full 40-character SHA. |
| H2 | The update gate only verifies the ref, stops the timers and waits. Checkout, `uv sync` and the `deployed-revision` comparison run on every run, so a half-finished update repairs itself. A Molecule case leaves `deployed-revision` stale and checks that it is repaired. |
| M7 | The wait treats `deactivating` and `reloading` as busy too. |
| Checker exit codes | `check-journal-json.py`: 0 pass, 1 violation, **2 no input or usage error** (T019 said exit 1 for empty input), 3 internal error with a fixed message. It reads bytes with `errors="replace"`, never prints a line or a secret, rejects secrets shorter than 8 characters, ignores empty entries, matches raw, URL-encoded, backslash-escaped and JSON-escaped forms, and has `--allow-no-json` (used for notify-retry, whose only output can be `nothing to retry`). |
| `dbus` | The role installs `dbus` and starts `dbus.socket` because the migration and the probes use `systemd-run`, which needs the system bus (not present on a fresh container, harmless on a normal host). |
| Tags | The role additionally has the tag `invio:account`. |
| H1 / D9 | The journal contract is checked on the real `invio-run-due.service` and its sandbox. While `invio run-due` does not exist, a `/run/systemd/system/invio-run-due.service.d` drop-in swaps `ExecStart` for `invio db upgrade` (it logs JSON lines) and is removed afterwards. `invio-notify-retry.service` is started unguarded and `Result=success` is a hard gate. |
| M5 | The sandbox probe `getaddrinfo('localhost', 80, AI_ADDRCONFIG)` succeeds with `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`; `AF_NETLINK` is not needed and the contract is unchanged. |
| D18 / T053 | `ls -A /home` under `ProtectHome=yes` returns "Permission denied" (mode 000), not an empty listing, so the probe asserts empty **stdout** only. Extra probes: a path owned by `invio` outside `/var/lib/invio` stays read-only, and a readable file below `/home` stays hidden. The probe properties are read from the installed unit file, not from `systemctl show`. |
| Catch-up (T024) | The container shares its kernel boot id with Docker, so `journalctl -b` mixes earlier "boots". The test marks the time before the restart, counts `Unit starting` messages `--since` it, and compares `ExecMainStartTimestamp` with `UserspaceTimestamp` (at most 120 s). If a quarter hour is less than 360 s away it waits until 70 s after it (D10). Only run-due is counted: the notify-retry random delay (up to 300 s) would make the check slow and sensitive to the hour boundary, so its catch-up relies on `Persistent=true` (checked statically) and the identical mechanism proven for run-due. |
| T059 | "changed=0 on the unchanged run" is covered by the Molecule idempotence stage (D11); the side effect additionally asserts unchanged `ActiveEnterTimestamp` of both timers and unchanged mtimes of `deployed-revision`, `invio.env` and the unit files. |
| Molecule dependency | Molecule 26's galaxy dependency step reports "Missing roles requirements file" and does not use `requirements-file`. Collections are installed ahead of time into `deploy/ansible/.collections` (`ansible.cfg`, CI step, docs), also for `ansible-lint` (`offline: true`). |
| QA fixes | `validate.yml` reads values with `lookup('vars', item)`, so values that reference other variables (`vault_*`) are validated; `invio_env_extra` may not set keys the role writes (list `invio_managed_env_keys`, kept in step with the template by a test); the env template's `q()` refuses newlines as defence in depth; with the server switch off and `invio_db_host: localhost` the role now administers the server over the root socket (no admin variables needed, as the contract says; it waits for `/run/mysqld/mysqld.sock` instead of TCP); `verify-units.sh` verifies every copied unit; `check-no-secrets.sh` exits 2 when all secrets are empty. `contracts/ansible-role.md` now states the full Python patch version (3.12.13) and the per-architecture `invio_uv_sha256` mapping. |
| Test repository copy | D24 mounts the repository read-only at `/src`. Docker Desktop on macOS fails object reads of the checkout tool on that mount with "Permission denied" (flaky, even for a fresh clone). Both `prepare.yml` files therefore copy `/src` with `git clone --no-hardlinks` to `/srv/invio-src` on the container's own file system, and `invio_git_repo` is `/srv/invio-src` (the update test resolves `REF~1` there). The mount and the `safe.directory` entry in `/etc/gitconfig` are still used for that copy. Harmless on CI. |
| `deploy.yml` | Besides the T042 paths it also watches `pyproject.toml`, `uv.lock` and `src/invio/db/**`, and has a weekly schedule. The update test converges `REF~1`, which is the first parent (the base branch tip on pull requests, because CI tests the merge commit). |
| `deploy-static` | Uses `uv run --locked --group deploy ...` and also runs `tests/test_deploy_journal_check.py`. `verify-units.sh` copies the host's `*.target` files into the temporary root because `systemd-analyze --root` otherwise cannot find `sysinit.target`, and it propagates systemd-analyze's exit code. |
| Env template | Values are written as `KEY="value"` through a `q()` macro; DB user and password are URL-encoded with `/` encoded too (D22). The tests render the template with plain Jinja (`StrictUndefined`, `autoescape=False`) and round-trip a hostile password through systemd's unquoting and `sqlalchemy.make_url`. |

## Status of #23 (R14)

`invio run-due` does not exist on `origin/main` yet (checked: no `run_due` module, no command in
`src/`), so the guard from research R14 stays in
`deploy/ansible/molecule/default/verify.yml` (`TODO(#23)`): if `invio run-due --help` fails, the
direct "start run-due, `Result=success`" check is reported as `skipped: requires #23`. The guard
is one-sided on purpose: when `--help` succeeds, a failing start fails the test. The journal and
sandbox checks run today against the real unit with the `ExecStart` swap described above.
**Remove the guard (and the drop-in fallback) when #23 is merged.** Until then a deployed
`invio-run-due.service` fails with `No such command 'run-due'`; the timer keeps firing.

## Not automated (plan Complexity Tracking, FR-024)

Two checks need real time or a person and are in `docs/deployment.md`, "Manual verification
checklist": the quarter-hour activation observed over one hour (quickstart 19a) and the timed
manual update (quickstart 23). `systemd-analyze` does not exist on macOS, so `verify-units.sh`
has `--if-available` for pre-commit; CI never passes it.

## Quickstart walk-through

| # | Covered by |
|---|---|
| 1 | CI `deploy-static`: `verify-units.sh` |
| 2 | `tests/test_deploy_units.py` (unit contracts, no unlisted directives) |
| 3 | `test_role_unit_files_are_symlinks_to_canonical_units` |
| 4 | env example tests |
| 5 | CI `deploy-static`: `ansible-lint` (production profile, `no-log-password` enabled) |
| 6, 7 | Molecule `default`: converge, idempotence (Debian 12 and 13) |
| 8 | Molecule verify: env file and directory owner and mode, `nobody` cannot read |
| 9 | Molecule verify: charset, grants, migration to head twice |
| 10 | Molecule verify: guarded run-due start (R14), journal probe on the real unit |
| 11 | Molecule verify: `check-journal-json.py` with the test secrets |
| 12 | Molecule verify: notify-retry start, `nothing to retry`, JSON/secret check |
| 13 | Molecule prepare: empty `invio_db_password` fails naming the variable, `/etc/invio` absent |
| 14 | `deploy.yml`: verbose converge log checked by `check-no-secrets.sh` |
| 15, 16 | Molecule side effect: update to the parent commit and back, unchanged re-run |
| 17, 18 | Molecule `external-db`: no server package, DB and user on `dbserver`, unreachable host fails |
| 19 | Molecule verify: next elapse of both timers |
| 19a | Manual |
| 20 | Molecule side effect: catch-up after a container restart |
| 21 | Molecule verify: sandbox probes |
| 22 | Molecule verify (`systemctl show`) and side effect (drop-in override and removal) |
| 23 | Manual |
| 24 | Molecule side effect: running service delays the update |
| 25 | Molecule side effect: unknown ref fails in the probe, timers untouched |

## Local verification

Run on Docker Desktop (macOS, arm64) with `INVIO_TEST_SRC` pointing at a clone of the worktree:

- `molecule test` for `default` (Debian 12 and 13) and for `external-db`: all stages passed
  (converge, idempotence, side effect, verify).
- `docker run` of `deploy/scripts/verify-units.sh` on `debian:12`, `debian:13` and
  `ubuntu:24.04`: exit 0 with no output.
- The verbose converge log of `external-db` was checked with `check-no-secrets.sh` (no secret).
  `deploy.yml` repeats this for both scenarios.

CI (`deploy.yml`) remains the authoritative run.
