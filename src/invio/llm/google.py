"""Google (Gemini) LLM provider built on the official ``google-genai`` SDK (2.x).

API choice: every call is one stateless ``generateContent`` request
(``aio.models.generate_content``) of the Gemini Developer API, with the system text as the
system instruction and the user text as the only content. Usage is reported as the prompt token
count and the candidate plus thought token count (thinking is billed as output), which fits
:class:`~invio.llm.base.Usage`. Structured output requests ``application/json`` with a
``response_json_schema``; the answer is validated by
:func:`~invio.llm.base.structured_with_repair` (one repair attempt at most). The
:class:`~invio.llm.base.LLMProvider` protocol hides this choice.

Thinking, allowance and temperature: Gemini 3 models always think. Every registry entry of this
provider therefore defines ``thinking_level`` (sent with every request) and
``thinking_allowance_tokens`` (added to the caller's ``max_tokens`` of a free-text call, because
thought tokens count against the output limit); building the provider raises
:class:`~invio.llm.base.LLMConfigError` otherwise. Structured calls send no output limit, so the
model maximum applies. A model flagged ``keep_default_temperature`` gets no temperature, as
Google recommends for Gemini 3.

Schema conversion: the service accepts a subset of JSON Schema. :func:`gemini_schema` inlines
local ``$ref``/``$defs`` and drops the keywords outside that subset; the original Pydantic model
still validates the answer, so dropped constraints are enforced locally.

All retrying is done by the shared loop in :mod:`invio.llm.http_retry` (the SDK retries only
when ``retry_options`` is set, which it is not); this module maps SDK and ``httpx`` exceptions to
:class:`~invio.llm.http_retry.Failure` records.

Blocks: a blocked prompt, or an answer that the service stopped for a policy reason, raises
:class:`~invio.llm.base.LLMInvalidOutputError` naming the reason; it is neither retried nor
repaired, and carries the usage of the call. A cut-off answer (``MAX_TOKENS``) and an empty
free-text answer raise :class:`~invio.llm.base.LLMUnavailableError`.

Error hygiene: messages are built from the status and the sanitized ``error.message`` of the
response body only; ``str()`` of an SDK exception (which embeds the whole body) is never used.
The API key, the prompt, the answer and the raw body never appear in them, and the SDK exception
is neither their ``__cause__`` nor their ``__context__``.

The client is built with ``vertexai=False``, an explicit ``api_key`` and an explicit base URL,
so ``GOOGLE_API_KEY``, ``GEMINI_API_KEY``, ``GOOGLE_GEMINI_BASE_URL`` and
``GOOGLE_GENAI_USE_VERTEXAI`` cannot replace the key or redirect it. The key travels only as
the ``x-goog-api-key`` header. There is one SDK client per running event loop
(:class:`~invio.llm.loop_clients.LoopClients`), created lazily from ``client_factory``.
"""

import asyncio
import copy
import random
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Self

import httpx
from google import genai
from google.genai import types
from pydantic import BaseModel

from invio.config.settings import Settings
from invio.llm import registry as llm_registry
from invio.llm.base import (
    LLMConfigError,
    LLMProvider,
    LLMUnavailableError,
    Usage,
    require_api_key,
    structured_with_repair,
    with_timeout,
)
from invio.llm.factory import register_provider
from invio.llm.http_retry import (
    RetryPolicy,
    describe,
    run_with_retries,
    utc_now,
)
from invio.llm.loop_clients import LoopClients
from invio.llm.registry import ModelInfo, ModelRegistry

PROVIDER = "google"
_LABEL = "Google"
_ENV_VAR = "INVIO_GOOGLE_API_KEY"
_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/"
_API_VERSION = "v1beta"
_SDK_TIMEOUT_MARGIN_S = 5
_LOCAL_REF_PREFIX = "#/$defs/"
# The schema keywords the service accepts; everything else is dropped.
_SCHEMA_KEYWORDS = (
    "type",
    "title",
    "description",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "prefixItems",
    "minItems",
    "maxItems",
    "enum",
    "format",
    "minimum",
    "maximum",
    "anyOf",
)
_SCHEMA_LISTS = ("prefixItems", "anyOf")


def _inline(node: Any, defs: dict[str, Any], stack: tuple[str, ...]) -> Any:
    """Return the service-compatible copy of one schema node (``$ref`` inlined)."""
    if not isinstance(node, dict):
        return copy.deepcopy(node)
    out: dict[str, Any] = {}
    ref = node.get("$ref")
    if ref is not None:
        name = ref[len(_LOCAL_REF_PREFIX) :] if isinstance(ref, str) else ""
        if not isinstance(ref, str) or not ref.startswith(_LOCAL_REF_PREFIX) or name not in defs:
            raise LLMConfigError(
                f"schema contains a reference the Google provider cannot resolve: {ref!r}"
            )
        if name in stack:
            raise LLMConfigError(
                f"schema {name} is recursive; Google structured output cannot express it"
            )
        out = _inline(defs[name], defs, (*stack, name))
    for key in _SCHEMA_KEYWORDS:
        if key not in node:
            continue
        value = node[key]
        if key == "properties" and isinstance(value, dict):
            # The keys are field names, never keywords: keep them all.
            out[key] = {name: _inline(sub, defs, stack) for name, sub in value.items()}
        elif key in _SCHEMA_LISTS and isinstance(value, list):
            out[key] = [_inline(sub, defs, stack) for sub in value]
        elif key in ("items", "additionalProperties"):
            out[key] = _inline(value, defs, stack)
        else:
            out[key] = copy.deepcopy(value)
    return out


def gemini_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a service-compatible copy of a Pydantic JSON schema; ``schema`` is not changed.

    Local ``#/$defs/<Name>`` references are replaced by copies of their definitions (``$defs`` is
    dropped) and only the keywords the service documents are kept. A recursive schema or a
    reference that is not local raises :class:`~invio.llm.base.LLMConfigError`.
    """
    defs = schema.get("$defs", {})
    converted: dict[str, Any] = _inline(schema, defs if isinstance(defs, dict) else {}, ())
    return converted


def _unavailable(message: str, model: str) -> LLMUnavailableError:
    return LLMUnavailableError(message, provider=PROVIDER, model=model)


def _answer(
    response: types.GenerateContentResponse, model: str, *, structured: bool
) -> tuple[str, Usage]:
    """Return the answer text and usage of a ``generateContent`` response.

    The answer is the joined non-thought text parts of the first candidate. A cut-off answer and
    empty free text raise ``LLMUnavailableError`` (no answer text is put into the message); an
    empty structured answer is returned as ``""``, which fails validation and is repaired once.
    """
    metadata = response.usage_metadata
    usage = Usage(
        (metadata.prompt_token_count if metadata else None) or 0,
        ((metadata.candidates_token_count if metadata else None) or 0)
        + ((metadata.thoughts_token_count if metadata else None) or 0),
    )
    candidate = response.candidates[0] if response.candidates else None
    if candidate is not None and candidate.finish_reason == types.FinishReason.MAX_TOKENS:
        raise _unavailable(describe("Google answer is incomplete: MAX_TOKENS", model, None), model)
    parts = candidate.content.parts if candidate and candidate.content else None
    text = "".join(part.text for part in parts or [] if part.text and not part.thought)
    if not text and not structured:
        raise _unavailable(describe("Google returned no answer text", model, None), model)
    return text, usage


@register_provider(PROVIDER)
class GoogleProvider:
    """An :class:`~invio.llm.base.LLMProvider` backed by the Gemini ``generateContent`` API."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float,
        registry: ModelRegistry,
        retry: RetryPolicy | None = None,
        client_factory: Callable[[], httpx.AsyncClient] | None = None,
        base_url: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = utc_now,
    ) -> None:
        missing = [
            info.model_id
            for info in registry.models_for(PROVIDER)
            if info.thinking_level is None or info.thinking_allowance_tokens is None
        ]
        if missing:
            raise LLMConfigError(
                f"registry entries of provider '{PROVIDER}' must define thinking_level and "
                f"thinking_allowance_tokens: {', '.join(missing)}"
            )
        self._api_key = api_key
        self.timeout_seconds = timeout_seconds
        self.retry = retry if retry is not None else RetryPolicy()
        self._registry = registry
        self._client_factory = client_factory or httpx.AsyncClient
        self._base_url = base_url or _DEFAULT_BASE_URL
        self._sleep = sleep
        self._uniform = uniform
        self._now = now
        self._clients = LoopClients(self._build_client)

    @classmethod
    def from_settings(cls, settings: Settings, *, registry: ModelRegistry | None = None) -> Self:
        """Build the provider; raises ``LLMAuthError`` naming the env var if the key is missing.

        ``registry`` defaults to ``default_registry()``, looked up at call time so that tests
        can replace it.
        """
        return cls(
            require_api_key(settings, PROVIDER),
            timeout_seconds=settings.llm_timeout_seconds,
            registry=registry if registry is not None else llm_registry.default_registry(),
        )

    def __repr__(self) -> str:
        return f"GoogleProvider(timeout_seconds={self.timeout_seconds!r}, retry={self.retry!r})"

    def _build_client(self) -> tuple[genai.client.AsyncClient, httpx.AsyncClient]:
        http_client = self._client_factory()
        sdk_client = genai.Client(
            # Always explicit (research R4): the environment must not switch the backend, replace
            # the key or redirect it to another host.
            vertexai=False,
            api_key=self._api_key,
            http_options=types.HttpOptions(
                base_url=self._base_url,
                api_version=_API_VERSION,
                timeout=int((self.timeout_seconds + _SDK_TIMEOUT_MARGIN_S) * 1000),
                httpx_async_client=http_client,
            ),
        )
        return sdk_client.aio, http_client

    async def aclose(self) -> None:
        """Close the HTTP client of the running loop; the next call builds a new one."""
        await self._clients.aclose()

    def _model_info(self, model: str) -> ModelInfo:
        return self._registry.require(model, PROVIDER)

    @staticmethod
    def _config(
        info: ModelInfo,
        system: str,
        temperature: float,
        max_output_tokens: int | None,
        schema_json: dict[str, Any] | None,
    ) -> types.GenerateContentConfig:
        assert info.thinking_level is not None  # checked when the provider is built
        config = types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(info.thinking_level.upper())
            )
        )
        if system:
            config.system_instruction = system
        if not info.keep_default_temperature:
            config.temperature = temperature
        if max_output_tokens is not None:
            config.max_output_tokens = max_output_tokens
        if schema_json is not None:
            config.response_mime_type = "application/json"
            config.response_json_schema = schema_json
        return config

    async def _attempt(
        self, model: str, user: str, config: types.GenerateContentConfig, *, structured: bool
    ) -> tuple[str, Usage]:
        """Send one HTTP request and return the answer text and usage."""
        response = await with_timeout(
            self._clients.get().models.generate_content(model=model, contents=user, config=config),
            seconds=self.timeout_seconds,
            provider=PROVIDER,
            model=model,
        )
        return _answer(response, model, structured=structured)

    async def _request(
        self, user: str, *, model: str, config: types.GenerateContentConfig, structured: bool
    ) -> tuple[str, Usage]:
        """Run :meth:`_attempt` with classification and the shared retry loop."""

        async def attempt() -> tuple[str, Usage]:
            return await self._attempt(model, user, config, structured=structured)

        return await run_with_retries(
            attempt,
            classify=lambda exc, model, now: None,
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
        info = self._model_info(model)
        assert info.thinking_allowance_tokens is not None  # checked when the provider is built
        config = self._config(
            info, system, temperature, max_tokens + info.thinking_allowance_tokens, None
        )
        return await self._request(user, model=model, config=config, structured=False)

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        info = self._model_info(model)
        schema_json = gemini_schema(schema.model_json_schema())

        async def request(system_text: str, user_text: str) -> tuple[str, Usage]:
            config = self._config(info, system_text, temperature, None, schema_json)
            return await self._request(user_text, model=model, config=config, structured=True)

        return await structured_with_repair(
            request, system, user, schema, provider=PROVIDER, model=model
        )


if TYPE_CHECKING:
    _check: type[LLMProvider] = GoogleProvider
