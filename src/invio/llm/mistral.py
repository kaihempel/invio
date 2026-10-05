"""Mistral LLM provider built on the official ``mistralai`` SDK (3.x).

The SDK talks HTTP through ``httpx2``, so this module imports its exception types. All
retrying is done here (the SDK's own retry configuration is never set): rate limits (429),
server errors (5xx) and connection failures are retried with exponential backoff and jitter
(:class:`RetryPolicy`), a ``Retry-After`` header is honoured up to ``max_retry_after`` seconds,
and timeouts, authentication failures, rejected requests and requests that cannot be sent are
never retried. Every ``httpx2`` error is mapped to a typed :class:`~invio.llm.base.LLMError`.

Errors raised here carry ``provider="mistral"`` and the model. Their messages are built from
the status and a sanitized provider message only; the API key, the prompt, the answer and the
raw response body never appear in them, and the SDK exception is neither their ``__cause__``
nor their ``__context__``.

The SDK client (and so its HTTP connection pool) is bound to the event loop that uses it, so
there is one client per running loop, created lazily. :meth:`MistralProvider.aclose` closes the
client of the running loop; clients of loops that were closed meanwhile cannot be closed any
more and are dropped on the next use (their sockets are released on garbage collection). The
client table is guarded by a lock, so threads running their own loops may share one provider.
"""

import asyncio
import json
import logging
import math
import random
import re
import threading
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
_CLIENT_ERRORS = range(400, 500)
_SERVER_ERRORS = range(500, 600)
_SCHEMA_NAME_LIMIT = 64
_DELTA_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
# Errors raised before anything was sent: retrying cannot help.
_UNSENDABLE = (httpx2.InvalidURL, httpx2.UnsupportedProtocol, httpx2.LocalProtocolError)


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

    Accepts ASCII integer or decimal seconds or an HTTP date (relative to ``now()``, floored at
    0). Seconds too large for a float give ``math.inf`` (above any cap). Anything else is
    ignored.
    """
    raw = headers.get("retry-after")
    if raw is None:
        return None
    if _DELTA_SECONDS.fullmatch(raw.strip()):
        return float(raw)
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
            text = "; ".join(item for item in map(_detail_item, detail) if item)
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
    name = type(exc).__name__
    if isinstance(exc, httpx2.TimeoutException):
        message = _describe("Mistral request timed out", model, None)
        return _Failure("timeout", False, _unavailable(message, model))
    if isinstance(exc, _UNSENDABLE):
        message = _describe(f"Mistral request could not be sent ({name})", model, None)
        return _Failure("unsendable", False, _unavailable(message, model))
    if isinstance(exc, httpx2.TransportError | errors.NoResponseError):
        message = _describe(f"Mistral connection failed ({name})", model, None)
        return _Failure("connection", True, _unavailable(message, model))
    if isinstance(exc, httpx2.HTTPError | httpx2.StreamError | errors.ResponseValidationError):
        # Undecodable body, redirect loop, stream misuse or a body the SDK cannot parse.
        message = _describe(f"Mistral returned an unexpected response ({name})", model, None)
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
        finite_wait = wait if wait is not None and math.isfinite(wait) else None
        limited = LLMRateLimitError(
            message, provider=PROVIDER, model=model, retry_after=finite_wait
        )
        return _Failure("rate_limit", True, limited, status, wait)
    if status in _SERVER_ERRORS:
        message = _describe("Mistral server error", model, status, detail)
        return _Failure("server", True, _unavailable(message, model), status)
    if status not in _CLIENT_ERRORS:
        message = _describe("Mistral returned an unexpected response", model, status)
        return _Failure("bad_response", False, _unavailable(message, model), status)
    message = _describe("Mistral rejected the request", model, status, detail)
    invalid = LLMInvalidRequestError(message, provider=PROVIDER, model=model, status=status)
    return _Failure("invalid_request", False, invalid, status)


def _strict_schema(node: object) -> object:
    """Return a copy of a JSON Schema with ``additionalProperties: false`` on every object.

    Mistral's strict mode expects closed objects (the SDK's own pydantic helper does the same).
    """
    if isinstance(node, dict):
        closed = {key: _strict_schema(value) for key, value in node.items()}
        if closed.get("type") == "object":
            closed["additionalProperties"] = False
        return closed
    if isinstance(node, list):
        return [_strict_schema(value) for value in node]
    return node


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
        self._clients: dict[asyncio.AbstractEventLoop, tuple[Mistral, httpx2.AsyncClient]] = {}
        self._clients_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build the provider; raises ``LLMAuthError`` naming the env var if the key is missing."""
        return cls(
            require_api_key(settings, PROVIDER), timeout_seconds=settings.llm_timeout_seconds
        )

    def __repr__(self) -> str:
        return f"MistralProvider(timeout_seconds={self.timeout_seconds!r}, retry={self.retry!r})"

    def _client_for_loop(self) -> Mistral:
        """Return the SDK client of the running loop, building it on its first use."""
        loop = asyncio.get_running_loop()
        with self._clients_lock:
            entry = self._clients.get(loop)
            if entry is None:
                # A closed loop's pool cannot be closed any more: drop it (sockets are released
                # on garbage collection). Clients of other live loops stay untouched.
                for closed in [other for other in self._clients if other.is_closed()]:
                    del self._clients[closed]
                http_client = self._client_factory()
                sdk_client = Mistral(
                    api_key=self._api_key,
                    async_client=http_client,
                    server_url=self._server_url,
                    timeout_ms=int((self.timeout_seconds + _SDK_TIMEOUT_MARGIN_S) * 1000),
                )
                entry = (sdk_client, http_client)
                self._clients[loop] = entry
        return entry[0]

    async def aclose(self) -> None:
        """Close the HTTP client of the running loop; the next call builds a new one."""
        loop = asyncio.get_running_loop()
        with self._clients_lock:
            entry = self._clients.pop(loop, None)
        if entry is not None:
            await entry[1].aclose()

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

    def _wait_before_retry(self, failure: _Failure, attempt: int) -> float | None:
        """Return the seconds to wait before the next attempt, or ``None`` to give up."""
        policy = self.retry
        if not failure.retryable or attempt > policy.max_retries:
            return None
        if failure.retry_after is None:
            jitter = self._uniform(-policy.jitter, policy.jitter)
            return float(policy.base_delay * 2 ** (attempt - 1) * (1 + jitter))
        if failure.retry_after > policy.max_retry_after:
            return None
        return failure.retry_after

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
            # Raised outside the handler, so the SDK exception is not kept as __context__.
            wait = self._wait_before_retry(failure, attempt)
            if wait is None:
                raise failure.error
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
                schema_definition=_strict_schema(schema.model_json_schema()),
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
