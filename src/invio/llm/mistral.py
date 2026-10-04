"""Mistral LLM provider built on the official ``mistralai`` SDK (3.x).

The SDK talks HTTP through ``httpx2``, so this module imports its exception types. All
retrying is done here (the SDK's own retry configuration is never set): rate limits (429),
server errors (5xx) and connection failures are retried with exponential backoff and jitter
(:class:`RetryPolicy`), a ``Retry-After`` header is honoured up to ``max_retry_after`` seconds,
and timeouts, authentication failures and rejected requests are never retried.

Errors raised here carry ``provider="mistral"`` and the model. Their messages are built from
the status and a sanitized provider message only; the API key, the prompt, the answer and the
raw response body never appear in them, and the SDK exception is not chained.

The SDK client (and so its HTTP connection pool) is bound to the event loop that first used it,
so it is created lazily and replaced (the old one dropped) when another loop uses the provider.
"""

import asyncio
import json
import logging
import math
import random
import re
import weakref
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Self

import httpx2
from mistralai.client import Mistral, errors, models
from mistralai.client.types import UNSET
from pydantic import BaseModel

from invio.config.settings import Settings
from invio.llm.base import (
    LLMAuthError,
    LLMError,
    LLMInvalidOutputError,
    LLMInvalidRequestError,
    LLMProvider,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
    require_api_key,
    structured_with_repair,
    with_timeout,
)
from invio.llm.factory import register_provider

logger = logging.getLogger("invio.llm")

PROVIDER = "mistral"
_ENV_VAR = "INVIO_MISTRAL_API_KEY"
_MAX_DETAIL_CHARS = 300
_SDK_TIMEOUT_MARGIN_S = 5
_AUTH_STATUSES = frozenset({401, 403})
_RATE_LIMIT_STATUS = 429
_SERVER_ERROR_STATUS = 500
_CLIENT_ERROR_MIN = 400
_SCHEMA_NAME_LIMIT = 64


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry behaviour of :class:`MistralProvider`.

    ``max_retries`` >= 0 (attempts = ``max_retries + 1``); ``base_delay`` > 0 seconds (the wait
    before retry *n* is ``base_delay * 2**(n-1)`` scaled by the jitter); ``0 <= jitter < 1``;
    ``max_retry_after`` > 0 seconds (a larger ``Retry-After`` fails immediately).
    """

    max_retries: int = 3
    base_delay: float = 1.0
    jitter: float = 0.25
    max_retry_after: float = 60.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        if not self.base_delay > 0:
            raise ValueError(f"base_delay must be > 0, got {self.base_delay}")
        if not 0 <= self.jitter < 1:
            raise ValueError(f"jitter must be >= 0 and < 1, got {self.jitter}")
        if not self.max_retry_after > 0:
            raise ValueError(f"max_retry_after must be > 0, got {self.max_retry_after}")


@dataclass(frozen=True, slots=True)
class _Failure:
    """One classified failed attempt."""

    kind: str
    retryable: bool
    error: LLMError
    status: int | None = None
    retry_after: float | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _retry_after(headers: Mapping[str, str], now: Callable[[], datetime]) -> float | None:
    """Return the seconds requested by a ``Retry-After`` header, or ``None``.

    Accepts non-negative integer or decimal seconds or an HTTP date (relative to ``now()``,
    floored at 0). Anything else is ignored.
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        return seconds if math.isfinite(seconds) and seconds >= 0 else None
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now()).total_seconds())


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
            text = "; ".join(_detail_item(item) for item in detail if _detail_item(item))
    printable = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(printable.split())[:_MAX_DETAIL_CHARS]


def _detail_item(item: object) -> str:
    if not isinstance(item, dict) or not isinstance(item.get("msg"), str):
        return ""
    loc = item.get("loc")
    location = ".".join(str(part) for part in loc) if isinstance(loc, list) else ""
    return f"{location}: {item['msg']}" if location else str(item["msg"])


def _describe(what: str, model: str, status: int | None, detail: str = "") -> str:
    where = f"HTTP {status}, model {model}" if status is not None else f"model {model}"
    return f"{what} ({where}){': ' + detail if detail else ''}"


def _unavailable(message: str, model: str) -> LLMUnavailableError:
    return LLMUnavailableError(message, provider=PROVIDER, model=model)


def _classify(exc: Exception, model: str, now: Callable[[], datetime]) -> _Failure | None:
    """Map an SDK or transport exception to a :class:`_Failure`; ``None`` if not recognized."""
    if isinstance(exc, httpx2.TimeoutException):
        message = _describe("Mistral request timed out", model, None)
        return _Failure("timeout", False, _unavailable(message, model))
    if isinstance(exc, httpx2.TransportError | errors.NoResponseError):
        name = type(exc).__name__
        message = _describe(f"Mistral connection failed ({name})", model, None)
        return _Failure("connection", True, _unavailable(message, model))
    if isinstance(exc, errors.ResponseValidationError):
        message = _describe("Mistral returned an unexpected response", model, None)
        return _Failure("bad_response", False, _unavailable(message, model))
    if not isinstance(exc, errors.MistralError):
        return None
    status = exc.status_code
    detail = _safe_detail(exc)
    if status in _AUTH_STATUSES:
        message = _describe(f"Mistral rejected the API key; check {_ENV_VAR}", model, status)
        return _Failure(
            "auth", False, LLMAuthError(message, provider=PROVIDER, model=model), status
        )
    if status == _RATE_LIMIT_STATUS:
        wait = _retry_after(exc.headers, now)
        message = _describe("Mistral rate limit exceeded", model, status, detail)
        error = LLMRateLimitError(message, provider=PROVIDER, model=model, retry_after=wait)
        return _Failure("rate_limit", True, error, status, wait)
    if _SERVER_ERROR_STATUS <= status < _SERVER_ERROR_STATUS + 100:
        message = _describe("Mistral server error", model, status, detail)
        return _Failure("server", True, _unavailable(message, model), status)
    if not _CLIENT_ERROR_MIN <= status < _SERVER_ERROR_STATUS:
        message = _describe("Mistral returned an unexpected response", model, status)
        return _Failure("bad_response", False, _unavailable(message, model), status)
    message = _describe("Mistral rejected the request", model, status, detail)
    error_ = LLMInvalidRequestError(message, provider=PROVIDER, model=model, status=status)
    return _Failure("invalid_request", False, error_, status)


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
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.retry = retry if retry is not None else RetryPolicy()
        self._client_factory = client_factory or (lambda: httpx2.AsyncClient(follow_redirects=True))
        self._server_url = server_url
        self._sleep = sleep
        self._uniform = uniform
        self._now = now
        # At most one entry: clients of other loops are dropped, never closed from this loop.
        self._clients: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, Mistral] = (
            weakref.WeakKeyDictionary()
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build the provider; raises ``LLMAuthError`` naming the env var if the key is missing."""
        return cls(
            require_api_key(settings, PROVIDER), timeout_seconds=settings.llm_timeout_seconds
        )

    def __repr__(self) -> str:
        return f"MistralProvider(timeout_seconds={self.timeout_seconds!r}, retry={self.retry!r})"

    def _client_for_loop(self) -> Mistral:
        """Return the SDK client of the running loop, building it on first use or loop change."""
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None:
            # Pools of other loops are dropped (sockets are released on garbage collection);
            # closing them from this loop is not safe.
            self._clients.clear()
            client = Mistral(
                api_key=self._api_key,
                async_client=self._client_factory(),
                server_url=self._server_url,
                timeout_ms=int((self.timeout_seconds + _SDK_TIMEOUT_MARGIN_S) * 1000),
            )
            self._clients[loop] = client
        return client

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
            self._client_for_loop().chat.complete_async(
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

    def _backoff(self, retry_number: int) -> float:
        policy = self.retry
        jitter = self._uniform(-policy.jitter, policy.jitter)
        return float(policy.base_delay * 2 ** (retry_number - 1) * (1 + jitter))

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
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._attempt(
                    model,
                    messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                )
            except LLMError:
                raise  # already typed (deadline, no answer text): never retried
            except Exception as exc:
                failure = _classify(exc, model, self._now)
                if failure is None:
                    raise
                if not failure.retryable or attempt > self.retry.max_retries:
                    raise failure.error from None
                wait = failure.retry_after
                if wait is None:
                    wait = self._backoff(attempt)
                elif wait > self.retry.max_retry_after:
                    raise failure.error from None
            logger.warning(
                "llm.retry",
                extra={
                    "provider": PROVIDER,
                    "model": model,
                    "attempt": attempt,
                    "status": failure.status,
                    "failure": failure.kind,
                    "wait_s": round(wait, 3),
                },
            )
            await self._sleep(wait)

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
                schema_definition=schema.model_json_schema(),
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

        try:
            return await structured_with_repair(request, system, user, schema)
        except LLMInvalidOutputError as exc:
            raise LLMInvalidOutputError(
                f"Mistral structured output for {schema.__name__} is invalid after one repair "
                f"attempt (model {model}): {exc.errors}",
                errors=exc.errors,
                usage=exc.usage,
                provider=PROVIDER,
                model=model,
            ) from None


if TYPE_CHECKING:
    _check: type[LLMProvider] = MistralProvider
