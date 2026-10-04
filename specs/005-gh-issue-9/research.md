# Research: CLI Job Management with Interactive Creation Wizard

Feature: [spec.md](spec.md) · Plan: [plan.md](plan.md)

## R1 — Interactive prompt library

- **Decision**: `questionary` (new runtime dependency, pulls in `prompt_toolkit`), used only
  behind a small `Prompter` protocol (`src/invio/cli/prompts.py`). The production
  implementation wraps `questionary.text/select/confirm/autocomplete`; tests use a scripted
  `FakePrompter`.
- **Rationale**: The issue names questionary. It provides per-question `validate=` callbacks that
  re-ask the question with an inline error message, which is exactly FR-005. It also offers
  select lists for frequency, weekday, provider and model, and autocomplete for IANA time zones.
  Hiding it behind a protocol keeps the wizard testable under `CliRunner`, where
  `prompt_toolkit` has no real terminal.
- **Alternatives considered**: `typer.prompt` / `click.prompt` (no select lists, no
  autocomplete, and their re-ask behaviour is less controllable); `InquirerPy` (unmaintained,
  not named in the issue); driving questionary through `prompt_toolkit` pipe input in tests
  (brittle, couples tests to key sequences).

## R2 — Testing the wizard without a TTY

- **Decision**: The wizard is a plain function, `run_wizard(prompter, checker, registry,
  existing_names, ...) -> JobDraft`, with no direct terminal I/O. The `job create` command gets
  the prompter, the checker and the TTY probe from module-level factory functions
  (`_make_prompter`, `_make_checker`, `_is_interactive`). Tests replace those factories with
  `monkeypatch`. `FakePrompter` takes a list of scripted answers. When a validator rejects an
  answer, the fake records the error message and takes the next answer for the same question.
  Tests can therefore assert both the inline rejection and that the wizard went on (FR-005).
- **Rationale**: This covers FR-036 and constitution III: deterministic, offline, and run through
  Typer's `CliRunner`.
- **Alternatives considered**: `pexpect` end-to-end tests (needs a pty, is slow and
  platform-dependent).

## R3 — Reachability and feed checks

- **Decision**: Use stdlib `urllib.request` in `src/invio/cli/source_check.py`, behind a
  `SourceChecker` protocol. For each URL:
  1. Send `HEAD` with a 10 s timeout, following redirects.
  2. If the server answers 405 or 501, or the source is `rss` (whose body we need), send a
     `GET` instead. At most 64 KiB of the body is read.
  3. A 2xx/3xx final status counts as reachable. A 4xx/5xx status, a timeout, a DNS error, a
     TLS error or a connection error gives a warning with a reason.

  Feed detection for `rss` sources: the source is a feed if the first XML start element of the
  body is `rss`, `rdf:RDF`, or Atom's `{http://www.w3.org/2005/Atom}feed`. This is found with
  `xml.etree.ElementTree.XMLPullParser`, which stops at the first start event. If the body is
  not XML, or the root is anything else, the result is "no feed detected". A user-agent header
  `invio/<version>` is sent.
- **Rationale**: These are the only HTTP calls in this feature, so a new HTTP dependency
  (`httpx`) is not justified yet (constitution: simplicity first; new dependencies need a
  reason). Reading only the first start element of a capped body avoids parsing large or hostile
  documents. Python 3.12's bundled expat guards against entity-expansion attacks. The check is
  advisory, never blocking (FR-008/FR-009). Every outcome maps to a `CheckResult`.
- **Alternatives considered**: `httpx` (better API, but a new dependency for about 40 lines of
  code; reconsider when the sources track adds fetching); `feedparser` (heavy, and parses the
  whole body); checking only `Content-Type` (unreliable, since many feeds are served as
  `text/xml` or `text/html`).

## R4 — Model choice and the model registry

- **Decision**: Add `ModelRegistry.models_for(provider: str) -> list[str]` (sorted model ids
  registered for that provider). The wizard offers those ids in a select list, plus an
  "Other…" entry that asks for free text. A model id that isn't registered for the provider
  shows the warning *"model 'x' is not registered for provider 'p'; runs will fail until it is
  added to models.d/p.yaml"* and needs confirmation before it is accepted. If the provider has no
  registered models (the shipped `models.d` is empty), the wizard asks for free text with the
  same warning. A broken registry (`ModelRegistryError`) is shown once as a warning, and the
  wizard falls back to free text.
- **Rationale**: This implements Clarification Q2. Runtime resolution
  (`invio.llm.factory.resolve`) rejects models that aren't registered, so the warning must say
  the run will fail rather than only mentioning cost tracking (the spec wording was corrected
  for this).
- **Alternatives considered**: Hard-rejecting unknown models (rejected in Clarification Q2).

## R5 — Editor integration for `job edit`

- **Decision**: Use `click.edit(text, extension=".yaml", require_save=True)`. Click is already a
  Typer dependency. It honours `$VISUAL`/`$EDITOR` and falls back to `vi` or `notepad`, and it
  returns `None` when the file was not saved (which we treat as unchanged). It is reached
  through a module-level `_edit_text` function so tests can monkeypatch it. A non-zero editor
  exit raises `click.UsageError`/`ClickException`, which is treated as an abort. Retry or abort
  is asked with `typer.confirm("Re-open the editor?", default=True)`, which works with
  `CliRunner(input=...)`.
- **Parsing**: Add `invio.config.job.loads_yaml(text: str) -> JobConfig`, which parses YAML text
  with `JobYamlLoader` and validates it. `load_yaml` is refactored to read the file and delegate
  to it, so error messages stay identical (no duplicate parser, constitution I).
- **Rationale**: Covers FR-024 to FR-027 with no new dependency.
- **Alternatives considered**: A hand-rolled `subprocess` call with a temp file (reimplements
  Click).

## R6 — Jobs with an invalid stored configuration and the last run status

- **Decision**: Leave `JobService.list()` unchanged, because the scheduler relies on it skipping
  invalid jobs. Add:
  - `JobService.overview() -> list[JobSummary]`: every job, including invalid ones, with
    `config: JobConfig | None`, `config_errors: list[str]` and `last_run_status: RunStatus |
    None`, loaded in one session.
  - `JobService.stored_config(name) -> dict[str, Any]`: the raw stored mapping, so `show` and
    `edit` can display and repair a broken job.
  - `RunRepository.latest_status_by_job() -> dict[int, RunStatus]`: one grouped query, so the
    list doesn't run one query per job.
- **Rationale**: Implements Clarification Q4 without changing existing callers. The service
  stays the only gateway to job storage (constitution I, and its own module contract).
  `enable` and `export` already raise `StoredJobConfigError`, and the CLI maps that to exit
  code 2 (FR-035).
- **Alternatives considered**: Changing `list()` to return invalid jobs (breaks the scheduler
  contract from #5); having the CLI query repositories directly (breaks the layering).

## R7 — Table output

- **Decision**: Render the `list` table with `rich.table.Table` through a `rich.console.Console`
  writing to stdout. Declare `rich` explicitly in `pyproject.toml`; it is already locked as a
  Typer dependency, so nothing new is downloaded. Times are shown as `YYYY-MM-DD HH:MM <TZ
  abbreviation>` in the job's own time zone; "—" means not scheduled (disabled) or not derivable
  (invalid config).
- **Rationale**: Aligned columns, Unicode-safe widths, and a plain-text fallback when stdout is
  not a terminal (which is what CliRunner gives).
- **Alternatives considered**: Hand-formatted columns (fragile with wide characters).

## R8 — Exit codes and TTY detection

- **Decision**:
  - 0: success, including no-op enable/disable and an unchanged edit.
  - 1: operational failure or a user abort or decline: not found, already exists, preview
    declined, delete declined, Ctrl+C, edit aborted.
  - 2: configuration or usage errors: invalid job file or stored config, missing
    `INVIO_DATABASE_URL`, interactive command without a TTY (`create` without `--from-file`,
    `delete` without `--yes`).

  The TTY probe is `sys.stdin.isatty() and sys.stdout.isatty()`, behind `_is_interactive()`.
- **Rationale**: Constitution II (configuration errors exit 2; failures exit non-zero). Treating
  "needs a terminal" as a usage error matches Click's convention for usage errors (2).

## R9 — Wizard defaults

- **Decision**:
  - The time zone is pre-filled with the system's IANA zone when it can be determined (via
    `TZ` or the `/etc/localtime` symlink target), otherwise `UTC`, and is autocompleted from
    `available_timezones()`.
  - The time defaults to `07:00`.
  - The notification subject defaults to `invio: <job name>`.
  - Limits use the `LimitsConfig` defaults (Clarification Q3).
  - Each source gets `name = None` and `enabled = true`.
  - Duplicate recipients and duplicate sources are warned about and skipped.
- **Rationale**: Short happy path (SC-001). Every default can be changed later with `edit`.
