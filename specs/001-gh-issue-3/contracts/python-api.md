# Contract: Python API

Consumers: CLI commands, scheduler, pipeline tracks. Signatures are the contract; bodies are
implementation details.

## `invio.config.job`

```python
SUPPORTED_SCHEMA_VERSION: Final = 1


class Frequency(StrEnum): ...  # daily | weekly | monthly


class Weekday(StrEnum): ...  # monday … sunday


class LLMProvider(StrEnum): ...  # mistral | openai | anthropic | google | ollama


class ScheduleConfig(BaseModel): ...


class NotificationConfig(BaseModel): ...


class RssSource(BaseModel): ...


class WebSource(BaseModel): ...


class SitemapSource(BaseModel): ...


class YoutubeChannelSource(BaseModel): ...


class YoutubePlaylistSource(BaseModel): ...


SourceConfig = Annotated[
    RssSource | WebSource | SitemapSource | YoutubeChannelSource | YoutubePlaylistSource,
    Field(discriminator="type"),
]


class KeywordsConfig(BaseModel): ...


class SearchConfig(BaseModel): ...


class LLMModels(BaseModel): ...


class LLMConfig(BaseModel): ...


class LimitsConfig(BaseModel): ...


class JobConfig(BaseModel): ...


class JobConfigError(Exception):
    path: Path | None  # always Path(given path) when raised by load_yaml
    errors: list[str]  # one "<loc>: <msg>" entry per problem


def load_yaml(path: str | os.PathLike[str]) -> JobConfig:
    ...
    # raises JobConfigError (never ValidationError / yaml.YAMLError / OSError)


def dump_yaml(config: JobConfig) -> str: ...
def write_yaml(config: JobConfig, path: str | os.PathLike[str]) -> None: ...  # UTF-8


def job_json_schema() -> dict[str, Any]: ...  # JobConfig.model_json_schema()
```

Guarantees:

- `load_yaml(p) == load_yaml(q)` after `write_yaml(load_yaml(p), q)` (lossless round-trip).
- All models are immutable (`frozen=True`: field assignment raises) and compare equal by value.
  Models holding lists (e.g. `NotificationConfig.to`) are not hashable, and the lists themselves
  stay mutable.
- Importing the module does no I/O and needs no settings/environment.

`python -m invio.config.job` writes `docs/job.schema.json` (relative to the current working
directory, which must be the repo root) and prints the path.

## `invio.domain`

```python
ItemType = Literal["article", "video"]


@dataclass(frozen=True, slots=True, kw_only=True)
class Candidate:
    url: str
    url_hash: str
    title: str
    published_at: datetime | None
    type: ItemType
    teaser: str | None
    content_hash: str | None


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessedItem:
    id: int
    url: str
    type: ItemType
    title: str
    raw_content: str
    relevance: float | None = None
    summary: str | None = None
    error: str | None = None
```

Guarantee: `import invio.domain` loads only the standard library (plus the `invio` package
root).
