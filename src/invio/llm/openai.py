"""OpenAI LLM provider built on the official ``openai`` SDK (2.x) and the Responses API.

API choice: every call is one stateless ``responses.create`` request (``store=False``) with the
system and user text as ``input``. The Responses API is OpenAI's recommended API for new work;
usage is reported as ``input_tokens``/``output_tokens`` (a 1:1 fit for
:class:`~invio.llm.base.Usage`) and ``max_output_tokens`` is the single limit parameter for
every model family (Chat Completions needs ``max_tokens`` or ``max_completion_tokens``
depending on the model). The :class:`~invio.llm.base.LLMProvider` protocol hides this choice,
so a switch would stay local to this module. Structured output uses a strict
``json_schema`` text format (closed objects, every property required, sanitized schema name)
plus :func:`~invio.llm.base.structured_with_repair`; a schema or model that OpenAI rejects
surfaces as :class:`~invio.llm.base.LLMInvalidRequestError` and is never silently retried
without strict mode. Only non-reasoning models (which accept ``temperature``) are registered.

All retrying is done by the shared loop in :mod:`invio.llm.http_retry` (the SDK's own retries
are disabled with ``max_retries=0``); this module only maps SDK exceptions to
:class:`~invio.llm.http_retry.Failure` records (:func:`_classify`): rate limits (429), server
errors (5xx) and connection failures are retried, timeouts, authentication failures, rejected
requests, requests that cannot be sent and unusable answers (empty, refused, cut off) are not.
A 429 whose code is ``insufficient_quota`` means exhausted billing, not a transient limit: it
is raised as a non-retryable :class:`~invio.llm.base.LLMRateLimitError` without a wait hint.
Known limitation: the graph-level :func:`invio.llm.retry.is_transient_llm` still treats any
``LLMRateLimitError`` as transient, so it retries such an error at that outer layer.

Errors raised here carry ``provider="openai"`` and the model. Their messages are built from the
status and the sanitized ``error.message`` of the response body only; ``str(exc)`` of an SDK
exception (which embeds the raw body) is never used. The API key, the prompt, the answer, a
refusal text and the raw body never appear in them, and the SDK exception is neither their
``__cause__`` nor their ``__context__``.

The SDK client (and so its HTTP connection pool) is bound to the event loop that uses it, so
there is one client per running loop, created lazily from ``client_factory``.
:meth:`OpenAIProvider.aclose` closes the client of the running loop; clients of loops that were
closed meanwhile cannot be closed any more and are dropped on the next use. The client table is
guarded by a lock, so threads running their own loops may share one provider.
"""

import asyncio
import json
import math
import random
import re
import threading
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self

import httpx
import openai
from openai import AsyncOpenAI
from openai.types.responses import Response, ResponseInputParam, ResponseTextConfigParam
from pydantic import BaseModel

from invio.config.settings import Settings
from invio.llm.base import (
    LLMAuthError,
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
from invio.llm.http_retry import (
    Failure,
    RetryPolicy,
    describe,
    retry_after,
    run_with_retries,
    sanitize_detail,
    strict_schema,
    utc_now,
)

PROVIDER = "openai"
_ENV_VAR = "INVIO_OPENAI_API_KEY"
_DEFAULT_BASE_URL = "https://api.openai.com/v1"
_SDK_TIMEOUT_MARGIN_S = 5
_AUTH_STATUSES = frozenset({401, 403})
_RATE_LIMIT_STATUS = 429
_CLIENT_ERRORS = range(400, 500)
_SERVER_ERRORS = range(500, 600)
_SCHEMA_NAME_LIMIT = 64
_INSUFFICIENT_QUOTA = "insufficient_quota"
# Causes of a connection error raised before anything was sent: retrying cannot help.
_UNSENDABLE = (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError)
# Causes that mean the server answered with something the client cannot use.
_BAD_RESPONSE = (httpx.DecodingError, httpx.TooManyRedirects, httpx.StreamError)


def _unavailable(message: str, model: str) -> LLMUnavailableError:
    return LLMUnavailableError(message, provider=PROVIDER, model=model)


def _safe_detail(exc: openai.APIStatusError) -> str:
    """Return the provider's own ``error.message``, never echoing request input.

    The SDK unwraps the ``error`` object of the response body into ``exc.body``. Whitespace is
    collapsed and the result truncated; anything that is not a string message gives ``""``.
    """
    body = exc.body
    message = body.get("message") if isinstance(body, dict) else None
    return sanitize_detail(message) if isinstance(message, str) else ""


def _is_insufficient_quota(exc: openai.APIStatusError) -> bool:
    body = exc.body
    kind = body.get("type") if isinstance(body, dict) else None
    return _INSUFFICIENT_QUOTA in (exc.code, kind)


def _classify_transport(cause: BaseException, model: str) -> Failure:
    """Classify a connection-level failure by the (``httpx``) exception that caused it."""
    name = type(cause).__name__
    if isinstance(cause, _UNSENDABLE):
        message = describe(f"OpenAI request could not be sent ({name})", model, None)
        return Failure("unsendable", False, _unavailable(message, model))
    if isinstance(cause, _BAD_RESPONSE):
        message = describe(f"OpenAI returned an unexpected response ({name})", model, None)
        return Failure("bad_response", False, _unavailable(message, model))
    message = describe(f"OpenAI connection failed ({name})", model, None)
    return Failure("connection", True, _unavailable(message, model))


def _classify_status(
    exc: openai.APIStatusError, model: str, now: Callable[[], datetime]
) -> Failure:
    status = exc.status_code
    detail = _safe_detail(exc)
    if status in _AUTH_STATUSES:
        message = describe(f"OpenAI rejected the API key; check {_ENV_VAR}", model, status)
        return Failure("auth", False, LLMAuthError(message, provider=PROVIDER, model=model), status)
    if status == _RATE_LIMIT_STATUS:
        if _is_insufficient_quota(exc):
            message = describe("OpenAI quota exhausted; check plan and billing", model, status)
            quota = LLMRateLimitError(message, provider=PROVIDER, model=model)
            return Failure("quota", False, quota, status)
        wait = retry_after(exc.response.headers, now)
        message = describe("OpenAI rate limit exceeded", model, status, detail)
        finite_wait = wait if wait is not None and math.isfinite(wait) else None
        limited = LLMRateLimitError(
            message, provider=PROVIDER, model=model, retry_after=finite_wait
        )
        return Failure("rate_limit", True, limited, status, wait)
    if status in _SERVER_ERRORS:
        message = describe("OpenAI server error", model, status, detail)
        return Failure("server", True, _unavailable(message, model), status)
    if status not in _CLIENT_ERRORS:
        message = describe("OpenAI returned an unexpected response", model, status)
        return Failure("bad_response", False, _unavailable(message, model), status)
    message = describe("OpenAI rejected the request", model, status, detail)
    invalid = LLMInvalidRequestError(message, provider=PROVIDER, model=model, status=status)
    return Failure("invalid_request", False, invalid, status)


def _classify(exc: Exception, model: str, now: Callable[[], datetime]) -> Failure | None:
    """Map an SDK or transport exception to a ``Failure``; ``None`` if not recognized."""
    name = type(exc).__name__
    # APITimeoutError is a subclass of APIConnectionError: it must be checked first.
    if isinstance(exc, openai.APITimeoutError):
        message = describe("OpenAI request timed out", model, None)
        return Failure("timeout", False, _unavailable(message, model))
    if isinstance(exc, openai.APIConnectionError):
        # The SDK raises these from the transport exception; inspect what caused them.
        return _classify_transport(exc.__cause__ or exc, model)
    # Raw httpx errors that the SDK did not wrap (for example an invalid base URL).
    if isinstance(exc, httpx.TimeoutException):
        message = describe("OpenAI request timed out", model, None)
        return Failure("timeout", False, _unavailable(message, model))
    if isinstance(exc, httpx.InvalidURL | httpx.HTTPError):
        return _classify_transport(exc, model)
    if isinstance(exc, openai.APIResponseValidationError | json.JSONDecodeError):
        message = describe(f"OpenAI returned an unexpected response ({name})", model, None)
        return Failure("bad_response", False, _unavailable(message, model))
    if isinstance(exc, openai.APIStatusError):
        return _classify_status(exc, model, now)
    return None


def _closed_schema(node: object) -> object:
    """Return ``node`` with every object closed and every property required.

    OpenAI's strict mode needs ``additionalProperties: false`` and ``required`` to list all
    properties of every object.
    """
    closed = strict_schema(node)
    _require_all(closed)
    return closed


def _require_all(node: object) -> None:
    if isinstance(node, dict):
        properties = node.get("properties")
        if node.get("type") == "object" and isinstance(properties, dict):
            node["required"] = list(properties)
        for value in node.values():
            _require_all(value)
    elif isinstance(node, list):
        for value in node:
            _require_all(value)


def _answer(response: Response, model: str) -> tuple[str, Usage]:
    """Return the answer text and usage of a completed response.

    Refusals, incomplete (cut off) responses and empty answers raise ``LLMUnavailableError``;
    neither the refusal nor any partial answer text is put into the message.
    """
    parts: list[str] = []
    refused = False
    for item in getattr(response, "output", None) or []:
        if getattr(item, "type", None) != "message":
            continue
        for content in getattr(item, "content", None) or []:
            kind = getattr(content, "type", None)
            if kind == "refusal":
                refused = True
            elif kind == "output_text" and isinstance(getattr(content, "text", None), str):
                parts.append(content.text)
    if refused:
        raise _unavailable(describe("OpenAI refused to answer", model, None), model)
    status = getattr(response, "status", None)
    if status == "incomplete":
        details = getattr(response, "incomplete_details", None)
        reason = getattr(details, "reason", None)
        suffix = f": {reason}" if isinstance(reason, str) else ""
        raise _unavailable(describe(f"OpenAI answer is incomplete{suffix}", model, None), model)
    if status != "completed":
        message = describe(f"OpenAI response did not complete (status {status})", model, None)
        raise _unavailable(message, model)
    text = "".join(parts)
    if not text:
        raise _unavailable(describe("OpenAI returned no answer text", model, None), model)
    usage = getattr(response, "usage", None)
    input_tokens = getattr(usage, "input_tokens", None) or 0
    output_tokens = getattr(usage, "output_tokens", None) or 0
    return text, Usage(input_tokens, output_tokens)


@register_provider(PROVIDER)
class OpenAIProvider:
    """An :class:`~invio.llm.base.LLMProvider` backed by the OpenAI Responses API."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float,
        retry: RetryPolicy | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        base_url: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.retry = retry if retry is not None else RetryPolicy()
        self._client_factory = client_factory or openai.DefaultAsyncHttpxClient
        self._base_url = base_url or _DEFAULT_BASE_URL
        self._sleep = sleep
        self._uniform = uniform
        self._now = now
        self._clients: dict[asyncio.AbstractEventLoop, tuple[AsyncOpenAI, httpx.AsyncClient]] = {}
        self._clients_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build the provider; raises ``LLMAuthError`` naming the env var if the key is missing."""
        return cls(
            require_api_key(settings, PROVIDER), timeout_seconds=settings.llm_timeout_seconds
        )

    def __repr__(self) -> str:
        return f"OpenAIProvider(timeout_seconds={self.timeout_seconds!r}, retry={self.retry!r})"

    def _client_for_loop(self) -> AsyncOpenAI:
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
                sdk_client = AsyncOpenAI(
                    api_key=self._api_key,
                    # Always explicit: the OPENAI_BASE_URL environment variable must not redirect
                    # the key to another host.
                    base_url=self._base_url,
                    http_client=http_client,
                    max_retries=0,
                    timeout=self.timeout_seconds + _SDK_TIMEOUT_MARGIN_S,
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
        messages: ResponseInputParam,
        *,
        temperature: float,
        max_tokens: int | None,
        text_format: ResponseTextConfigParam | None,
    ) -> tuple[str, Usage]:
        """Send one HTTP request and return the answer text and usage."""
        options: dict[str, Any] = {}
        if max_tokens is not None:
            options["max_output_tokens"] = max_tokens
        if text_format is not None:
            options["text"] = text_format
        response = await with_timeout(
            self._client_for_loop().responses.create(
                model=model,
                input=messages,
                temperature=temperature,
                store=False,
                **options,
            ),
            seconds=self.timeout_seconds,
            provider=PROVIDER,
            model=model,
        )
        return _answer(response, model)

    async def _request(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        text_format: ResponseTextConfigParam | None,
    ) -> tuple[str, Usage]:
        """Run :meth:`_attempt` with classification and the shared retry loop."""
        messages: ResponseInputParam = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

        async def attempt() -> tuple[str, Usage]:
            return await self._attempt(
                model,
                messages,
                temperature=temperature,
                max_tokens=max_tokens,
                text_format=text_format,
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
            text_format=None,
        )

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        schema_object = _closed_schema(schema.model_json_schema())
        assert isinstance(schema_object, dict)
        text_format: ResponseTextConfigParam = {
            "format": {
                "type": "json_schema",
                "name": re.sub(r"[^a-zA-Z0-9_-]", "_", schema.__name__)[:_SCHEMA_NAME_LIMIT],
                "schema": schema_object,
                "strict": True,
            }
        }

        async def request(system_text: str, user_text: str) -> tuple[str, Usage]:
            return await self._request(
                system_text,
                user_text,
                model=model,
                temperature=temperature,
                max_tokens=None,
                text_format=text_format,
            )

        return await structured_with_repair(
            request, system, user, schema, provider=PROVIDER, model=model
        )


if TYPE_CHECKING:
    _check: type[LLMProvider] = OpenAIProvider
