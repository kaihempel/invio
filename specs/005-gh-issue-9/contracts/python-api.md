# Contract: Python additions for `invio job`

Signatures are the contract; bodies are implementation detail. All datetimes are
timezone-aware UTC. Existing signatures (specs/003-gh-issue-5/contracts/python-api.md) stay
unchanged.

## `invio.config.job` (addition)

```python
def loads_yaml(text: str, *, source: Path | None = None) -> JobConfig
    """Parse and validate job YAML text; raises JobConfigError(source, [...]) on any problem.

    Same loader (JobYamlLoader), same messages as load_yaml; load_yaml reads the file and
    delegates here.
    """
```

## `invio.llm.registry` (addition)

```python
class ModelRegistry:
    def models_for(self, provider: str) -> list[str]
        """Sorted model ids registered for ``provider`` (empty list if none)."""
```

## `invio.db.repositories` (addition)

```python
class RunRepository:
    def latest_status_by_job(self) -> dict[int, RunStatus]
        """job_id -> status of its newest run (started_at desc, id desc); one query."""
```

## `invio.services.jobs` (additions)

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class JobSummary:
    name: str
    enabled: bool
    next_run_at: datetime | None
    config: JobConfig | None          # None iff the stored config does not validate
    last_run_status: RunStatus | None # None = never run

class JobService:
    def overview(self) -> list[JobSummary]
        """All jobs ordered by name, including ones whose stored config is invalid."""

    def names(self) -> list[str]
        """All job names ordered by name; no config validation (cheap existence checks)."""

    def is_enabled(self, name: str) -> bool
        """Whether the job is enabled, even with an invalid config; JobNotFoundError if missing."""

    def stored_config(self, name: str) -> dict[str, Any]
        """The raw stored config mapping (may be invalid); JobNotFoundError if missing."""
```

`list()`, `get_by_name()`, `set_enabled()`, `export_yaml()` and `import_yaml()` keep their
current behaviour. The CLI relies on these existing behaviours:
- `set_enabled(name, False)` on an invalid stored config commits the disable, then raises
  `StoredJobConfigError`. The CLI reports this as success with a warning.
- `set_enabled(name, True)`, `get_by_name()` and `export_yaml()` raise `StoredJobConfigError`
  for an invalid config. The CLI maps this to exit 2.

## `invio.cli.prompts` (new)

```python
Validator = Callable[[str], bool | str]   # True = ok, str = inline error message

class Prompter(Protocol):
    def text(self, message: str, *, default: str = "", validate: Validator | None = None,
             multiline: bool = False) -> str: ...
    def select(self, message: str, choices: Sequence[str], *, default: str | None = None) -> str: ...
    def confirm(self, message: str, *, default: bool = True) -> bool: ...
    def autocomplete(self, message: str, choices: Sequence[str], *, default: str = "",
                     validate: Validator | None = None) -> str: ...

class QuestionaryPrompter:  # production; raises WizardAborted on Ctrl+C / EOF (questionary returns None)
class WizardAborted(Exception): ...
```

Test double (in `tests/cli_helpers.py`, not shipped):

```python
class FakePrompter:
    def __init__(self, answers: Iterable[str | bool]) -> None: ...

    errors: list[tuple[str, str]]  # (question message, inline error) for every rejected answer
    asked: list[str]  # question messages in order
    # Validator rejection -> record error, consume next answer for the same question.
    # Running out of answers -> WizardAborted (simulates Ctrl+C).
```

## `invio.cli.source_check` (new)

```python
@dataclass(frozen=True, slots=True)
class CheckResult:
    reachable: bool
    status: int | None
    reason: str | None
    is_feed: bool | None

class SourceChecker(Protocol):
    def check(self, url: str, *, expect_feed: bool) -> CheckResult: ...

class HttpSourceChecker:
    def __init__(self, *, timeout: float = 10.0, max_bytes: int = 65_536) -> None: ...
    def check(self, url: str, *, expect_feed: bool) -> CheckResult: ...

def is_feed_document(head: bytes) -> bool
    """True if the first XML start element is rss, rdf:RDF or Atom feed."""
```

`HttpSourceChecker.check` never raises for network problems. Every failure becomes
`CheckResult(reachable=False, reason=...)`.

## `invio.cli.wizard` (new)

```python
def run_wizard(
    prompter: Prompter,
    checker: SourceChecker,
    *,
    registry: ModelRegistry | None,     # None = registry unavailable -> free-text models
    existing_names: Collection[str],
    name: str | None = None,            # pre-filled from --name
    default_timezone: str = "UTC",
) -> tuple[str, JobConfig] | None
    """Ask all steps and show the preview; None if the operator declined saving.

    Raises WizardAborted on Ctrl+C / EOF. Never touches storage or the terminal directly.
    """
```

## `invio.cli.commands.job` (new; test seams)

```python
app: typer.Typer
def _make_service() -> JobService          # JobService.from_settings()
def _make_prompter() -> Prompter           # QuestionaryPrompter()
def _make_checker() -> SourceChecker       # HttpSourceChecker()
def _is_interactive() -> bool              # stdin and stdout are TTYs
def _edit_text(text: str) -> str | None    # click.edit(..., extension=".yaml")
```

Tests replace these with `monkeypatch.setattr` and drive commands with `CliRunner`.
