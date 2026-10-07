# Quickstart & Validation: Unattended systemd Deployment (gh-issue-24)

Feature: [spec.md](spec.md) · Plan: [plan.md](plan.md) · Contracts:
[units](contracts/systemd-units.md), [env file](contracts/env-file.md),
[role](contracts/ansible-role.md)

Each scenario names the spec items it proves. The "Where" column says how it runs: **CI** = every
PR (`checks` + `deploy-static`) and pre-commit, **Molecule** = opt-in `deploy.yml` workflow,
**Manual** = checklist in `docs/deployment.md` (only #19a and #23).

## Prerequisites

- Developer machine: `uv sync --locked` (normal), plus `uv sync --locked --group deploy` for
  ansible-lint and Molecule.
- Static unit verification: Linux with `systemd-analyze` (≥ 252).
- Molecule: Docker able to run privileged containers.
- Manual checks: a Debian 12/13 host or VM provisioned with the role.

## Commands

```bash
# Static checks (CI: deploy-static)
deploy/scripts/verify-units.sh                 # must print nothing and exit 0
uv run --group deploy ansible-lint deploy/ansible
uv run pytest tests/test_deploy_units.py

# Provisioning test (CI: deploy.yml, opt-in)
cd deploy/ansible && INVIO_TEST_REF=$(git rev-parse HEAD) uv run --group deploy molecule test

# On a provisioned host
systemctl list-timers 'invio-*'
journalctl -u invio-run-due -o cat --since today
```

## Scenarios

| # | Scenario | Expected | Proves | Where |
|---|---|---|---|---|
| 1 | Run `verify-units.sh` | exit 0, no output | FR-010, SC-001 | CI |
| 2 | Unit settings test | every MUST row in the units contract matches; no unlisted directive | FR-001–FR-003, FR-005–FR-008, FR-013, FR-020 | CI |
| 3 | Role symlinks test | `roles/invio/files/*` resolve to `deploy/systemd/*` | R11, G9 | CI |
| 4 | Env example test | every key in `deploy/env/invio.env.example` is a known setting; no value looks like a real secret (placeholders only) | FR-012 | CI |
| 5 | `ansible-lint` | no violations (production profile) | FR-014, FR-017 (`no_log` rule) | CI |
| 6 | Molecule converge on Debian 12 and 13 (default vars, server switch on) | play succeeds; both timers `enabled` + `active`; `mariadb` running | US5 sc.1, SC-004 | Molecule |
| 7 | Molecule idempotence | `changed=0` on second converge | FR-015, SC-005, US5 sc.5 | Molecule |
| 8 | Env file permissions | `stat` → `invio:invio 0600`; `/etc/invio` `root:invio 0750`; `sudo -u nobody cat` fails | FR-011, SC-006, US5 sc.3 | Molecule |
| 9 | DB checks | database exists with `utf8mb4`; `SHOW GRANTS FOR invio@localhost` lists only `invio.*`; `invio db upgrade` (via systemd-run) reports head and runs no migration | US5 sc.4 | Molecule |
| 10 | `systemctl start invio-run-due.service` | exit 0 (no due jobs); unit `inactive (dead)`, result `success` | US1 sc.2, SC-004 | Molecule (guarded until #23, R14) |
| 11 | Journal JSON check | every `journalctl -u invio-run-due -o cat` line that starts with `{` parses as JSON with `level` and `message`; no line contains the DB password, SMTP password or API key | US2, FR-013, SC-007 | Molecule |
| 12 | `systemctl start invio-notify-retry.service` with nothing to retry | exit 0, stdout `nothing to retry` in the journal | US3 sc.3 | Molecule |
| 13 | Missing required var (`invio_db_password` unset) | play fails in `validate.yml` naming the variable; no host change | FR-016, US5 sc.6 | Molecule (negative converge) |
| 14 | Secrets in output | the converge log (`-v`) contains none of the test secrets | FR-017, US5 sc.7 | Molecule |
| 15 | Update: converge with the parent commit, then the original commit | timers stopped and restarted; `deployed-revision` = new SHA; schema at head | FR-021, US6 sc.1, SC-009 | Molecule |
| 16 | Unchanged ref re-run | timers' `ActiveEnterTimestamp` unchanged; no migrate task changed | US6 sc.5 | Molecule (part of 7) |
| 17 | Server switch off + reachable server (DebOps case) | no `mariadb-server` package task runs; DB and user created via the admin login | FR-018, US5 sc.2 | Molecule (second scenario `external-db`) |
| 18 | Server switch off + unreachable host | fails in `mariadb.yml` with the host name; no units installed | edge case | Molecule (`external-db`, negative) |
| 19 | Timer schedule | `NextElapseUSecRealtime` of `invio-run-due.timer` is at minute 0/15/30/45 with ≤ 60 s offset; `invio-notify-retry.timer` next elapse ≤ 65 min ahead | FR-002/003, SC-002 (schedule), US1 sc.1, US3 sc.1 | Molecule (verify) |
| 19a | Quarter-hour activation observed over 1 h | 4 starts, each ≤ 60 s after :00/:15/:30/:45 | SC-002 (observation) | Manual |
| 20 | Catch-up after a systemd restart with a missed slot (stamp set 1 h back, `docker restart`) | exactly one `invio-run-due.service` start ≤ 2 min after boot; timers active again | FR-002/004, SC-003, US1 sc.3, US3 sc.4 | Molecule (side_effect) |
| 21 | Sandbox probes via `systemd-run` with the unit's sandbox properties | `touch /opt/invio/x` and `touch /etc/x` denied; `ls -A /home` empty; `touch /var/lib/invio/probe` succeeds; `sudo -n true` refused | FR-006/007, SC-008, US4 sc.1–3, 6 | Molecule (verify) |
| 22 | Memory / timeout limits | `systemctl show -p MemoryMax,TimeoutStartUSec` show 1G/3h and 256M/15min; drop-in override changes MemoryMax and is removed when reset | FR-008, US4 sc.4–5 | Molecule (verify + side_effect) |
| 23 | Manual update procedure on a second host | same result as #15, done in < 10 min | FR-019, US6 sc.2, SC-009 (manual) | Manual |
| 24 | Running service during update | a running `invio-run-due.service` (sleep drop-in) delays the update until it is inactive | US6 sc.3 | Molecule (side_effect) |
| 25 | Nonexistent ref | role fails in the probe; both timers still `active` | edge case, G7 | Molecule (side_effect) |

## Out of scope for validation

Playwright/`render` extra inside the sandbox, `.deb` packaging, alerting on failed units.
