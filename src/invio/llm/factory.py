"""Provider registration, discovery and resolution.

Provider modules decorate their class with :func:`register_provider`; :func:`get_provider`
imports every module of ``invio.llm`` once, so adding a provider needs a new module and a
``models.d/<provider>.yaml`` file only. Providers are returned wrapped in a private logging
and validation wrapper that writes one ``llm.call`` line per call.
"""

import importlib
import logging
import pkgutil
import time
from collections.abc import Callable
from typing import Literal, Protocol, Self, get_args

from pydantic import BaseModel

import invio.llm
from invio.config.job import LLMConfig
from invio.config.settings import Settings, get_settings
from invio.llm.base import LLMConfigError, LLMError, LLMInvalidOutputError, LLMProvider, Usage
from invio.llm.registry import ModelRegistry, default_registry

logger = logging.getLogger("invio.llm")

Role = Literal["fast", "smart"]

_MAX_TEMPERATURE = 2.0


class RegisteredProvider(LLMProvider, Protocol):
    """A provider class that can be built from settings (reads its key and the timeout).

    :func:`get_provider` passes the model ``registry`` it prices calls with, so a provider that
    reads model limits uses the same entries; providers that need none ignore it.
    """

    @classmethod
    def from_settings(
        cls, settings: Settings, *, registry: ModelRegistry | None = None
    ) -> Self: ...


_REGISTRY: dict[str, type[RegisteredProvider]] = {}
_discovered = False


def register_provider[P: RegisteredProvider](name: str) -> Callable[[type[P]], type[P]]:
    """Class decorator registering a provider under ``name`` (duplicates are an error)."""

    def decorator(cls: type[P]) -> type[P]:
        existing = _REGISTRY.get(name)
        if existing is not None:
            raise LLMConfigError(
                f"LLM provider '{name}' is registered twice "
                f"({existing.__module__}, {cls.__module__})"
            )
        _REGISTRY[name] = cls
        return cls

    return decorator


def _discover() -> None:
    """Import every non-private module of ``invio.llm`` once so providers can register."""
    global _discovered
    if _discovered:
        return
    for module in pkgutil.iter_modules(invio.llm.__path__):
        if not module.name.startswith("_"):
            try:
                importlib.import_module(f"invio.llm.{module.name}")
            except LLMConfigError:
                raise
            except Exception as exc:
                raise LLMConfigError(
                    f"cannot import LLM provider module '{module.name}': {type(exc).__name__}"
                ) from exc
    _discovered = True


class _LoggedProvider:
    """Validates call parameters and logs every call of the wrapped provider."""

    def __init__(self, provider: LLMProvider, *, name: str, registry: ModelRegistry) -> None:
        self._provider = provider
        self._name = name
        self._registry = registry

    @staticmethod
    def _check(temperature: float, max_tokens: int | None) -> None:
        if not 0 <= temperature <= _MAX_TEMPERATURE:
            raise ValueError(f"temperature must be between 0 and 2, got {temperature}")
        if max_tokens is not None and max_tokens < 1:
            raise ValueError(f"max_tokens must be >= 1, got {max_tokens}")

    def _usage_fields(self, model: str, usage: Usage) -> dict[str, object]:
        cost = self._registry.cost(model, usage)
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cost_usd": str(cost) if cost is not None else None,
            "repaired": usage.requests > 1,
        }

    def _log_success(self, model: str, usage: Usage, started: float) -> None:
        logger.info(
            "llm.call",
            extra={
                "provider": self._name,
                "model": model,
                "duration_ms": _elapsed_ms(started),
                **self._usage_fields(model, usage),
            },
        )

    def _log_error(self, err: LLMError, model: str, started: float) -> None:
        # An invalid structured answer still consumed (and cost) tokens: report them.
        usage_fields = (
            self._usage_fields(model, err.usage) if isinstance(err, LLMInvalidOutputError) else {}
        )
        logger.warning(
            "llm.error",
            extra={
                "error": type(err).__name__,
                "provider": self._name,
                "model": model,
                "duration_ms": _elapsed_ms(started),
                **usage_fields,
            },
        )

    async def complete(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int
    ) -> tuple[str, Usage]:
        self._check(temperature, max_tokens)
        started = time.perf_counter()
        try:
            text, usage = await self._provider.complete(
                system, user, model=model, temperature=temperature, max_tokens=max_tokens
            )
        except LLMError as err:
            self._log_error(err, model, started)
            raise
        self._log_success(model, usage, started)
        return text, usage

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        self._check(temperature, None)
        started = time.perf_counter()
        try:
            value, usage = await self._provider.complete_structured(
                system, user, schema, model=model, temperature=temperature
            )
        except LLMError as err:
            self._log_error(err, model, started)
            raise
        self._log_success(model, usage, started)
        return value, usage

    async def aclose(self) -> None:
        await self._provider.aclose()


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000, 1)


def get_provider(
    name: str, settings: Settings | None = None, *, registry: ModelRegistry | None = None
) -> LLMProvider:
    """Return the provider registered as ``name``, wrapped with logging and validation."""
    _discover()
    cls = _REGISTRY.get(name)
    if cls is None:
        registered = ", ".join(sorted(_REGISTRY)) or "none"
        raise LLMConfigError(f"unknown LLM provider '{name}'; registered: {registered}")
    models = registry if registry is not None else default_registry()
    provider = cls.from_settings(
        settings if settings is not None else get_settings(), registry=models
    )
    return _LoggedProvider(provider, name=name, registry=models)


def resolve(
    llm_config: LLMConfig,
    role: Role,
    settings: Settings | None = None,
    *,
    registry: ModelRegistry | None = None,
) -> tuple[LLMProvider, str]:
    """Return the provider of ``llm_config`` and the model id for ``role``.

    The model must be declared in the registry for that provider; this is checked before the
    provider is built, so registry problems surface even when the API key is missing.
    """
    roles = get_args(Role)
    if role not in roles:
        raise LLMConfigError(f"unknown LLM role '{role}'; expected one of: {', '.join(roles)}")
    model: str = getattr(llm_config.models, role)
    provider_name = llm_config.provider.value
    registry = registry if registry is not None else default_registry()
    registry.require(model, provider_name)
    return get_provider(provider_name, settings, registry=registry), model
