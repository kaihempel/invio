"""Anthropic (Claude) LLM provider built on the official ``anthropic`` SDK (1.x).

API choice: every call is one stateless Messages API request (``messages.create``) with the
system text as the top-level ``system`` parameter and the user text as the only message.
Usage is reported as ``input_tokens``/``output_tokens``, a 1:1 fit for
:class:`~invio.llm.base.Usage`. Structured output declares one tool whose ``input_schema`` is
the Pydantic JSON schema (sent unchanged: no strict mode, so every schema construct stays
available) and forces it with ``tool_choice``; the tool input is serialized to JSON and
validated by :func:`~invio.llm.base.structured_with_repair` (one repair attempt at most). The
:class:`~invio.llm.base.LLMProvider` protocol hides this choice, so a switch would stay local to
this module.

Only models that accept a forced ``tool_choice`` and a ``temperature`` may be registered (see
``models.d/anthropic.yaml``). The 1.x SDK dropped ``temperature`` from ``messages.create``, so
it is passed through ``extra_body``. Structured calls request the model's registered
``max_output_tokens`` (the answer size is not otherwise known), so every ``anthropic`` registry
entry must define it: building the provider raises :class:`~invio.llm.base.LLMConfigError`
otherwise. Providers built by the factory read the registry via ``default_registry()`` (the
factory does not forward its own registry to ``from_settings``); tests that need other limits
construct the class directly with ``registry=``.

All retrying is done by the shared loop in :mod:`invio.llm.http_retry` (the SDK's own retries
are disabled with ``max_retries=0``); this module only maps SDK exceptions to
:class:`~invio.llm.http_retry.Failure` records (:func:`_classify`): rate limits (429), server
errors (5xx, including 529 overloaded) and connection failures are retried; timeouts,
authentication failures, rejected requests, requests that cannot be sent and unusable answers
(empty, refused, cut off) are not. Exhausted credit (402, or the legacy 400 about the credit
balance) is raised as a non-retryable :class:`~invio.llm.base.LLMQuotaError`.

Errors raised here carry ``provider="anthropic"`` and the model. Their messages are built from
the status and the sanitized ``error.message`` of the response body only; ``str(exc)`` of an SDK
exception (which embeds the raw body) is never used. The API key, the prompt, the answer, a
refusal text and the raw body never appear in them, and the SDK exception is neither their
``__cause__`` nor their ``__context__``.

Only the ``x-api-key`` credential is ever sent, to an explicit base URL, so neither
``ANTHROPIC_AUTH_TOKEN`` nor ``ANTHROPIC_BASE_URL`` can redirect or replace it.

The SDK client (and so its HTTP connection pool) is bound to the event loop that uses it, so
there is one client per running loop, created lazily from ``client_factory``.
:meth:`AnthropicProvider.aclose` closes the client of the running loop; clients of loops that
were closed meanwhile cannot be closed any more and are dropped on the next use. The client
table is guarded by a lock, so threads running their own loops may share one provider.
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

import anthropic
import httpx2
from anthropic import AsyncAnthropic
from anthropic.types import Message
from pydantic import BaseModel

from invio.config.settings import Settings
from invio.llm import registry as llm_registry
from invio.llm.base import (
    LLMAuthError,
    LLMConfigError,
    LLMInvalidRequestError,
    LLMProvider,
    LLMQuotaError,
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
    utc_now,
)
from invio.llm.registry import ModelRegistry

PROVIDER = "anthropic"
_ENV_VAR = "INVIO_ANTHROPIC_API_KEY"
_DEFAULT_BASE_URL = "https://api.anthropic.com"
_SDK_TIMEOUT_MARGIN_S = 5
_AUTH_STATUSES = frozenset({401, 403})
_PAYMENT_REQUIRED = 402
_BAD_REQUEST = 400
_RATE_LIMIT_STATUS = 429
_CLIENT_ERRORS = range(400, 500)
_SERVER_ERRORS = range(500, 600)
_TOOL_NAME_LIMIT = 64
_BILLING_ERROR = "billing_error"
_CREDIT_BALANCE = "credit balance"
# Causes of a connection error raised before anything was sent: retrying cannot help.
_UNSENDABLE = (httpx2.InvalidURL, httpx2.UnsupportedProtocol, httpx2.LocalProtocolError)
# Causes that mean the server answered with something the client cannot use.
_BAD_RESPONSE = (httpx2.DecodingError, httpx2.TooManyRedirects, httpx2.StreamError)


def _unavailable(message: str, model: str) -> LLMUnavailableError:
    return LLMUnavailableError(message, provider=PROVIDER, model=model)


def _safe_detail(exc: anthropic.APIStatusError) -> str:
    """Return the provider's own ``error.message``, never echoing request input.

    The SDK passes the whole response document as ``exc.body``
    (``{"type": "error", "error": {"type": ..., "message": ...}}``); a flat
    ``{"message": ...}`` is accepted too. Whitespace is collapsed and the result truncated;
    anything that is not a string message gives ``""``.
    """
    body = exc.body
    if not isinstance(body, dict):
        return ""
    error = body.get("error")
    container = error if isinstance(error, dict) else body
    message = container.get("message")
    return sanitize_detail(message) if isinstance(message, str) else ""


def _is_credit_exhausted(exc: anthropic.APIStatusError) -> bool:
    """Return whether the request was refused because the account has no credit left."""
    if exc.status_code == _PAYMENT_REQUIRED:
        return True
    body = exc.body
    body_type = None
    if isinstance(body, dict):
        error = body.get("error")
        body_type = error.get("type") if isinstance(error, dict) else body.get("type")
    if _BILLING_ERROR in (getattr(exc, "type", None), body_type):
        return True
    return exc.status_code == _BAD_REQUEST and _CREDIT_BALANCE in _safe_detail(exc).lower()


def _classify_transport(cause: BaseException, model: str) -> Failure:
    """Classify a connection-level failure by the (``httpx2``) exception that caused it."""
    name = type(cause).__name__
    if isinstance(cause, _UNSENDABLE):
        message = describe(f"Anthropic request could not be sent ({name})", model, None)
        return Failure("unsendable", False, _unavailable(message, model))
    if isinstance(cause, _BAD_RESPONSE):
        message = describe(f"Anthropic returned an unexpected response ({name})", model, None)
        return Failure("bad_response", False, _unavailable(message, model))
    message = describe(f"Anthropic connection failed ({name})", model, None)
    return Failure("connection", True, _unavailable(message, model))


def _classify_status(
    exc: anthropic.APIStatusError, model: str, now: Callable[[], datetime]
) -> Failure:
    status = exc.status_code
    detail = _safe_detail(exc)
    if status in _AUTH_STATUSES:
        message = describe(f"Anthropic rejected the API key; check {_ENV_VAR}", model, status)
        return Failure("auth", False, LLMAuthError(message, provider=PROVIDER, model=model), status)
    if _is_credit_exhausted(exc):
        message = describe("Anthropic credit exhausted; check plan and billing", model, status)
        quota = LLMQuotaError(message, provider=PROVIDER, model=model)
        return Failure("quota", False, quota, status)
    if status == _RATE_LIMIT_STATUS:
        wait = retry_after(exc.response.headers, now)
        message = describe("Anthropic rate limit exceeded", model, status, detail)
        finite_wait = wait if wait is not None and math.isfinite(wait) else None
        limited = LLMRateLimitError(
            message, provider=PROVIDER, model=model, retry_after=finite_wait
        )
        return Failure("rate_limit", True, limited, status, wait)
    if status in _SERVER_ERRORS:
        message = describe("Anthropic server error", model, status, detail)
        return Failure("server", True, _unavailable(message, model), status)
    if status not in _CLIENT_ERRORS:
        message = describe("Anthropic returned an unexpected response", model, status)
        return Failure("bad_response", False, _unavailable(message, model), status)
    message = describe("Anthropic rejected the request", model, status, detail)
    invalid = LLMInvalidRequestError(message, provider=PROVIDER, model=model, status=status)
    return Failure("invalid_request", False, invalid, status)


def _classify(exc: Exception, model: str, now: Callable[[], datetime]) -> Failure | None:
    """Map an SDK or transport exception to a ``Failure``; ``None`` if not recognized."""
    name = type(exc).__name__
    # APITimeoutError is a subclass of APIConnectionError: it must be checked first.
    if isinstance(exc, anthropic.APITimeoutError):
        message = describe("Anthropic request timed out", model, None)
        return Failure("timeout", False, _unavailable(message, model))
    if isinstance(exc, anthropic.APIConnectionError):
        # The SDK raises these from the transport exception; inspect what caused them.
        return _classify_transport(exc.__cause__ or exc, model)
    # Raw transport errors that the SDK did not wrap.
    if isinstance(exc, httpx2.TimeoutException):
        message = describe("Anthropic request timed out", model, None)
        return Failure("timeout", False, _unavailable(message, model))
    if isinstance(exc, httpx2.InvalidURL | httpx2.HTTPError):
        return _classify_transport(exc, model)
    if isinstance(exc, anthropic.APIResponseValidationError | json.JSONDecodeError):
        message = describe(f"Anthropic returned an unexpected response ({name})", model, None)
        return Failure("bad_response", False, _unavailable(message, model))
    if isinstance(exc, anthropic.APIStatusError):
        return _classify_status(exc, model, now)
    return None


def _answer(message: object, model: str, *, structured: bool) -> tuple[str, Usage]:
    """Return the answer text and usage of a Messages response.

    Cut-off (``max_tokens``) and refused answers raise ``LLMUnavailableError`` for both
    operations, and so does empty free text; neither the refusal nor any partial answer text is
    put into the message. For structured calls the answer is the JSON of the first ``tool_use``
    input, or else the (possibly empty) text, which fails validation and is repaired once.
    """
    # The SDK returns the raw body when a 200 response is not JSON.
    if not isinstance(message, Message):
        raise _unavailable(
            describe("Anthropic returned an unexpected response", model, None), model
        )
    if message.stop_reason == "max_tokens":
        raise _unavailable(
            describe("Anthropic answer is incomplete: max_tokens", model, None), model
        )
    if message.stop_reason == "refusal":
        raise _unavailable(describe("Anthropic refused to answer", model, None), model)
    text = "".join(block.text for block in message.content if block.type == "text")
    answer = text
    if structured:
        tool_input = next(
            (block.input for block in message.content if block.type == "tool_use"), None
        )
        if tool_input is not None:
            answer = json.dumps(tool_input)
    elif not text:
        raise _unavailable(describe("Anthropic returned no answer text", model, None), model)
    usage = message.usage
    input_tokens = getattr(usage, "input_tokens", None) or 0
    output_tokens = getattr(usage, "output_tokens", None) or 0
    return answer, Usage(input_tokens, output_tokens)


@register_provider(PROVIDER)
class AnthropicProvider:
    """An :class:`~invio.llm.base.LLMProvider` backed by the Anthropic Messages API."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float,
        registry: ModelRegistry,
        retry: RetryPolicy | None = None,
        client_factory: Callable[[], httpx2.AsyncClient] | None = None,
        base_url: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        missing = [
            info.model_id
            for info in registry.models_for(PROVIDER)
            if info.max_output_tokens is None
        ]
        if missing:
            raise LLMConfigError(
                f"registry entries of provider '{PROVIDER}' must define max_output_tokens: "
                f"{', '.join(missing)}"
            )
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.retry = retry if retry is not None else RetryPolicy()
        self._registry = registry
        self._client_factory = client_factory or anthropic.DefaultAsyncHttpxClient
        self._base_url = base_url or _DEFAULT_BASE_URL
        self._sleep = sleep
        self._uniform = uniform
        self._now = now
        self._clients: dict[
            asyncio.AbstractEventLoop, tuple[AsyncAnthropic, httpx2.AsyncClient]
        ] = {}
        self._clients_lock = threading.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build the provider; raises ``LLMAuthError`` naming the env var if the key is missing."""
        return cls(
            require_api_key(settings, PROVIDER),
            timeout_seconds=settings.llm_timeout_seconds,
            # Looked up at call time so that tests can replace the default registry.
            registry=llm_registry.default_registry(),
        )

    def __repr__(self) -> str:
        return f"AnthropicProvider(timeout_seconds={self.timeout_seconds!r}, retry={self.retry!r})"

    def _client_for_loop(self) -> AsyncAnthropic:
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
                sdk_client = AsyncAnthropic(
                    api_key=self._api_key,
                    # Always explicit: the ANTHROPIC_BASE_URL environment variable must not
                    # redirect the key to another host. An explicit api_key also keeps
                    # ANTHROPIC_AUTH_TOKEN out of the request (research R10).
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
        system: str,
        user: str,
        *,
        temperature: float,
        max_tokens: int,
        tool: dict[str, Any] | None,
    ) -> tuple[str, Usage]:
        """Send one HTTP request and return the answer text and usage."""
        options: dict[str, Any] = {}
        if tool is not None:
            options["tools"] = [tool]
            options["tool_choice"] = {
                "type": "tool",
                "name": tool["name"],
                "disable_parallel_tool_use": True,
            }
        message = await with_timeout(
            self._client_for_loop().messages.create(
                model=model,
                max_tokens=max_tokens,
                system=system,
                messages=[{"role": "user", "content": user}],
                # The 1.x SDK has no temperature parameter; the API still honours it.
                extra_body={"temperature": temperature},
                **options,
            ),
            seconds=self.timeout_seconds,
            provider=PROVIDER,
            model=model,
        )
        return _answer(message, model, structured=tool is not None)

    async def _request(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float,
        max_tokens: int,
        tool: dict[str, Any] | None,
    ) -> tuple[str, Usage]:
        """Run :meth:`_attempt` with classification and the shared retry loop."""

        async def attempt() -> tuple[str, Usage]:
            return await self._attempt(
                model, system, user, temperature=temperature, max_tokens=max_tokens, tool=tool
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
            system, user, model=model, temperature=temperature, max_tokens=max_tokens, tool=None
        )

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        info = self._registry.get(model)
        if info is None or info.max_output_tokens is None:
            raise LLMConfigError(
                f"model '{model}' is not registered with max_output_tokens "
                f"for LLM provider '{PROVIDER}'"
            )
        max_tokens = info.max_output_tokens
        tool: dict[str, Any] = {
            "name": re.sub(r"[^a-zA-Z0-9_-]", "_", schema.__name__)[:_TOOL_NAME_LIMIT],
            "description": "Return the answer as the tool input.",
            "input_schema": schema.model_json_schema(),
        }

        async def request(system_text: str, user_text: str) -> tuple[str, Usage]:
            return await self._request(
                system_text,
                user_text,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                tool=tool,
            )

        return await structured_with_repair(
            request, system, user, schema, provider=PROVIDER, model=model
        )


if TYPE_CHECKING:
    _check: type[LLMProvider] = AnthropicProvider
