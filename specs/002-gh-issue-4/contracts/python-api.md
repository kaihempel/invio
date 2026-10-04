# Contract: Python API of `invio.db` and new `invio.domain` enums

Consumers (graph, scheduling, notify, later repository issues) import only these names.

## `invio.domain` (stdlib only, additions)

```python
class ItemStatus(StrEnum):
    NEW = "new"; EXTRACTED = "extracted"; SKIPPED_KEYWORD = "skipped_keyword"
    SKIPPED_IRRELEVANT = "skipped_irrelevant"; RELEVANT = "relevant"
    SUMMARIZED = "summarized"; FAILED = "failed"

class RunStatus(StrEnum):
    RUNNING = "running"; SUCCEEDED = "succeeded"; PARTIAL = "partial"; FAILED = "failed"

class NotificationStatus(StrEnum):
    PENDING = "pending"; SENT = "sent"; FAILED = "failed"; SKIPPED = "skipped"

def url_hash(url: str) -> str
    """Lower-case SHA-256 hex digest (64 chars) of the UTF-8 encoded URL."""
```

The domain module must stay importable without SQLAlchemy (existing fresh-interpreter test).

## `invio.db.models`

```python
class Base(DeclarativeBase):  # metadata with naming convention (R8)
    ...


class Job(Base):
    __tablename__ = "jobs"


class Run(Base):
    __tablename__ = "runs"


class Item(Base):
    __tablename__ = "items"


class Digest(Base):
    __tablename__ = "digests"


class Notification(Base):
    __tablename__ = "notifications"


class LlmUsage(Base):
    __tablename__ = "llm_usage"
```

Columns, types, defaults and constraints exactly as in [data-model.md](../data-model.md),
declared with typed `Mapped[...]` / `mapped_column(...)`. No `relationship()`s (R7).

## `invio.db.types`

```python
class UTCDateTime(TypeDecorator[datetime]):
    """Stores naive UTC, returns aware UTC; raises ValueError for naive input."""

def utcnow() -> datetime   # aware UTC "now", used as column default
```

## `invio.db.session`

```python
def normalize_url(url: str | URL) -> URL
    """Parse the URL; for mysql/mariadb dialects force query charset=utf8mb4.
    Raises sqlalchemy.exc.ArgumentError for unparsable URLs."""

def create_db_engine(url: str | URL, **kwargs: Any) -> Engine
    """Engine with invio's connection rules:
    - mysql/mariadb: utf8mb4 charset, init_command (strict sql_mode, UTC time_zone),
      pool_pre_ping=True, pool_recycle=3600
    - sqlite: PRAGMA foreign_keys=ON on every connection"""

def session_factory(engine: Engine) -> sessionmaker[Session]
    """expire_on_commit=False."""

def redact(text: str, url: str | URL) -> str
    """Replace the URL's password and its full string form with '***'."""
```

## `invio.db.migrate`

```python
MIGRATIONS = "invio.db:migrations"

def alembic_config(*, url: str | URL | None = None,
                   connection: Connection | None = None) -> alembic.config.Config
    """Config with script_location=MIGRATIONS and the url/connection in config.attributes
    (never in the 'sqlalchemy.url' option)."""

def upgrade(config: Config, revision: str = "head") -> str      # returns resulting revision
def downgrade(config: Config, revision: str = "base") -> None
def current_revision(connection: Connection) -> str | None
```

## Errors

- `invio.config.settings.MissingSettingError` — propagated when no URL is configured.
- `sqlalchemy.exc.ArgumentError` — invalid URL.
- `sqlalchemy.exc.IntegrityError` — duplicate `(job_id, url_hash)`, duplicate job name,
  CHECK violations (SQLite), FK violations.
- `sqlalchemy.exc.DataError` / `IntegrityError` / `StatementError` — invalid enum values
  (MariaDB strict mode → `DataError`; SQLite CHECK → `IntegrityError`; ORM with
  `validate_strings=True` → `StatementError` wrapping `LookupError` before reaching the DB).
