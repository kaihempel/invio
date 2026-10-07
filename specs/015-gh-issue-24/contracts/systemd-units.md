# Contract: systemd units

Files: `deploy/systemd/invio-run-due.service`, `invio-run-due.timer`,
`invio-notify-retry.service`, `invio-notify-retry.timer`.
Research: [R7](../research.md#r7--unit-design-scheduling), [R8](../research.md#r8--unit-design-service-and-hardening), [R11](../research.md#r11--units-in-the-repository-vs-units-installed-by-the-role).

`tests/test_deploy_units.py` asserts every **MUST** row: the key is present with exactly this
value (order-insensitive for list values). Extra directives are allowed only if they appear in
the "Additional hardening" list. Any other directive fails the test, so every change to the
sandbox is reviewed deliberately.

## Services (both)

| Section | Key | Value | Source |
|---|---|---|---|
| Unit | `Description` | non-empty | — |
| Unit | `Documentation` | `file:///opt/invio/docs/deployment.md` | FR-019 |
| Unit | `Wants` | `network-online.target` | FR-009 |
| Unit | `After` | `network-online.target mariadb.service` | FR-009 |
| Service | `Type` | `oneshot` | issue, FR-001 |
| Service | `User` / `Group` | `invio` / `invio` | FR-001, FR-020 |
| Service | `EnvironmentFile` | `/etc/invio/invio.env` (no `-` prefix: a missing file fails the start) | FR-001, edge case |
| Service | `WorkingDirectory` | `/var/lib/invio` | R8 |
| Service | `Environment` | `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1`, `HOME=/var/lib/invio`, `XDG_CACHE_HOME=/var/lib/invio/cache` | R3, R8 |
| Service | `StateDirectory` / `StateDirectoryMode` | `invio` / `0750` | FR-007 |
| Service | `ReadWritePaths` | `/var/lib/invio` | issue, FR-007 |
| Service | `NoNewPrivileges` | `yes` | issue, FR-006 |
| Service | `ProtectSystem` | `strict` | issue, FR-006 |
| Service | `ProtectHome` | `yes` | issue, FR-006 |
| Service | `PrivateTmp` | `yes` | issue, FR-006 |
| Service | `UMask` | `0027` | R8 |
| Service | `Restart` | absent (`no`) | FR-005 |
| Service | `SuccessExitStatus` | absent (exit codes pass through) | FR-005 |
| — | `[Install]` section | absent (services are started only by their timers) | FR-004 |

| Key | `invio-run-due.service` | `invio-notify-retry.service` |
|---|---|---|
| `ExecStart` | `/opt/invio/.venv/bin/invio run-due` | `/opt/invio/.venv/bin/invio notify retry` |
| `SyslogIdentifier` | `invio-run-due` | `invio-notify-retry` |
| `MemoryMax` | `1G` | `256M` |
| `TimeoutStartSec` | `3h` | `15min` |

### Additional hardening (allowed and expected, R8)

`PrivateDevices=yes`, `ProtectKernelTunables=yes`, `ProtectKernelModules=yes`,
`ProtectKernelLogs=yes`, `ProtectControlGroups=yes`, `ProtectClock=yes`, `ProtectHostname=yes`,
`RestrictSUIDSGID=yes`, `RestrictRealtime=yes`, `RestrictNamespaces=yes`, `LockPersonality=yes`,
`SystemCallArchitectures=native`, `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6`,
`CapabilityBoundingSet=` (empty).

### Forbidden

`MemoryDenyWriteExecute`, `PrivateNetwork`, `IPAddressDeny`, `DynamicUser`, `User=root`,
`ExecStartPre`/`ExecStartPost` that run as root (`+`/`!` prefixes).

## Timers

| Section | Key | `invio-run-due.timer` | `invio-notify-retry.timer` | Source |
|---|---|---|---|---|
| Unit | `Description` | non-empty | non-empty | — |
| Timer | `OnCalendar` | `*:0/15` | `hourly` | issue, FR-002/003 |
| Timer | `Persistent` | `true` | `true` | issue, FR-002/003 |
| Timer | `RandomizedDelaySec` | `60` | `300` | issue, R7 |
| Timer | `AccuracySec` | `1s` | absent | SC-002, R7 |
| Timer | `Unit` | `invio-run-due.service` | `invio-notify-retry.service` | — |
| Install | `WantedBy` | `timers.target` | `timers.target` | FR-004 |

## Role overrides (drop-in)

The role may override **only** `MemoryMax` (both services) through
`/etc/systemd/system/<service>.d/50-invio-role.conf`, and writes the file only when
`invio_run_due_memory_max` or `invio_notify_retry_memory_max` differ from the defaults above
(R11). Everything else stays as shipped.

## Verification

- `deploy/scripts/verify-units.sh` must exit 0 **with no output** from
  `systemd-analyze verify` (SC-001).
- `systemd-analyze calendar '*:0/15'` lists the quarter hours. This is documented in the manual
  checklist, not asserted in CI.
