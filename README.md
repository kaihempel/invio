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
- `write_yaml` replaces the file atomically, keeps an existing file's mode and follows
  symlinks (the link stays, its target is updated).
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

## Database

Invio stores jobs, runs, items, digests, notifications and LLM usage in MariaDB (production) or
SQLite (development and tests) through SQLAlchemy 2.x. Configure the engine in
`INVIO_DATABASE_URL`:

```bash
INVIO_DATABASE_URL=mysql+pymysql://user:pass@host:3306/invio?charset=utf8mb4
INVIO_DATABASE_URL=sqlite:////absolute/path/invio.sqlite   # development only
```

- MariaDB needs `utf8mb4` (emoji). Invio forces `charset=utf8mb4`, strict SQL mode and a UTC
  session on every connection, runs transactions at `READ COMMITTED`, and creates all tables as
  InnoDB / `utf8mb4_unicode_ci`.
- Set the server's `max_allowed_packet` to at least 32M if you store raw content up to 16 MB.
- Timestamps are aware UTC in Python (naive datetimes are rejected) and naive UTC in the database.
- The schema is versioned with Alembic; the migrations ship inside the package.

```bash
uv run invio db upgrade       # apply migrations; prints "database at revision 0002"
```

DDL is not transactional on MariaDB: if an upgrade fails midway, earlier steps stay applied and
may need manual cleanup before retrying. Job names compare exactly on every backend (binary
collation on MariaDB since revision `0002`), so `Digest` and `digest` are two different jobs.

Exit codes: `0` success (also when already up to date), `2` `INVIO_DATABASE_URL` missing or not a
valid URL, or its driver is not installed, `1` any other failure (unreachable server, migration error, unknown revision). The
password never appears in output.

Developers can use Alembic directly (reads `INVIO_DATABASE_URL`):

```bash
uv run alembic downgrade base
uv run alembic revision --autogenerate -m "describe the change"
```

Tests marked `db` (`uv run pytest -m db`) run on in-memory SQLite by default. To run them on
MariaDB, point `INVIO_TEST_DATABASE_URL` at an empty test database (the schema is managed by the
tests, so never use a database you care about):

```bash
INVIO_TEST_DATABASE_URL="mysql+pymysql://root@127.0.0.1:3306/invio_test?charset=utf8mb4" \
  uv run pytest -m db
```

Run them serially against MariaDB (no `pytest -n`): the migration tests downgrade and re-upgrade
the shared test database.

### Job service

`invio.services.jobs.JobService` manages jobs; use it instead of touching SQLAlchemy directly
(records and errors never expose SQLAlchemy types).

```python
from invio.services.jobs import JobService

service = JobService.from_settings()  # needs INVIO_DATABASE_URL
service.import_yaml("docs/job.example.yaml")  # stored as "job.example"; replace=True to overwrite
record = service.create("ai-news", {...})  # mapping or JobConfig; JobConfigError if invalid
service.list(enabled_only=True)  # ordered by name
service.set_enabled("ai-news", False)  # keeps history, clears next_run_at
print(service.export_yaml("ai-news"))  # YAML text; pass a path to write a file
service.delete("ai-news")  # removes the job and its history
```

Errors: `JobNameError`, `JobExistsError`, `JobNotFoundError`, `JobConfigError` (invalid input) and
`StoredJobConfigError` (a stored config no longer validates; `list` skips such jobs and logs a
warning). Disabling a job with a broken stored config still pauses it, then raises
`StoredJobConfigError`. `next_run_at` is a placeholder (the current time) until scheduling (#6) lands.
Record repositories for runs, items, digests, notifications and LLM usage live in
`invio.db.repositories`; they flush but never commit (use `session_scope`).

## Layout

```
src/invio/
  cli/          Typer app (main.py) and auto-discovered commands/
  config/       settings, job.py (job file models + YAML load/save)
  domain.py     shared records, status enums, url_hash (stdlib only)
  db/           models, engine/session helpers, repositories.py, migrations/ (Alembic)
  services/     jobs.py (JobService: job CRUD, YAML import/export)
  graph/        LangGraph pipelines
  sources/      source adapters
  llm/          LLM providers
  notify/       notifications
  scheduling/   scheduling
alembic.ini     developer entry point for `uv run alembic ...`
tests/
```
