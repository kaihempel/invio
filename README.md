# invio

CLI-based AI research system.

## Development quickstart

Requirements: [uv](https://docs.astral.sh/uv/) (Python 3.12 is installed by uv if missing).

```bash
uv sync                      # create .venv and install runtime + dev dependencies
uv run invio --version       # smoke test
cp .env.example .env         # local configuration (never commit .env)
uv run pre-commit install    # optional: run checks on every commit
```

### Checks (same as CI)

```bash
uv run ruff check
uv run ruff format --check   # `uv run ruff format` to fix
uv run mypy src
uv run pytest
```

### Adding a CLI command

Create `src/invio/cli/commands/<name>.py` exposing a module-level `typer.Typer` named `app`:

```python
import typer

app = typer.Typer(help="Say hello.")


@app.command()
def greet(name: str = "world") -> None:
    typer.echo(f"Hello {name}")
```

It is registered automatically as the command group `invio <name>` (underscores become
dashes, e.g. `run_now.py` -> `invio run-now`), so the example above runs as
`invio <name> greet --name you`. No other file needs to change. Modules whose name starts
with `_` are ignored, so use them for shared helpers.

### Configuration & logging

Settings come from `INVIO_*` environment variables or a `.env` file in the working directory
(see `.env.example`; real environment variables win over `.env`). Set `INVIO_ENV_FILE` to load
the file from elsewhere, e.g. under cron/systemd where the working directory differs; a missing
file is then an error. Secrets are `SecretStr` and never appear in `repr`/logs. Provider keys
are optional until the provider is actually used:

```python
from invio.config.settings import get_settings

settings = get_settings()  # cached instance
api_key = settings.require_secret("openai_api_key")  # raises "INVIO_OPENAI_API_KEY is not set"
```

Every `invio` sub-command configures logging on startup: one JSON object per line on stderr,
level from `INVIO_LOG_LEVEL`. Unknown `INVIO_*` keys (usually typos) are logged as warnings, and
configuration errors end the command with exit code 2. Wrap a job run in `run_context` to tag
every line with `job` and `run_id` (a nested context without `job` keeps the outer one). `extra`
keys that clash with built-in fields such as `job` or `level` are emitted as `extra_<key>`:

```python
import logging

from invio.log import configure_logging, run_context

configure_logging()  # done by the CLI already; call it yourself in scripts
with run_context(job="digest") as run_id:
    logging.getLogger(__name__).info("started", extra={"sources": 3})
```

## Job files

A job file is a YAML description of one research job: schedule, notification, sources, search,
LLM and limits. See [docs/job.example.yaml](docs/job.example.yaml) for a complete example and
[docs/job.schema.json](docs/job.schema.json) for the JSON Schema (for editor validation).

```python
from invio.config.job import JobConfigError, dump_yaml, load_yaml, write_yaml

try:
    job = load_yaml("docs/job.example.yaml")
except JobConfigError as exc:
    print(exc)  # one line per problem
write_yaml(job, "job.yaml")  # or dump_yaml(job) for a string
```

- Unknown keys are errors at every level. Scalars follow YAML 1.2 style: `time: 17:30`, `on`
  and `2026-10-04` stay strings.
- Comments are not preserved on save, and every field (defaults included) is written.
- `weekday` accepts any capitalisation, but the JSON Schema lists the lowercase values only.
- Cross-field errors (e.g. `schedule: weekday is required ...`) may appear only after the
  field errors of the same section are fixed.

```text
invalid job file job.yaml:
  schedule.timezone: unknown timezone 'Europe/Atlantis'
  notification.to[0]: value is not a valid email address: An email address must have an @-sign.
  sources[2].url: URL scheme should be 'http' or 'https'
  llm.frequncy: Extra inputs are not permitted
```

Regenerate the schema after changing the models (a test fails when it is stale):

```bash
uv run python -m invio.config.job
```

## Layout

```
src/invio/
  cli/          Typer app (main.py) and auto-discovered commands/
  config/       settings, job.py (job file models + YAML load/save)
  domain.py     shared Candidate / ProcessedItem records (stdlib only)
  db/           SQLAlchemy models and sessions
  graph/        LangGraph pipelines
  sources/      source adapters
  llm/          LLM providers
  notify/       notifications
  scheduling/   scheduling
alembic/        database migrations (placeholder)
tests/
```
