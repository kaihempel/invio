# Contract: opt-in Whisper deployment switch

**Feature**: [../spec.md](../spec.md) · FR-018, SC-010 · amends
`specs/015-gh-issue-24/contracts/ansible-role.md` · research R12.

## Role variables (added to the defaults table)

| Variable | Default |
|---|---|
| `invio_whisper_enabled` | `false` (boolean) |
| `invio_whisper_run_due_memory_max` | `3G` (same format rule as the other memory limits) |

Validation (`validate.yml`, no changes on the host): `invio_whisper_enabled` is a boolean;
`invio_whisper_run_due_memory_max` matches `^[0-9]+[KMGT]?$`. A failure names the variable.

## Behaviour

| Switch | `uv sync` (install.yml) | run-due `MemoryMax` | Sandbox, timeouts, other units |
|---|---|---|---|
| off (default) | `--locked --no-dev --no-editable --compile-bytecode` (unchanged; removes the extra if it was installed) | `invio_run_due_memory_max` (default `1G`: no drop-in, as today) | unchanged |
| on | same plus `--extra whisper` | drop-in `50-invio-role.conf` with `MemoryMax={{ invio_whisper_run_due_memory_max }}` | unchanged |

- Switching off after on removes the extra on the next sync and removes the drop-in when the
  effective value equals the shipped default (existing mechanism).
- The `notify-retry` service is never affected.
- `INVIO_WHISPER_MODEL_SIZE`, `INVIO_WHISPER_MAX_DURATION_MINUTES` and
  `INVIO_WHISPER_MAX_PER_RUN` are set through `invio_env_extra`; the role manages no new env
  keys.

## Runtime facts the docs must state (docs/deployment.md)

- The model is downloaded on the first transcription into
  `/var/lib/invio/cache/huggingface` (`XDG_CACHE_HOME` of the units), about 0.5 GB for `small`;
  the host needs outbound HTTPS to the Hugging Face hub at that moment.
- Temp audio lives in the unit's private `/tmp` and is removed after each job.
- Memory guidance: `small` → `3G` (default), `medium` → `4G`, `large-v3` → `6G`.
- YouTube audio formats may need a JavaScript runtime for yt-dlp (research R9).
- `TimeoutStartSec=3h` of `invio-run-due.service` covers all due jobs of one start, while each
  job's run-time check only knows its own lock deadline: with several transcription-heavy jobs,
  keep `INVIO_WHISPER_MAX_PER_RUN` low or stagger the schedules.
- Keep `INVIO_WHISPER_MAX_PER_RUN` below `INVIO_MAX_PARALLEL_ITEMS`; on slow CPUs prefer a
  smaller model or a longer `INVIO_RUN_LOCK_SECONDS` (run-time check, research R11).

## Checks

| Id | Check | Where |
|---|---|---|
| W1 | defaults match this table and the ansible-role contract table | `tests/test_deploy_role.py` |
| W2 | switch off with default variables → no run-due drop-in is written | `tests/test_deploy_role.py` |
| W3 | switch on → drop-in value `3G`; custom value respected | `tests/test_deploy_role.py` |
| W4 | `--extra whisper` appears in the sync argv only when enabled | `tests/test_deploy_role.py` |
| W5 | invalid values rejected naming the variable | `tests/test_deploy_role.py` |
| W6 | converge with the switch on: effective `MemoryMax=3G` (3221225472), `import faster_whisper` succeeds in `/opt/invio/.venv` | Molecule `verify.yml` |
