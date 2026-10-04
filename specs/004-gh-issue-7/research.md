# Research: LLM Provider Abstraction, Usage Tracking and Model Registry

**Feature**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md) · **Date**: 2026-10-04

Each entry records a decision, why it was made, and the alternatives rejected. No
NEEDS CLARIFICATION items remain.

---

## R1 — Package placement and module split

- **Decision**: Everything lives in the existing `src/invio/llm/` package:
  `base.py` (contract, `Usage`, errors, repair helper, timeout and credential helpers),
  `registry.py` (model registry loading and cost), `factory.py` (registration, discovery,
  `get_provider`, `resolve`, logging wrapper), `fake.py` (`FakeProvider`) and a `models.d/`
  directory for per-provider registry files. The issue's `scout/llm/...` paths map 1:1.
- **Rationale**: Matches the README layout (`llm/  LLM providers`) and the constitution's
  dependency direction: `invio.llm` (adapter) imports only `invio.config` and the standard
  library / Pydantic / PyYAML; nothing in `config`, `db` or `domain` imports `invio.llm`.
  Splitting registry from factory keeps cost calculation usable on its own (e.g. by a later
  usage-reporting command) without importing provider discovery.
- **Alternatives**: A single `llm/__init__.py` module (mixes discovery side effects with pure
  helpers; discovery would run on every `import invio.llm`); a separate top-level `registry`
  package (no other consumer justifies it).

## R2 — Sync or async contract

- **Decision**: Both contract operations are `async def`. The timeout is applied with
  `asyncio.timeout()`.
- **Rationale**: (1) The pipeline is LangGraph-based (README), which runs `async` nodes
  natively; relevance filtering of up to `max_items_per_run` (default 100) items with the
  `fast` model benefits from bounded concurrency (`asyncio.gather` + semaphore) in a later
  issue without changing the contract. (2) The clarified call timeout (FR-022) can be enforced
  generically with `asyncio.timeout()`, one helper for every provider, and tested with the fake
  provider and a tiny timeout — a sync contract would need per-SDK timeout plumbing or threads.
  (3) The planned SDKs (Mistral, OpenAI, Anthropic, Google GenAI, Ollama via HTTP) all offer
  async clients. (4) The project already ships `pytest-asyncio` with `asyncio_mode = "auto"`.
- **Alternatives**: Sync contract (simpler call sites, but timeout enforcement would differ per
  SDK and concurrency later would need a contract change); both sync and async methods
  (doubles every provider's surface — rejected by "simplicity first").

## R3 — Contract shape

- **Decision**: `LLMProvider` is a `typing.Protocol` (not runtime-checkable) with:
  - `async complete(system: str, user: str, *, model: str, temperature: float, max_tokens: int) -> tuple[str, Usage]`
  - `async complete_structured(system: str, user: str, schema: type[T], *, model: str, temperature: float) -> tuple[T, Usage]`
    with `T = TypeVar("T", bound=pydantic.BaseModel)`.

  Exactly the issue's signatures; providers are plain classes, no base class required.
- **Rationale**: Structural typing lets mypy prove `FakeProvider` (and every real provider)
  satisfies the contract without inheritance (acceptance criterion "checked via mypy").
- **Alternatives**: ABC base class (forces inheritance; mypy check would be implicit rather
  than structural); `runtime_checkable` (only checks method names at runtime, adds nothing over
  mypy).

## R4 — `Usage` and the repair flag

- **Decision**: `Usage` is a frozen, slotted dataclass `Usage(input_tokens: int, output_tokens: int, requests: int = 1)`
  with `__add__` (field-wise sum) and validation `>= 0` (`requests >= 1`). `requests` counts
  provider requests behind a result; `requests > 1` means a repair attempt happened.
- **Rationale**: FR-007 needs usage summed over the original and repair request; the logging
  wrapper (FR-019) needs to know whether a repair happened without coupling to the repair
  helper. A defaulted third field keeps the issue's `Usage(input_tokens, output_tokens)`
  constructor valid. A dataclass (not a Pydantic model) is enough for an internal value object
  and is cheap to create per call.
- **Alternatives**: A context variable set by the repair helper (hidden coupling, breaks with
  concurrency); returning a third tuple element (changes the issue's contract).

## R5 — Typed errors and their hierarchy

- **Decision** (all in `base.py`):
  - `LLMError(Exception)` — base for provider-call failures.
    - `LLMRateLimitError`, `LLMAuthError`, `LLMUnavailableError`.
    - `LLMInvalidOutputError(LLMError)` with attributes `errors: str` (validation summary
      without input values) and `usage: Usage` (accumulated, FR-007).
  - `LLMConfigError(ValueError)` — configuration problems detected before any call: unknown
    provider, unknown role, model missing from the registry or belonging to another provider,
    duplicate provider registration.
  - `ModelRegistryError(LLMConfigError)` — invalid or conflicting registry files.
  Every error carries `provider`/`model` keyword attributes where known (used by the logging
  wrapper).
- **Rationale**: Callers (pipeline, later retry/fallback) catch `LLMError` for runtime failures
  and treat `LLMConfigError` like other configuration errors (CLI exit code 2, constitution II).
  Deriving `LLMConfigError` from `ValueError` matches how the project's config errors behave.
- **Alternatives**: One flat error with a `kind` enum (callers cannot `except` by type); making
  config problems `LLMError`s (would be retried/fallen back by later logic although retrying
  cannot fix them).

## R6 — Credential checks

- **Decision**: `base.require_api_key(settings, provider: str) -> str` calls
  `settings.require_secret(f"{provider}_api_key")` and converts `MissingSettingError` into
  `LLMAuthError(f"LLM provider '{provider}' needs an API key: set {env_var}", provider=provider)`.
  Providers call it in their `from_settings()` constructor, so the check happens when
  `get_provider()` is called (FR-011), never at startup. The returned plain key is stored only
  in the provider's private client and never in `repr`/messages. Ollama's provider (later
  issue) will not call it.
- **Note**: `Settings.require_secret` rejects only `None` and the empty string; a key that is
  only whitespace passes it. `require_api_key` therefore additionally treats a key that is empty
  after `.strip()` as missing (spec edge case "empty or whitespace only").
- **Rationale**: Reuses the existing `require_secret` (rejects missing and empty values) and its
  `env_var` attribute, so the message names exactly the variable to set (SC-003).
- **Alternatives**: Checking keys in `get_provider` itself via a "needs_key" registration flag
  (adds a second place that knows provider-specific settings).

## R7 — Registration and discovery

- **Decision**: `factory.register_provider(name)` is a class decorator that stores the class in
  a module-level `_REGISTRY: dict[str, type[RegisteredProvider]]`; a second registration of the
  same name raises `LLMConfigError` naming it (FR-009). `RegisteredProvider` is a Protocol
  extending `LLMProvider` with `@classmethod from_settings(cls, settings: Settings) -> Self`.
  `_discover()` (run once, guarded by a module flag) iterates
  `pkgutil.iter_modules(invio.llm.__path__)` and imports every module whose name does not start
  with `_` via `importlib.import_module(f"invio.llm.{name}")`. Importing a module triggers its
  decorators. `get_provider()` calls `_discover()` before looking up the name.
- **Rationale**: Exactly the issue's mechanism; using the package `__path__` (a list) means a
  test can append a temporary directory to `invio.llm.__path__` with `monkeypatch` and drop in
  a new provider module — proving SC-001 without touching repository files. Lazy discovery
  avoids import-time side effects for code that only needs `base` or `registry`.
- **Alternatives**: Entry points in `pyproject.toml` (edits a shared file per provider —
  violates the requirement); explicit import list in `factory.py` (same).

## R8 — Model registry files

- **Decision**: One YAML file per provider in `models.d/<provider>.yaml`:

  ```yaml
  schema_version: 1
  provider: mistral
  models:
    <model-id>:
      input_price_per_mtok: 0.10   # USD per 1M input tokens
      output_price_per_mtok: 0.30  # USD per 1M output tokens
      context_window: 128000
  ```

  Parsed with strict Pydantic models (`extra="forbid"`, `frozen`), prices as `Decimal >= 0`
  (YAML floats are converted via `str()`, so `0.1` stays `Decimal("0.1")`), `context_window`
  `int > 0`, `schema_version == 1`. The file stem must equal `provider`. The merged
  `ModelRegistry` maps model id → `ModelInfo(model_id, provider, input_price_per_mtok,
  output_price_per_mtok, context_window)`. A model id seen in two files raises
  `ModelRegistryError` naming the id and both files. Only `*.yaml` files are read; a missing or
  empty directory yields an empty registry.
  Registry directories default to `<p>/models.d` for every `p` in `invio.llm.__path__`, so a
  test package directory contributes its own registry file like a real provider would.
  `load_registry(dirs: Sequence[Path] | None = None)`; the default registry is cached
  (`functools.cache`) with a `cache_clear()` for tests.
- **Rationale**: Grouping models under a file-level `provider` key makes "entry belongs to the
  file's provider" (FR-015) structural; `schema_version` is required for versioned file formats
  (constitution I). uv_build ships all files inside the module directory, so `models.d/*.yaml`
  is included in wheels without packaging config.
- **Alternatives**: One flat `models.yaml` (every provider edits the same file — merge
  conflicts across tracks, explicitly rejected by the issue); per-entry `provider` field
  (redundant with the file name and allows mismatches).

## R9 — Shipped registry content

- **Decision**: This issue creates `src/invio/llm/models.d/` with a `README.md` describing the
  format (not loaded — only `*.yaml` is read) and no provider files. Test registries live under
  `tests/fixtures/llm/models.d/`. The Mistral issue adds `models.d/mistral.yaml`.
- **Rationale**: Without a provider implementation a registry file would only be dead data;
  prices also change, so they belong with the provider that is tested against them.
- **Alternatives**: Ship files for all five providers now (unverified prices, no consumer).

## R10 — Cost calculation

- **Decision**: `ModelRegistry.cost(model_id, usage) -> Decimal | None`:
  `(input_tokens * input_price + output_tokens * output_price) / 1_000_000`, computed with
  `Decimal` and quantized to `Decimal("0.000001")` with `ROUND_HALF_UP`; `None` if the model is
  not in the registry (FR-016). Usage from all requests (including repair) is priced.
- **Rationale**: `llm_usage.cost_usd` is `Numeric(12, 6)`; quantizing in one place makes logged
  and stored costs identical and SC-004 testable "to the smallest stored unit". `None` maps to
  the nullable column.
- **Alternatives**: float (rounding drift, SC-004 fails); unquantized Decimal (log and DB values
  would differ after the DB rounds).

## R11 — Resolution

- **Decision**: `resolve(llm_config: LLMConfig, role: Role, settings: Settings | None = None, *, registry: ModelRegistry | None = None) -> tuple[LLMProvider, str]`
  with `Role = Literal["fast", "smart"]`. Steps: validate role (runtime check too, message lists
  `fast, smart`) → model id from `llm_config.models.<role>` → look up in registry; missing or
  `info.provider != llm_config.provider.value` → `LLMConfigError` naming model and provider →
  `get_provider(llm_config.provider.value, settings or get_settings())`.
  Registry check happens before the provider is constructed, so a wrong model id is reported
  even when the key is also missing.
- **Rationale**: Issue signature plus optional injection points for tests; validating the model
  first gives the most actionable error for job-file mistakes.
- **Alternatives**: Resolving only by role without registry check (typos in model ids would
  surface as provider 404s mid-run).

## R12 — Structured output and repair

- **Decision**: `structured_with_repair(request, system, user, schema) -> tuple[T, Usage]` where
  `request: Callable[[str, str], Awaitable[tuple[str, Usage]]]` sends one (system, user) pair to
  the model and returns raw text. The helper:
  1. appends a JSON instruction with `schema.model_json_schema()` to the system message;
  2. calls `request`; strips a surrounding Markdown code fence if present; validates with
     `schema.model_validate_json()` (JSON syntax errors surface as `ValidationError` too —
     FR-006);
  3. on failure logs a warning (`llm.repair`) and calls `request` once more with the user
     message extended by: the previous answer, the validation problems and an instruction to
     return only corrected JSON;
  4. on a second failure raises `LLMInvalidOutputError(errors=..., usage=total)`.
  Provider errors raised by `request` propagate unchanged (edge case: repair request
  rate-limited → rate-limit error). Validation problems are formatted from
  `ValidationError.errors(include_input=False, include_url=False)` as `loc: msg` lines, so no
  answer text reaches messages or logs (FR-021).
  Providers implement `complete_structured` by passing their own raw request function (which
  may enable a native JSON mode) to the helper.
- **Rationale**: One helper gives identical repair behaviour for all providers (FR-005) while
  letting each provider choose its request style. The previous answer goes into the repair
  *request* (needed for repair) but never into logs/errors.
- **Alternatives**: Repair loop in the logging wrapper calling `complete()` (loses native JSON
  mode per provider); instructor-style library (new dependency for ~40 lines of logic).

## R13 — Call timeout

- **Decision**: New setting `llm_timeout_seconds: float = Field(default=60.0, gt=0)` in
  `Settings` (`INVIO_LLM_TIMEOUT_SECONDS`, added to `.env.example`). Helper
  `base.with_timeout(awaitable, *, seconds, provider, model)` wraps one request in
  `asyncio.timeout(seconds)` and converts `TimeoutError` into
  `LLMUnavailableError("LLM provider '<p>' did not answer within <s> s (model <m>)")`.
  Providers receive the timeout via `from_settings()` and wrap every raw request with it —
  including each request issued through `structured_with_repair`, so the original and repair
  requests are bounded separately (FR-022).
- **Rationale**: A per-request bound is what the clarification requires; applying it inside the
  provider's raw request function is the only place that sees individual requests.
- **Alternatives**: Timeout in the logging wrapper around the whole operation (would bound
  original + repair together, contradicting FR-022); SDK-level timeouts only (inconsistent
  across SDKs, not testable with the fake).

## R14 — Logging wrapper

- **Decision**: `get_provider()` returns the registered provider wrapped in a private
  `_LoggedProvider` that itself satisfies `LLMProvider`. Per call it: validates
  `0 <= temperature <= 2` and `max_tokens >= 1` (raises `ValueError` before contacting the
  provider); measures duration with `time.perf_counter()`; on success logs one info line
  `llm.call` with `extra={"provider", "model", "input_tokens", "output_tokens", "cost_usd"
  (str or None), "duration_ms", "repaired" (usage.requests > 1)}`; on `LLMError` logs one
  warning `llm.error` with `error` (class name), `provider`, `model`, `duration_ms` and
  re-raises. Logger: `logging.getLogger("invio.llm")`; the existing JSON formatter adds `job`
  and `run_id` from the run context.
- **Rationale**: Central, provider-independent logging (FR-019/020) without each provider
  re-implementing it; cost needs the registry, which the wrapper already has.
- **Alternatives**: Logging inside each provider (duplication, easy to forget fields); a
  decorator on provider methods (harder to type-check with mypy strict than a wrapper class).

## R15 — FakeProvider

- **Decision**: `src/invio/llm/fake.py` (in `src/` so `mypy` — configured for `src` only —
  verifies it). `FakeProvider(script: Iterable[FakeStep], *, timeout_seconds: float = 60.0)`
  where `FakeStep = FakeReply | FakeDelay | LLMError` and
  `FakeReply(text: str, usage: Usage = Usage(10, 5))`, `FakeDelay(seconds: float, then: FakeReply)`.
  Each request pops the next step: replies are returned, errors raised, delays `await
  asyncio.sleep()` inside `with_timeout` (for timeout tests). Every request is recorded as
  `FakeRequest(system, user, model, temperature, max_tokens | None)` in `.requests`. An
  exhausted script raises `FakeScriptExhaustedError(AssertionError)`.
  `complete_structured` uses `structured_with_repair`, so repair behaviour is tested through
  it. A `TYPE_CHECKING`-only assignment `_check: type[LLMProvider] = FakeProvider` makes the
  protocol conformance a mypy error if it ever breaks; `if TYPE_CHECKING:` is added to
  coverage `exclude_also`.
  It is not decorated with `@register_provider` (no `fake` provider in production); tests
  register it under a provider name via a fixture that patches `_REGISTRY`.
- **Rationale**: Matches the acceptance criterion and the constitution's "no real providers in
  unit tests" rule; scripting errors and delays covers stories 2, 3 and FR-022.
- **Alternatives**: Fake under `tests/` (mypy would not check it); `unittest.mock` (no
  structural guarantee).

## R16 — Tests and the "no hard-coded model ids" guard

- **Decision**: New test modules `tests/test_llm_base.py`, `test_llm_repair.py`,
  `test_llm_registry.py`, `test_llm_factory.py`, `test_llm_fake.py`, `test_llm_logging.py`;
  helpers in `tests/llm_helpers.py`; fixture registries in `tests/fixtures/llm/models.d/`.
  The extensibility test (SC-001) writes `acme.py` (decorated with `@register_provider("acme")`)
  and `models.d/acme.yaml` into `tmp_path`, prepends it to `invio.llm.__path__`, resets
  discovery/registry caches, and asserts `get_provider("acme", settings)` and cost lookups
  work. SC-005: a test collects every model id from the shipped `models.d/*.yaml` files and
  asserts none occurs as a string literal in `src/invio/**/*.py`.
- **Rationale**: Each acceptance criterion maps to at least one test (constitution III); all
  run offline.
