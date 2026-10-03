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

## Layout

```
src/invio/
  cli/          Typer app (main.py) and auto-discovered commands/
  config/       settings
  db/           SQLAlchemy models and sessions
  graph/        LangGraph pipelines
  sources/      source adapters
  llm/          LLM providers
  notify/       notifications
  scheduling/   scheduling
alembic/        database migrations (placeholder)
tests/
```
