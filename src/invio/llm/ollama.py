"""Ollama LLM provider for local models, built on the native ``POST /api/chat`` endpoint.

There is no SDK: every call is one ``httpx2`` request with ``stream: false`` to
``{ollama_base_url}/api/chat`` carrying the system and user message and the ``temperature`` and
``num_predict`` options. The answer is ``message.content``; usage is ``prompt_eval_count`` and
``eval_count`` (missing counts are 0). Ollama needs no API key, so :meth:`from_settings` reads
only ``ollama_base_url`` and ``llm_timeout_seconds``; local models are priced at 0 in
``models.d/ollama.yaml``.

Structured output sends the JSON Schema of the Pydantic model as ``format``. When the server
rejects a schema-valued ``format`` with HTTP 400 (older Ollama versions or models without
support), the same request is repeated in JSON mode (``format: "json"``); if that succeeds,
the model is remembered and later structured calls go straight to JSON mode. Either way the
answer is validated (and repaired once) by :func:`~invio.llm.base.structured_with_repair`.

The HTTP connect timeout is :data:`CONNECT_TIMEOUT_S` (5 s), the whole request is bounded by
``llm_timeout_seconds`` (:func:`~invio.llm.base.with_timeout`). A server that cannot be reached
(connection refused, connect timeout, DNS failure) raises
:class:`~invio.llm.base.LLMUnavailableError` at once, *without* retrying: a local server that is
down does not come back within the backoff. Other transport failures, 429 and 5xx are retried by
the shared loop in :mod:`invio.llm.http_retry` (:class:`RetryPolicy`); read timeouts, rejected
requests (a 404 for a model that is not pulled is an
:class:`~invio.llm.base.LLMInvalidRequestError`), requests that cannot be sent (a malformed base
URL) and unusable answers are not. Every ``httpx2`` error is mapped to a typed
:class:`~invio.llm.base.LLMError` by :func:`_classify`.

Errors raised here carry ``provider="ollama"`` and the model. Their messages are built from the
status and the sanitized ``error`` string of Ollama's JSON body only; the base URL (which may
embed credentials of a proxy), the prompt, the answer and the raw response body never appear in
them, and the ``httpx2`` exception is neither their ``__cause__`` nor their ``__context__``.

There is one HTTP client per running event loop (:class:`~invio.llm.loop_clients.LoopClients`),
created lazily from ``client_factory``.
"""

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self

import httpx2
from pydantic import BaseModel

from invio.config.settings import Settings
from invio.llm.base import (
    LLMAuthError,
    LLMConfigError,
    LLMInvalidRequestError,
    LLMProvider,
    LLMUnavailableError,
    Usage,
    structured_with_repair,
    with_timeout,
)
from invio.llm.factory import register_provider
from invio.llm.http_retry import (
    AUTH_STATUSES,
    Failure,
    RetryPolicy,
    bad_response_failure,
    classify_status,
    describe,
    run_with_retries,
    sanitize_detail,
    timeout_failure,
    utc_now,
)
from invio.llm.loop_clients import LoopClients
from invio.llm.registry import ModelRegistry

logger = logging.getLogger("invio.llm")

PROVIDER = "ollama"
_LABEL = "Ollama"
_ENV_VAR = "INVIO_OLLAMA_BASE_URL"
CONNECT_TIMEOUT_S = 5.0
"""Seconds to establish the connection; an unreachable server fails within this bound."""
_CHAT_PATH = "/api/chat"
_SCHEMA_REJECTED_STATUS = 400
_JSON_MODE = "json"
# Errors raised before anything was sent: retrying cannot help.
_UNSENDABLE = (httpx2.InvalidURL, httpx2.UnsupportedProtocol, httpx2.LocalProtocolError)
# The server could not be reached at all: fail fast, do not retry.
_UNREACHABLE = (httpx2.ConnectError, httpx2.ConnectTimeout)

Format = dict[str, Any] | str | None
"""The ``format`` of a request: a JSON Schema, ``"json"`` (JSON mode) or ``None`` (free text)."""


def _unavailable(message: str, model: str) -> LLMUnavailableError:
    return LLMUnavailableError(message, provider=PROVIDER, model=model)


def _safe_detail(response: httpx2.Response) -> str:
    """Return Ollama's own ``error`` message, sanitized and truncated; ``""`` if there is none."""
    try:
        document = json.loads(response.content)
    except ValueError:
        return ""
    error = document.get("error") if isinstance(document, dict) else None
    return sanitize_detail(error) if isinstance(error, str) else ""


def _classify_status(response: httpx2.Response, model: str, now: Callable[[], datetime]) -> Failure:
    status = response.status_code
    detail = _safe_detail(response)
    if status in AUTH_STATUSES:
        # Ollama itself has no authentication; a proxy in front of it refused the request.
        message = describe(f"{_LABEL} server refused access; check {_ENV_VAR}", model, status)
        return Failure("auth", False, LLMAuthError(message, provider=PROVIDER, model=model), status)
    return classify_status(
        status,
        label=_LABEL,
        env_var=_ENV_VAR,
        provider=PROVIDER,
        model=model,
        detail=detail,
        headers=response.headers,
        now=now,
    )


def _classify(exc: Exception, model: str, now: Callable[[], datetime]) -> Failure | None:
    """Map an ``httpx2`` exception to a ``Failure``; ``None`` if not recognized."""
    name = type(exc).__name__
    if isinstance(exc, httpx2.HTTPStatusError):
        return _classify_status(exc.response, model, now)
    if isinstance(exc, _UNREACHABLE):
        message = describe(f"{_LABEL} server unreachable ({name}); check {_ENV_VAR}", model, None)
        return Failure("unreachable", False, _unavailable(message, model))
    if isinstance(exc, httpx2.TimeoutException):
        return timeout_failure(_LABEL, provider=PROVIDER, model=model)
    if isinstance(exc, _UNSENDABLE):
        message = describe(
            f"{_LABEL} request could not be sent ({name}); check {_ENV_VAR}", model, None
        )
        return Failure("unsendable", False, _unavailable(message, model))
    if isinstance(exc, httpx2.TransportError):
        message = describe(f"{_LABEL} connection failed ({name})", model, None)
        return Failure("connection", True, _unavailable(message, model))
    if isinstance(exc, httpx2.HTTPError | httpx2.StreamError):
        # Undecodable body, redirect loop or stream misuse.
        return bad_response_failure(_LABEL, provider=PROVIDER, model=model, cause=f" ({name})")
    return None


def _token_count(document: dict[str, Any], key: str) -> int:
    value = document.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _parse(response: httpx2.Response) -> tuple[str, Usage] | None:
    """Return the answer text and usage, or ``None`` if the body is not a chat response."""
    try:
        document = json.loads(response.content)
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    message = document.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    text = content if isinstance(content, str) else ""
    usage = Usage(_token_count(document, "prompt_eval_count"), _token_count(document, "eval_count"))
    return text, usage


@register_provider(PROVIDER)
class OllamaProvider:
    """An :class:`~invio.llm.base.LLMProvider` backed by a (local) Ollama server."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float,
        retry: RetryPolicy | None = None,
        client_factory: Callable[[], httpx2.AsyncClient] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        self._chat_url = base_url.strip().rstrip("/") + _CHAT_PATH
        self.timeout_seconds = timeout_seconds
        self.http_timeout = httpx2.Timeout(timeout_seconds, connect=CONNECT_TIMEOUT_S)
        self.retry = retry if retry is not None else RetryPolicy()
        self._client_factory = client_factory or (
            lambda: httpx2.AsyncClient(follow_redirects=True, timeout=self.http_timeout)
        )
        self._sleep = sleep
        self._uniform = uniform
        self._now = now
        self._clients = LoopClients(self._build_client)
        # Models whose server rejected a schema-valued ``format``: they get JSON mode.
        self._json_mode_models: set[str] = set()

    @classmethod
    def from_settings(cls, settings: Settings, *, registry: ModelRegistry | None = None) -> Self:
        """Build the provider from ``ollama_base_url``; no API key is needed.

        Raises ``LLMConfigError`` if the base URL is blank. The model ``registry`` is not needed
        by this provider.
        """
        if not settings.ollama_base_url.strip():
            raise LLMConfigError(f"LLM provider '{PROVIDER}' needs a server URL: set {_ENV_VAR}")
        return cls(settings.ollama_base_url, timeout_seconds=settings.llm_timeout_seconds)

    def __repr__(self) -> str:
        return f"OllamaProvider(timeout_seconds={self.timeout_seconds!r}, retry={self.retry!r})"

    def _build_client(self) -> tuple[httpx2.AsyncClient, httpx2.AsyncClient]:
        client = self._client_factory()
        return client, client

    async def aclose(self) -> None:
        """Close the HTTP client of the running loop; the next call builds a new one."""
        await self._clients.aclose()

    async def _send(self, payload: dict[str, Any]) -> httpx2.Response:
        response = await self._clients.get().post(
            self._chat_url, json=payload, timeout=self.http_timeout
        )
        response.raise_for_status()
        return response

    async def _attempt(self, model: str, payload: dict[str, Any]) -> tuple[str, Usage]:
        """Send one HTTP request and return the answer text and usage."""
        response = await with_timeout(
            self._send(payload), seconds=self.timeout_seconds, provider=PROVIDER, model=model
        )
        parsed = _parse(response)
        if parsed is None:
            raise bad_response_failure(
                _LABEL, provider=PROVIDER, model=model, cause="", status=response.status_code
            ).error
        text, usage = parsed
        if not text:
            raise _unavailable(f"{_LABEL} returned no answer text (model {model})", model)
        return text, usage

    async def _request(
        self,
        system: str,
        user: str,
        *,
        model: str,
        temperature: float,
        max_tokens: int | None,
        response_format: Format,
    ) -> tuple[str, Usage]:
        """Run :meth:`_attempt` with classification and the retry loop."""
        options: dict[str, Any] = {"temperature": temperature}
        if max_tokens is not None:
            options["num_predict"] = max_tokens
        payload: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": options,
        }
        if response_format is not None:
            payload["format"] = response_format

        async def attempt() -> tuple[str, Usage]:
            return await self._attempt(model, payload)

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

    async def _structured_request(
        self, system: str, user: str, schema: dict[str, Any], *, model: str, temperature: float
    ) -> tuple[str, Usage]:
        """Request with the schema as ``format``; fall back to JSON mode if it is rejected."""
        if model not in self._json_mode_models:
            try:
                return await self._request(
                    system,
                    user,
                    model=model,
                    temperature=temperature,
                    max_tokens=None,
                    response_format=schema,
                )
            except LLMInvalidRequestError as err:
                if err.status != _SCHEMA_REJECTED_STATUS:
                    raise
            # Outside the handler, so the rejection is not kept as __context__.
            result = await self._request(
                system,
                user,
                model=model,
                temperature=temperature,
                max_tokens=None,
                response_format=_JSON_MODE,
            )
            # Only now is it clear that the schema (not the request) was the problem.
            self._json_mode_models.add(model)
            logger.warning("llm.format_fallback", extra={"provider": PROVIDER, "model": model})
            return result
        return await self._request(
            system,
            user,
            model=model,
            temperature=temperature,
            max_tokens=None,
            response_format=_JSON_MODE,
        )

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        json_schema = schema.model_json_schema()

        async def request(system_text: str, user_text: str) -> tuple[str, Usage]:
            return await self._structured_request(
                system_text, user_text, json_schema, model=model, temperature=temperature
            )

        return await structured_with_repair(
            request, system, user, schema, provider=PROVIDER, model=model
        )


if TYPE_CHECKING:
    _check: type[LLMProvider] = OllamaProvider
