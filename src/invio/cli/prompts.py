"""Interactive prompting seam for the job wizard.

The wizard talks to a :class:`Prompter`, never to a terminal library. Production code uses
:class:`QuestionaryPrompter` (questionary on prompt_toolkit, research R1/R2); tests inject a
scripted fake, so no test needs a TTY. A validator returns ``True`` or an inline error message.
"""

from collections.abc import Callable, Sequence
from typing import Any, Protocol

import questionary

__all__ = ["Prompter", "QuestionaryPrompter", "Validator", "WizardAborted", "adapt_validator"]

Validator = Callable[[str], bool | str]
"""``True`` accepts the answer; a ``str`` rejects it and is shown as the error."""


class WizardAborted(Exception):
    """The operator aborted the wizard (Ctrl+C / EOF)."""


class Prompter(Protocol):
    """Everything the wizard asks of a terminal."""

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Validator | None = None,
        multiline: bool = False,
    ) -> str: ...

    def select(
        self, message: str, choices: Sequence[str], *, default: str | None = None
    ) -> str: ...

    def confirm(self, message: str, *, default: bool = True) -> bool: ...

    def autocomplete(
        self,
        message: str,
        choices: Sequence[str],
        *,
        default: str = "",
        validate: Validator | None = None,
    ) -> str: ...


def adapt_validator(validate: Validator | None) -> Validator | None:
    """Map a validator that returns ``False`` to a generic message (questionary needs text)."""
    if validate is None:
        return None

    def adapted(value: str) -> bool | str:
        result = validate(value)
        return "invalid value" if result is False else result

    return adapted


def _answer[T](value: T | None) -> T:
    """Unwrap a questionary answer; ``None`` means Ctrl+C / EOF."""
    if value is None:
        raise WizardAborted
    return value


# A thin wrapper over a real terminal: exercised manually (tasks T040), not in unit tests.
class QuestionaryPrompter:
    """Production :class:`Prompter` backed by questionary."""

    def text(  # pragma: no cover
        self,
        message: str,
        *,
        default: str = "",
        validate: Validator | None = None,
        multiline: bool = False,
    ) -> str:
        kwargs: dict[str, Any] = {"default": default, "multiline": multiline}
        adapted = adapt_validator(validate)
        if adapted is not None:
            kwargs["validate"] = adapted
        return str(_answer(questionary.text(message, **kwargs).ask()))

    def select(  # pragma: no cover
        self, message: str, choices: Sequence[str], *, default: str | None = None
    ) -> str:
        question = questionary.select(message, choices=list(choices), default=default)
        return str(_answer(question.ask()))

    def confirm(self, message: str, *, default: bool = True) -> bool:  # pragma: no cover
        return bool(_answer(questionary.confirm(message, default=default).ask()))

    def autocomplete(  # pragma: no cover
        self,
        message: str,
        choices: Sequence[str],
        *,
        default: str = "",
        validate: Validator | None = None,
    ) -> str:
        kwargs: dict[str, Any] = {"default": default, "match_middle": True}
        adapted = adapt_validator(validate)
        if adapted is not None:
            kwargs["validate"] = adapted
        return str(_answer(questionary.autocomplete(message, list(choices), **kwargs).ask()))
