---

description: "Task list for the Anthropic (Claude) provider (issue #31)"
---

# Tasks: Anthropic (Claude) Provider

**Input**: Design documents from `/specs/018-gh-issue-31/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Requested (spec FR-015, constitution principle III). Tests use recorded HTTP fixtures only, with no network and no real key.

**Organization**: Tasks are grouped by user story. Paths are relative to the repository root.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**:
  - US1: run a job on Claude
  - US2: failures behave like other providers' failures
  - US3: `llm test`
  - US4: shared contract

---

## Phase 1: Setup

- [x] T001 Add `anthropic>=1.12,<2` to `dependencies` in `pyproject.toml`, run `uv lock` and commit the updated `uv.lock`. Against the installed SDK, verify and record the results in `specs/018-gh-issue-31/research.md` (new section "R10 – SDK verification"):
  - (a) `AsyncAnthropic.messages.create` accepts `extra_body={"temperature": ...}` and sends it in the JSON body.
  - (b) An explicit client `timeout` skips the SDK's non-streaming large-`max_tokens` guard for `max_tokens=128000`.
  - (c) The shape of `APIStatusError.body` for an Anthropic error response (`{"type": "error", "error": {"type", "message"}}` vs. an unwrapped `error`), plus `.type` and `.status_code`.
  - (d) How to make sure only `x-api-key` is sent when `ANTHROPIC_AUTH_TOKEN` is set (for example the `auth_token` argument or a `default_headers` `Omit`).
  - (e) The exception types for 402 and 529, and the `httpx2` exception wrapped by `APIConnectionError` / `APITimeoutError`.
  - (f) The attribute names of `Message.stop_reason`, `Message.usage`, `TextBlock.text` and `ToolUseBlock.name` / `.input`.
- [x] T002 [P] Create `src/invio/llm/models.d/anthropic.yaml` (`schema_version: 1`, `provider: anthropic`) with exactly two models, first = fast and second = smart:
  - `claude-haiku-4-5-20251001`: `input_price_per_mtok: 1.00`, `output_price_per_mtok: 5.00`, `context_window: 200000`, `max_output_tokens: 64000`.
  - `claude-sonnet-4-6`: `input_price_per_mtok: 3.00`, `output_price_per_mtok: 15.00`, `context_window: 1000000`, `max_output_tokens: 128000`.
  - Re-verify IDs, prices and limits against Anthropic's model and pricing pages on the day of implementation.
  - Add a header comment recording the verification date and sources, and saying that only models accepting forced `tool_choice` and `temperature` may be listed (spec Clarifications), as in `models.d/openai.yaml`.
  - Depends on T003 for the new field to parse.

---

## Phase 2: Foundational (blocks all user stories)

**Purpose**: the registry field required by FR-011a/b.

- [x] T003 In `src/invio/llm/registry.py` add the optional field `max_output_tokens`:
  - `ModelInfo.max_output_tokens: int | None = None`.
  - `_ModelEntry.max_output_tokens: StrictInt | None = Field(default=None, gt=0)`.
  - Copy the value in `load_registry`. `schema_version` stays `Literal[1]`.
  - Update the module docstring, and document the field in `src/invio/llm/models.d/README.md` as `max_output_tokens: <integer > 0>  # optional; tokens; required for anthropic models`.
- [x] T004 [P] Extend `tests/test_llm_registry.py`:
  - The field is accepted and exposed on `ModelInfo`.
  - It is `None` when absent, and the existing `mistral.yaml` and `openai.yaml` still load unchanged.
  - `0`, negative values, floats and strings are rejected with a `ModelRegistryError` naming the file and `max_output_tokens`.
  - The real `anthropic.yaml` defines it for every entry.

**Checkpoint**: the registry suite is green and the existing providers are unaffected.

---

## Phase 3: User Story 1 - Run a job on Claude models (Priority: P1) 🎯 MVP

**Goal**: `llm.provider: anthropic` resolves and returns text and validated structured data with usage.

**Independent Test**: with recorded responses, resolve `fast`/`smart` for an Anthropic job. Get text, a validated nested structured value and token usage. A bad tool input triggers exactly one repair, then `LLMInvalidOutputError` with summed usage.

### Tests for User Story 1

- [x] T005 [P] [US1] Create the offline harness `tests/anthropic_helpers.py`, modelled on `tests/openai_helpers.py` but using `httpx2`:
  - `FIXTURE_DIR = tests/fixtures/anthropic`, `API_KEY = "sk-ant-test-SECRET123"`, `BASE_URL = "https://api.anthropic.test"`.
  - `load_fixture(name)` returning an `httpx2.Response`.
  - A `Recorder` handler for `httpx2.MockTransport` that replays a queue of replies (fixture name, response, exception or `HANG`). For each request it records method, path, JSON body, whether an `x-api-key` header was sent, whether an `Authorization` header was sent, and the host.
  - A `make_provider(...)` builder injecting `client_factory`, `base_url`, `registry`, `retry`, and fake `sleep` / `uniform` / `now` (reuse `tests.async_helpers.RecordingSleep`).
- [x] T006 [P] [US1] Create the recorded success fixtures in `tests/fixtures/anthropic/`, in the Messages API response shape (`{"id", "type": "message", "role", "model", "content": [...], "stop_reason", "usage": {"input_tokens": 12, "output_tokens": 3}}`):
  - `message_ok.json`: text block, `end_turn`.
  - `message_no_usage.json`: `usage` omitted.
  - `message_empty.json`: no text blocks.
  - `message_max_tokens.json`: partial text, `stop_reason: "max_tokens"`.
  - `message_refusal.json`: `stop_reason: "refusal"`.
  - `tool_use_ok.json`: `tool_use` block whose `input` matches the contract suite's `Score` model.
  - `tool_use_nested.json`: `tool_use` input with a nested model and a list of models.
  - `tool_use_invalid.json`: `tool_use` input violating the schema.
  - `tool_use_max_tokens.json`: `tool_use` block, `stop_reason: "max_tokens"`.
  - `text_instead_of_tool.json`: a text block only.
- [x] T007 [US1] Write the success cases in `tests/test_llm_anthropic.py`:
  - **Free text.** `complete` returns the joined text and `Usage(12, 3)`. The request is `POST /v1/messages` with `model`, `max_tokens` equal to the argument, top-level `system`, one user message, and `temperature` in the body. There are no `tools` and no `tool_choice`. Missing usage gives `Usage(0, 0)`. An empty answer, `stop_reason` `max_tokens` and `refusal` each raise `LLMUnavailableError` after exactly one request, and the message contains no answer or refusal text.
  - **Structured request.** `complete_structured` sends one tool. Its `name` is the schema class name sanitised to `[a-zA-Z0-9_-]{1,64}`, and its `input_schema` equals `schema.model_json_schema()`. `tool_choice` is `{"type": "tool", "name": <same>, "disable_parallel_tool_use": true}`, and `max_tokens` equals the model's registered `max_output_tokens` (64000 / 128000).
  - **Structured results.**
    - A valid tool input returns the validated value with `Usage(12, 3, requests=1)`.
    - Nested models and lists of models round-trip (spec SC-003).
    - A schema using an optional field, a `Literal`/enum field, a union, and a self-referencing (recursive) model is sent unchanged as `input_schema` (`$defs`/`$ref` kept), and a matching tool input validates (spec edge case on schema constructs).
    - Invalid input, or text instead of the tool, triggers exactly one repair request, then `LLMInvalidOutputError` with usage summed over two requests.
    - A cut-off tool answer raises `LLMUnavailableError` with no repair (clarification Q1).
  - **Configuration.**
    - A missing key raises `LLMAuthError` naming `INVIO_ANTHROPIC_API_KEY`, with no request.
    - A registry where an `anthropic` model lacks `max_output_tokens` raises `LLMConfigError` when the provider is built (FR-011b).
    - A structured call for a model absent from the registry raises `LLMConfigError`.
    - `resolve()` returns the provider for both roles of a job with `llm.provider: anthropic`, using the real registry file.
    - Registry source (research R5): a provider built via `get_provider("anthropic", settings, registry=<custom>)` reads `max_output_tokens` from `default_registry()`, not from the custom registry. The test pins this so a future factory change is noticed. A provider constructed directly with `registry=` uses that registry.
  - **Client and logging.**
    - One provider instance used from two event loops (threads) builds one client per loop.
    - With `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` set in the environment, requests still go only to the injected base URL with `x-api-key` and no `Authorization` header (research R7).
    - A successful call emits one `llm.call` record with provider `anthropic`, model, token counts and cost.

### Implementation for User Story 1

- [x] T008 [US1] Create `src/invio/llm/anthropic.py`:
  - **Module docstring.** Covers the API choice (Messages API, forced single tool for structured output), the model restriction (forced `tool_choice` and `temperature`, research R1), where output limits come from (`default_registry()` when built by the factory, research R5), the retry and error-hygiene rules, and the client lifecycle. Model it on `src/invio/llm/openai.py`.
  - **Constants.** `PROVIDER = "anthropic"`, `_ENV_VAR = "INVIO_ANTHROPIC_API_KEY"`, `_DEFAULT_BASE_URL = "https://api.anthropic.com"`.
  - **Class and constructor.** `AnthropicProvider` is decorated with `@register_provider(PROVIDER)`. Constructor arguments: `api_key`, `*`, `timeout_seconds`, `registry: ModelRegistry`, `retry: RetryPolicy | None`, `client_factory` (default `anthropic.DefaultAsyncHttpxClient`), `base_url`, `sleep`, `uniform`, `now`. It raises `LLMConfigError` if any `registry.models_for("anthropic")` entry has `max_output_tokens is None`.
  - **`from_settings`.** Uses `require_api_key(settings, PROVIDER)`, `settings.llm_timeout_seconds` and `default_registry()`.
  - **Clients.** A per-event-loop client table with a lock, as in `OpenAIProvider._client_for_loop`. The `AsyncAnthropic` client is built with an explicit `base_url`, `max_retries=0`, `timeout=timeout_seconds + 5`, and the auth-token suppression found in T001(d). `aclose()` closes the running loop's client.
  - **Other.** `__repr__` without the key, and `if TYPE_CHECKING: _check: type[LLMProvider] = AnthropicProvider`.
- [x] T009 [US1] In `src/invio/llm/anthropic.py`, implement the single attempt `_attempt(model, system, user, *, temperature, max_tokens, tool)`:
  - Call `messages.create(model=..., max_tokens=..., system=..., messages=[{"role": "user", "content": user}], extra_body={"temperature": temperature}, **tool options)`, wrapped in `with_timeout(..., seconds=timeout_seconds, provider=PROVIDER, model=model)`.
  - Add `_answer(message, model, *, structured)`:
    - `stop_reason` `"max_tokens"` raises `LLMUnavailableError("Anthropic answer is incomplete: max_tokens")`; `"refusal"` raises `LLMUnavailableError("Anthropic refused to answer")`. Neither message includes any text.
    - Structured: return `json.dumps(first tool_use .input)` if there is a `tool_use` block, else the joined text, which may be empty.
    - Free text: return the joined text blocks; empty text raises `LLMUnavailableError`.
    - Usage is `Usage(input_tokens or 0, output_tokens or 0)`.
- [x] T010 [US1] In `src/invio/llm/anthropic.py`, implement `complete` and `complete_structured`, both routed through `http_retry.run_with_retries` with `classify=_classify` (a stub returning `None` until T015).
  - `complete` passes the caller's `max_tokens`.
  - `complete_structured`:
    - looks up `max_output_tokens` from the registry (raising `LLMConfigError` if the model is unknown);
    - builds the tool `{"name": re.sub(r"[^a-zA-Z0-9_-]", "_", schema.__name__)[:64], "description": "Return the answer as the tool input.", "input_schema": schema.model_json_schema()}`;
    - sets `tool_choice={"type": "tool", "name": name, "disable_parallel_tool_use": True}`;
    - runs through `structured_with_repair(request, system, user, schema, provider=PROVIDER, model=model)`.
- [x] T011 [US1] Update the `src/invio/llm/__init__.py` docstring to list `anthropic`, if it lists providers.
  - Confirm `tests/test_llm_layering.py`, `tests/test_job_providers.py` and `tests/test_wizard.py` still pass.
  - Adjust `tests/test_llm_factory.py` / `tests/test_cli_llm.py` only if real provider discovery now registers `anthropic` where a test expects it to be unknown. Use `google` as the unknown name.

**Checkpoint**: US1 tests pass; an Anthropic job resolves and runs against recorded responses.

---

## Phase 4: User Story 2 - Failures behave like other providers' failures (Priority: P1)

**Goal**: the same typed errors, retries and message hygiene as the Mistral and OpenAI providers, plus overloaded (529) and exhausted-credit handling.

**Independent Test**: replay recorded failure responses and compare error type, retry count and message hygiene with the equivalent `tests/test_llm_openai.py` cases.

### Tests for User Story 2

- [x] T012 [P] [US2] Create the failure fixtures in `tests/fixtures/anthropic/`, with Anthropic error bodies `{"type": "error", "error": {"type": ..., "message": ...}}`:
  - `error_400.json` (`invalid_request_error`)
  - `error_400_credit_balance.json` (`invalid_request_error`, message "Your credit balance is too low to access the Anthropic API...")
  - `error_401.json` (`authentication_error`)
  - `error_402.json` (`billing_error`)
  - `error_403.json` (`permission_error`)
  - `error_404_model.json` (`not_found_error`)
  - `error_413.json` (`request_too_large`)
  - `error_429.json` (`rate_limit_error`, no `retry-after`)
  - `error_429_retry_after.json` (`retry-after: 2`)
  - `error_429_retry_after_long.json` (`retry-after: 3600`)
  - `error_500.json` (`api_error`)
  - `error_529.json` (`overloaded_error`)
  - `malformed_200.json` (non-JSON body)
- [ ] T013 [US2] Add the failure tests to `tests/test_llm_anthropic.py`, mirroring `tests/test_llm_openai.py`:
  - **Auth.** 401 and 403 raise `LLMAuthError` after one attempt, with a message naming `INVIO_ANTHROPIC_API_KEY`.
  - **Exhausted credit.** 402 and the credit-balance 400 raise `LLMQuotaError` (an `LLMRateLimitError` with `retry_after is None`) after one attempt, and `invio.llm.retry.is_transient_llm` is false for it.
  - **Rate limit.** 429 raises `LLMRateLimitError` with `retry_after`. It is retried with waits honoured up to `max_retry_after`; a longer hint fails immediately.
  - **Server errors.** 500, 529 and connection errors are retried with exponential backoff and jitter (recorded sleeps), then raise `LLMUnavailableError` after `max_retries + 1` requests; recovery on a later attempt returns the answer (acceptance criterion "overloaded/5xx retried then raised").
  - **Timeouts.** SDK or `httpx2` timeouts and the `with_timeout` deadline (`HANG`) raise `LLMUnavailableError` with no retry.
  - **Other rejections.** 400, 404 and 413 raise `LLMInvalidRequestError` carrying `status`, with no retry. This includes a 400 for a free-text `max_tokens` above the model's limit (spec edge case). A malformed 200 raises `LLMUnavailableError` with no retry.
  - **Message hygiene.**
    - Every error has `provider == "anthropic"` and the model.
    - No error message, log record or `repr` contains the key, the prompt, the answer, or the raw body beyond the sanitized `error.message`.
    - The SDK exception is neither `__cause__` nor `__context__`.
    - One `llm.retry` warning is logged per retry.

### Implementation for User Story 2

- [x] T014 [US2] In `src/invio/llm/anthropic.py`, implement `_safe_detail(exc)`: read the provider's `error.message` from `exc.body` in the shape verified in T001(c) and sanitise it with `http_retry.sanitize_detail`. Implement `_is_credit_exhausted(exc)`: true for status 402, for `exc.type == "billing_error"`, or for a 400 whose sanitized message contains `"credit balance"` (case-insensitive).
- [x] T015 [US2] In `src/invio/llm/anthropic.py`, implement `_classify(exc, model, now) -> Failure | None` per `research.md` R6, in this order:
  1. `anthropic.APITimeoutError` / `httpx2.TimeoutException` → `"timeout"`, not retryable.
  2. `anthropic.APIConnectionError` → classify `exc.__cause__ or exc` like `_classify_transport` in `openai.py`, using the `httpx2` exception classes (unsendable and bad response not retryable; connection retryable).
  3. Raw `httpx2.HTTPError` → the same transport classification.
  4. `anthropic.APIResponseValidationError` / `json.JSONDecodeError` → `"bad_response"`, not retryable.
  5. `anthropic.APIStatusError`, by status:
     - 401/403 → `LLMAuthError`;
     - credit exhausted → `LLMQuotaError` `"quota"`, not retryable;
     - 429 → `LLMRateLimitError` with `retry_after(exc.response.headers, now)` (finite only), retryable;
     - 500–599 including 529 → `"server"`, retryable;
     - other 4xx → `LLMInvalidRequestError(status=...)`, not retryable;
     - anything else → `"bad_response"`.

  Return `None` for unrecognised exceptions. Replace the T010 stub, and keep the typed error raised outside the handler (the shared loop already does this).

**Checkpoint**: failure behaviour matches the other providers category by category.

---

## Phase 5: User Story 4 - Provider passes the shared contract (Priority: P2)

**Goal**: the existing contract suite also covers Anthropic.

**Independent Test**: `uv run pytest tests/test_llm_provider_contract.py` is green for fake, Mistral, OpenAI and Anthropic.

*(US4 comes before US3: both are P2, US4 needs the harness from T005, and US3 only adds tests for an already-generic command.)*

- [ ] T016 [US4] In `tests/test_llm_provider_contract.py`, add an `AnthropicHarness(ProviderHarness)` (`name = "anthropic"`, `model = "claude-haiku-4-5-20251001"`) to `HARNESSES`, mapping each `Outcome` to a fixture:
  - `TEXT` → `message_ok`
  - `STRUCTURED` → `tool_use_ok`
  - `STRUCTURED_INVALID` → `tool_use_invalid`
  - `AUTH` → `error_401`
  - `RATE_LIMIT` → `error_429`
  - `UNAVAILABLE` → `error_529`
  - `INVALID_REQUEST` → `error_400`

  Build it with `RetryPolicy(max_retries=0)` via `tests/anthropic_helpers.py`, and update the module docstring.
- [ ] T017 [P] [US4] In `tests/test_llm_factory.py`, add a test that real discovery makes `get_provider("anthropic", settings)` resolvable without editing `factory.py`, and that a missing key raises `LLMAuthError` naming `INVIO_ANTHROPIC_API_KEY`.

---

## Phase 6: User Story 3 - Verify the setup with `llm test` (Priority: P2)

**Goal**: `invio llm test anthropic` gives a clear pass/fail; the command itself is unchanged (clarification Q3).

**Independent Test**: run the command against recorded success and failure responses.

- [ ] T018 [US3] Add `anthropic` cases to `tests/test_cli_llm.py`, following the existing OpenAI cases (patched provider with recorded responses):
  - Success prints `ok provider=anthropic model=claude-haiku-4-5-20251001 input_tokens=<n> output_tokens=<n> duration_ms=<ms>` on stdout and exits 0.
  - `--model claude-sonnet-4-6` is accepted; an unregistered `--model` exits 2.
  - A missing key exits 2 naming `INVIO_ANTHROPIC_API_KEY`, with no request made.
  - An invalid key exits 1 with `Error: LLMAuthError: ...` on stderr.
  - An overloaded service exits 1 with `Error: LLMUnavailableError: ...`.
  - A hanging service is cut off by the command's existing timeout cap and exits 1 with `LLMUnavailableError` (SC-005), as in the existing OpenAI timeout case.
  - All of these pass with `src/invio/cli/commands/llm.py` left unmodified; the diff must not touch that file (FR-013).
- [ ] T019 [P] [US3] Create `tests/test_llm_anthropic_live.py`, modelled on `tests/test_llm_openai_live.py`:
  - Marked `live`, and skipped unless `INVIO_ANTHROPIC_API_KEY` is set (captured at import).
  - One `complete` and one `complete_structured` call (`tests.llm_helpers.Score`) against the cheapest registered Anthropic model.

---

## Phase 7: Polish & cross-cutting

- [ ] T020 [P] Update the docs:
  - `README.md`: provider list, `llm.provider: anthropic`, `INVIO_ANTHROPIC_API_KEY`, the supported models and why (forced tool use and temperature).
  - `docs/deployment.md` line 81: "jobs can use `mistral`, `openai` and `anthropic`".
  - `src/invio/llm/models.d/README.md`: already updated in T003; check the wording.
  - Check `docs/job.example.yaml` mentions anthropic only where appropriate.
- [ ] T021 Run the full gates from `quickstart.md`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`. Fix findings; type-ignores must be narrow and justified in a comment.
- [ ] T022 Walk through `specs/018-gh-issue-31/quickstart.md`. If a real key is available, run `uv run invio llm test anthropic` (both models) and `uv run pytest -m live tests/test_llm_anthropic_live.py`. Record the result in the PR description, together with the justification for the new `anthropic` dependency and the model restriction (research R1). If no key is available, say so explicitly there.

---

## Dependencies & Execution Order

- **Phase order**: Phase 1 → Phase 2 → Phase 3 (US1) → Phase 4 (US2). Phase 5 (US4) and Phase 6 (US3) need US1 and US2. Phase 7 runs last.
- **Setup**: T001 comes before any code importing `anthropic`. T002 needs T003 to load, but can be written in parallel.
- **Registry**: T003 → T004.
- **US1**: T005 and T006 come before T007. T008 → T009 → T010 (same file).
- **US2**: T012 comes before T013. T014 → T015 (same file, after T010).
- **US4 and US3**: T016 needs T005, T006 and T012. T018 needs a working provider (T015).

### Parallel opportunities

- T001 ‖ T002 ‖ T003 (different files)
- T004 ‖ T005 ‖ T006
- T012 can be prepared while T008–T010 are written
- T017 ‖ T016; T019 ‖ T018; T020 ‖ T021 preparation

## Implementation Strategy

1. **MVP = Phases 1–3 (US1)**: an Anthropic job resolves and works against recorded responses, including nested structured output.
2. Add US2 for production-grade failure handling, which is required before real unattended use and covers the acceptance criterion on overloaded/5xx retries.
3. Add US4 (shared contract, acceptance criterion) and US3 (operator check), then polish.
4. The registry change (T003–T004) may go in its own commit ahead of the provider.

## Notes

- Model IDs, prices and limits in T002 come from the design documents (cached 2026-10-06) and must be re-verified on Anthropic's pages when written.
- Make one commit per task or logical group. Work happens on the issue branch, merged through a PR referencing #31.
