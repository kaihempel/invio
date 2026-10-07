# Data Model: Unattended systemd Deployment (gh-issue-24)

Feature: [spec.md](spec.md) · Plan: [plan.md](plan.md)

This feature adds **no database tables, columns or migrations**, and no Python domain models.
Its "data" is the deployment state on the host. This document lists those entities, their
attributes, and how they change over time. The detailed schemas are in the contracts.

## Entities

### Service account

| Attribute | Value |
|---|---|
| Name / group | `invio` / `invio` (FR-020) |
| Kind | system account (UID < 1000), shell `/usr/sbin/nologin` |
| Home | `/var/lib/invio` (the same as the state directory; not created by `useradd`) |
| Owns | state directory, env file |
| Does not own | `/opt/invio` (code, venv, Python), `/etc/invio` directory, units |

### Env file

See [contracts/env-file.md](contracts/env-file.md). Holds all `INVIO_*` settings, including
secrets. Validation: role variable rules (FR-016) at write time, and invio's `Settings` at every
service start (constitution I; configuration errors → exit code 2 → failed unit).

### Installation tree

| Path | Owner | Mode | Writable by service | Content |
|---|---|---|---|---|
| `/opt/invio/` | root | `0755` | no | Git checkout at `invio_git_version` |
| `/opt/invio/.venv/` | root | `0755` | no | venv, pre-compiled bytecode |
| `/opt/invio-python/` | root | `0755` | no | uv-managed CPython 3.12 (R1) |
| `/usr/local/bin/uv` | root | `0755` | no | pinned uv (R2) |
| `/var/cache/invio-uv/` | root | `0700` | no | uv cache during deploys |
| `/var/lib/invio/` | invio | `0750` | **yes** (only writable path) | state, `cache/`, `archive/` |
| `/etc/invio/deployed-revision` | root | `0644` | no | commit SHA that was last migrated |
| `/etc/systemd/system/invio-*.{service,timer}` | root | `0644` | no | units from `deploy/systemd/` |
| `/etc/systemd/system/invio-*.service.d/50-invio-role.conf` | root | `0644` | no | optional `MemoryMax` override |

### Units

See [contracts/systemd-units.md](contracts/systemd-units.md).

| Unit | Activated by | Runs | Bounded by |
|---|---|---|---|
| `invio-run-due.timer` | boot (`timers.target`) | — | — |
| `invio-run-due.service` | timer, every 15 min (+≤60 s) | `invio run-due` | `TimeoutStartSec=3h`, `MemoryMax=1G` |
| `invio-notify-retry.timer` | boot (`timers.target`) | — | — |
| `invio-notify-retry.service` | timer, hourly (+≤300 s) | `invio notify retry` | `TimeoutStartSec=15min`, `MemoryMax=256M` |

### Role variables

See [contracts/ansible-role.md](contracts/ansible-role.md). Inputs from the operator's inventory.
The required ones are validated before any change.

### Database objects (managed, not modelled)

| Object | Rule |
|---|---|
| Database `invio_db_name` | `utf8mb4` / `utf8mb4_unicode_ci`; created if missing, never dropped |
| User `invio_db_user@invio_db_user_host` | `ALL` on `invio_db_name.*` only; password updated only when it changed |
| Schema | Alembic head after each deploy (`invio db upgrade`); the existing migrations are unchanged |

## State transitions

### Service (per activation)

```text
inactive ──timer elapses──▶ activating (running invio …)
activating ──exit 0──▶ inactive (result: success)
activating ──exit ≠0 / MemoryMax / TimeoutStartSec──▶ failed ──next timer elapse──▶ activating
```

A timer elapse while the service is `activating` does not start a second instance.

### Host deployment (role run)

```text
absent ──first role run──▶ installed(rev A, timers active)
installed(A) ──role run, ref A──▶ installed(A)             [no changes, timers untouched]
installed(A) ──role run, ref B──▶ updating                  [timers stopped, wait for idle]
updating ──checkout+sync+migrate OK──▶ installed(B, timers active)
updating ──any step fails──▶ stopped(partial)               [timers stopped; docs: recover]
stopped(partial) ──role run (fixed vars/ref)──▶ updating
```

`deployed-revision` is written only after a successful migration, so a failed run is detected
and migrated on the next run (research R4).
