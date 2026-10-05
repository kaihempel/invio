"""Interactive job creation wizard.

``run_wizard`` asks the questions of ``specs/005-gh-issue-9`` (FR-004 to FR-016) through a
:class:`~invio.cli.prompts.Prompter`, checks sources through a
:class:`~invio.cli.source_check.SourceChecker` and returns a validated ``JobConfig``. It never
touches storage or the terminal directly (output goes through the injected ``echo``).
"""

import re
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from typing import Annotated, Any, Final, get_args

import typer
from pydantic import BaseModel, StrictInt, TypeAdapter, ValidationError

from invio.cli.prompts import Prompter, Validator
from invio.cli.source_check import SourceChecker
from invio.config.job import (
    TIME_PATTERN,
    Frequency,
    JobConfig,
    JobConfigError,
    LimitsConfig,
    LLMProvider,
    NotificationConfig,
    RssSource,
    ScheduleConfig,
    Weekday,
    dump_yaml,
    known_timezones,
    validate_job,
)
from invio.llm.base import ModelRegistryError
from invio.llm.registry import ModelRegistry
from invio.services.jobs import JobNameError, check_job_name
from invio.sources.urls import redact

__all__ = [
    "OTHER",
    "WARNING_NO_REGISTRY",
    "run_wizard",
    "validate_day_of_month",
    "validate_email",
    "validate_limit",
    "validate_name",
    "validate_nonempty",
    "validate_time",
    "validate_timezone",
    "validate_url",
]

Echo = Callable[..., None]

OTHER: Final = "Other…"
WARNING_NO_REGISTRY: Final = "warning: model registry unavailable ({}); model names are not checked"

Q_NAME: Final = "Job name"
Q_FREQUENCY: Final = "How often should it run?"
Q_WEEKDAY: Final = "On which weekday?"
Q_DAY: Final = "On which day of the month (1-31)?"
Q_TIME: Final = "At what time (HH:MM)?"
Q_TIMEZONE: Final = "Time zone"
Q_EMAIL: Final = "Recipient e-mail"
Q_MORE_EMAIL: Final = "Add another recipient?"
Q_SUBJECT: Final = "E-mail subject"
Q_SOURCE_TYPE: Final = "Source type"
Q_FEED_URL: Final = "Feed URL"
Q_PAGE_URL: Final = "Page URL"
Q_SITEMAP_URL: Final = "Sitemap URL"
Q_CHANNEL: Final = "YouTube channel id"
Q_PLAYLIST: Final = "YouTube playlist id"
Q_KEEP_SOURCE: Final = "Keep this source anyway?"
Q_MORE_SOURCE: Final = "Add another source?"
Q_KW_ANY: Final = "Keywords — any of (comma-separated)"
Q_KW_ALL: Final = "Keywords — all of"
Q_KW_EXCLUDE: Final = "Keywords — exclude"
Q_DESC: Final = "Describe what you are looking for (finish with Esc+Enter)"
Q_PROVIDER: Final = "LLM provider"
Q_FAST: Final = "Fast model"
Q_SMART: Final = "Smart model"
Q_FAST_ID: Final = "Fast model id"
Q_SMART_ID: Final = "Smart model id"
Q_USE_ANYWAY: Final = "Use it anyway?"
Q_KEEP_LIMITS: Final = "Keep the default limits?"
Q_SAVE: Final = "Save this job?"

_URL_QUESTIONS: Final = {"rss": Q_FEED_URL, "web": Q_PAGE_URL, "sitemap": Q_SITEMAP_URL}
_ID_QUESTIONS: Final = {
    "youtube_channel": ("channel_id", Q_CHANNEL),
    "youtube_playlist": ("playlist_id", Q_PLAYLIST),
}
_LIMIT_QUESTIONS: Final = (
    ("max_items_per_source", "Max items per source"),
    ("max_items_per_run", "Max items per run"),
    ("max_items_in_notification", "Max items in notification"),
    ("max_llm_tokens_per_run", "Max LLM tokens per run"),
)


def _int_field_adapter(model: type[BaseModel], name: str) -> TypeAdapter[Any]:
    """A ``TypeAdapter`` with the constraints of an (optional) integer model field.

    Built from the model so the wizard's bounds cannot drift from ``JobConfig``.
    """
    return TypeAdapter(Annotated[StrictInt, *model.model_fields[name].metadata])


# Element and field types taken from the models (``to`` is ``list[EmailStr]``).
_EMAIL: Final = TypeAdapter(get_args(NotificationConfig.model_fields["to"].annotation)[0])
_HTTP_URL: Final[TypeAdapter[Any]] = TypeAdapter(RssSource.model_fields["url"].annotation)
_DAY_OF_MONTH: Final = _int_field_adapter(ScheduleConfig, "day_of_month")
_LIMITS: Final = {key: _int_field_adapter(LimitsConfig, key) for key in LimitsConfig.model_fields}


# --- validators (True = ok, str = inline error) ----------------------------------------------


def validate_name(existing: Collection[str]) -> Validator:
    """Return a validator for a new job name that must not be in ``existing``."""

    def validator(value: str) -> bool | str:
        try:
            check_job_name(value)
        except JobNameError as exc:
            return exc.rule
        if value in existing:
            return f"job '{value}' already exists"
        return True

    return validator


def validate_time(value: str) -> bool | str:
    if re.fullmatch(TIME_PATTERN, value.strip()):
        return True
    return "time must be HH:MM (00:00–23:59)"  # noqa: RUF001 (en dash as in the contract)


def validate_timezone(value: str) -> bool | str:
    if value.strip() in known_timezones():
        return True
    return f"unknown timezone '{value.strip()}'"


def validate_email(value: str) -> bool | str:
    try:
        _EMAIL.validate_python(value.strip())
    except ValidationError:
        return "not a valid e-mail address"
    return True


def validate_url(value: str) -> bool | str:
    try:
        _HTTP_URL.validate_python(value.strip())
    except ValidationError:
        return "not a valid http(s) URL"
    return True


def validate_nonempty(value: str) -> bool | str:
    return True if value.strip() else "must not be empty"


def _valid_int(adapter: TypeAdapter[Any], value: str) -> bool:
    text = value.strip()
    if not (text.isascii() and text.isdigit()):
        return False
    try:
        adapter.validate_python(int(text))
    except ValidationError:
        return False
    return True


def validate_limit(key: str) -> Validator:
    """Return a validator for the ``limits.<key>`` field (constraints from ``LimitsConfig``)."""
    adapter = _LIMITS[key]

    def validator(value: str) -> bool | str:
        return True if _valid_int(adapter, value) else "must be a whole number ≥ 1"

    return validator


def validate_day_of_month(value: str) -> bool | str:
    if _valid_int(_DAY_OF_MONTH, value):
        return True
    return "day of month must be a whole number from 1 to 31"


# --- session ---------------------------------------------------------------------------------


@dataclass
class _Session:
    """Answers so far plus the collaborators; earlier answers are the defaults on a re-ask."""

    prompter: Prompter
    checker: SourceChecker
    registry: ModelRegistry | None
    echo: Echo
    default_timezone: str
    name: str = ""
    frequency: str = ""
    weekday: str | None = None
    day_of_month: int | None = None
    time: str = ""
    timezone: str = ""
    to: list[str] = field(default_factory=list)
    subject: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    keywords: dict[str, list[str]] = field(default_factory=dict)
    description: str = ""
    provider: str = ""
    fast: str = ""
    smart: str = ""
    limits: dict[str, int] | None = None
    registry_broken: bool = False

    def warn(self, message: str) -> None:
        self.echo(message, err=True)

    def mapping(self) -> dict[str, Any]:
        schedule: dict[str, Any] = {
            "frequency": self.frequency,
            "time": self.time,
            "timezone": self.timezone,
        }
        if self.weekday is not None:
            schedule["weekday"] = self.weekday
        if self.day_of_month is not None:
            schedule["day_of_month"] = self.day_of_month
        data: dict[str, Any] = {
            "schedule": schedule,
            "notification": {"to": list(self.to), "subject": self.subject},
            "sources": [dict(source) for source in self.sources],
            "search": {
                "keywords": {key: list(values) for key, values in self.keywords.items()},
                "semantic_description": self.description,
            },
            "llm": {
                "provider": self.provider,
                "models": {"fast": self.fast, "smart": self.smart},
            },
        }
        if self.limits is not None:
            data["limits"] = dict(self.limits)
        return data


# --- steps -----------------------------------------------------------------------------------


def _step_schedule(s: _Session) -> None:
    p = s.prompter
    s.frequency = p.select(
        Q_FREQUENCY, [f.value for f in Frequency], default=s.frequency or Frequency.DAILY.value
    )
    # Earlier answers are the defaults on a re-ask (FR-005).
    weekday, day_of_month = s.weekday, s.day_of_month
    s.weekday, s.day_of_month = None, None
    if s.frequency == Frequency.WEEKLY:
        s.weekday = p.select(Q_WEEKDAY, [d.value for d in Weekday], default=weekday)
    elif s.frequency == Frequency.MONTHLY:
        default_day = "" if day_of_month is None else str(day_of_month)
        day = int(p.text(Q_DAY, default=default_day, validate=validate_day_of_month).strip())
        s.day_of_month = day
        if day >= 29:
            s.echo(f"note: months shorter than {day} days run on their last day")
    s.time = p.text(Q_TIME, default=s.time or "07:00", validate=validate_time).strip()
    s.timezone = p.autocomplete(
        Q_TIMEZONE,
        sorted(known_timezones()),
        default=s.timezone or s.default_timezone,
        validate=validate_timezone,
    ).strip()


def _step_notification(s: _Session) -> None:
    p = s.prompter
    s.to = []
    while True:
        address = str(_EMAIL.validate_python(p.text(Q_EMAIL, validate=validate_email).strip()))
        if address in s.to:
            s.warn(f"! recipient '{address}' already added; skipped")
        else:
            s.to.append(address)
        if not p.confirm(Q_MORE_EMAIL, default=False):
            break
    s.subject = p.text(
        Q_SUBJECT, default=s.subject or f"invio: {s.name}", validate=validate_nonempty
    ).strip()


def _check_source(s: _Session, source_type: str, url: str) -> bool:
    """Check a URL source; return False if the operator wants to enter another URL."""
    shown = redact(url)  # never echo a password embedded in the URL
    s.echo(f"checking {shown} …")
    result = s.checker.check(url, expect_feed=source_type == "rss")
    if not result.reachable:
        s.warn(f"! could not reach {shown}: {result.reason or 'unknown problem'}")
    elif source_type == "rss" and result.is_feed is False:
        s.warn("! reachable, but no RSS/Atom feed detected")
    else:
        s.echo("✓ feed detected" if result.is_feed else "✓ reachable")
        return True
    return s.prompter.confirm(Q_KEEP_SOURCE, default=False)


def _ask_source(s: _Session) -> tuple[tuple[str, str], dict[str, Any]]:
    """Ask for one source; return its duplicate key and its mapping."""
    source_type = s.prompter.select(Q_SOURCE_TYPE, [*_URL_QUESTIONS, *_ID_QUESTIONS])
    if source_type in _ID_QUESTIONS:
        field_name, question = _ID_QUESTIONS[source_type]
        ident = s.prompter.text(question, validate=validate_nonempty).strip()
        return (source_type, ident), {"type": source_type, field_name: ident}
    while True:
        url = s.prompter.text(_URL_QUESTIONS[source_type], validate=validate_url).strip()
        if _check_source(s, source_type, url):
            return (source_type, str(_HTTP_URL.validate_python(url))), {
                "type": source_type,
                "url": url,
            }


def _step_sources(s: _Session) -> None:
    s.sources = []
    seen: set[tuple[str, str]] = set()
    while True:
        key, source = _ask_source(s)
        if key in seen:
            s.warn(f"! source {key[0]} '{key[1]}' already added; skipped")
        else:
            seen.add(key)
            s.sources.append(source)
        if not s.prompter.confirm(Q_MORE_SOURCE, default=False):
            break


def _split_keywords(text: str) -> list[str]:
    return [item for item in (part.strip() for part in text.split(",")) if item]


def _step_keywords(s: _Session) -> None:
    p = s.prompter
    s.keywords = {
        "any": _split_keywords(p.text(Q_KW_ANY, default=", ".join(s.keywords.get("any", [])))),
        "all": _split_keywords(p.text(Q_KW_ALL, default=", ".join(s.keywords.get("all", [])))),
        "exclude": _split_keywords(
            p.text(Q_KW_EXCLUDE, default=", ".join(s.keywords.get("exclude", [])))
        ),
    }


def _step_description(s: _Session) -> None:
    s.description = s.prompter.text(
        Q_DESC, default=s.description, validate=validate_nonempty, multiline=True
    ).strip()


def _known_models(s: _Session) -> list[str] | None:
    """Registered models of the chosen provider; ``None`` if the registry is unusable."""
    if s.registry is None or s.registry_broken:
        return None
    try:
        return [info.model_id for info in s.registry.models_for(s.provider)]
    except ModelRegistryError as exc:
        s.registry_broken = True
        s.warn(WARNING_NO_REGISTRY.format(exc))
        return None


def _ask_model(s: _Session, select_question: str, id_question: str) -> str:
    known = _known_models(s)
    while True:
        question = id_question
        if known:
            choice = s.prompter.select(select_question, [*known, OTHER])
            if choice != OTHER:
                return choice
        model = s.prompter.text(question, validate=validate_nonempty).strip()
        if known is None or model in known:
            return model
        s.warn(
            f"! model '{model}' is not registered for provider '{s.provider}'; runs will fail "
            f"until it is added to models.d/{s.provider}.yaml"
        )
        if s.prompter.confirm(Q_USE_ANYWAY, default=False):
            return model


def _step_llm(s: _Session) -> None:
    s.provider = s.prompter.select(
        Q_PROVIDER, [p.value for p in LLMProvider], default=s.provider or None
    )
    s.fast = _ask_model(s, Q_FAST, Q_FAST_ID)
    s.smart = _ask_model(s, Q_SMART, Q_SMART_ID)


def _step_limits(s: _Session) -> None:
    defaults = LimitsConfig().model_dump()
    if s.prompter.confirm(Q_KEEP_LIMITS, default=True):
        s.limits = None
        return
    previous = s.limits or {}
    s.limits = {
        key: int(
            s.prompter.text(
                question,
                default=str(previous.get(key, defaults[key])),
                validate=validate_limit(key),
            ).strip()
        )
        for key, question in _LIMIT_QUESTIONS
    }


_Step = Callable[[_Session], None]
_STEPS: Final[tuple[_Step, ...]] = (
    _step_schedule,
    _step_notification,
    _step_sources,
    _step_keywords,
    _step_description,
    _step_llm,
    _step_limits,
)
# Longest prefix first: ``search.keywords`` must win over ``search``.
_STEP_FOR_PREFIX: Final[tuple[tuple[str, _Step], ...]] = (
    ("schedule", _step_schedule),
    ("notification", _step_notification),
    ("sources", _step_sources),
    ("search.keywords", _step_keywords),
    ("search", _step_description),
    ("llm", _step_llm),
    ("limits", _step_limits),
)


def _owning_step(errors: list[str]) -> _Step | None:
    """Return the step owning the first error's field path, if any."""
    if not errors:
        return None
    path = errors[0].partition(":")[0]
    for prefix, step in _STEP_FOR_PREFIX:
        if path == prefix or path.startswith((f"{prefix}.", f"{prefix}[")):
            return step
    return None


def run_wizard(
    prompter: Prompter,
    checker: SourceChecker,
    *,
    registry: ModelRegistry | None,
    existing_names: Collection[str],
    name: str | None = None,
    default_timezone: str = "UTC",
    echo: Echo = typer.echo,
) -> tuple[str, JobConfig] | None:
    """Ask all steps and show the preview; ``None`` if the operator declined saving.

    Raises ``WizardAborted`` on Ctrl+C / EOF. Warnings go to ``echo(..., err=True)``.
    """
    s = _Session(
        prompter=prompter,
        checker=checker,
        registry=registry,
        echo=echo,
        default_timezone=default_timezone,
    )
    s.name = (
        name if name is not None else prompter.text(Q_NAME, validate=validate_name(existing_names))
    )
    for step in _STEPS:
        step(s)
    while True:
        try:
            config = validate_job(s.mapping())
        except JobConfigError as exc:
            owner = _owning_step(exc.errors)
            if owner is None:
                raise
            for line in exc.errors:
                echo(f"✗ {line}", err=True)
            owner(s)
            continue
        break
    echo("")
    echo(dump_yaml(config).rstrip("\n"))
    echo("")
    if not prompter.confirm(Q_SAVE, default=True):
        return None
    return s.name, config
