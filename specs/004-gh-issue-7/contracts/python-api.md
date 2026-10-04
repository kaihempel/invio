# Contract: `invio.llm` Python API

**Feature**: [../spec.md](../spec.md) · **Data model**: [../data-model.md](../data-model.md)

This is the public surface pipeline nodes, provider modules and tests may use. Names not
listed here (leading underscore) are private.

---

## `invio.llm.base`

```python
T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int
    output_tokens: int
    requests: int = 1

    def __add__(self, other: Usage) -> Usage: ...


class LLMProvider(Protocol):
    async def complete(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int
    ) -> tuple[str, Usage]: ...

    async def complete_structured(
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]: ...


class LLMError(Exception):
    def __init__(
        self, message: str, *, provider: str | None = None, model: str | None = None
    ) -> None: ...


class LLMRateLimitError(LLMError): ...


class LLMAuthError(LLMError): ...


class LLMUnavailableError(LLMError): ...


class LLMInvalidOutputError(LLMError):
    errors: str  # "loc: msg" lines, no input values
    usage: Usage  # accumulated over original + repair request


class LLMConfigError(ValueError): ...


class ModelRegistryError(LLMConfigError): ...


RawRequest = Callable[[str, str], Awaitable[tuple[str, Usage]]]  # (system, user) -> (text, usage)


async def structured_with_repair(
    request: RawRequest, system: str, user: str, schema: type[T]
) -> tuple[T, Usage]:
    ...
    # Exactly one repair request on validation/parse failure, then LLMInvalidOutputError.


async def with_timeout(awaitable: Awaitable[R], *, seconds: float, provider: str, model: str) -> R:
    ...
    # TimeoutError -> LLMUnavailableError naming provider, model, seconds.


def require_api_key(settings: Settings, provider: str) -> str:
    ...
    # MissingSettingError -> LLMAuthError("LLM provider '<p>' needs an API key: set INVIO_<P>_API_KEY")
```

### Behavioural guarantees

| Call | Guarantee |
|---|---|
| `structured_with_repair` | 1 request if first answer valid; exactly 2 otherwise; provider errors propagate unchanged; repair request = original system (+ JSON-schema instruction) and user + previous answer + problems |
| `with_timeout` | bounds one request; never swallows other exceptions |
| `require_api_key` | blank or missing → `LLMAuthError`; key never in message |

## `invio.llm.registry`

```python
@dataclass(frozen=True, slots=True)
class ModelInfo:
    model_id: str
    provider: str
    input_price_per_mtok: Decimal
    output_price_per_mtok: Decimal
    context_window: int


class ModelRegistry:
    def get(self, model_id: str) -> ModelInfo | None: ...
    def cost(self, model_id: str, usage: Usage) -> Decimal | None: ...  # quantized to 0.000001
    def model_ids(self) -> frozenset[str]: ...


def load_registry(dirs: Sequence[Path] | None = None) -> ModelRegistry:
    ...
    # None -> <p>/models.d for p in invio.llm.__path__; raises ModelRegistryError


def default_registry() -> ModelRegistry: ...  # cached load_registry(); .cache_clear() for tests
```

## `invio.llm.factory`

```python
Role = Literal["fast", "smart"]


class RegisteredProvider(LLMProvider, Protocol):
    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        ...
        # reads key via require_api_key (if needed) and settings.llm_timeout_seconds


def register_provider(name: str) -> Callable[[type[P]], type[P]]:
    ...
    # duplicate name -> LLMConfigError


def get_provider(
    name: str, settings: Settings | None = None, *, registry: ModelRegistry | None = None
) -> LLMProvider:
    ...
    # discovers modules once; unknown name -> LLMConfigError (lists registered);
    # missing key -> LLMAuthError; returns provider wrapped with logging/validation


def resolve(
    llm_config: LLMConfig,
    role: Role,
    settings: Settings | None = None,
    *,
    registry: ModelRegistry | None = None,
) -> tuple[LLMProvider, str]:
    ...
    # role check -> registry check (exists, provider matches) -> get_provider
```

### Returned provider (logging wrapper) guarantees

| Situation | Behaviour |
|---|---|
| `temperature` outside `[0, 2]` or `max_tokens < 1` | `ValueError` before any request |
| success | one INFO `llm.call` line (provider, model, tokens, cost_usd, duration_ms, repaired) |
| `LLMError` | one WARNING `llm.error` line, error re-raised unchanged |
| any case | no prompt, answer or credential text in log lines |

## `invio.llm.fake`

```python
@dataclass(frozen=True)
class FakeReply:
    text: str
    usage: Usage = Usage(10, 5)


@dataclass(frozen=True)
class FakeDelay:
    seconds: float
    then: FakeReply


FakeStep = FakeReply | FakeDelay | LLMError


@dataclass(frozen=True)
class FakeRequest:
    system: str
    user: str
    model: str
    temperature: float
    max_tokens: int | None


class FakeScriptExhaustedError(AssertionError): ...


class FakeProvider:  # satisfies LLMProvider (mypy-checked)
    requests: list[FakeRequest]

    def __init__(
        self, script: Iterable[FakeStep], *, timeout_seconds: float = 60.0, name: str = "fake"
    ) -> None: ...
    @classmethod
    def from_settings(cls, settings: Settings) -> Self: ...  # empty script; for registry tests
```

## Provider module template (for later provider issues)

```python
# src/invio/llm/<provider>.py
@register_provider("<provider>")
class <Provider>Provider:
    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        return cls(api_key=require_api_key(settings, "<provider>"),
                   timeout_seconds=settings.llm_timeout_seconds)
    async def complete(...): ...            # each request wrapped in with_timeout; SDK errors -> LLM*Error
    async def complete_structured(...):     # return await structured_with_repair(self._raw, ...)
        ...
```

Plus `src/invio/llm/models.d/<provider>.yaml`. No other file changes (SC-001).
