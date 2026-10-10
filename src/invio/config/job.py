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
from collections.abc import Callable, Hashable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self, get_args
from urllib.parse import parse_qs, urlsplit
from zoneinfo import available_timezones

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    EmailStr,
    Field,
    HttpUrl,
    StrictBool,
    StrictFloat,
    StrictInt,
    Tag,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)
from selectolax.lexbor import LexborHTMLParser

from invio.config.languages import ISO_639_1

__all__ = [
    "SUPPORTED_SCHEMA_VERSION",
    "TIME_PATTERN",
    "ArchiveConfig",
    "Frequency",
    "JobConfig",
    "JobConfigError",
    "JobYamlLoader",
    "KeywordsConfig",
    "LLMConfig",
    "LLMFallbackModels",
    "LLMModels",
    "LLMProvider",
    "LLMRole",
    "LLMRoleModel",
    "LLMTarget",
    "LimitsConfig",
    "NotificationConfig",
    "RoleSelection",
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
    "known_timezones",
    "load_yaml",
    "loads_yaml",
    "parse_youtube_channel",
    "parse_youtube_playlist",
    "validate_job",
    "write_yaml",
]

SUPPORTED_SCHEMA_VERSION: Final = 1
TIME_PATTERN: Final = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
"""Pattern of ``schedule.time`` (``HH:MM``); [0-9] on purpose, see ``ScheduleConfig``."""


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
def known_timezones() -> frozenset[str]:
    return frozenset(available_timezones())


class ScheduleConfig(_StrictModel):
    """When the job runs."""

    frequency: Frequency
    # [0-9], not \d: pydantic-core's regex treats \d as any Unicode digit (e.g. "0\u0663:30").
    time: str = Field(pattern=TIME_PATTERN)
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
        if value not in known_timezones():
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


LLMRole = Literal["fast", "smart"]
"""The two model tiers a job uses: ``fast`` (cheap, per item) and ``smart`` (merging, digest)."""

_ModelId = Annotated[str, Field(min_length=1)]


class LLMTarget(_StrictModel):
    """A model of a named provider (a role's fallback)."""

    provider: LLMProvider
    model: _ModelId


class LLMRoleModel(_StrictModel):
    """Long form of a role: its own provider (default ``llm.provider``) and fallback."""

    provider: LLMProvider | None = None
    model: _ModelId
    fallback: LLMTarget | None = None


def _role_form(value: object) -> str:
    """Tag of a role entry: a mapping is the long form, anything else must be a model id."""
    return "long_form" if isinstance(value, Mapping | LLMRoleModel) else "shorthand"


# Discriminated by type, so a bad entry reports the problems of one form only.
_RoleEntry = Annotated[
    Annotated[_ModelId, Tag("shorthand")] | Annotated[LLMRoleModel, Tag("long_form")],
    Discriminator(_role_form),
]


class LLMModels(_StrictModel):
    """Model of the cheap and the capable tier: a model id of ``llm.provider`` or the long form."""

    fast: _RoleEntry
    smart: _RoleEntry


class LLMFallbackModels(_StrictModel):
    """Model ids of ``llm.fallback_provider`` for the roles without their own fallback."""

    fast: _ModelId
    smart: _ModelId


@dataclass(frozen=True, slots=True)
class RoleSelection:
    """What one role resolves to: provider, model and the fallback, if any."""

    provider: LLMProvider
    model: str
    fallback: LLMTarget | None


class LLMConfig(_StrictModel):
    """LLM provider selection.

    ``provider`` is the default provider of every role. A role's fallback is its own
    ``fallback`` if set, else ``fallback_provider`` with the role's ``fallback_models`` entry.
    ``fallback_provider`` alone (the old form) validates but configures no fallback.
    """

    provider: LLMProvider
    models: LLMModels
    fallback_provider: LLMProvider | None = None
    fallback_models: LLMFallbackModels | None = None

    @model_validator(mode="after")
    def _fallback_differs(self) -> Self:
        if self.fallback_provider is not None and self.fallback_provider == self.provider:
            raise ValueError("fallback_provider must differ from provider")
        if self.fallback_models is not None and self.fallback_provider is None:
            raise ValueError("fallback_models requires fallback_provider")
        for name in get_args(LLMRole):
            selection = self.role(name)
            if selection.fallback is not None and selection.fallback.provider == selection.provider:
                raise ValueError(
                    f"the fallback provider of role '{name}' must differ from its provider"
                )
        return self

    def role(self, name: LLMRole) -> RoleSelection:
        """Resolve role ``name`` (shorthand or long form) to its provider, model and fallback."""
        entry: str | LLMRoleModel = getattr(self.models, name)
        if isinstance(entry, str):
            return RoleSelection(self.provider, entry, self._job_fallback(name))
        fallback = entry.fallback if entry.fallback is not None else self._job_fallback(name)
        return RoleSelection(entry.provider or self.provider, entry.model, fallback)

    def _job_fallback(self, name: LLMRole) -> LLMTarget | None:
        if self.fallback_provider is None or self.fallback_models is None:
            return None
        return LLMTarget(provider=self.fallback_provider, model=getattr(self.fallback_models, name))


class LimitsConfig(_StrictModel):
    """Per-run caps (``baseline_items``: per source, first runs only)."""

    max_items_per_source: StrictInt = Field(default=20, ge=1)
    max_items_per_run: StrictInt = Field(default=100, ge=1)
    baseline_items: StrictInt = Field(default=10, ge=1)
    max_items_in_notification: StrictInt = Field(default=20, ge=1)
    max_llm_tokens_per_run: StrictInt = Field(default=200000, ge=1)


class ArchiveConfig(_StrictModel):
    """Static HTML archive of the job's digests (``base_url`` only feeds the link in the mail)."""

    enabled: StrictBool = False
    base_url: str | None = None

    @field_validator("base_url")
    @classmethod
    def _http_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if any(char.isspace() or not char.isprintable() for char in value):
            raise ValueError("base_url must not contain whitespace or control characters")
        try:
            parts = urlsplit(value)
            port = parts.port
        except ValueError as exc:
            raise ValueError("base_url must be a valid http or https URL") from exc
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("base_url must be an http or https URL with a host")
        if parts.username is not None or parts.password is not None or "@" in parts.netloc:
            raise ValueError("base_url must not contain credentials")
        if port == 0:
            raise ValueError("base_url must be a valid http or https URL")
        if parts.query or parts.fragment or "?" in value or "#" in value:
            raise ValueError("base_url must not contain a query or a fragment")
        return value.rstrip("/")


# Source models repeat ``name``/``enabled`` instead of inheriting them: Pydantic puts inherited
# fields first, but saved files list ``type`` and the locator first (contracts/job-file.md).
_SourceName = Annotated[str | None, Field(min_length=1)]


class RssSource(_StrictModel):
    """An RSS/Atom feed; entries older than ``max_age_days`` (if set) are ignored."""

    type: Literal["rss"]
    url: HttpUrl
    name: _SourceName = None
    enabled: StrictBool = True
    max_age_days: StrictInt | None = Field(default=None, ge=1)


_OptionalText = Annotated[str | None, Field(min_length=1)]


def _check_css_selector(value: str | None) -> str | None:
    """Reject a CSS selector the page parser cannot parse (``None`` passes).

    Validated with the engine that evaluates ``selector`` on a page. ``wait_for`` is evaluated by
    the browser (Playwright's ``css=`` engine), which accepts more; checking it here limits it to
    plain CSS, and a selector the browser still refuses fails the render with
    ``invalid_wait_for``. Any parser error becomes a stable message, so the text does not
    depend on the library version.
    """
    if value is None:
        return None
    try:
        LexborHTMLParser("<html></html>").css(value)
    except Exception:  # the library's error types are not part of its contract
        raise ValueError("invalid CSS selector") from None
    return value


def _check_regex(value: str) -> None:
    """Raise ``ValueError`` if ``value`` is not a valid regular expression."""
    try:
        re.compile(value)
    except re.error as exc:
        raise ValueError(f"invalid regular expression: {exc}") from None


class WebSource(_StrictModel):
    """A web page tracked for changes, or an index page whose article links are collected.

    ``selector`` limits the tracked text (``mode: page``) or the collected links
    (``mode: links``) to the matching elements; without it the whole ``<body>`` counts.
    ``url_pattern`` (a regular expression searched in the absolute link URL, any host) only
    applies to ``mode: links``; without it only links on the page's own host are kept.
    ``render: js`` loads the page in a headless browser first (optional extra), optionally
    waiting for the element ``wait_for``.
    """

    type: Literal["web"]
    url: HttpUrl
    name: _SourceName = None
    enabled: StrictBool = True
    selector: _OptionalText = None
    mode: Literal["page", "links"] = "page"
    url_pattern: _OptionalText = None
    render: Literal["static", "js"] = "static"
    wait_for: _OptionalText = None

    @field_validator("selector", "wait_for")
    @classmethod
    def _valid_selector(cls, value: str | None) -> str | None:
        return _check_css_selector(value)

    @field_validator("url_pattern")
    @classmethod
    def _valid_pattern(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is None:
            return None
        _check_regex(value)
        # ``mode`` is missing from ``info.data`` when it failed validation: that error is
        # reported on its own.
        if info.data.get("mode", "links") != "links":
            raise ValueError("only allowed with mode: links")
        return value

    @field_validator("wait_for")
    @classmethod
    def _wait_for_needs_js(cls, value: str | None, info: ValidationInfo) -> str | None:
        if value is not None and info.data.get("render", "js") != "js":
            raise ValueError("only allowed with render: js")
        return value


class SitemapSource(_StrictModel):
    """A sitemap (``urlset``, or a ``sitemapindex`` one level deep) whose URLs are candidates.

    ``url_pattern`` (a regular expression searched in the absolute URL) keeps only matching
    URLs; entries whose ``lastmod`` is older than ``max_age_days`` (if set) are ignored.
    """

    type: Literal["sitemap"]
    url: HttpUrl
    name: _SourceName = None
    enabled: StrictBool = True
    url_pattern: _OptionalText = None
    max_age_days: StrictInt | None = Field(default=None, ge=1)

    @field_validator("url_pattern")
    @classmethod
    def _valid_pattern(cls, value: str | None) -> str | None:
        if value is None:
            return None
        _check_regex(value)
        return value


_YOUTUBE_HOSTS: Final = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
)
_YOUTUBE_ID: Final = re.compile(r"[A-Za-z0-9_-]+")
_YOUTUBE_HANDLE: Final = re.compile(r"[A-Za-z0-9._-]+")


def _youtube_url_parts(value: str, field: str) -> tuple[str, str]:
    """``(path, query)`` of an https URL on an allowed YouTube host, else ``ValueError``."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise ValueError(f"{field}: not a valid URL") from None
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in _YOUTUBE_HOSTS:
        raise ValueError(f"{field}: a URL must be https on youtube.com")
    if parts.username is not None or parts.password is not None or port is not None:
        raise ValueError(f"{field}: a URL must not contain credentials or a port")
    return parts.path, parts.query


def parse_youtube_channel(value: str) -> tuple[Literal["channel_id", "handle"], str]:
    """Parse a ``channel_id`` value into ``(kind, token)``; the handle has no leading ``@``.

    Accepts a channel id, an ``@handle`` or an https YouTube channel URL (``/channel/<id>`` or
    ``/@<handle>``, optionally followed by one tab segment such as ``/videos``). Raises
    ``ValueError`` naming the field otherwise.
    """
    rule = "channel_id: expected a channel id, an @handle or a YouTube channel URL"
    if "://" in value:
        path, _ = _youtube_url_parts(value, "channel_id")
        segments = [part for part in path.split("/") if part]
        is_channel = bool(segments) and segments[0] == "channel"
        # /channel/<id>[/<tab>] or /@<handle>[/<tab>]
        if not segments or len(segments) > (3 if is_channel else 2):
            raise ValueError(rule)
        if (is_channel and len(segments) < 2) or (not is_channel and segments[0][0] != "@"):
            raise ValueError(rule)
        tab = segments[2:] if is_channel else segments[1:]
        if any(not _YOUTUBE_ID.fullmatch(part) for part in tab):
            raise ValueError(rule)
        return _channel_token(segments[1] if is_channel else segments[0], rule)
    return _channel_token(value, rule)


def _channel_token(token: str, rule: str) -> tuple[Literal["channel_id", "handle"], str]:
    if token.startswith("@"):
        if _YOUTUBE_HANDLE.fullmatch(token[1:]):
            return "handle", token[1:]
    elif _YOUTUBE_ID.fullmatch(token):
        return "channel_id", token
    raise ValueError(rule)


def parse_youtube_playlist(value: str) -> str:
    """Parse a ``playlist_id`` value (a playlist id or a YouTube URL with ``list=``).

    Returns the validated playlist id; raises ``ValueError`` naming the field otherwise.
    """
    rule = "playlist_id: expected a playlist id or a YouTube URL with list=<id>"
    if "://" in value:
        _, query = _youtube_url_parts(value, "playlist_id")
        values = parse_qs(query).get("list", [])
        if len(values) != 1:
            raise ValueError(rule)
        value = values[0]
    if not _YOUTUBE_ID.fullmatch(value):
        raise ValueError(rule)
    return value


def _locator_check(parse: Callable[[str], object]) -> Callable[[object], object]:
    """A ``before`` validator that runs ``parse`` on a non-empty string and returns it unchanged.

    ``before``: the base model strips whitespace first, which would hide " UC1".
    """

    def check(value: object) -> object:
        if isinstance(value, str) and value:
            parse(value)
        return value

    return check


class YoutubeChannelSource(_StrictModel):
    """A YouTube channel (id, ``@handle`` or channel URL); its latest videos are collected.

    ``max_items`` caps the listing (yt-dlp's ``playlistend``) before the result is sorted newest
    first. ``max_age_days`` (if set) drops videos older than that, but only when yt-dlp supplies
    a date; flat listings often lack it, and undated videos are kept.
    """

    type: Literal["youtube_channel"]
    channel_id: str = Field(min_length=1)
    name: _SourceName = None
    enabled: StrictBool = True
    max_age_days: StrictInt | None = Field(default=None, ge=1)
    max_items: StrictInt = Field(default=20, ge=1, le=200)

    valid_locator = field_validator("channel_id", mode="before")(
        _locator_check(parse_youtube_channel)
    )


class YoutubePlaylistSource(_StrictModel):
    """A YouTube playlist (id or URL with ``list=``); its first ``max_items`` entries are collected.

    ``max_items`` caps the listing (yt-dlp's ``playlistend``) before the result is sorted newest
    first, so a playlist listed oldest first yields its first (oldest) ``max_items`` entries,
    not the latest ones. ``max_age_days`` (if set) drops videos older than that, but only when
    yt-dlp supplies a date; flat listings often lack it, and undated videos are kept.
    """

    type: Literal["youtube_playlist"]
    playlist_id: str = Field(min_length=1)
    name: _SourceName = None
    enabled: StrictBool = True
    max_age_days: StrictInt | None = Field(default=None, ge=1)
    max_items: StrictInt = Field(default=20, ge=1, le=200)

    valid_locator = field_validator("playlist_id", mode="before")(
        _locator_check(parse_youtube_playlist)
    )


_SourceUnion = RssSource | WebSource | SitemapSource | YoutubeChannelSource | YoutubePlaylistSource
SourceConfig = Annotated[_SourceUnion, Field(discriminator="type")]


class JobConfig(_StrictModel):
    """A complete research job definition."""

    schema_version: StrictInt = Field(
        default=1, ge=1, json_schema_extra={"maximum": SUPPORTED_SCHEMA_VERSION}
    )
    language: str = Field(
        default="en",
        pattern=r"^[a-z]{2}$",
        description="Language summaries are written in (ISO 639-1 code)",
    )
    schedule: ScheduleConfig
    notification: NotificationConfig
    sources: list[SourceConfig] = Field(min_length=1)
    search: SearchConfig
    llm: LLMConfig
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    archive: ArchiveConfig = Field(default_factory=ArchiveConfig)

    @field_validator("schema_version")
    @classmethod
    def _supported_version(cls, value: int) -> int:
        if value > SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"schema_version {value} is not supported (max {SUPPORTED_SCHEMA_VERSION})"
            )
        return value

    @field_validator("language")
    @classmethod
    def _known_language(cls, value: str) -> str:
        if value not in ISO_639_1:
            raise ValueError(f"unknown ISO 639-1 language code '{value}'")
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


# Discriminator values of a role entry in ``LLMModels``; Pydantic puts them into its locations.
_ROLE_FORM_TAGS: Final = frozenset({"shorthand", "long_form"})


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
        if len(loc) > 3 and loc[:2] == ["llm", "models"] and loc[3] in _ROLE_FORM_TAGS:
            del loc[3]
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


def loads_yaml(text: str, *, source: Path | None = None) -> JobConfig:
    """Parse and validate job YAML text; raises ``JobConfigError(source, [...])`` on any problem.

    Same loader and messages as ``load_yaml``; ``source`` only names the file in the messages.
    """
    text = text.removeprefix("\ufeff")
    where = f"job file {source}" if source is not None else "job file"
    try:
        data: object = yaml.load(text, Loader=JobYamlLoader)
    except (yaml.YAMLError, ValueError) as exc:
        prefix = f"invalid YAML in {where}" if source is not None else "invalid YAML"
        raise JobConfigError(source, [f"{prefix}: {_yaml_problem(exc)}"]) from exc
    if not isinstance(data, dict):
        raise JobConfigError(source, [f"{where} must contain a mapping at the top level"])
    try:
        return validate_job(data)
    except JobConfigError as exc:
        raise JobConfigError(source, exc.errors) from exc


def load_yaml(path: str | os.PathLike[str]) -> JobConfig:
    """Load and validate a job file; raises ``JobConfigError`` on any problem."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as exc:
        raise JobConfigError(file, [f"cannot read job file {file}: {exc}"]) from exc
    return loads_yaml(text, source=file)


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
    """Create an exclusive temp file next to ``target`` with mode ``0o600``.

    Job files hold recipient addresses (and maybe URL credentials), so a new file is private.
    The mode is passed to ``os.open``, so the process-wide umask is never touched.
    """
    while True:
        tmp = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
        try:
            return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), tmp
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
    file keeps its mode; a new file is private (``0o600``).
    """
    target = Path(os.path.realpath(path))
    text = dump_yaml(config)
    fd, tmp = _create_temp(target)
    try:
        with open(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        with contextlib.suppress(FileNotFoundError):  # new file: keep the private mode
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
