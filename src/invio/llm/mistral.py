"""Mistral LLM provider built on the official ``mistralai`` SDK (3.x).

The SDK talks HTTP through ``httpx2``, so this module imports its exception types. All
retrying is done here (the SDK's own retry configuration is never set): rate limits (429),
server errors (5xx) and connection failures are retried with exponential backoff and jitter
(:class:`RetryPolicy`, re-exported from :mod:`invio.llm.http_retry`, which also owns the retry
loop shared with the other HTTP providers), a ``Retry-After`` header is honoured up to
``max_retry_after`` seconds, and timeouts, authentication failures, rejected requests and
requests that cannot be sent are never retried. Every ``httpx2`` error is mapped to a typed
:class:`~invio.llm.base.LLMError` by :func:`_classify`, the Mistral-specific part.

Errors raised here carry ``provider="mistral"`` and the model. Their messages are built from
the status and a sanitized provider message only; the API key, the prompt, the answer and the
raw response body never appear in them, and the SDK exception is neither their ``__cause__``
nor their ``__context__``.

There is one SDK client per running event loop (:class:`~invio.llm.loop_clients.LoopClients`),
created lazily from ``client_factory``.
"""

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Self

import httpx2
from mistralai.client import Mistral, errors, models
from mistralai.client.types import UNSET
from pydantic import BaseModel

from invio.config.settings import Settings
from invio.llm.base import (
    LLMProvider,
    LLMUnavailableError,
    Usage,
    require_api_key,
    structured_with_repair,
    with_timeout,
)
from invio.llm.factory import register_provider
from invio.llm.http_retry import (
    Failure,
    bad_response_failure,
    classify_status,
    describe,
    retry_after,
    run_with_retries,
    sanitize_detail,
    strict_schema,
    timeout_failure,
    utc_now,
)
from invio.llm.http_retry import RetryPolicy as RetryPolicy
from invio.llm.loop_clients import LoopClients
from invio.llm.registry import ModelRegistry

# Names the Mistral tests (and older callers) import from here.
_retry_after = retry_after
_strict_schema = strict_schema

PROVIDER = "mistral"
_LABEL = "Mistral"
_ENV_VAR = "INVIO_MISTRAL_API_KEY"
_SDK_TIMEOUT_MARGIN_S = 5
_SCHEMA_NAME_LIMIT = 64
# Errors raised before anything was sent: retrying cannot help.
_UNSENDABLE = (httpx2.InvalidURL, httpx2.UnsupportedProtocol, httpx2.LocalProtocolError)


def _safe_detail(exc: errors.MistralError) -> str:
    """Return the provider's own error message, never echoing request input.

    Uses the JSON body's top-level ``message`` or ``detail`` (a string, or a list of
    ``{loc, msg}`` items whose ``input``/``ctx`` are ignored). Whitespace is collapsed and the
    result truncated; anything unparseable gives ``""``.
    """
    try:
        document = json.loads(exc.body)
    except ValueError:
        return ""
    text = ""
    if isinstance(document, dict):
        message = document.get("message")
        detail = document.get("detail")
        if isinstance(message, str):
            text = message
        elif isinstance(detail, str):
            text = detail
        elif isinstance(detail, list):
            text = "; ".join(item for item in map(_detail_item, detail) if item)
    return sanitize_detail(text)


def _detail_item(item: object) -> str:
    if not isinstance(item, dict) or not isinstance(item.get("msg"), str):
        return ""
    loc = item.get("loc")
    location = ".".join(str(part) for part in loc) if isinstance(loc, list) else ""
    return f"{location}: {item['msg']}" if location else str(item["msg"])


def _unavailable(message: str, model: str) -> LLMUnavailableError:
    return LLMUnavailableError(message, provider=PROVIDER, model=model)


def _classify(exc: Exception, model: str, now: Callable[[], datetime]) -> Failure | None:
    """Map an SDK or transport exception to a ``Failure``; ``None`` if not recognized."""
    name = type(exc).__name__
    if isinstance(exc, httpx2.TimeoutException):
        return timeout_failure(_LABEL, provider=PROVIDER, model=model)
    if isinstance(exc, _UNSENDABLE):
        message = describe(f"{_LABEL} request could not be sent ({name})", model, None)
        return Failure("unsendable", False, _unavailable(message, model))
    if isinstance(exc, httpx2.TransportError | errors.NoResponseError):
        message = describe(f"{_LABEL} connection failed ({name})", model, None)
        return Failure("connection", True, _unavailable(message, model))
    if isinstance(exc, httpx2.HTTPError | httpx2.StreamError | errors.ResponseValidationError):
        # Undecodable body, redirect loop, stream misuse or a body the SDK cannot parse.
        return bad_response_failure(_LABEL, provider=PROVIDER, model=model, cause=f" ({name})")
    if not isinstance(exc, errors.MistralError):
        return None
    return classify_status(
        exc.status_code,
        label=_LABEL,
        env_var=_ENV_VAR,
        provider=PROVIDER,
        model=model,
        detail=_safe_detail(exc),
        headers=exc.headers,
        now=now,
    )


def _answer_text(response: models.ChatCompletionResponse) -> str:
    """Return the first choice's text (string content or concatenated text chunks)."""
    text = ""
    message = response.choices[0].message if response.choices else None
    if message is not None:
        content = message.content
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            text = "".join(c.text for c in content if isinstance(c, models.TextChunk))
    return text


@register_provider(PROVIDER)
class MistralProvider:
    """An :class:`~invio.llm.base.LLMProvider` backed by the Mistral chat completions API."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float,
        retry: RetryPolicy | None = None,
        client_factory: Callable[[], httpx2.AsyncClient] | None = None,
        server_url: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.retry = retry if retry is not None else RetryPolicy()
        self._client_factory = client_factory or (lambda: httpx2.AsyncClient(follow_redirects=True))
        self._server_url = server_url
        self._sleep = sleep
        self._uniform = uniform
        self._now = now
        self._clients = LoopClients(self._build_client)

    @classmethod
    def from_settings(cls, settings: Settings, *, registry: ModelRegistry | None = None) -> Self:
        """Build the provider; raises ``LLMAuthError`` naming the env var if the key is missing.

        The model ``registry`` is not needed by this provider.
        """
        return cls(
            require_api_key(settings, PROVIDER), timeout_seconds=settings.llm_timeout_seconds
        )

    def __repr__(self) -> str:
        return f"MistralProvider(timeout_seconds={self.timeout_seconds!r}, retry={self.retry!r})"

    def _build_client(self) -> tuple[Mistral, httpx2.AsyncClient]:
        http_client = self._client_factory()
        sdk_client = Mistral(
            api_key=self._api_key,
            async_client=http_client,
            server_url=self._server_url,
            timeout_ms=int((self.timeout_seconds + _SDK_TIMEOUT_MARGIN_S) * 1000),
        )
        return sdk_client, http_client

    async def aclose(self) -> None:
        """Close the HTTP client of the running loop; the next call builds a new one."""
        await self._clients.aclose()

    async def _attempt(
        self,
        model: str,
        messages: list[models.SystemMessage | models.UserMessage],
        *,
        temperature: float,
        max_tokens: int | None,
        response_format: models.ResponseFormat | None,
    ) -> tuple[str, Usage]:
        """Send one HTTP request and return the answer text and usage."""
        response = await with_timeout(
            self._clients.get().chat.complete_async(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens if max_tokens is not None else UNSET,
                response_format=response_format,
            ),
            seconds=self.timeout_seconds,
            provider=PROVIDER,
            model=model,
        )
        text = _answer_text(response)
        if not text:
            raise _unavailable(f"Mistral returned no answer text (model {model})", model)
        usage = response.usage
        return text, Usage(usage.prompt_tokens or 0, usage.completion_tokens or 0)

    async def _request(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        response_format: models.ResponseFormat | None,
    ) -> tuple[str, Usage]:
        """Run :meth:`_attempt` with classification and the retry loop."""
        messages: list[models.SystemMessage | models.UserMessage] = [
            models.SystemMessage(content=system),
            models.UserMessage(content=user),
        ]

        async def attempt() -> tuple[str, Usage]:
            return await self._attempt(
                model,
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
            )

        return await run_with_retries(
            attempt,
            classify=_classify,
            policy=self.retry,
            provider=PROVIDER,
            model=model,
            sleep=self._sleep,
            uniform=self._uniform,
            now=self._now,
        )

    async def complete(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int
    ) -> tuple[str, Usage]:
        return await self._request(
            system,
            user,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=None,
        )

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        response_format = models.ResponseFormat(
            type="json_schema",
            json_schema=models.JSONSchema(
                name=re.sub(r"[^a-zA-Z0-9_-]", "_", schema.__name__)[:_SCHEMA_NAME_LIMIT],
                schema_definition=strict_schema(schema.model_json_schema()),
                strict=True,
            ),
        )

        async def request(system_text: str, user_text: str) -> tuple[str, Usage]:
            return await self._request(
                system_text,
                user_text,
                model=model,
                temperature=temperature,
                max_tokens=None,
                response_format=response_format,
            )

        return await structured_with_repair(
            request, system, user, schema, provider=PROVIDER, model=model
        )


if TYPE_CHECKING:
    _check: type[LLMProvider] = MistralProvider
