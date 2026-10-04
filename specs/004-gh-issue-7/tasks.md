---

description: "Task list for LLM provider abstraction, usage tracking and model registry (GitHub issue #7)"
---

# Tasks: LLM Provider Abstraction, Usage Tracking and Model Registry

**Input**: Design documents from `specs/004-gh-issue-7/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md),
[data-model.md](./data-model.md), [contracts/python-api.md](./contracts/python-api.md),
[quickstart.md](./quickstart.md)

**Tests**: Included — the issue's acceptance criteria and constitution principle III require an
automated test for every acceptance criterion, including rejection paths.

**Organization**: Tasks are grouped by user story (US1–US6 from spec.md) so each story can be
implemented and tested on its own once the Foundational phase is done.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story from spec.md (US1–US6)

## Conventions that apply to every task

- Package is `invio` (issue's `scout/llm/...` → `src/invio/llm/...`). Work on branch
  `gh-issue-7`.
- Research decisions are referenced as R1–R16 ([research.md](./research.md)); entities, file
  format, errors and log events are in [data-model.md](./data-model.md); exact signatures in
  [contracts/python-api.md](./contracts/python-api.md).
- `invio.llm` may import only `invio.config`, the standard library, Pydantic and PyYAML — never
  `invio.db`, `invio.graph`, `invio.cli`, `invio.services` (R1).
- `src/invio/llm/__init__.py` stays a docstring only — no imports (discovery imports every
  module of the package; eager imports would cause cycles).
- Contract operations are `async def` (R2); tests are plain `async def test_...` functions
  (pytest-asyncio `asyncio_mode = "auto"` is already configured — no marker needed).
- `tests/conftest.py` has an **autouse fixture that deletes every `INVIO_*` variable and
  `chdir`s into `tmp_path`**; build settings in tests with `Settings(_env_file=None, ...)`.
  The `job_data` fixture returns a fresh minimal valid job mapping (validate with
  `JobConfig.model_validate`).
- No credential, prompt text or answer text may appear in any exception message, `repr` or log
  line (FR-004, FR-021). Validation summaries use
  `ValidationError.errors(include_input=False, include_url=False)` (R12).
- Logger is `logging.getLogger("invio.llm")`; the event name is the log message
  (`llm.call`, `llm.repair`, `llm.error`); data goes in `extra=` with the keys from
  data-model.md "Log events". Never use `job` / `run_id` / `level` as extra keys (reserved by
  the JSON formatter).
- Model ids must never appear as string literals in `src/` (FR-017); tests use ids from
  fixture registries only.
- Match existing style: module docstrings, Python 3.12 typing, ruff line length 100,
  `mypy --strict` clean over `src/`, coverage ≥ 95 %.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Branch, settings, packaging and directory scaffolding

- [X] T000 Create and switch to branch `gh-issue-7` from the current `main` (constitution "Development Workflow": work happens on `gh-issue-<N>`, never on `main`); confirm the current branch is `gh-issue-7` before starting any other task
- [X] T001 Add `llm_timeout_seconds: float = Field(default=60.0, gt=0)` under the "# LLM providers" block of `Settings` in src/invio/config/settings.py (env var `INVIO_LLM_TIMEOUT_SECONDS`, rule "`> 0`", default `60.0`); add tests in tests/test_settings.py: default is `60.0`, `INVIO_LLM_TIMEOUT_SECONDS=2.5` is read, `0` and `-1` and `abc` raise `ValidationError` naming `llm_timeout_seconds`
- [X] T002 [P] Add `INVIO_LLM_TIMEOUT_SECONDS=60` (with a one-line comment "per-request LLM call timeout in seconds") to the "LLM providers" section of .env.example (keeps `tests/test_settings.py::test_env_example_keys_are_all_known_settings` green after T001)
- [X] T003 [P] Add `"if TYPE_CHECKING:"` to `[tool.coverage.report] exclude_also` in pyproject.toml (R15 — the FakeProvider protocol check block is type-check only)
- [X] T004 [P] Replace the docstring of src/invio/llm/__init__.py with a short description of the package modules (base, registry, factory, fake, models.d) — no imports; create src/invio/llm/models.d/README.md documenting the registry file format from data-model.md "Registry file" (`schema_version: 1`, `provider` = file stem, `models: {<id>: {input_price_per_mtok, output_price_per_mtok, context_window}}`, USD per 1M tokens) and stating that only `*.yaml` files are loaded

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Contract types, errors, shared helpers, registry core and the fake provider that
every story and its tests depend on

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T005 Create src/invio/llm/base.py with: `T = TypeVar("T", bound=BaseModel)`; `@dataclass(frozen=True, slots=True) class Usage(input_tokens: int, output_tokens: int, requests: int = 1)` validating in `__post_init__` "`input_tokens >= 0`", "`output_tokens >= 0`", "`requests >= 1`" (raise `ValueError`), with `__add__` returning the field-wise sum; and `class LLMProvider(Protocol)` with exactly `async def complete(self, system: str, user: str, *, model: str, temperature: float, max_tokens: int) -> tuple[str, Usage]` and `async def complete_structured(self, system: str, user: str, schema: type[T], *, model: str, temperature: float) -> tuple[T, Usage]` (R3, R4)
- [X] T006 Add the error hierarchy to src/invio/llm/base.py (R5, data-model.md "Errors"): `LLMError(Exception)` with `__init__(self, message: str, *, provider: str | None = None, model: str | None = None)` storing both attributes; subclasses `LLMRateLimitError`, `LLMAuthError`, `LLMUnavailableError`; `LLMInvalidOutputError(LLMError)` with extra keyword args/attributes `errors: str` and `usage: Usage`; `LLMConfigError(ValueError)`; `ModelRegistryError(LLMConfigError)`
- [X] T007 Add `async def with_timeout(awaitable: Awaitable[R], *, seconds: float, provider: str, model: str) -> R` to src/invio/llm/base.py: run inside `asyncio.timeout(seconds)`; convert `TimeoutError` into `LLMUnavailableError(f"LLM provider '{provider}' did not answer within {seconds:g} s (model {model})", provider=provider, model=model)`; let every other exception propagate (R13)
- [X] T008 Add `RawRequest = Callable[[str, str], Awaitable[tuple[str, Usage]]]` and `async def structured_with_repair(request: RawRequest, system: str, user: str, schema: type[T]) -> tuple[T, Usage]` to src/invio/llm/base.py per R12: append a JSON instruction containing `json.dumps(schema.model_json_schema())` to the system message; strip a single surrounding Markdown code fence (leading ```` ``` ```` or ```` ```json ```` line and trailing ```` ``` ````); validate with `schema.model_validate_json()`; on `ValidationError` send exactly one repair request whose user message is the original user message + the previous answer + the validation problems + "Return only the corrected JSON."; on a second failure raise `LLMInvalidOutputError("structured output for <SchemaName> is invalid after one repair attempt: <errors>", errors=..., usage=<sum>)`; return `(value, usage_total)` where usage is summed with `+` (so `requests` is 1 or 2); format problems as one line per error `"<loc joined by '.'>: <msg>"` from `errors(include_input=False, include_url=False)`, using `(root)` for an empty `loc`; provider errors from `request` propagate unchanged
- [X] T009 [P] Create src/invio/llm/registry.py (R8, R10 core, data-model.md "ModelInfo"/"ModelRegistry"/"Registry file"): `@dataclass(frozen=True, slots=True) class ModelInfo(model_id, provider, input_price_per_mtok: Decimal, output_price_per_mtok: Decimal, context_window: int)`; private strict Pydantic file models (`ConfigDict(extra="forbid", frozen=True)`) — `_ModelEntry(input_price_per_mtok: Decimal = Field(ge=0), output_price_per_mtok: Decimal = Field(ge=0), context_window: int = Field(gt=0))`, `_RegistryFile(schema_version: Literal[1], provider: str = Field(min_length=1), models: dict[str, _ModelEntry])` with model ids "non-empty"; `class ModelRegistry` (immutable mapping) with `get(model_id) -> ModelInfo | None` and `model_ids() -> frozenset[str]`; `load_registry(dirs: Sequence[Path] | None = None) -> ModelRegistry` reading only `*.yaml` (sorted) from each dir (default: `Path(p) / "models.d"` for each `p` in `invio.llm.__path__`), treating a missing directory as empty, wrapping YAML/validation errors in `ModelRegistryError` naming the file (and model/field from the Pydantic loc); `@functools.cache def default_registry() -> ModelRegistry` returning `load_registry()`. Load YAML with `yaml.safe_load` into `object` and validate — no `Any`
- [X] T010 [P] Create src/invio/llm/fake.py (R15, contracts "invio.llm.fake"): frozen dataclasses `FakeReply(text: str, usage: Usage = Usage(10, 5))`, `FakeDelay(seconds: float, then: FakeReply)`, `FakeRequest(system, user, model, temperature, max_tokens: int | None)`; `FakeStep = FakeReply | FakeDelay | LLMError`; `class FakeScriptExhaustedError(AssertionError)`; `class FakeProvider` with `__init__(self, script: Iterable[FakeStep], *, timeout_seconds: float = 60.0, name: str = "fake")`, public `requests: list[FakeRequest]`, `@classmethod from_settings(cls, settings: Settings) -> Self` (empty script, `timeout_seconds=settings.llm_timeout_seconds`), a private `_request(system, user, *, model, temperature, max_tokens)` that records the request, pops the next step (exhausted → `FakeScriptExhaustedError("FakeProvider script exhausted after N requests")`), raises `LLMError` steps, and returns replies — `FakeDelay` via `await asyncio.sleep(seconds)` — all wrapped in `with_timeout(..., seconds=self._timeout, provider=self._name, model=model)`; `complete()` returns `_request(...)`; `complete_structured()` returns `await structured_with_repair(lambda s, u: self._request(s, u, model=model, temperature=temperature, max_tokens=None), system, user, schema)`; end the module with `if TYPE_CHECKING: _check: type[LLMProvider] = FakeProvider`
- [X] T011 Create test support (depends on T009, T010): tests/llm_helpers.py with a Pydantic `Score(BaseModel)` schema (`score: float = Field(ge=0, le=1)`, `reason: str`), `VALID_SCORE_JSON`, helper `write_registry(dir: Path, provider: str, models: dict[str, dict[str, object]]) -> Path` that writes a `models.d/<provider>.yaml` file, and `make_settings(**kwargs) -> Settings` (`Settings(_env_file=None, **kwargs)`); tests/fixtures/llm/models.d/fakeco.yaml (`schema_version: 1`, `provider: fakeco`, one priced model e.g. input 2.50 / output 10.00 per 1M, context 128000, and one zero-priced model) and tests/fixtures/llm/models.d/other.yaml (second provider, one model)
- [X] T012 [P] Write tests/test_llm_base.py: `Usage` rejects negative tokens and `requests < 1`, `Usage(1, 2) + Usage(3, 4) == Usage(4, 6, 2)`, default `requests == 1`; every error class's inheritance (`LLMRateLimitError`/`LLMAuthError`/`LLMUnavailableError`/`LLMInvalidOutputError` are `LLMError`; `ModelRegistryError` is `LLMConfigError` is `ValueError`) and `provider`/`model` attributes; `with_timeout` returns the value when fast, raises `LLMUnavailableError` naming provider, model and seconds when `asyncio.sleep(1)` exceeds `seconds=0.01`, and re-raises a non-timeout exception unchanged

**Checkpoint**: Foundation ready — contract, errors, helpers, registry core and fake exist.

---

## Phase 3: User Story 1 - Pipeline asks for a model by role and gets text or validated data back (Priority: P1) 🎯 MVP

**Goal**: `resolve(llm_config, role)` returns the configured provider and the role's model,
checked against the registry; the provider returns text or validated data plus usage.

**Independent Test**: Register `FakeProvider` under `mistral` in a patched registry, use a
fixture registry declaring the job's models as `mistral` models, resolve `fast` and `smart`
from a `JobConfig`, run `complete` and `complete_structured`, and compare text, value and
usage with the script.

### Tests for User Story 1

- [X] T013 [P] [US1] Add fixtures to tests/conftest.py: `llm_registry_dir(tmp_path)` that writes `models.d/mistral.yaml` declaring the `fast` and `smart` model ids used by the `job_data` fixture (read them from `job_data["llm"]["models"]`; set `job_data["llm"]["provider"] = "mistral"`); `patched_providers(monkeypatch)` that replaces `invio.llm.factory._REGISTRY` with an empty dict, marks discovery as done, and yields a helper `register(name, provider_instance)` that registers a class whose `from_settings` returns the given `FakeProvider` instance
- [X] T014 [US1] Write resolution tests in tests/test_llm_factory.py (AS1–AS4, FR-012, FR-013): `fast` → (provider, `models.fast`), `smart` → (same provider, `models.smart`); `complete()` through the resolved provider returns the scripted text and `Usage`; `complete_structured(..., Score, ...)` returns a `Score` and usage; role `"medium"` → `LLMConfigError` whose message contains `medium`, `fast` and `smart`; model id absent from the registry → `LLMConfigError` naming model and provider; model registered for another provider (`other.yaml`) → `LLMConfigError` naming model and provider; registry error is reported even when the provider's key is missing (model check before provider construction, R11)

### Implementation for User Story 1

- [X] T015 [US1] Create src/invio/llm/factory.py (R7, R11, contracts "invio.llm.factory"): `Role = Literal["fast", "smart"]`; `class RegisteredProvider(LLMProvider, Protocol)` with `@classmethod def from_settings(cls, settings: Settings) -> Self`; module-level `_REGISTRY: dict[str, type[RegisteredProvider]] = {}`; `register_provider(name: str)` class decorator that stores the class (duplicate handling in US5); `get_provider(name: str, settings: Settings | None = None, *, registry: ModelRegistry | None = None) -> LLMProvider` that calls `_discover()` (stub returning immediately until US5), looks up the name (unknown → `LLMConfigError(f"unknown LLM provider '{name}'; registered: {', '.join(sorted(_REGISTRY)) or 'none'}")`) and returns `cls.from_settings(settings or get_settings())`; `resolve(llm_config: LLMConfig, role: Role, settings: Settings | None = None, *, registry: ModelRegistry | None = None) -> tuple[LLMProvider, str]` that validates `role in ("fast", "smart")` at runtime, takes `getattr(llm_config.models, role)`, checks `(registry or default_registry()).get(model)` exists and `info.provider == llm_config.provider.value` (else `LLMConfigError(f"model '{model}' is not registered for LLM provider '{provider}'")`), then calls `get_provider(llm_config.provider.value, settings, registry=registry)`
- [X] T016 [US1] Run `uv run pytest tests/test_llm_factory.py tests/test_llm_base.py` and `uv run mypy` and fix failures

**Checkpoint**: Pipeline code can resolve a role and call a (fake) provider — MVP.

---

## Phase 4: User Story 2 - Malformed structured answers are repaired once, then rejected (Priority: P1)

**Goal**: Exactly one repair request for invalid structured output, then
`LLMInvalidOutputError` carrying the final problems and the summed usage.

**Independent Test**: Script `FakeProvider` with invalid→valid and invalid→invalid answers and
count requests and inspect the repair request.

### Tests for User Story 2

- [X] T017 [P] [US2] Write tests/test_llm_repair.py using `FakeProvider` and `Score` (AS1–AS4, FR-005–FR-007): valid first answer → 1 request, `usage.requests == 1`; invalid (`{"score": 5, "reason": "x"}`) then valid → exactly 2 requests, the second request's `user` contains the original user message, the previous answer and the problem line `score: ...`, returned usage equals the sum of both scripted usages with `requests == 2`; invalid then invalid → `LLMInvalidOutputError` after exactly 2 requests with `errors` describing the second answer's problem and `usage` summed; plain prose answer (`"The score is high"`) is treated as invalid and repaired once; answer wrapped in a ```` ```json ```` fence validates without repair; system message of both requests contains the JSON schema instruction; `LLMRateLimitError` scripted as the second step propagates unchanged (not `LLMInvalidOutputError`); `FakeDelay` exceeding a tiny `timeout_seconds` on the repair request raises `LLMUnavailableError`; neither the exception message nor `errors` contains the invalid answer text (use a distinctive token like `SECRET_ANSWER_TOKEN` in an invalid field value)

### Implementation for User Story 2

- [X] T018 [US2] In `structured_with_repair` (src/invio/llm/base.py) log one WARNING `llm.repair` on `logging.getLogger("invio.llm")` before the repair request with `extra={"schema": schema.__name__, "errors": <problem summary>}` (data-model.md "Log events"; no answer text); add a test to tests/test_llm_repair.py using `caplog` asserting exactly one `llm.repair` record per repair and no answer text in it
- [X] T019 [US2] Run `uv run pytest tests/test_llm_repair.py` and fix failures

**Checkpoint**: Repair behaviour is uniform and fully tested.

---

## Phase 5: User Story 3 - Provider failures surface as clear, typed errors (Priority: P1)

**Goal**: Missing keys raise `LLMAuthError` naming the env var at `get_provider` time;
provider failures and timeouts reach callers as the shared typed errors; no secrets leak.

**Independent Test**: Request a key-requiring provider with an empty key setting; script the
fake to raise each typed error and to exceed the timeout.

### Tests for User Story 3

- [X] T020 [P] [US3] Add credential tests to tests/test_llm_base.py (AS1, AS4, FR-011): `require_api_key(make_settings(), "mistral")` raises `LLMAuthError` whose message contains `mistral` and `INVIO_MISTRAL_API_KEY` and whose `provider == "mistral"`; an empty key (`mistral_api_key=""`) and a whitespace-only key (`mistral_api_key="   "`) each raise the same `LLMAuthError` naming `INVIO_MISTRAL_API_KEY`; a set key `"sk-test-SECRET123"` is returned and never appears in `repr(settings)`; for every `LLMError` subclass constructed by the LLM layer in tests, `"sk-test-SECRET123"` is not in `str(err)`
- [X] T021 [P] [US3] Add tests to tests/test_llm_factory.py (AS1–AS3): a test provider class registered via `patched_providers` whose `from_settings` calls `require_api_key(settings, "mistral")` → `get_provider("mistral", make_settings())` raises `LLMAuthError` naming `INVIO_MISTRAL_API_KEY`; with the key set it is returned; a keyless provider class (no `require_api_key` call, like Ollama) is returned without any key configured; `FakeProvider` scripted with `LLMRateLimitError`, `LLMAuthError`, `LLMUnavailableError` → `complete()` through `get_provider` raises exactly that type; `FakeDelay(1, ...)` with `timeout_seconds=0.01` raises `LLMUnavailableError` (FR-022); creating `Settings` does not raise when no key is set (keys are checked only at use)

### Implementation for User Story 3

- [X] T022 [US3] Add `def require_api_key(settings: Settings, provider: str) -> str` to src/invio/llm/base.py (R6): call `settings.require_secret(f"{provider}_api_key")` (cast the name to `SecretName` after checking it is one of `get_args(SecretName)`, else raise `LLMConfigError(f"LLM provider '{provider}' has no API key setting")`); convert `MissingSettingError` into `LLMAuthError(f"LLM provider '{provider}' needs an API key: set {err.env_var}", provider=provider)` raised `from None`; additionally treat a returned key that is empty after `.strip()` as missing and raise the same `LLMAuthError` (`require_secret` only rejects `None` and `""` — research R6 note); otherwise return the key unchanged
- [X] T023 [US3] Run `uv run pytest tests/test_llm_base.py tests/test_llm_factory.py` and fix failures

**Checkpoint**: Failures are typed, actionable and secret-free.

---

## Phase 6: User Story 4 - Token cost is computed from a central model registry (Priority: P2)

**Goal**: Registry files merge into one strict registry; cost per call is exact and quantized.

**Independent Test**: Load fixture registries and compare computed costs with hand-calculated
values; feed malformed and conflicting files and check the errors.

### Tests for User Story 4

- [X] T024 [P] [US4] Write tests/test_llm_registry.py (AS1–AS5, FR-014–FR-016, SC-004): loading `tests/fixtures/llm/models.d` contains the models of both files with correct `ModelInfo` values; cost for the priced model with `Usage(1_000_000, 1_000_000)` equals `input + output` price exactly, with `Usage(1234, 567)` equals the hand-calculated value quantized to `Decimal("0.000001")` (`ROUND_HALF_UP`), with `Usage(0, 0)` is `Decimal("0.000000")`, with `Usage(10**12, 10**12)` is exact; zero-priced model → `Decimal("0.000000")`; unknown model → `None`; price given as YAML float `0.1` becomes `Decimal("0.1")`; errors (each a `ModelRegistryError` whose message names the file, and model/field where applicable): unknown key in an entry, missing `context_window`, negative price, `context_window: 0`, `schema_version: 2`, invalid YAML, `provider` ≠ file stem, same model id in two files (message names the id and both files); missing directory and a directory with only `README.md` → empty registry; `default_registry()` is cached and `cache_clear()` reloads
- [X] T025 [US4] Add `test_no_model_ids_in_source` to tests/test_llm_registry.py (FR-017, SC-005): load the shipped registry (`load_registry()` default dirs) and assert no model id occurs as a quoted string literal (`'id'` or `"id"`) in any `src/invio/**/*.py` file

### Implementation for User Story 4

- [X] T026 [US4] Add `def cost(self, model_id: str, usage: Usage) -> Decimal | None` to `ModelRegistry` in src/invio/llm/registry.py (R10): `None` if unknown, else `((usage.input_tokens * info.input_price_per_mtok + usage.output_tokens * info.output_price_per_mtok) / Decimal(1_000_000)).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)`; convert YAML float prices via `Decimal(str(value))` before validation so `0.1` stays exact (field validator `mode="before"`)
- [X] T027 [US4] Add the cross-file checks to `load_registry` in src/invio/llm/registry.py (FR-015): `provider` must equal the file stem (`ModelRegistryError(f"{file}: provider '{p}' does not match file name '{stem}'")`); a model id already loaded from another file raises `ModelRegistryError(f"model '{id}' is defined in both {first} and {second}")`; ensure every validation error message names the file and the Pydantic loc (e.g. `models.<id>.context_window`)
- [X] T028 [US4] Run `uv run pytest tests/test_llm_registry.py` and fix failures

**Checkpoint**: Costs are computable for any registered model.

---

## Phase 7: User Story 5 - Developers add a provider without touching shared code (Priority: P2)

**Goal**: Provider modules self-register and are discovered automatically; duplicates and
unknown names fail clearly.

**Independent Test**: Drop a provider module and its registry file into a temporary package
directory and request it by name without changing repository files.

### Tests for User Story 5

- [X] T029 [P] [US5] Add discovery tests to tests/test_llm_factory.py (AS1–AS3, FR-008–FR-010, SC-001): `test_new_provider_module_is_discovered` writes `tmp_path/pkg/acme.py` (a class decorated with `@register_provider("acme")` implementing `from_settings`, `complete`, `complete_structured`) and `tmp_path/pkg/models.d/acme.yaml` (one model), monkeypatches `invio.llm.__path__` to `[str(tmp_path / "pkg"), *invio.llm.__path__]`, resets `invio.llm.factory._REGISTRY` (copy) and the discovery flag, calls `default_registry.cache_clear()`, then asserts `get_provider("acme", make_settings())` returns a working provider and `default_registry().cost("<acme model>", Usage(1000, 1000))` is a `Decimal`; teardown removes `invio.llm.acme` from `sys.modules` and clears the registry cache again; registering two classes under the same name raises `LLMConfigError` naming the name; `get_provider("nope")` raises `LLMConfigError` naming `nope` and listing registered names sorted; modules whose name starts with `_` are not imported; discovery runs only once (second `get_provider` call does not re-import)

### Implementation for User Story 5

- [X] T030 [US5] Implement discovery and duplicate protection in src/invio/llm/factory.py (R7): `register_provider` raises `LLMConfigError(f"LLM provider '{name}' is registered twice ({existing.__module__}, {cls.__module__})")` on duplicates; `_discovered: bool` module flag; `_discover()` iterates `pkgutil.iter_modules(invio.llm.__path__)`, skips names starting with `_`, imports `f"invio.llm.{name}"` via `importlib.import_module`, then sets the flag; `get_provider` calls it before lookup
- [X] T031 [US5] Run `uv run pytest tests/test_llm_factory.py` and fix failures (including test isolation: no registration leaks between tests)

**Checkpoint**: New providers plug in with two new files and no shared edits.

---

## Phase 8: User Story 6 - Tests run against a scripted fake provider (Priority: P2)

**Goal**: A scripted, recording fake that mypy verifies against the contract.

**Independent Test**: Script the fake with answers, errors and delays and inspect returned
values and recorded requests; run mypy.

### Tests for User Story 6

- [X] T032 [P] [US6] Write tests/test_llm_fake.py (AS1–AS4, FR-018): answers A then B are returned in order with their scripted `Usage`; `requests` records `system`, `user`, `model`, `temperature`, `max_tokens` for `complete()` and `max_tokens is None` for `complete_structured()`; an exhausted script raises `FakeScriptExhaustedError` (an `AssertionError`) with the request count in the message; a scripted `LLMUnavailableError` instance is raised as-is; `FakeDelay(0.001, reply)` with the default timeout returns the reply; `FakeDelay(1, reply)` with `timeout_seconds=0.01` raises `LLMUnavailableError` naming the fake's `name`; `FakeProvider.from_settings(make_settings(llm_timeout_seconds=5))` has an empty script and the configured timeout

### Implementation for User Story 6

- [X] T033 [US6] Run `uv run mypy` and confirm the `TYPE_CHECKING` protocol check in src/invio/llm/fake.py passes; temporarily break `FakeProvider.complete`'s signature locally to confirm mypy reports it, then revert (do not commit the break); fix any failures from `uv run pytest tests/test_llm_fake.py`

**Checkpoint**: All six user stories are functional and independently tested.

---

## Phase 9: Cross-Cutting — Call logging and input validation (FR-019–FR-021)

**Purpose**: One provider-independent wrapper returned by `get_provider` that validates inputs
and logs every call (R14)

- [X] T034 Write tests/test_llm_logging.py using `caplog` (logger `invio.llm`) and a `FakeProvider` registered via `patched_providers` with the `fakeco` fixture registry passed as `registry=`: one successful `complete()` → exactly one INFO record `llm.call` with `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd` (string equal to the registry cost), `duration_ms` (>= 0), `repaired is False`; a structured call with one repair → one `llm.call` with `repaired is True` and summed tokens, plus one `llm.repair` warning; model not in the registry → `cost_usd is None`; a scripted `LLMRateLimitError` → exactly one WARNING `llm.error` with `error == "LLMRateLimitError"`, `provider`, `model`, `duration_ms`, and the error re-raised; `temperature=-0.1`, `temperature=2.1` and `max_tokens=0` raise `ValueError` with zero requests recorded by the fake; no log record (message or any extra value) contains the prompt text, the answer text or a configured API key (use distinctive tokens)
- [X] T035 Implement `_LoggedProvider` in src/invio/llm/factory.py (R14, data-model.md "Log events"): wraps a provider with `name` and `registry`; satisfies `LLMProvider`; validates "`0 <= temperature <= 2`" and "`max_tokens >= 1`" (raise `ValueError` before delegating); times each call with `time.perf_counter()`; on success logs INFO `llm.call` with `extra={"provider", "model", "input_tokens", "output_tokens", "cost_usd": str(cost) if cost is not None else None, "duration_ms": round(ms, 1), "repaired": usage.requests > 1}`; on `LLMError` logs WARNING `llm.error` with `extra={"error": type(err).__name__, "provider", "model", "duration_ms"}` and re-raises; make `get_provider` return `_LoggedProvider(cls.from_settings(...), name=name, registry=registry or default_registry())`; update `resolve` tests if they assert on the concrete type
- [X] T036 Run `uv run pytest tests/test_llm_logging.py tests/test_llm_factory.py` and fix failures

---

## Phase 10: Polish & Cross-Cutting Concerns

**Purpose**: Documentation and full quality gates

- [X] T037 [P] Update README.md: add an "LLM layer" developer section (contract `complete`/`complete_structured`, roles `fast`/`smart`, `resolve`, typed errors, one repair attempt, registry files in `src/invio/llm/models.d/<provider>.yaml` with the format, cost in USD, `FakeProvider` for tests, how to add a provider: one module with `@register_provider("<name>")` + one registry file — and that a provider beyond the five in the job schema also needs the job schema's provider list extended); document `INVIO_LLM_TIMEOUT_SECONDS` (default 60) in the settings/configuration section; update the Layout block (`llm/  base, registry, factory, fake, models.d/`)
- [X] T038 [P] Verify the LLM layer dependency rule (R1): `grep -rn "from invio\.\(db\|graph\|cli\|services\)\|import invio\.\(db\|graph\|cli\|services\)" src/invio/llm/` returns nothing; verify no `Any` and no unjustified `# type: ignore` in src/invio/llm/
- [X] T039 Run all CI gates and fix failures: `uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest` (coverage ≥ 95 %)
- [X] T040 Run the scenarios in specs/004-gh-issue-7/quickstart.md (sections 1–4) and confirm the expected outputs, including the smoke check printing `score=0.8 Usage(input_tokens=22, output_tokens=8, requests=2) 2`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies. T000 first (branch); then T002–T004 parallel; T001 before T010 (`from_settings` reads `llm_timeout_seconds`).
- **Foundational (Phase 2)**: After Setup. T005 → T006 → T007 → T008 (same file, sequential); T009 and T010 parallel after T008 (T010 needs T007/T008); T011 after T009/T010; T012 after T007. BLOCKS all stories.
- **US1 (Phase 3)**: After Foundational. Creates `factory.py`.
- **US2 (Phase 4)**: After Foundational (uses `FakeProvider` directly; independent of US1).
- **US3 (Phase 5)**: After Foundational; T021 needs `factory.py` from US1 (T015).
- **US4 (Phase 6)**: After Foundational (registry only; independent of US1–US3).
- **US5 (Phase 7)**: After US1 (extends `factory.py`) and benefits from US4 (cost assertion).
- **US6 (Phase 8)**: After Foundational (independent).
- **Cross-cutting logging (Phase 9)**: After US1 and US4 (needs `get_provider` and `cost`); best after US2 (repair flag).
- **Polish (Phase 10)**: After all phases.

### User Story Dependencies

- **US1 (P1)**: Foundational only — MVP.
- **US2 (P1)**: Foundational only.
- **US3 (P1)**: Foundational; factory tests reuse US1's `factory.py`.
- **US4 (P2)**: Foundational only.
- **US5 (P2)**: US1 (factory), US4 (cost in extensibility test).
- **US6 (P2)**: Foundational only.

### Within Each User Story

- Tests are written first and must fail before the implementation task.
- Same-file tasks run sequentially (`base.py`: T005–T008, T018, T022; `factory.py`: T015, T030, T035; `registry.py`: T009, T026, T027; `tests/test_llm_factory.py`: T014, T021, T029).

### Parallel Opportunities

- Setup: T002, T003, T004.
- Foundational: T009 ∥ T010 (after T008); T012 ∥ T011.
- After Foundational: US2 (T017–T019), US4 (T024–T028) and US6 (T032–T033) can proceed in parallel with US1 — they touch different files (`test_llm_repair.py`/`base.py` logging, `registry.py`/`test_llm_registry.py`, `test_llm_fake.py`).
- Polish: T037 ∥ T038.

---

## Parallel Example: after Foundational

```bash
# Three independent streams, different files:
Task: "T014/T015 [US1] resolution tests + factory.py"                    # tests/test_llm_factory.py, src/invio/llm/factory.py
Task: "T024/T025 [US4] registry tests + T026/T027 cost and checks"       # tests/test_llm_registry.py, src/invio/llm/registry.py
Task: "T032 [US6] fake provider tests"                                   # tests/test_llm_fake.py
```

## Parallel Example: User Story 4

```bash
Task: "T024 [US4] registry load/cost tests in tests/test_llm_registry.py"
Task: "T025 [US4] SC-005 hard-coded model id guard in tests/test_llm_registry.py"   # same file → run after T024
```

---

## Implementation Strategy

### MVP First (User Story 1)

1. Phase 1: Setup → Phase 2: Foundational.
2. Phase 3: US1 — role resolution against the registry and calls through the fake.
3. **STOP and VALIDATE**: `uv run pytest tests/test_llm_factory.py tests/test_llm_base.py`, `uv run mypy`.

### Incremental Delivery

1. Foundation → US1 (MVP: pipeline nodes can be written against the contract).
2. US2 (repair) → US3 (typed errors, missing-key message) — completes all P1 stories.
3. US4 (cost) → US5 (discovery/extensibility) → US6 (fake tests).
4. Phase 9 logging wrapper → Phase 10 docs and gates.

### Parallel Team Strategy

1. Together: Setup + Foundational.
2. Then: Developer A — US1 → US3 → US5 → Phase 9 (factory track); Developer B — US2 → US4 →
   US6 (base/registry/fake tests).

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks.
- Commit after each phase checkpoint; reference issue #7 in commit messages.
- Out of scope (separate issues): concrete providers (Mistral first), retry/back-off on rate
  limits, `fallback_provider`, enforcing `max_llm_tokens_per_run`, persisting `llm_usage` rows,
  any `invio` CLI command.
