"""Interactive preview, edit and refine loop of ``invio job suggest``.

Talks to the operator only through an :class:`~invio.cli.prompts.Prompter` and an injected
``echo``; the model call is the injected ``ask`` callable. It never touches storage, asyncio or
providers.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import typer
from pydantic import ValidationError

from invio.cli.errors import format_llm_error
from invio.cli.prompts import Prompter, Validator
from invio.llm.base import LLMError
from invio.services.suggest import ModelChoice, SearchSuggestion
from invio.textsafe import strip_control

__all__ = [
    "Create",
    "Discard",
    "FlowResult",
    "PrintYaml",
    "Refine",
    "render_preview",
    "run_suggest_flow",
]

Echo = Callable[..., None]
Refine = Callable[[str, SearchSuggestion], SearchSuggestion]
"""``(remark, previous) -> new suggestion``; raises ``LLMError`` (the loop keeps ``previous``)."""

NONE: Final = "(none)"
Q_NEXT: Final = "What next?"
Q_REMARK: Final = "How should it change?"
Q_FIELD: Final = "Which field?"

A_CREATE: Final = "Create job"
A_REFINE: Final = "Refine"
A_EDIT: Final = "Edit a field"
A_YAML: Final = "Print YAML"
A_DISCARD: Final = "Discard"
_ACTIONS: Final = (A_CREATE, A_REFINE, A_EDIT, A_YAML, A_DISCARD)

F_ANY: Final = "Keywords — any of"
F_ALL: Final = "Keywords — all of"
F_EXCLUDE: Final = "Keywords — exclude"
F_DESCRIPTION: Final = "Description"
_FIELDS: Final = (F_ANY, F_ALL, F_EXCLUDE, F_DESCRIPTION)


@dataclass(frozen=True, slots=True)
class Create:
    """Hand the suggestion over to the job wizard."""

    suggestion: SearchSuggestion


@dataclass(frozen=True, slots=True)
class PrintYaml:
    """Print the suggestion as YAML and finish."""

    suggestion: SearchSuggestion


@dataclass(frozen=True, slots=True)
class Discard:
    """Finish without saving anything."""


FlowResult = Create | PrintYaml | Discard


def _joined(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else NONE


def render_preview(s: SearchSuggestion, *, choice: ModelChoice, language: str) -> str:
    """Return the preview text (the full description and hint, every warning as ``! ...``)."""
    hint_lines = s.suggested_sources_hint.splitlines() or [""]
    lines = [
        f"Suggestion ({choice.provider_name} / {choice.model}, language: {language})",
        "",
        f"  {F_ANY}:   {_joined(s.keywords_any)}",
        f"  {F_ALL}:   {_joined(s.keywords_all)}",
        f"  {F_EXCLUDE}:  {_joined(s.keywords_exclude)}",
        f"  {F_DESCRIPTION}:",
        *(f"    {line}" for line in s.semantic_description.splitlines()),
        f"  Suggested sources:   {hint_lines[0]}",
        *(f"    {line}" for line in hint_lines[1:]),
    ]
    if s.warnings:
        lines.append("")
        lines.extend(f"  ! {warning}" for warning in s.warnings)
    return "\n".join(lines)


def _validate_remark(value: str) -> bool | str:
    return True if strip_control(value) else "must not be empty"


def _first_message(exc: ValidationError) -> str:
    message = str(exc.errors(include_url=False)[0]["msg"])
    return message.removeprefix("Value error, ")


def _edited(current: SearchSuggestion, field: str, value: str) -> SearchSuggestion:
    """Return ``current`` with ``field`` replaced by the entered ``value`` (may raise)."""
    if field == F_DESCRIPTION:
        return current.replace(semantic_description=value)
    keywords = (value,)  # ``replace`` splits at commas and drops empty entries
    if field == F_ANY:
        return current.replace(keywords_any=keywords)
    if field == F_ALL:
        return current.replace(keywords_all=keywords)
    return current.replace(keywords_exclude=keywords)


def _current_value(current: SearchSuggestion, field: str) -> str:
    if field == F_DESCRIPTION:
        return current.semantic_description
    values = {
        F_ANY: current.keywords_any,
        F_ALL: current.keywords_all,
        F_EXCLUDE: current.keywords_exclude,
    }[field]
    return ", ".join(values)


def _edit_field(prompter: Prompter, current: SearchSuggestion) -> SearchSuggestion:
    field = prompter.select(Q_FIELD, list(_FIELDS))

    def validate(value: str) -> bool | str:
        try:
            _edited(current, field, value)
        except ValidationError as exc:
            return _first_message(exc)
        return True

    validator: Validator = validate
    value = prompter.text(
        field,
        default=_current_value(current, field),
        validate=validator,
        multiline=field == F_DESCRIPTION,
    )
    return _edited(current, field, value)


def run_suggest_flow(
    prompter: Prompter,
    ask: Refine,
    *,
    first: SearchSuggestion,
    choice: ModelChoice,
    language: str,
    echo: Echo = typer.echo,
) -> FlowResult:
    """Show ``first`` and loop over the actions until the operator creates, prints or discards.

    ``ask`` is only called for a refinement; an ``LLMError`` from it is reported on stderr and
    the previous suggestion stays. ``WizardAborted`` (Ctrl+C / EOF) propagates.
    """
    current = first
    while True:
        echo(render_preview(current, choice=choice, language=language))
        action = prompter.select(Q_NEXT, list(_ACTIONS))
        if action == A_CREATE:
            return Create(current)
        if action == A_YAML:
            return PrintYaml(current)
        if action == A_DISCARD:
            return Discard()
        if action == A_EDIT:
            current = _edit_field(prompter, current)
            continue
        remark = strip_control(prompter.text(Q_REMARK, validate=_validate_remark))
        try:
            current = ask(remark, current)
        except LLMError as exc:
            echo(format_llm_error(exc, choice), err=True)
