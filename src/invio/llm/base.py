"""Provider-neutral LLM contract: usage, protocol, typed errors and shared helpers.

Every provider module implements :class:`LLMProvider`. The helpers here keep behaviour uniform
across providers: :func:`structured_with_repair` (one repair request for malformed structured
answers), :func:`with_timeout` (per-request bound) and :func:`require_api_key` (clear message
for a missing credential).

Typed errors: :class:`LLMRateLimitError` (with the provider's ``retry_after``),
:class:`LLMAuthError`, :class:`LLMUnavailableError`, :class:`LLMInvalidRequestError` (the
provider rejected the request itself), :class:`LLMInvalidOutputError`.

No exception message, attribute or log line produced here contains a credential or a prompt,
and the answer text is never included verbatim. Caveat: validation problems are reported as
``<loc>: <message>``, and ``loc`` can contain keys taken from the answer (for example an
unexpected extra field name); no redaction is applied.
"""

import asyncio
import json
import logging
import math
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol, cast, get_args

from pydantic import BaseModel, ValidationError

from invio.config.settings import ENV_PREFIX, MissingSettingError, SecretName, Settings

logger = logging.getLogger("invio.llm")

_FENCE_START = re.compile(r"\A```(?:json)?[ \t]*\r?\n")
_FENCE_END = re.compile(r"\r?\n```[ \t]*\Z")


@dataclass(frozen=True, slots=True)
class Usage:
    """Token usage behind one result; ``requests`` counts the provider requests summed in."""

    input_tokens: int
    output_tokens: int
    requests: int = 1

    def __post_init__(self) -> None:
        if self.input_tokens < 0:
            raise ValueError(f"input_tokens must be >= 0, got {self.input_tokens}")
        if self.output_tokens < 0:
            raise ValueError(f"output_tokens must be >= 0, got {self.output_tokens}")
        if self.requests < 1:
            raise ValueError(f"requests must be >= 1, got {self.requests}")

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.requests + other.requests,
        )


class LLMProvider(Protocol):
    """What the pipeline needs from an LLM provider."""

    async def complete(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int
    ) -> tuple[str, Usage]:
        """Return the model's text answer and the token usage."""
        ...

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        """Return an answer validated against ``schema`` and the (summed) token usage."""
        ...

    async def aclose(self) -> None:
        """Release the connections opened from the running event loop (safe to call twice)."""
        ...


class LLMError(Exception):
    """Base class of LLM call failures."""

    def __init__(
        self, message: str, *, provider: str | None = None, model: str | None = None
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.model = model


class LLMRateLimitError(LLMError):
    """The provider reported rate limiting; ``retry_after`` is the wait it asked for, if any."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        retry_after: float | None = None,
    ) -> None:
        if retry_after is not None and not (math.isfinite(retry_after) and retry_after >= 0):
            raise ValueError(f"retry_after must be finite and >= 0, got {retry_after}")
        super().__init__(message, provider=provider, model=model)
        self.retry_after = retry_after


class LLMQuotaError(LLMRateLimitError):
    """The account's quota or billing is exhausted; waiting does not help, so it is not retried."""


class LLMAuthError(LLMError):
    """The API key is missing or was rejected by the provider."""


class LLMUnavailableError(LLMError):
    """The provider is unreachable, failed with a server error or did not answer in time."""


class LLMInvalidRequestError(LLMError):
    """The provider rejected the request itself (malformed input, unknown model, bad schema)."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: int | None = None,
    ) -> None:
        super().__init__(message, provider=provider, model=model)
        self.status = status


class LLMInvalidOutputError(LLMError):
    """A structured answer was still invalid after one repair request."""

    def __init__(
        self,
        message: str,
        *,
        errors: str,
        usage: Usage,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        super().__init__(message, provider=provider, model=model)
        self.errors = errors
        self.usage = usage


class LLMConfigError(ValueError):
    """Unknown provider or role, model missing from the registry, duplicate registration."""


class ModelRegistryError(LLMConfigError):
    """A model registry file is invalid or conflicts with another one."""


RawRequest = Callable[[str, str], Awaitable[tuple[str, Usage]]]
"""Sends one ``(system, user)`` pair to a model and returns ``(text, usage)``."""


async def with_timeout[R](
    awaitable: Awaitable[R], *, seconds: float, provider: str, model: str
) -> R:
    """Await ``awaitable`` for at most ``seconds``; a timeout raises ``LLMUnavailableError``."""
    try:
        async with asyncio.timeout(seconds) as scope:
            return await awaitable
    except TimeoutError:
        if not scope.expired():
            raise  # a TimeoutError raised by the awaitable itself, not our deadline
        raise LLMUnavailableError(
            f"LLM provider '{provider}' did not answer within {seconds:g} s (model {model})",
            provider=provider,
            model=model,
        ) from None


def api_key_env_var(provider: str) -> str:
    """Return the environment variable of ``provider``'s API key (``INVIO_<PROVIDER>_API_KEY``)."""
    return f"{ENV_PREFIX}{provider.upper()}_API_KEY"


def require_api_key(settings: Settings, provider: str) -> str:
    """Return the API key of ``provider`` or raise :class:`LLMAuthError` naming the env var."""
    name = f"{provider}_api_key"
    if name not in get_args(SecretName):
        raise LLMConfigError(f"LLM provider '{provider}' has no API key setting")
    env_var = api_key_env_var(provider)
    try:
        key = settings.require_secret(cast(SecretName, name))
    except MissingSettingError:
        key = ""
    if not key.strip():
        raise LLMAuthError(
            f"LLM provider '{provider}' needs an API key: set {env_var}", provider=provider
        )
    return key


def _format_problems(exc: ValidationError) -> str:
    """One ``"<loc>: <msg>"`` line per problem; never includes the offending input."""
    lines = []
    for problem in exc.errors(include_input=False, include_url=False):
        location = ".".join(str(part) for part in problem["loc"]) or "(root)"
        lines.append(f"{location}: {problem['msg']}")
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    """Remove one surrounding Markdown code fence, if present."""
    stripped = text.strip()
    opening = _FENCE_START.match(stripped)
    closing = _FENCE_END.search(stripped)
    if opening and closing and closing.start() >= opening.end():
        return stripped[opening.end() : closing.start()]
    return text


def _validate[T: BaseModel](schema: type[T], text: str) -> T | str:
    """Return the validated value or the problem summary."""
    try:
        return schema.model_validate_json(_strip_code_fence(text))
    except ValidationError as exc:
        return _format_problems(exc)


async def structured_with_repair[T: BaseModel](
    request: RawRequest,
    system: str,
    user: str,
    schema: type[T],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[T, Usage]:
    """Run ``request`` and validate the answer against ``schema``; repair at most once.

    A second invalid answer raises :class:`LLMInvalidOutputError` carrying the final problems,
    the usage of both requests, ``provider`` and ``model``. Errors raised by ``request``
    propagate unchanged.
    """
    instruction = (
        f"{system}\n\nAnswer with a single JSON document that matches this JSON Schema and "
        f"nothing else:\n{json.dumps(schema.model_json_schema())}"
    )
    text, usage = await request(instruction, user)
    result = _validate(schema, text)
    if not isinstance(result, str):
        return result, usage

    logger.warning("llm.repair", extra={"schema": schema.__name__, "errors": result})
    repair_user = (
        f"{user}\n\nYour previous answer:\n{text}\n\n"
        f"It is invalid:\n{result}\n\nReturn only the corrected JSON."
    )
    text, repair_usage = await request(instruction, repair_user)
    usage += repair_usage
    result = _validate(schema, text)
    if not isinstance(result, str):
        return result, usage
    where = f" (model {model})" if model is not None else ""
    raise LLMInvalidOutputError(
        f"structured output for {schema.__name__} is invalid after one repair attempt{where}: "
        f"{result}",
        errors=result,
        usage=usage,
        provider=provider,
        model=model,
    )
