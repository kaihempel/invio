# Implementation Plan: CLI Job Management with Interactive Creation Wizard

**Branch**: `gh-issue-9` | **Date**: 2026-10-04 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/005-gh-issue-9/spec.md`

## Summary

Add the `invio job` command group: `create` (interactive wizard or `--from-file`), `list`,
`show`, `edit`, `enable`, `disable`, `delete`, `export` and `import`. It sits on top of the
existing `JobService`, job file contract and next-run calculation.

- **Wizard.** The wizard is a pure function driven by a `Prompter` protocol. Questionary is the
  production implementation and a scripted fake is used in tests. Each answer is validated with
  the rules of the job contract, and an invalid answer is asked again in place.
- **Source checks.** URL sources are checked with a small stdlib HTTP checker: reachability
  (HEAD, falling back to GET, 10 s timeout) and RSS/Atom detection. A failed check only warns,
  and the operator can keep the source.
- **Models.** Model ids are offered from the model registry. A model that isn't registered is
  accepted only after a warning and confirmation.
- **`edit`.** It goes through `click.edit` and a new `loads_yaml`.
- **Service changes.** The service gains `overview()` and `stored_config()`, so `list`, `show`
  and `edit` can show and repair jobs whose stored config no longer validates.

## Technical Context

**Language/Version**: Python 3.12+ (uv-managed)

**Primary Dependencies**: Typer (existing), Click (existing via Typer, used for `edit`), Rich
(existing via Typer, now declared explicitly), **questionary** (new; needed for the wizard,
justified in research R1), Pydantic v2 / PyYAML (existing). HTTP checks use stdlib `urllib`, so
there is no new HTTP dependency (R3).

**Storage**: Existing `jobs` and `runs` tables via `JobService` / repositories; no schema change.

**Testing**: pytest + `typer.testing.CliRunner`; `FakePrompter`, a fake `SourceChecker` and a
fake editor are injected via `monkeypatch`; SQLite (optionally MariaDB via
`INVIO_TEST_DATABASE_URL`). `HttpSourceChecker` is tested against a local
`http.server` on `127.0.0.1` (loopback only, no external network).

**Target Platform**: Linux/macOS terminals; the non-interactive paths also run under cron/CI.

**Project Type**: CLI (single project)

**Performance Goals**: Each source check finishes within 10 s (SC-005). `list` uses a fixed
number of queries regardless of the job count (one for jobs, one for the latest run statuses).

**Constraints**: Offline, deterministic tests. No prompt ever appears without a TTY. Stack
traces are never shown for expected errors. Exit codes follow [contracts/cli-job.md](contracts/cli-job.md).

**Scale/Scope**: A single operator with tens of jobs. 9 sub-commands. About 4 new source
modules and 4–5 new test modules.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Assessment | Status |
|---|---|---|
| I. Strict contracts at boundaries | Wizard answers, files and editor text all go through `validate_job` / `loads_yaml` (one loader, shared with `load_yaml`); no re-declared job model; errors name the field | ✅ |
| II. CLI-first | New `invio job` module in `src/invio/cli/commands/`, auto-discovered; results to stdout, diagnostics to stderr; config errors exit 2 | ✅ |
| III. Test-covered behaviour | Every acceptance scenario gets a CliRunner test, including rejection paths; network, terminal and editor are faked; loopback-only server for the HTTP checker | ✅ |
| IV. Quality gates | ruff, ruff format, mypy strict (questionary is untyped in parts → a narrow, commented `type: ignore` only where needed), pytest with the 95 % coverage gate; `uv.lock` updated | ✅ |
| V. Secrets / observability | No credentials handled; the database URL is never echoed (`pretty_exceptions_show_locals=False` stays); job changes are already logged by the service | ✅ |
| Layering | `cli` → `services`/`llm.registry`/`config`; services gain only a repository call; `scheduling` untouched (TID251 bans unaffected) | ✅ |
| New dependency justified | `questionary`: required by the issue, provides inline validation and select/autocomplete (R1). `rich`: already locked, made explicit | ✅ (state in PR) |
| Docs updated | README section "Managing jobs" with the commands, plus a note on the wizard and `--from-file` | ✅ (task) |

**Post-design re-check (after Phase 1)**: still passes. The design added no new layers, schema
changes or speculative abstractions. The `Prompter`/`SourceChecker` protocols exist only as test
seams, which constitution III requires.

## Project Structure

### Documentation (this feature)

```text
specs/005-gh-issue-9/
├── plan.md              # This file
├── research.md          # Phase 0: decisions R1–R9
├── data-model.md        # Phase 1: JobSummary, CheckResult, wizard steps, lifecycle
├── quickstart.md        # Phase 1: validation guide
├── contracts/
│   ├── cli-job.md       # Command, output and exit-code contract
│   └── python-api.md    # New/changed Python signatures and test seams
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks)
```

### Source Code (repository root)

```text
src/invio/
├── cli/
│   ├── commands/
│   │   └── job.py           # NEW: Typer sub-app `invio job` (9 commands, test seams)
│   ├── prompts.py           # NEW: Prompter protocol, QuestionaryPrompter, WizardAborted
│   ├── source_check.py      # NEW: CheckResult, SourceChecker, HttpSourceChecker, is_feed_document
│   └── wizard.py            # NEW: run_wizard (steps, inline validators, preview)
├── config/job.py            # CHANGED: + loads_yaml (load_yaml delegates)
├── db/repositories.py       # CHANGED: + RunRepository.latest_status_by_job
├── llm/registry.py          # CHANGED: + ModelRegistry.models_for
└── services/jobs.py         # CHANGED: + JobSummary, overview(), stored_config()

tests/
├── cli_helpers.py           # NEW: FakePrompter, FakeChecker, fake editor, service fixture wiring
├── test_cli_job.py          # NEW: list/show/enable/disable/delete/export/import/create --from-file
├── test_cli_job_create.py   # NEW: wizard via CLI (inline rejection, preview, abort, no TTY)
├── test_cli_job_edit.py     # NEW: edit loop (valid, invalid→retry, abort, unchanged, broken stored)
├── test_wizard.py           # NEW: step logic, registry choices, limits, duplicates
├── test_source_check.py     # NEW: loopback HTTP server: 200/404/405→GET/timeout/redirect, feed sniffing
├── test_job_yaml.py         # CHANGED: loads_yaml parity with load_yaml
├── test_llm_registry.py     # CHANGED: models_for
└── test_job_service.py      # CHANGED: overview (incl. invalid + last status), stored_config

pyproject.toml / uv.lock     # + questionary, + rich (explicit)
README.md                    # + "Managing jobs" section
```

**Structure Decision**: This is a single project with the existing layout. The CLI-only helpers
(prompts, wizard, source checks) live under `src/invio/cli/`, outside `commands/`, so
auto-discovery does not register them. Keeping them out of `invio.sources` also avoids
colliding with the sources track.

## Complexity Tracking

No constitution violations to justify.
