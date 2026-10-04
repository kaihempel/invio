"""Job file contract: validated research job configuration with YAML import/export.

A job file is a UTF-8 YAML mapping describing one research job (schedule, notification,
sources, search, LLM and limits). Models are immutable and reject unknown keys. Comments in a
job file are not preserved when a job is saved; every field is written on save.
"""

import contextlib
import datetime as dt
import functools
import json
import os
import re
import secrets
import stat
from collections.abc import Hashable, Mapping
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self, get_args
from zoneinfo import available_timezones

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    HttpUrl,
    StrictBool,
    StrictFloat,
    StrictInt,
    ValidationError,
    field_validator,
    model_validator,
)

__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "Frequency",
    "JobConfig",
    "JobConfigError",
    "JobYamlLoader",
    "KeywordsConfig",
    "LLMConfig",
    "LLMModels",
    "LLMProvider",
    "LimitsConfig",
    "NotificationConfig",
    "RssSource",
    "ScheduleConfig",
    "SearchConfig",
    "SitemapSource",
    "SourceConfig",
    "WebSource",
    "Weekday",
    "YoutubeChannelSource",
    "YoutubePlaylistSource",
    "dump_yaml",
    "job_json_schema",
    "load_yaml",
    "validate_job",
    "write_yaml",
]

SUPPORTED_SCHEMA_VERSION: Final = 1


class Frequency(StrEnum):
    """How often a job runs."""

    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


class Weekday(StrEnum):
    """Day of the week for weekly jobs.

    Input is case-insensitive; the JSON Schema lists the lowercase values only.
    """

    MONDAY = "monday"
    TUESDAY = "tuesday"
    WEDNESDAY = "wednesday"
    THURSDAY = "thursday"
    FRIDAY = "friday"
    SATURDAY = "saturday"
    SUNDAY = "sunday"

    @property
    def number(self) -> int:
        """Day number as returned by ``datetime.date.weekday()`` (Monday is 0)."""
        return list(Weekday).index(self)


class LLMProvider(StrEnum):
    """Supported LLM providers (kept in sync with ``Settings`` by a test)."""

    MISTRAL = "mistral"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OLLAMA = "ollama"


class _StrictModel(BaseModel):
    """Base for all job models: no unknown keys, immutable, whitespace-stripped strings."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class JobConfigError(Exception):
    """A job file could not be read or is invalid; ``errors`` has one entry per problem."""

    def __init__(self, path: Path | None, errors: list[str]) -> None:
        super().__init__(path, errors)
        self.path = path
        self.errors = errors

    def __str__(self) -> str:
        header = f"invalid job file {self.path}:" if self.path is not None else "invalid job file:"
        return "\n".join([header, *(f"  {line}" for line in self.errors)])


@functools.cache
def _known_timezones() -> frozenset[str]:
    return frozenset(available_timezones())


class ScheduleConfig(_StrictModel):
    """When the job runs."""

    frequency: Frequency
    # [0-9], not \d: pydantic-core's regex treats \d as any Unicode digit (e.g. "0\u0663:30").
    time: str = Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    weekday: Weekday | None = None
    day_of_month: StrictInt | None = Field(default=None, ge=1, le=31)
    timezone: str

    @field_validator("weekday", mode="before")
    @classmethod
    def _lowercase_weekday(cls, value: object) -> object:
        return value.lower() if isinstance(value, str) else value

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        # Exact IANA names only: ``ZoneInfo`` alone would accept ``europe/berlin`` on
        # case-insensitive filesystems (macOS) but not on Linux.
        if value not in _known_timezones():
            raise ValueError(f"unknown timezone {value!r}")
        return value

    @model_validator(mode="after")
    def _weekday_and_day_match_frequency(self) -> Self:
        weekly = self.frequency is Frequency.WEEKLY
        monthly = self.frequency is Frequency.MONTHLY
        if weekly and self.weekday is None:
            raise ValueError("weekday is required when frequency is 'weekly'")
        if monthly and self.day_of_month is None:
            raise ValueError("day_of_month is required when frequency is 'monthly'")
        if self.weekday is not None and not weekly:
            raise ValueError("weekday is only allowed when frequency is 'weekly'")
        if self.day_of_month is not None and not monthly:
            raise ValueError("day_of_month is only allowed when frequency is 'monthly'")
        return self

    @property
    def local_time(self) -> dt.time:
        """``time`` as a ``datetime.time`` in the schedule's time zone."""
        hours, minutes = self.time.split(":")
        return dt.time(int(hours), int(minutes))


class NotificationConfig(_StrictModel):
    """Who gets the digest and how it is titled."""

    to: list[EmailStr] = Field(min_length=1)
    subject: str = Field(min_length=1)
    send_if_empty: StrictBool = False


_Keyword = Annotated[str, Field(min_length=1)]


class KeywordsConfig(_StrictModel):
    """Keyword pre-filters (lists stay mutable even though the model is frozen)."""

    any: list[_Keyword] = Field(default_factory=list)
    all: list[_Keyword] = Field(default_factory=list)
    exclude: list[_Keyword] = Field(default_factory=list)


class SearchConfig(_StrictModel):
    """What the job is looking for."""

    keywords: KeywordsConfig = Field(default_factory=KeywordsConfig)
    semantic_description: str = Field(min_length=1)
    min_relevance: StrictFloat = Field(default=0.6, ge=0, le=1)


class LLMModels(_StrictModel):
    """Model names for the cheap and the capable tier."""

    fast: str = Field(min_length=1)
    smart: str = Field(min_length=1)


class LLMConfig(_StrictModel):
    """LLM provider selection."""

    provider: LLMProvider
    models: LLMModels
    fallback_provider: LLMProvider | None = None

    @model_validator(mode="after")
    def _fallback_differs(self) -> Self:
        if self.fallback_provider is not None and self.fallback_provider == self.provider:
            raise ValueError("fallback_provider must differ from provider")
        return self


class LimitsConfig(_StrictModel):
    """Per-run caps."""

    max_items_per_source: StrictInt = Field(default=20, ge=1)
    max_items_per_run: StrictInt = Field(default=100, ge=1)
    max_items_in_notification: StrictInt = Field(default=20, ge=1)
    max_llm_tokens_per_run: StrictInt = Field(default=200000, ge=1)


# Source models repeat ``name``/``enabled`` instead of inheriting them: Pydantic puts inherited
# fields first, but saved files list ``type`` and the locator first (contracts/job-file.md).
_SourceName = Annotated[str | None, Field(min_length=1)]


class RssSource(_StrictModel):
    """An RSS/Atom feed."""

    type: Literal["rss"]
    url: HttpUrl
    name: _SourceName = None
    enabled: StrictBool = True


class WebSource(_StrictModel):
    """A web page."""

    type: Literal["web"]
    url: HttpUrl
    name: _SourceName = None
    enabled: StrictBool = True


class SitemapSource(_StrictModel):
    """A sitemap."""

    type: Literal["sitemap"]
    url: HttpUrl
    name: _SourceName = None
    enabled: StrictBool = True


class YoutubeChannelSource(_StrictModel):
    """A YouTube channel."""

    type: Literal["youtube_channel"]
    channel_id: str = Field(min_length=1)
    name: _SourceName = None
    enabled: StrictBool = True


class YoutubePlaylistSource(_StrictModel):
    """A YouTube playlist."""

    type: Literal["youtube_playlist"]
    playlist_id: str = Field(min_length=1)
    name: _SourceName = None
    enabled: StrictBool = True


_SourceUnion = RssSource | WebSource | SitemapSource | YoutubeChannelSource | YoutubePlaylistSource
SourceConfig = Annotated[_SourceUnion, Field(discriminator="type")]


class JobConfig(_StrictModel):
    """A complete research job definition."""

    schema_version: StrictInt = Field(
        default=1, ge=1, json_schema_extra={"maximum": SUPPORTED_SCHEMA_VERSION}
    )
    schedule: ScheduleConfig
    notification: NotificationConfig
    sources: list[SourceConfig] = Field(min_length=1)
    search: SearchConfig
    llm: LLMConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value > SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version {value} is not supported (max {SUPPORTED_SCHEMA_VERSION})"
            )
        return value


class JobYamlLoader(yaml.SafeLoader):
    """SafeLoader with YAML-1.2-core style scalars.

    Resolvers are strict subsets of PyYAML's YAML 1.1 ones, so ``safe_dump`` quotes every string
    this loader would otherwise read as a non-string. ``17:30``, ``on``, ``yes`` and dates stay
    strings.
    """

    # A new dict (populated below with new lists): SafeLoader's own resolvers stay untouched.
    yaml_implicit_resolvers = {}  # noqa: RUF012 (PyYAML class attribute, replaced per subclass)

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        """Reject duplicate keys, which PyYAML would otherwise resolve silently (last wins)."""
        self.flatten_mapping(node)
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=True)
            if not isinstance(key, Hashable):
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found unhashable key {key!r}",
                    key_node.start_mark,
                )
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


for _tag, _pattern, _first in (
    ("tag:yaml.org,2002:bool", r"^(?:true|True|TRUE|false|False|FALSE)$", "tTfF"),
    ("tag:yaml.org,2002:int", r"^[-+]?(?:0|[1-9][0-9]*)$", "-+0123456789"),
    (
        "tag:yaml.org,2002:float",
        # Like YAML 1.1, a leading-dot float has no sign: ``-.5`` stays a string (else saved
        # strings such as ``-.5`` would be written unquoted and reload as numbers).
        r"^(?:[-+]?[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][-+][0-9]+)?$",
        "-+0123456789.",
    ),
    ("tag:yaml.org,2002:null", r"^(?:~|null|Null|NULL|)$", ["~", "n", "N", ""]),
    ("tag:yaml.org,2002:merge", r"^(?:<<)$", ["<"]),
):
    JobYamlLoader.add_implicit_resolver(_tag, re.compile(_pattern), list(_first))


# Discriminator values of ``SourceConfig``; Pydantic puts them into source error locations.
_SOURCE_TAGS: Final = frozenset(
    tag
    for model in get_args(_SourceUnion)
    for tag in get_args(model.model_fields["type"].annotation)
)


def _format_validation_error(exc: ValidationError) -> list[str]:
    """Render each Pydantic error as ``<dotted.loc>: <message>``."""
    lines: list[str] = []
    for error in exc.errors(include_url=False, include_context=False, include_input=False):
        loc = list(error["loc"])
        if (
            len(loc) > 2
            and loc[0] == "sources"
            and isinstance(loc[1], int)
            and loc[2] in _SOURCE_TAGS
        ):
            del loc[2]
        text = ""
        for part in loc:
            text += f"[{part}]" if isinstance(part, int) else (f".{part}" if text else str(part))
        message = error["msg"].removeprefix("Value error, ")
        lines.append(f"{text}: {message}" if text else message)
    return lines


def validate_job(data: Mapping[str, Any]) -> JobConfig:
    """Validate a job mapping; raises ``JobConfigError`` (``path`` is ``None``) on any problem."""
    try:
        return JobConfig.model_validate(dict(data))
    except ValidationError as exc:
        raise JobConfigError(None, _format_validation_error(exc)) from exc


def _yaml_problem(exc: Exception) -> str:
    """Describe a YAML error on a single line."""
    if isinstance(exc, yaml.MarkedYAMLError) and exc.problem_mark is not None:
        mark = exc.problem_mark
        return f"{exc.problem} (line {mark.line + 1}, column {mark.column + 1})"
    lines = str(exc).splitlines()
    return lines[0] if lines else type(exc).__name__


def load_yaml(path: str | os.PathLike[str]) -> JobConfig:
    """Load and validate a job file; raises ``JobConfigError`` on any problem."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise JobConfigError(file, [f"cannot read job file {file}: {exc}"]) from exc
    try:
        data: object = yaml.load(text, Loader=JobYamlLoader)
    except (yaml.YAMLError, ValueError) as exc:
        raise JobConfigError(
            file, [f"invalid YAML in job file {file}: {_yaml_problem(exc)}"]
        ) from exc
    if not isinstance(data, dict):
        raise JobConfigError(file, [f"job file {file} must contain a mapping at the top level"])
    try:
        return validate_job(data)
    except JobConfigError as exc:
        raise JobConfigError(file, exc.errors) from exc


class _JobYamlDumper(yaml.SafeDumper):
    """SafeDumper that double-quotes strings containing NEL (U+0085).

    With ``allow_unicode`` PyYAML writes NEL raw into single-quoted scalars, where a reader folds
    it into a space; double quotes escape it as ``\\N``.
    """


def _represent_str(dumper: yaml.SafeDumper, value: str) -> yaml.ScalarNode:
    if "\x85" in value:
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"')
    return dumper.represent_str(value)


_JobYamlDumper.add_representer(str, _represent_str)


def dump_yaml(config: JobConfig) -> str:
    """Render a job as YAML: every field (defaults and ``None`` included) in definition order."""
    return yaml.dump(
        config.model_dump(mode="json"),
        Dumper=_JobYamlDumper,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )


def _create_temp(target: Path) -> tuple[int, Path]:
    """Create an exclusive temp file next to ``target`` with mode ``0o666`` minus umask.

    The kernel applies the umask on creation, so the process-wide umask is never touched.
    """
    while True:
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        try:
            return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666), tmp
        except FileExistsError:
            continue


def _fsync_directory(directory: Path) -> None:
    """Make a rename in ``directory`` durable (POSIX only; Windows cannot open directories)."""
    if os.name != "posix":
        return
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_yaml(config: JobConfig, path: str | os.PathLike[str]) -> str:
    """Write a job as UTF-8 YAML atomically (temp file + replace) and return the text.

    Raises ``OSError``.

    A symlinked ``path`` is followed, so the link stays and its target is updated. An existing
    file keeps its mode; a new file gets ``0o666`` minus umask.
    """
    target = Path(os.path.realpath(path))
    text = dump_yaml(config)
    fd, tmp = _create_temp(target)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(FileNotFoundError):  # new file: keep the umask-based mode
            os.chmod(tmp, stat.S_IMODE(target.stat().st_mode))
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_directory(target.parent)
    return text


def job_json_schema() -> dict[str, Any]:
    """Return the JSON Schema of ``JobConfig`` (published as ``docs/job.schema.json``)."""
    return JobConfig.model_json_schema()


def main() -> None:
    """Write ``docs/job.schema.json`` relative to the current directory (the repo root)."""
    target = Path("docs/job.schema.json")
    target.write_text(
        json.dumps(job_json_schema(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(target)


if __name__ == "__main__":
    main()
