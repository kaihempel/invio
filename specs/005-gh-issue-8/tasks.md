---

description: "Task list for the Mistral LLM provider (GitHub issue #8)"
---

# Tasks: Mistral LLM Provider

**Input**: Design documents from `specs/005-gh-issue-8/`

**Prerequisites**: [plan.md](./plan.md), [spec.md](./spec.md), [research.md](./research.md),
[data-model.md](./data-model.md), [contracts/python-api.md](./contracts/python-api.md),
[contracts/cli.md](./contracts/cli.md), [quickstart.md](./quickstart.md)

**Tests**: Included. The issue requires tests with recorded HTTP fixtures plus one optional
live test, and constitution principle III requires a test for every acceptance criterion,
rejection paths included.

**Organization**: Tasks are grouped by user story (US1–US5 from spec.md). Every story builds
on the Foundational phase (Phase 2).

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies on incomplete tasks)
- **[Story]**: User story from spec.md (US1–US5)

## Conventions that apply to every task

- **Branch**: before T001, create `gh-issue-8` from `main` in this worktree (`git switch -c gh-issue-8 main`; the worktree is on `worktree-track-llm`). Do not use the issue's `issue/08-add-mistral-provider`; `gh-issue-N` matches the #6/#7 PRs (plan note 7).
- The package is `invio`: the issue's `scout/llm/mistral.py` becomes `src/invio/llm/mistral.py`.
- Decisions are referenced as R1–R14 ([research.md](./research.md)). Entities, retry policy,
  failure classes and fixture format are in [data-model.md](./data-model.md). Exact signatures
  and the behaviour table are in [contracts/python-api.md](./contracts/python-api.md).
- **SDK facts (R1/R2)**: import `from mistralai.client import Mistral` (3.x). The SDK's HTTP
  stack is **`httpx2`**, not `httpx`. Exception types: `mistralai.client.errors.MistralError`
  (has `status_code`, `headers`, `body`; `SDKError` and `HTTPValidationError` subclass it),
  `errors.NoResponseError`, `errors.ResponseValidationError`, `httpx2.TimeoutException` (a
  subclass of `httpx2.TransportError`, so check it first).
- **Never** use `str(sdk_exc)` in messages: it embeds the response body (R5). Always raise
  invio errors `from None`.
- **Never** set `retry_config` / `retries` on the SDK (R4). invio does all retrying.
- `invio.llm` may import only `invio.config`, the stdlib, Pydantic, `mistralai` and `httpx2`.
  `src/invio/llm/__init__.py` stays docstring-only (no imports).
- Tests are plain `async def test_...` (pytest-asyncio auto mode). `tests/conftest.py` has an
  autouse fixture that removes all `INVIO_*` env vars and `chdir`s to `tmp_path`. Build
  settings with `tests/llm_helpers.make_settings(...)`. Tests must never sleep for real:
  inject a recording `sleep`.
- Gates after each phase: `uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest`.

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Dependencies and test configuration

- [X] T001 Add runtime dependencies `"mistralai>=3.0,<4"` and `"httpx2>=2.13"` to `[project] dependencies` in pyproject.toml, then run `uv lock` to update uv.lock (R1, R2). Verify `uv run python -c "from mistralai.client import Mistral; import httpx2"` works.
- [X] T002 In `[tool.pytest.ini_options]` of pyproject.toml, register marker `"live: real calls to external LLM providers (opt-in: pytest -m live)"` and append `"-m", "not live"` to `addopts`, keeping `--strict-markers` (R13). Verify that `uv run pytest -m live --collect-only -q` selects live tests and plain `uv run pytest` deselects them.

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared error additions, provider skeleton and the offline HTTP test harness that every story uses

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T003 [P] Write tests in tests/test_llm_base.py. Cover `LLMRateLimitError("m", provider="p", model="x", retry_after=2.5).retry_after == 2.5`, the default `retry_after is None`, and `ValueError` when `retry_after` is negative or non-finite. Also cover the new `LLMInvalidRequestError("m", provider="p", model="x", status=422)`: it is an `LLMError` but not an `LLMUnavailableError`, and `.status == 422`. Existing positional/keyword call sites must keep working (data-model "Shared errors").
- [X] T004 Implement both additions in src/invio/llm/base.py. Give `LLMRateLimitError` its own `__init__(message, *, provider=None, model=None, retry_after: float | None = None)` that validates "must be finite and ≥ 0". Add `class LLMInvalidRequestError(LLMError)` with docstring "The provider rejected the request itself (malformed input, unknown model, unsupported schema)." and keyword `status: int | None = None`. Update the module docstring's error list. Make T003 pass.
- [X] T005 [P] Create tests/mistral_helpers.py with:
  - (a) `load_fixture(name) -> httpx2.Response`, reading `tests/fixtures/mistral/<name>.json` (`{"status", "headers", "body"}`; `body` is a JSON object or a raw string).
  - (b) `class Recorder`, a callable `httpx2.MockTransport` handler that replays a queue of responses or exceptions in order (an `Exception` instance is raised, e.g. `httpx2.ConnectError("x")`) and records each `httpx2.Request` as `(method, url.path, json body, has Authorization header)`. It fails clearly when the queue is exhausted.
  - (c) `make_provider(*responses, retry=RetryPolicy(), timeout_seconds=60.0) -> tuple[MistralProvider, Recorder, list[float]]`, which builds `MistralProvider("sk-test-SECRET123", timeout_seconds=..., retry=..., client_factory=<counting factory returning httpx2.AsyncClient(transport=httpx2.MockTransport(recorder))>, sleep=<records waits, returns immediately>, uniform=lambda a, b: 0.0)`. The third element is the list of recorded waits. Expose the factory call count (e.g. `recorder.clients_created`) for the FR-027 tests. Runnable only after T007 (it imports `MistralProvider`/`RetryPolicy`); it can be written in parallel.
- [X] T006 [P] Create the recorded fixtures under tests/fixtures/mistral/ in the data-model format, scrubbed of real ids and keys:
  - `chat_ok.json`: 200, content `"Hello"`, usage prompt 12 / completion 3.
  - `chat_ok_chunks.json`: content as a list of `{"type":"text","text":...}` chunks.
  - `chat_no_usage.json`: no `usage`, or usage without counts.
  - `chat_empty_choices.json`: `choices: []`.
  - `structured_ok.json`: content is `{"score": 0.8, "reason": "fits"}` as a string, matching `tests/llm_helpers.Score`; check that model's fields and adapt.
  - `structured_invalid.json`: content is a JSON string violating `Score`.
  - `error_401.json`, `error_403.json`: body `{"message": "Unauthorized"}`.
  - `error_429.json`: no `Retry-After`.
  - `error_429_retry_after.json`: `Retry-After: 7`.
  - `error_429_retry_after_long.json`: `Retry-After: 120`.
  - `error_400.json`, `error_404_model.json`: `{"message": "Invalid model: x"}`.
  - `error_422.json`: FastAPI-style `{"detail": [{"loc": ["body","messages"], "msg": "field required", "type": "missing", "input": "PROMPT-ECHO-SENTINEL"}]}`.
  - `error_500.json`, `error_503.json`.
  - `malformed_200.json`: 200 with body `{"unexpected": true}`.
- [X] T007 Create src/invio/llm/mistral.py with the module docstring and `RetryPolicy` (frozen, slots). Fields: `max_retries: int = 3` ("≥ 0; attempts = max_retries + 1"), `base_delay: float = 1.0` ("> 0"), `jitter: float = 0.25` ("0 ≤ jitter < 1"), `max_retry_after: float = 60.0` ("> 0"). `__post_init__` raises `ValueError` naming the field. Also add `@register_provider("mistral") class MistralProvider` with the constructor from contracts/python-api.md (api_key, timeout_seconds, retry, client_factory, server_url, sleep, uniform, now). Add a private `_client_for_loop() -> Mistral` (R15, FR-027). It compares `asyncio.get_running_loop()` with the loop the cached client was built for. On first use or a loop mismatch it builds `Mistral(api_key=..., async_client=self._client_factory(), server_url=..., timeout_ms=int((timeout_seconds + 5) * 1000))` without `retry_config` (R4, R7), with `client_factory` defaulting to `lambda: httpx2.AsyncClient(follow_redirects=True)`. It closes the previous `httpx2.AsyncClient` with best effort only if its loop is still open; otherwise it drops it. Never build the client in `__init__`. Add `from_settings(settings)` using `require_api_key(settings, "mistral")` and `settings.llm_timeout_seconds`, plus `__repr__` without the key. Add stub `complete`/`complete_structured` raising `NotImplementedError` and the `if TYPE_CHECKING: _check: type[LLMProvider] = MistralProvider` assertion.
- [X] T008 Add tests in tests/test_llm_mistral.py for the skeleton:
  - `RetryPolicy` rejects each invalid field value with a message naming the field.
  - `from_settings` without a key raises `LLMAuthError` matching `INVIO_MISTRAL_API_KEY`, and with a blank key too.
  - `from_settings(make_settings(mistral_api_key="sk-test-SECRET123", llm_timeout_seconds=5))` builds a provider whose `repr` does not contain the key.
  - `invio.llm.factory.get_provider("mistral", settings)` returns a provider. This uses real discovery, so call `factory._discover()` without the `patched_providers` fixture.
  - Constructing the provider creates no HTTP client (factory count 0). The FR-027 lifecycle tests are in T009, because they need a working `complete`.

**Checkpoint**: The foundation is ready, and `uv run mypy` confirms `MistralProvider` matches the protocol shape.

---

## Phase 3: User Story 1 - Pipeline gets text and token usage from Mistral (Priority: P1) 🎯 MVP

**Goal**: `complete()` returns the answer text and the exact `Usage` reported by Mistral.

**Independent Test**: Replay `chat_ok.json` through the MockTransport and check the text, the usage and the sent request body.

### Tests for User Story 1

- [X] T009 [US1] Add US1 tests to tests/test_llm_mistral.py:
  - (1) `chat_ok` → `("Hello", Usage(12, 3))`, `usage.requests == 1`.
  - (2) The recorded request is `POST /v1/chat/completions`. Its JSON body has `model`, `temperature`, `max_tokens` and `messages == [{"role":"system","content":sys},{"role":"user","content":usr}]` (compare the relevant keys), there is no `response_format`, and the Authorization header is present.
  - (3) `chat_ok_chunks` → the concatenated text.
  - (4) `chat_no_usage` → `Usage(0, 0)`.
  - (5) `chat_empty_choices` → `LLMUnavailableError` with `provider == "mistral"` and the model, after exactly 1 request.
  - (6) `malformed_200` → `LLMUnavailableError`, 1 request.
  - (7) FR-027: in a plain **sync** test, build one provider via `make_provider(chat_ok, chat_ok)` and call `asyncio.run(provider.complete(...))` twice. Both return `"Hello"`, and the client factory was called exactly twice.
  - (8) Two `complete` calls inside one async test → the factory was called once (client reused within a loop).

### Implementation for User Story 1

- [X] T010 [US1] Implement in src/invio/llm/mistral.py:
  - A private `_attempt(model, messages, *, temperature, max_tokens, response_format) -> tuple[str, Usage]`. It calls `self._client_for_loop().chat.complete_async(model=..., messages=..., temperature=..., max_tokens=... (omit when None), response_format=... (omit when None))` wrapped in `with_timeout(..., seconds=self.timeout_seconds, provider="mistral", model=model)`.
  - Text extraction (R9): `choices[0].message.content`, either a `str` or the concatenated `.text` of `TextChunk` items. A missing or empty result raises `LLMUnavailableError("Mistral returned no answer text (model …)")`.
  - Usage: `Usage(prompt_tokens or 0, completion_tokens or 0)`, with a missing `usage` giving zeros.
  - `complete()`: build the system/user messages and call `_attempt` through the request path (single attempt for now; T018 adds the retry loop). Map `errors.ResponseValidationError` to `LLMUnavailableError` without retry. Make T009 pass.

**Checkpoint**: Free-text completion works against recorded responses (MVP).

---

## Phase 4: User Story 2 - Pipeline gets validated structured data from Mistral (Priority: P1)

**Goal**: `complete_structured()` requests strict JSON-schema output and returns a validated Pydantic value through the shared repair procedure.

**Independent Test**: Replay `structured_ok.json`; replay `structured_invalid` then `structured_ok`; replay `structured_invalid` twice.

### Tests for User Story 2

- [X] T011 [US2] Add US2 tests to tests/test_llm_mistral.py using `tests/llm_helpers.Score`:
  - (1) `structured_ok` → a `Score` instance with the expected values, `Usage.requests == 1`.
  - (2) The request body has `response_format == {"type": "json_schema", "json_schema": {"name": "Score", "schema": Score.model_json_schema(), "strict": True}}`, compared by keys, and no `max_tokens`.
  - (3) invalid then ok → a value, 2 requests recorded, the second request's user message contains the validation problem, `usage.requests == 2` and the tokens are summed.
  - (4) invalid twice → `LLMInvalidOutputError` with summed usage and `requests == 2`.

### Implementation for User Story 2

- [X] T012 [US2] Implement `complete_structured` in src/invio/llm/mistral.py (R8). Define an inner `request(system_text, user_text)` that sends both messages with `response_format={"type": "json_schema", "json_schema": {"name": schema.__name__, "schema": schema.model_json_schema(), "strict": True}}` and `max_tokens=None` through the same request path as `complete` (so later retries apply per request). Return `await structured_with_repair(request, system, user, schema)`. Make T011 pass.

**Checkpoint**: US1 and US2 both work.

---

## Phase 5: User Story 3 - Mistral failures surface as the common typed errors, with retries for transient ones (Priority: P1)

**Goal**: Status, timeout and connection mapping (R5), bounded retries with backoff and `Retry-After` (R6), sanitized messages and `llm.retry` logs (R14).

**Independent Test**: Replay error fixtures and transport exceptions; assert the error type, the number of requests, the recorded waits and the log lines, with no real sleeping.

### Tests for User Story 3

- [X] T013 [P] [US3] Add mapping tests to tests/test_llm_mistral.py (use `RetryPolicy()` defaults unless stated):
  - (1) `error_401` and `error_403` → `LLMAuthError` after exactly 1 request, message contains `INVIO_MISTRAL_API_KEY`.
  - (2) `error_400`, `error_404_model`, `error_422` → `LLMInvalidRequestError` with `.status` set, 1 request, message contains the status and Mistral's message, and for 422 contains `field required` but **not** `PROMPT-ECHO-SENTINEL`.
  - (3) A timeout: use `timeout_seconds=0.01` and a Recorder handler that awaits `asyncio.sleep(1)`, or raise `httpx2.ReadTimeout("x")` → `LLMUnavailableError` naming the timeout, 1 request (no retry).
  - (4) A `MistralError` built with a 10 000-char body → the raised message is ≤ 400 chars and contains no body text beyond the sanitized message.
  - (5) No raised error's `str()` or `repr()` contains `sk-test-SECRET123` or the prompt text.
- [X] T014 [P] [US3] Add retry tests to tests/test_llm_mistral.py:
  - (1) `error_429` ×4 → `LLMRateLimitError` after 4 requests, waits `== [1.0, 2.0, 4.0]` (`uniform` returns 0).
  - (2) `error_503` ×4 → `LLMUnavailableError` naming status 503, 4 requests.
  - (3) `httpx2.ConnectError` ×4 → `LLMUnavailableError`, 4 requests.
  - (4) `error_429` then `chat_ok` → success, `usage.requests == 1`, waits `[1.0]`.
  - (5) `error_500` then `chat_ok` → success.
  - (6) `error_429_retry_after` (7 s) then `chat_ok` → waits `[7.0]`.
  - (7) `error_429_retry_after_long` (120 s) → `LLMRateLimitError` immediately (1 request, no waits), `retry_after == 120`.
  - (8) `error_429_retry_after` ×4 → the final error has `retry_after == 7`.
  - (9) `Retry-After` given as an HTTP date 10 s after the injected `now` → wait 10.0.
  - (10) An unparseable `Retry-After` → computed backoff.
  - (11) Jitter: with `uniform=lambda a, b: b`, the first wait == 1.25.
  - (12) `RetryPolicy(max_retries=0)` with `error_503` → exactly 1 request (proves the SDK does not retry).
  - (13) Structured: `error_503` on the repair request, then `structured_ok` → success (the repair request has its own retry budget).
  - (14) Each retry logs one `llm.retry` warning with `provider`, `model`, `attempt`, `status` or `failure`, and `wait_s`, without prompt text (`caplog`).
  - (15) Cancelling the task during a retry wait (a `sleep` that awaits an event, then `task.cancel()`) → `CancelledError` and no further request.

### Implementation for User Story 3

- [X] T015 [US3] Implement `_retry_after(headers, now) -> float | None` in src/invio/llm/mistral.py (R6). Accept non-negative integer or decimal seconds. Otherwise try `email.utils.parsedate_to_datetime` and take the delta to `now()`, floored at 0. Anything unparseable gives `None`.
- [X] T016 [US3] Implement `_safe_detail(exc: MistralError) -> str` in src/invio/llm/mistral.py (R5). Parse `exc.body` as JSON: use the top-level `"message"` if it is a string, or for `"detail"` lists join `"<loc joined by '.'>: <msg>"` per item, ignoring `input` and `ctx`. Fall back to `""` on any parse problem. Collapse whitespace and truncate to 300 chars.
- [X] T017 [US3] Implement `_classify(exc, model) -> _Failure` in src/invio/llm/mistral.py, where `_Failure` is a private frozen dataclass with `kind`, `retryable`, `retry_after` and `error: LLMError`. Follow the data-model "Failure classification" table:
  - `httpx2.TimeoutException` → timeout, not retryable.
  - Other `httpx2.TransportError` or `errors.NoResponseError` → connection, retryable.
  - `MistralError` 401/403 → `LLMAuthError("Mistral rejected the API key (HTTP 401); check INVIO_MISTRAL_API_KEY")`, not retryable.
  - 429 → `LLMRateLimitError(retry_after=…)`, retryable.
  - ≥ 500 → `LLMUnavailableError`, retryable.
  - Other 4xx → `LLMInvalidRequestError(status=…)`, not retryable.
  - `errors.ResponseValidationError` → bad_response, not retryable.

  Every message names `mistral`, the model, the status and the sanitized detail.
- [X] T018 [US3] Implement the retry loop `_request(...)` in src/invio/llm/mistral.py around `_attempt`, used by both `complete` and the structured `request`:
  - Loop over attempts `1..max_retries+1`.
  - On an exception, `_classify` it; `asyncio.CancelledError` is never caught.
  - If the failure is not retryable or attempts are exhausted → `raise failure.error from None`.
  - If `retry_after` is set and > `max_retry_after` → raise immediately.
  - Otherwise wait = `retry_after` if set, else `base_delay * 2**(n-1) * (1 + uniform(-jitter, jitter))`.
  - Log `logger.warning("llm.retry", extra={"provider": "mistral", "model": model, "attempt": n, "status": status or None, "failure": kind, "wait_s": round(wait, 3)})`, then `await self._sleep(wait)`.
  - The `with_timeout` deadline error (already an `LLMUnavailableError`) is re-raised unchanged (not retried).

  Make T013 and T014 pass.

**Checkpoint**: All P1 stories are complete. The issue's acceptance criteria for complete, structured, 429 retry and invalid key are met offline.

---

## Phase 6: User Story 4 - Operator checks the Mistral setup from the CLI (Priority: P2)

**Goal**: `invio llm test PROVIDER [--model MODEL]` per [contracts/cli.md](./contracts/cli.md).

**Independent Test**: Run the command through Typer's `CliRunner` against a patched provider or MockTransport and a temporary registry; check stdout, stderr and exit codes.

### Tests for User Story 4

- [X] T019 [P] [US4] Write tests/test_cli_llm.py using `typer.testing.CliRunner` on `invio.cli.main.app`. Use the `patched_providers` fixture to register `mistral` as a `FakeProvider` scripted with `FakeReply("OK", Usage(12, 2))`, and pass a temporary registry via `monkeypatch` of `invio.llm.registry.default_registry` (check how `default_registry` is cached and patch it accordingly) with two mistral models at different prices. Cases:
  - (1) Success → exit 0, stdout matches `^ok provider=mistral model=<cheapest> input_tokens=12 output_tokens=2 duration_ms=`, and the fake recorded `temperature=0, max_tokens=5`.
  - (2) `--model <other>` → that model is used.
  - (3) `--model mistral-small-latest` (not registered) → exit 2, stderr `Configuration error: model ... is not registered for LLM provider 'mistral'`, no request recorded.
  - (4) Unknown provider `nope` → exit 2, stderr lists the registered providers.
  - (5) With the real `MistralProvider` (no `patched_providers`) and no key → exit 2, stderr contains `INVIO_MISTRAL_API_KEY`.
  - (6) Fake scripted with `LLMAuthError`, `LLMRateLimitError`, `LLMUnavailableError`, `LLMInvalidRequestError` → each exits 1 with stderr `Error: <TypeName>: ...` and no traceback.
  - (7) The provider has no registry models → exit 2.
  - (8) The answer text never appears in stdout.
  - (9) FR-026: register a provider class whose `from_settings` captures the settings it receives. With `INVIO_LLM_TIMEOUT_SECONDS=60` the captured `llm_timeout_seconds == 20.0`; with `5` it stays `5.0`.

### Implementation for User Story 4

- [X] T020 [US4] Create src/invio/cli/commands/llm.py in the style of src/invio/cli/commands/db.py: `app = typer.Typer(help="LLM provider tools.", no_args_is_help=True)` with `@app.command() def test(provider: Annotated[str, typer.Argument(...)], model: Annotated[str | None, typer.Option("--model", ...)] = None)` (R12). Steps:
  1. `settings = get_settings()`; `capped = settings.model_copy(update={"llm_timeout_seconds": min(settings.llm_timeout_seconds, 20.0)})` (FR-026); `get_provider(provider, capped)`. `LLMConfigError`/`LLMAuthError` → `Configuration error: <msg>`, exit 2.
  2. Collect `[registry.get(m) for m in registry.model_ids()]` where `.provider == provider`. If `--model` is given it must be among them; otherwise pick the minimum by `(input_price_per_mtok + output_price_per_mtok, model_id)`. None available or not registered → exit 2.
  3. `started = time.perf_counter()`; `asyncio.run(p.complete("Connectivity check.", "Reply with OK.", model=..., temperature=0, max_tokens=5))`; an `LLMError` → `Error: {type(err).__name__}: {err}`, exit 1.
  4. Print `ok provider=… model=… input_tokens=… output_tokens=… duration_ms=…` to stdout.

  Make T019 pass.

**Checkpoint**: The operator can verify the Mistral setup with one command.

---

## Phase 7: User Story 5 - Mistral models are known to the registry and resolvable by configuration (Priority: P2)

**Goal**: Ship `models.d/mistral.yaml` with pinned ids, so `resolve()` works with real Mistral model ids and costs are computed.

**Independent Test**: Load `default_registry()` and resolve the `fast`/`smart` roles from a job config naming the shipped ids.

### Tests for User Story 5

- [X] T021 [P] [US5] Add tests to tests/test_llm_mistral.py (or tests/test_llm_registry.py if that fits the existing layout):
  - (1) `default_registry()` contains ≥ 2 models with `provider == "mistral"`, each with prices ≥ 0 and `context_window > 0`.
  - (2) No mistral model id ends with `-latest` (FR-004).
  - (3) A job config with `llm: {provider: mistral, models: {fast: <shipped fast id>, smart: <shipped smart id>}}` → `resolve(..., "fast", make_settings(mistral_api_key="sk-test"))` returns a provider and the fast id. Read the ids from the YAML file, not hard-coded.
  - (4) A job config naming `mistral-small-latest` → `LLMConfigError` "not registered".
  - (5) `registry.cost(<fast id>, Usage(1_000_000, 1_000_000))` equals the input + output price.

### Implementation for User Story 5

- [X] T022 [US5] Create src/invio/llm/models.d/mistral.yaml in the #7 format (`schema_version: 1`, `provider: mistral`, `models:`) with pinned ids only (R11). First **verify the current ids and prices** on Mistral's public pricing page and models docs. The proposed starting point is `mistral-small-2603` (fast; 0.15 / 0.60 USD per 1M; context 262144) and `mistral-medium-2604` (smart; 1.50 / 7.50; 262144). Use the newest pinned versions and prices if they differ, and add a short comment line with the verification date. Make T021 pass and run the full suite, because existing tests that use the default registry or real discovery must still pass.

**Checkpoint**: All user stories are complete.

---

## Phase 8: Polish & Cross-Cutting Concerns

**Purpose**: Live check, docs, gates

- [X] T023 [P] Create tests/test_llm_mistral_live.py with one `@pytest.mark.live` async test (R13, FR-025). Capture `os.environ.get("INVIO_MISTRAL_API_KEY")` **at module import**, because the autouse `isolated_settings` fixture removes `INVIO_*` vars before each test. `pytest.skip("INVIO_MISTRAL_API_KEY not set")` when it is missing. Otherwise build `MistralProvider.from_settings(make_settings(mistral_api_key=key))`, take the cheapest shipped mistral model from `default_registry()`, call `complete("Connectivity check.", "Reply with OK.", temperature=0, max_tokens=5)`, and assert non-empty text and `usage.input_tokens > 0`.
- [X] T024 [P] Update README.md:
  - In the "LLM layer" section, add a "Mistral" paragraph: setup via `INVIO_MISTRAL_API_KEY`, pinned models in `models.d/mistral.yaml`, retry behaviour (429/5xx/connection failures retried up to 3 times with backoff, `Retry-After` honoured up to 60 s, timeouts and other 4xx not retried), and the new `LLMInvalidRequestError` and `LLMRateLimitError.retry_after`.
  - Document `invio llm test <provider> [--model]` with its exit codes 0/1/2.
  - Document `uv run pytest -m live`.
  - Update the package tree line for `llm/`.
- [X] T025 [P] Update the docstring of src/invio/llm/__init__.py (module list: add `mistral`, the Mistral provider) and check that `.env.example` already documents `INVIO_MISTRAL_API_KEY` (add a pointer to `invio llm test mistral` in its comment).
- [X] T026 Run all CI gates (`uv run ruff check`, `uv run ruff format --check`, `uv run mypy`, `uv run pytest --cov=invio.llm.mistral --cov=invio.cli.commands.llm --cov-report=term-missing`). Fix all findings, keep coverage of the new modules ≥ 95 %, and confirm that `INVIO_MISTRAL_API_KEY=dummy uv run pytest -q` makes no network calls (SC-005).
- [X] T027 Run the steps of specs/005-gh-issue-8/quickstart.md that need no real key (offline suite, gates, CLI exit-2 cases), and note in the PR description which live steps were run. Also justify the new dependencies `mistralai` and `httpx2` in the PR description (constitution; R2/R3 explain why `respx` is not used).

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: no dependencies.
- **Foundational (Phase 2)**: depends on Setup. T004 depends on T003; T007 depends on T001 and T004; T008 depends on T005 and T007. T005 and T006 can run in parallel with T003/T004.
- **US1 (Phase 3)**: depends on Phase 2.
- **US2 (Phase 4)**: depends on US1 (T010 provides `_attempt` and the request path).
- **US3 (Phase 5)**: depends on US1. It can run in parallel with US2 only if coordinated, because both edit `mistral.py`; sequential is recommended. Test T014-(13) needs US2.
- **US4 (Phase 6)**: depends on Phase 2 only (its tests use `FakeProvider` and a temporary registry). The real-provider case (5) needs only T007. It can run in parallel with US1–US3 (different files).
- **US5 (Phase 7)**: depends on Phase 2. The registry file and its tests are independent of US1–US4 and can run in parallel.
- **Polish (Phase 8)**: depends on all stories. T023 needs US1 and US5.

### User Story Dependencies

- US1 → US2 → US3 (same source file, each building on the request path).
- US4 and US5 are independent of US1–US3 and of each other.

### Within Each User Story

- Write the tests first and confirm they fail, then implement until they pass.
- Run the gates at each checkpoint.

### Parallel Opportunities

- Phase 2: T003, T005 and T006 in parallel.
- After Phase 2: the US4 track (T019–T020) and the US5 track (T021–T022) can run in parallel with the US1→US2→US3 chain.
- Within US3: T013 and T014 (both tests) in parallel. T015 and T016 are small, independent helpers in the same file, so do them sequentially.
- Phase 8: T023, T024 and T025 in parallel.

---

## Parallel Example: after Phase 2

```bash
# Track A (mistral.py chain):
Task: "T009 [US1] tests for complete() in tests/test_llm_mistral.py"
Task: "T010 [US1] implement complete() in src/invio/llm/mistral.py"

# Track B (CLI), in parallel:
Task: "T019 [US4] tests in tests/test_cli_llm.py"
Task: "T020 [US4] implement src/invio/cli/commands/llm.py"

# Track C (registry), in parallel:
Task: "T021 [US5] registry tests"
Task: "T022 [US5] create src/invio/llm/models.d/mistral.yaml"
```

## Parallel Example: User Story 3

```bash
Task: "T013 [US3] error-mapping tests in tests/test_llm_mistral.py"
Task: "T014 [US3] retry/backoff tests in tests/test_llm_mistral.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1 + Phase 2 (dependencies, shared errors, harness, skeleton).
2. Phase 3 (US1): `complete()` returns text and `Usage`.
3. **STOP and VALIDATE**: `uv run pytest tests/test_llm_mistral.py -q`.

### Incremental Delivery

1. US1 → free-text completion.
2. US2 → structured output (issue AC 2).
3. US3 → error mapping and retries (issue AC 3 and 4). All issue ACs are met here.
4. US5 → shipped pinned models (real jobs can select Mistral).
5. US4 → operator CLI check.
6. Polish → live test, docs, gates and quickstart.

---

## Notes

- [P] tasks touch different files and have no dependency on incomplete tasks.
- Most source work is in `src/invio/llm/mistral.py`, so the US1–US3 tasks there are sequential.
- Commit after each phase checkpoint, on branch `gh-issue-8`.
