# Contract: Python API of `invio.services.jobs`, `invio.db.repositories` and helpers

Consumers: CLI (#9), scheduler (#23), pipeline/persistence (#14, #19). Signatures are the
contract; bodies are implementation detail. All datetimes are timezone-aware UTC.

## `invio.config.job` (addition)

```python
def validate_job(data: Mapping[str, Any]) -> JobConfig
    """Validate a job mapping; raises JobConfigError(None, [<dotted.loc>: <msg>, ...])."""
```

`load_yaml` delegates its model validation to `validate_job` (messages unchanged).

## `invio.db.session` (addition)

```python
@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]
    """Commit on success, roll back on any exception (re-raised), always close."""
```

## `invio.services.jobs`

```python
NextRun = Callable[[ScheduleConfig, datetime], datetime]   # (schedule, after) -> next UTC run

@dataclass(frozen=True, slots=True, kw_only=True)
class JobRecord:
    name: str
    enabled: bool
    config: JobConfig
    next_run_at: datetime | None
    created_at: datetime
    updated_at: datetime

class JobExistsError(Exception):           name: str   # "job 'x' already exists"
class JobNotFoundError(LookupError):       name: str   # "job 'x' not found"
class JobNameError(ValueError):            name: str
class StoredJobConfigError(JobConfigError): name: str  # "invalid stored job 'x':\n  <errors>"

class JobService:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        next_run: NextRun = _next_run_stub,     # replaced by scheduling.compute_next_run (#6)
        clock: Callable[[], datetime] = utcnow,
    ) -> None: ...

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "JobService": ...
        # engine from settings.require_secret("database_url"); raises MissingSettingError if unset

    def create(self, name: str, config: JobConfig | Mapping[str, Any]) -> JobRecord
        # JobNameError, JobConfigError, JobExistsError
    def get_by_name(self, name: str) -> JobRecord
        # JobNotFoundError, StoredJobConfigError
    def list(self, *, enabled_only: bool = False) -> list[JobRecord]
        # ordered by name; skips + warns on invalid stored config
    def update(self, name: str, config: JobConfig | Mapping[str, Any]) -> JobRecord
        # full replacement; JobNotFoundError, JobConfigError
    def set_enabled(self, name: str, enabled: bool) -> JobRecord
        # JobNotFoundError; no-op if already in that state
    def delete(self, name: str) -> None
        # JobNotFoundError; history removed by cascade
    def export_yaml(self, name: str, path: str | os.PathLike[str] | None = None) -> str
        # returns YAML text; writes atomically when path given; JobNotFoundError, OSError
    def import_yaml(
        self, path: str | os.PathLike[str], name: str | None = None, *, replace: bool = False
    ) -> JobRecord
        # name defaults to Path(path).stem; JobConfigError, JobNameError, JobExistsError
```

Guarantees:

- Each method runs in exactly one `session_scope`; on any exception nothing is stored.
- No SQLAlchemy type or exception crosses the service boundary (`IntegrityError` on create →
  `JobExistsError`). `MissingSettingError` and `OSError` are the only non-service errors besides
  `JobConfigError`.
- After a successful change exactly one `INFO` record is logged on `invio.services.jobs` with
  `event` ∈ {`job.created`, `job.updated`, `job.enabled`, `job.disabled`, `job.deleted`,
  `job.imported`} and `job_name`; never config contents. No-ops and failures log nothing.
- `list` logs one `WARNING` (`event="job.invalid_config"`, `job_name`, `error_count`) per
  skipped job.

## `invio.db.repositories`

All repositories take a `Session` in `__init__`, flush but never commit, and return ORM rows
from `invio.db.models`. History repositories take a numeric `job_id`; consumers obtain it in the
same unit of work via `JobRepository(session).get_by_name(name).id` (`JobRecord` exposes no id,
see research R4):

```python
with session_scope(factory) as session:
    job = JobRepository(session).get_by_name("ai-news")  # None -> treat as not found
    run = RunRepository(session).start(job.id)
```

```python
class JobRepository:
    def get_by_name(self, name: str) -> Job | None
    def list(self, *, enabled_only: bool = False) -> list[Job]          # by name
    def add(self, job: Job) -> Job                                     # flush; may raise IntegrityError
    def delete(self, job: Job) -> None

class RunRepository:
    def start(self, job_id: int, *, started_at: datetime | None = None) -> Run   # status running
    def finish(self, run: Run, status: RunStatus, *, stats: dict[str, Any] | None = None,
               error: str | None = None, finished_at: datetime | None = None) -> Run
    def get(self, run_id: int) -> Run | None
    def list_for_job(self, job_id: int, *, limit: int | None = None) -> list[Run]  # newest first

class ItemRepository:
    def add(self, job_id: int, candidate: Candidate, *, run_id: int | None = None
            ) -> tuple[Item, bool]                                     # (item, created)
    def get(self, item_id: int) -> Item | None
    def seen(self, job_id: int, url_hash: str) -> bool
    def list_for_job(self, job_id: int, *, status: ItemStatus | None = None) -> list[Item]

class DigestRepository:
    def add(self, job_id: int, title: str, body: str, item_ids: list[int], *,
            run_id: int | None = None) -> Digest
    def list_for_job(self, job_id: int) -> list[Digest]               # newest first

class NotificationRepository:
    def add(self, job_id: int, channel: str, recipient: str, *, run_id: int | None = None,
            digest_id: int | None = None, payload: dict[str, Any] | None = None) -> Notification
    def mark(self, notification: Notification, status: NotificationStatus, *,
             error: str | None = None, sent_at: datetime | None = None) -> Notification
    def list_for_job(self, job_id: int, *, status: NotificationStatus | None = None
                     ) -> list[Notification]
    def list_for_run(self, run_id: int) -> list[Notification]

@dataclass(frozen=True, slots=True)
class UsageTotals:
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal

class UsageRepository:
    def add(self, job_id: int, provider: str, model: str, input_tokens: int,
            output_tokens: int, *, run_id: int | None = None, purpose: str | None = None,
            cost_usd: Decimal | None = None) -> LlmUsage
    def totals_for_run(self, run_id: int) -> UsageTotals
    def totals_for_job(self, job_id: int) -> UsageTotals
```
