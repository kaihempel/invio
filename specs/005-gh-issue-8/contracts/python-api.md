# Contract: Python API (#8)

## `invio.llm.base` (additions)

```python
class LLMRateLimitError(LLMError):
    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        retry_after: float | None = None,
    ) -> None: ...

    retry_after: float | None  # seconds requested by the provider, if any


class LLMInvalidRequestError(LLMError):
    """The provider rejected the request itself (malformed input, unknown model, schema)."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        status: int | None = None,
    ) -> None: ...

    status: int | None
```

Existing call sites (`LLMRateLimitError("msg", provider=..., model=...)`) keep working.

```python
class LLMProvider(Protocol):
    ...

    async def aclose(self) -> None:
        """Release the connections opened from the running event loop (safe to call twice)."""


async def structured_with_repair[T: BaseModel](
    request: RawRequest,
    system: str,
    user: str,
    schema: type[T],
    *,
    provider: str | None = None,
    model: str | None = None,
) -> tuple[T, Usage]: ...  # LLMInvalidOutputError carries provider and model
```

## `invio.llm.registry` (additions)

```python
class ModelRegistry:
    def models_for(self, provider: str) -> list[ModelInfo]: ...  # ordered by model id
    def cheapest(self, provider: str) -> ModelInfo | None: ...  # min input+output price, ties by id
    def require(self, model_id: str, provider: str) -> ModelInfo: ...  # else LLMConfigError
```

## `invio.llm.mistral`

```python
@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_retries: int = 3
    base_delay: float = 1.0
    jitter: float = 0.25
    max_retry_after: float = 60.0


@register_provider("mistral")
class MistralProvider:
    def __init__(
        self,
        api_key: str,
        *,
        timeout_seconds: float,
        retry: RetryPolicy | None = None,  # None: RetryPolicy()
        client_factory: Callable[[], httpx2.AsyncClient]
        | None = None,  # called once per event loop
        server_url: str | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        uniform: Callable[[float, float], float] = random.uniform,
        now: Callable[[], datetime] = ...,  # aware UTC now
    ) -> None: ...

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Raises LLMAuthError naming INVIO_MISTRAL_API_KEY if the key is missing/blank."""

    async def aclose(self) -> None:
        """Close the HTTP client of the running loop; the next call builds a new one."""

    async def complete(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int
    ) -> tuple[str, Usage]: ...

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]: ...


if TYPE_CHECKING:
    _check: type[LLMProvider] = MistralProvider
```

### Behavioural guarantees

| Situation | Result |
|---|---|
| 2xx with text | `(text, Usage(prompt, completion))`; missing counts → 0 |
| 2xx, no choices / empty content / unparseable body | `LLMUnavailableError`, 1 attempt |
| 401 / 403 | `LLMAuthError`, 1 attempt, message names `INVIO_MISTRAL_API_KEY` |
| 429 ×4 | 4 attempts, waits ≈ 1, 2, 4 s (±25 %), then `LLMRateLimitError` |
| 429 with `Retry-After: s ≤ 60` | next wait = `s` |
| 429 with `Retry-After > 60` | `LLMRateLimitError(retry_after=s)` immediately |
| 429 with `Retry-After` too large for a float | `LLMRateLimitError(retry_after=None)` immediately |
| 5xx ×4 / connection failure ×4 | 4 attempts, then `LLMUnavailableError` (names last status/failure) |
| transient failure then 2xx | result of the successful attempt; `Usage.requests == 1` |
| timeout | `LLMUnavailableError` naming timeout, 1 attempt |
| `InvalidURL` / `UnsupportedProtocol` / `LocalProtocolError` | `LLMUnavailableError` ("could not be sent"), 1 attempt |
| `DecodingError` / `TooManyRedirects` / other `httpx2` error | `LLMUnavailableError` ("unexpected response"), 1 attempt |
| 400 / 404 / 422 / other 4xx | `LLMInvalidRequestError(status=…)`, 1 attempt |
| structured: valid JSON | validated `T`, request carries `response_format.type == "json_schema"`, `strict: true`, `additionalProperties: false` on every object |
| structured: invalid then valid | 2 successful requests, `Usage.requests == 2` |
| structured: invalid twice | `LLMInvalidOutputError` with summed usage |
| same instance used from two successive `asyncio.run` calls | both succeed; a new HTTP client is created for the second loop (FR-027) |
| several calls within one loop | one HTTP client reused |
| threads running their own loops | one client per live loop; clients of closed loops are dropped |
| `aclose()` | closes the running loop's HTTP client; safe before any call and twice |

All raised errors carry `provider="mistral"` and `model`; none contains the API key, prompt
or answer text, or the raw response body, and none keeps the SDK exception as `__cause__` or
`__context__`. Each retry logs one `llm.retry` warning.

## `pyproject.toml`

- `[project] dependencies`: `+ "mistralai>=3.0,<4"`, `+ "httpx2>=2.13"`.
- `[tool.pytest.ini_options]`: `markers += "live: ..."`. `tests/conftest.py` skips `live` tests unless the `-m` expression names `live` (so `-m db` does not run them).
