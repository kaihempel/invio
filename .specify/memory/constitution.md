<!--
Sync Impact Report
- Version change: (unversioned template) → 1.0.0
- Modified principles: none (initial adoption; all placeholders replaced)
- Added principles:
  I. Strict Contracts at Boundaries
  II. CLI-First Operation
  III. Test-Covered Behaviour
  IV. Quality Gates Mirror CI
  V. Secrets Stay Secret, Runs Stay Observable
- Added sections: Technology & Architecture Constraints; Development Workflow; Governance
- Removed sections: none
- Templates: plan/spec/tasks templates read the constitution at runtime; not modified by this command
- Deferred TODOs: none
-->

# invio Constitution

## Core Principles

### I. Strict Contracts at Boundaries

Every piece of data that enters the system from outside — job files, environment/settings,
CLI arguments, source feeds, LLM responses — MUST be parsed into a typed, validated model at
the boundary before any other code uses it.

- Configuration models MUST reject unknown keys and MUST fail fast with messages that name the
  offending field and the violated rule.
- Shared contracts between components (e.g. job configuration, domain records) MUST be defined
  once and imported, never re-declared.
- Versioned file formats MUST carry an explicit schema version.

Rationale: the CLI, scheduler and pipeline only cooperate through these contracts; silent
acceptance of bad data turns into misdirected or failed unattended runs.

### II. CLI-First Operation

All user-facing functionality MUST be reachable through the `invio` CLI.

- Commands live in `src/invio/cli/commands/` and are auto-discovered; adding a command MUST NOT
  require editing other files.
- Results go to stdout; logs and diagnostics go to stderr.
- Commands MUST exit non-zero on failure; configuration errors exit with code 2.

Rationale: invio runs unattended under cron/systemd, so behaviour must be scriptable and its
outcome detectable from the exit code alone.

### III. Test-Covered Behaviour

Every acceptance criterion of an issue or spec MUST be covered by at least one automated test,
including the rejection paths for invalid input.

- Unit tests MUST NOT require network access, real LLM providers or a production database;
  external dependencies are faked or replaced by local fixtures.
- Bug fixes MUST add a test that fails without the fix.
- Tests MUST be deterministic; flaky tests are fixed or removed, not retried.

Rationale: a research tool that runs unattended can only be trusted if its behaviour is pinned
down by tests that run anywhere.

### IV. Quality Gates Mirror CI

A change is mergeable only when all CI gates pass: `ruff check`, `ruff format --check`,
`mypy` in strict mode over `src/`, and `pytest`.

- Local pre-commit hooks MUST run the same tools with the same configuration as CI.
- Type-check suppressions (`# type: ignore`, `Any`) MUST be narrow and justified in a comment.
- Dependencies MUST be locked (`uv.lock`) and CI MUST install with `--locked`.

Rationale: identical local and CI gates keep main green and remove "works on my machine"
surprises.

### V. Secrets Stay Secret, Runs Stay Observable

- Credentials MUST be read only via settings, held as secret types, and MUST NOT appear in
  logs, `repr`, error messages, job files or committed files (`.env` is never committed).
- Provider credentials are optional at startup and MUST be checked when the provider is used.
- Logging MUST be structured (one JSON object per line on stderr); job runs MUST be wrapped in
  a run context so every line carries `job` and `run_id`.

Rationale: unattended runs are debugged from logs after the fact, and those logs must be safe
to share.

## Technology & Architecture Constraints

- Language and tooling: Python 3.12+, managed with `uv`; source in `src/invio/`, tests in
  `tests/`.
- Validation and settings use Pydantic v2 / pydantic-settings; the CLI uses Typer.
- Package layout follows the README (`cli`, `config`, `db`, `graph`, `sources`, `llm`,
  `notify`, `scheduling`). Shared domain records MUST stay dependency-free (no DB, network or
  LLM imports) so every track can use them.
- Dependency direction: `cli` → orchestration (`graph`, `scheduling`) → adapters (`sources`,
  `llm`, `notify`, `db`) → `config` / domain. Lower layers MUST NOT import higher ones.
- New runtime dependencies MUST be justified in the PR description.
- Simplicity first: no speculative abstractions; build what the current issue requires.

## Development Workflow

- Work starts from a GitHub issue and happens on a branch named `gh-issue-<N>`; nothing is
  committed directly to `main`.
- Non-trivial features go through Spec Kit: specify → clarify → plan → tasks → implement. Specs
  live in `specs/<NNN>-<name>/`.
- Every change merges through a pull request that references its issue, passes CI and has been
  reviewed (human or code-reviewer agent); review findings are resolved before merge.
- User-facing behaviour changes (CLI commands, configuration, file formats) MUST update the
  README or `docs/` in the same PR.

## Governance

This constitution takes precedence over other project conventions. Plans and reviews MUST check
compliance with it; any deviation MUST be recorded with its justification in the plan's
complexity tracking or the PR description.

- Amendments are made via pull request that updates this file, states the version bump and its
  reason, and adjusts dependent guidance if needed.
- Versioning follows semantic versioning: MAJOR for removing or redefining a principle, MINOR for
  adding a principle or section or materially expanding guidance, PATCH for clarifications and
  wording.
- Compliance is reviewed at every PR review and at each `/speckit-plan` constitution check.

**Version**: 1.0.0 | **Ratified**: 2026-10-04 | **Last Amended**: 2026-10-04
