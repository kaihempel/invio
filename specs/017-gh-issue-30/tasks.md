---

description: "Task list for the OpenAI provider (issue #30)"
---

# Tasks: OpenAI Provider

**Input**: Design documents from `/specs/017-gh-issue-30/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Requested (spec FR-015, constitution principle III). Tests use recorded HTTP fixtures only: no network, no real key.

**Organization**: Grouped by user story. Paths are relative to the repository root.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**: US1 run a job on OpenAI; US2 failures behave like Mistral; US3 `llm test`; US4 shared contract

---

## Phase 1: Setup

- [ ] T001 Add `openai>=2,<3` to `dependencies` in `pyproject.toml`, run `uv lock`, commit the updated `uv.lock`; confirm the exact SDK field names used in `research.md` R1 (`output_text`, `usage`, `status`, `incomplete_details`, refusal items) against the installed version and note any difference in `specs/017-gh-issue-30/research.md`
- [ ] T002 [P] Create `src/invio/llm/models.d/openai.yaml` (`schema_version: 1`, `provider: openai`) with two pinned, non-reasoning models that support strict JSON-schema output (first = fast, second = smart), no `-latest` aliases; copy ids, `input_price_per_mtok`, `output_price_per_mtok` and `context_window` from OpenAI's official model and pricing pages and record the verification date and sources in a header comment, as in `models.d/mistral.yaml`

---

## Phase 2: Foundational (blocks all user stories)

**Purpose**: share the retry/backoff logic (spec FR-008) before a second provider uses it.

- [ ] T003 Create `src/invio/llm/http_retry.py`: move `RetryPolicy` (same fields and validation: `max_retries >= 0`, `base_delay > 0`, `0 <= jitter < 1`, `max_retry_after > 0`), the failure record, `_retry_after` (seconds or HTTP date), `_wait_before_retry`, the safe-detail/describe helpers and `_strict_schema` out of `src/invio/llm/mistral.py`, making the helpers provider-neutral (they take `status`, response `headers` and body text; each provider adapts its own SDK exception to those); expose one async retry-loop function taking a provider-specific `classify(exc, model, now)`, plus `sleep`, `uniform`, `now`, and the `llm.retry` warning log line unchanged
- [ ] T004 Refactor `src/invio/llm/mistral.py` to use `http_retry.py` (keep `RetryPolicy` importable from `invio.llm.mistral`; keep `_classify` and the SDK calls in place); no behaviour change
- [ ] T005 Run `tests/test_llm_mistral.py`, `tests/test_llm_retry.py` and `tests/test_llm_layering.py` unchanged and confirm they pass (regression guard for T003/T004); fix the extraction, not the tests

**Checkpoint**: Mistral behaves identically; the shared loop exists.

---

## Phase 3: User Story 1 - Run a job on OpenAI models (Priority: P1) 🎯 MVP

**Goal**: `llm.provider: openai` resolves and returns text and validated structured data with usage.

**Independent Test**: with recorded responses, resolve `fast`/`smart` for an OpenAI job; get text, a validated structured value and token usage; a bad structured answer triggers exactly one repair then `LLMInvalidOutputError` with summed usage.

### Tests for User Story 1

- [ ] T006 [P] [US1] Create the offline harness `tests/openai_helpers.py`: `Recorder` (like `tests/mistral_helpers.py`) serving replies from `tests/fixtures/openai/<name>.json` through `httpx.MockTransport`, recording method, path, JSON body and whether an `Authorization` header was sent; helpers to build `OpenAIProvider` with injected `http_client`, `base_url`, fake `sleep`/`uniform`/`now`
- [ ] T007 [P] [US1] Create recorded success fixtures in `tests/fixtures/openai/`: `response_ok.json`, `response_no_usage.json`, `structured_ok.json`, `structured_invalid.json`, `response_empty.json`, `response_refusal.json`, `response_incomplete.json`
- [ ] T008 [US1] Write `tests/test_llm_openai.py` success cases: `complete` returns text and `Usage(input_tokens, output_tokens)` from the response; request body has system and user input, `temperature`, `max_output_tokens`, `store: false`; missing usage gives `Usage(0, 0)`; `complete_structured` sends a strict `json_schema` format with closed objects, every property in `required` and a sanitized name; invalid structured answer triggers exactly one repair call and then `LLMInvalidOutputError` with usage of both calls; empty / refusal / incomplete answer raises `LLMUnavailableError`; key missing raises `LLMAuthError` naming `INVIO_OPENAI_API_KEY`; the `openai` provider resolves for both roles from a job config using the real registry file; one provider instance used from two event loops (threads) concurrently builds one client per loop and returns correct results; a successful call emits one `llm.call` log record with provider `openai`, model, token counts and cost (`tests/test_llm_openai.py`)

### Implementation for User Story 1

- [ ] T009 [US1] Create `src/invio/llm/openai.py`: module docstring documenting the Responses-API choice and rationale (spec FR-004); `OpenAIProvider` decorated with `@register_provider("openai")`; `from_settings` via `require_api_key(settings, "openai")` and `settings.llm_timeout_seconds`; one lazily built `AsyncOpenAI` per running event loop guarded by a lock, with `max_retries=0`, injectable `http_client`/`base_url`; `aclose()`; `__repr__` without the key; `if TYPE_CHECKING: _check: type[LLMProvider] = OpenAIProvider`
- [ ] T010 [US1] In `src/invio/llm/openai.py` implement the single attempt: `client.responses.create(model, input=[system, user], temperature, max_output_tokens, store=False, text=...)` wrapped in `with_timeout(..., seconds=timeout_seconds, provider="openai", model=model)`; extract text from the response; raise `LLMUnavailableError` for empty text, refusal or incomplete status; return `Usage(input_tokens or 0, output_tokens or 0)`
- [ ] T011 [US1] In `src/invio/llm/openai.py` implement `complete` and `complete_structured` (strict closed schema via the shared `_strict_schema` plus all properties in `required`; name sanitized to `[a-zA-Z0-9_-]{1,64}`; run through `structured_with_repair(..., provider="openai", model=model)`), routing requests through the shared retry loop from `http_retry.py`
- [ ] T012 [US1] Update the `src/invio/llm/__init__.py` docstring to list `openai`; confirm `tests/test_llm_layering.py` and `tests/test_job_providers.py` still pass and fix `tests/test_llm_factory.py` / `tests/test_cli_llm.py` only if real discovery now registers `openai` (switch their "unknown provider" name to `anthropic`)

**Checkpoint**: US1 tests pass; an OpenAI job resolves and runs against recorded responses.

---

## Phase 4: User Story 2 - Failures behave like Mistral failures (Priority: P1)

**Goal**: identical typed errors, retries and messages as Mistral for equivalent situations.

**Independent Test**: replay recorded failure responses and compare error type, retry count and message hygiene with the Mistral cases.

### Tests for User Story 2

- [ ] T013 [P] [US2] Create failure fixtures in `tests/fixtures/openai/`: `error_400.json`, `error_401.json`, `error_403.json`, `error_404_model.json`, `error_422.json`, `error_429.json`, `error_429_retry_after.json`, `error_429_retry_after_long.json`, `error_429_insufficient_quota.json`, `error_500.json`, `error_503.json`, `malformed_200.json`
- [ ] T014 [US2] Add failure tests to `tests/test_llm_openai.py`, mirroring the cases of `tests/test_llm_mistral.py`: 401/403 → `LLMAuthError`, one attempt, message names `INVIO_OPENAI_API_KEY`; 429 → `LLMRateLimitError` with `retry_after`, retried and waits honoured up to `max_retry_after`, longer hint fails immediately; 5xx and connection errors retried with exponential backoff and jitter, `LLMUnavailableError` after `max_retries`; timeout and `with_timeout` deadline not retried; other 4xx → `LLMInvalidRequestError` with status, no retry; malformed 200 → `LLMUnavailableError`, no retry; 429 `insufficient_quota` → non-retryable `LLMRateLimitError`; every error names provider and model; no error, log record or `repr` contains the key, prompt or answer text; the SDK exception is neither `__cause__` nor `__context__`; `llm.retry` warnings logged per retry

### Implementation for User Story 2

- [ ] T015 [US2] In `src/invio/llm/openai.py` implement `_classify(exc, model, now)` per `research.md` R3 for the SDK's exception types (timeout, connection, status errors by code) and the sanitized message from the JSON body's `error.message` (whitespace collapsed, at most 300 characters, never request input); mark `insufficient_quota` non-retryable; return `None` for unrecognized exceptions so they propagate
- [ ] T016 [US2] In `src/invio/llm/openai.py` wire `_classify` and the `RetryPolicy` (default `RetryPolicy()`, overridable) into the shared loop from T003; raise the typed error outside the exception handler so the SDK exception is not chained

**Checkpoint**: failure behaviour matches Mistral category by category.

---

## Phase 5: User Story 4 - Provider passes the shared contract (Priority: P2)

**Goal**: one contract suite that every provider must pass.

**Independent Test**: `pytest tests/test_llm_provider_contract.py` is green for fake, Mistral and OpenAI.

*(US4 is deliberately ordered before US3: both are P2, the contract suite needs the provider's test harness, and US3 only adds tests for an already-generic command.)*

- [ ] T017 [US4] Create `tests/test_llm_provider_contract.py` parametrized over `FakeProvider` (scripted), `MistralProvider` (recorded HTTP via `tests/mistral_helpers.py`) and `OpenAIProvider` (recorded HTTP via `tests/openai_helpers.py`); cases: text and usage; structured value and usage; one repair then `LLMInvalidOutputError` with summed usage; auth, rate-limit, unavailable and invalid-request errors carry `provider` and `model` and are `LLMError` subclasses
- [ ] T018 [US4] In `tests/test_llm_factory.py` add a test that adding only `openai.py` and `models.d/openai.yaml` makes `get_provider("openai", settings)` resolvable without editing `factory.py`, and that a missing key raises `LLMAuthError` naming `INVIO_OPENAI_API_KEY`

---

## Phase 6: User Story 3 - Verify the setup with `llm test` (Priority: P2)

**Goal**: `invio llm test openai` gives a clear pass/fail.

**Independent Test**: run the command against recorded success and failure responses.

- [ ] T019 [US3] Add `openai` cases to `tests/test_cli_llm.py` following the existing Mistral cases (patched provider with recorded responses): success prints `ok provider=openai model=<cheapest> input_tokens=<n> output_tokens=<n> duration_ms=<ms>` on stdout and exits 0; `--model` must be registered for `openai`; missing key exits 2 naming `INVIO_OPENAI_API_KEY` with no request made; invalid key exits 1 with `Error: LLMAuthError: ...`; unavailable service exits 1; the command's existing timeout cap bounds the duration (SC-004)
- [ ] T020 [P] [US3] Create `tests/test_llm_openai_live.py` modelled on `tests/test_llm_mistral_live.py`: skipped unless `INVIO_OPENAI_API_KEY` is set; one `complete` and one `complete_structured` call against the cheapest registered model

---

## Phase 7: Polish & cross-cutting

- [ ] T021 [P] Update docs (include a note in `src/invio/llm/models.d/README.md` only if provider-specific rules need mentioning): provider list and `INVIO_OPENAI_API_KEY` in `README.md` and `docs/deployment.md`; short note on the OpenAI provider and `llm test openai` (constitution: user-facing changes update docs in the same PR)
- [ ] T022 Run the full gates from `quickstart.md`: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, `uv run pytest`; fix findings (narrow, justified type-ignores only)
- [ ] T023 Walk through `specs/017-gh-issue-30/quickstart.md`; if a real key is available run `uv run invio llm test openai` and the live test, and record the result in the PR description together with the justification for the new `openai` dependency

---

## Dependencies & Execution Order

- Phase 1 → Phase 2 → Phase 3 (US1) → Phase 4 (US2); Phase 5 (US4) and Phase 6 (US3) need US1 and US2; Phase 7 last.
- T002 is independent of T001. T003 → T004 → T005. T006 and T007 before T008; T009 → T010 → T011 (same file). T013 before T014; T015 → T016 (same file as T009–T011, so after them).
- US4 (T017) needs both providers' test harnesses; US3 (T019) needs a working provider.

### Parallel opportunities

- T001 ‖ T002
- T006 ‖ T007 (then T008)
- T013 can be prepared while T009–T011 are written
- T020 ‖ T019

## Implementation Strategy

1. **MVP = Phases 1–3 (US1)**: an OpenAI job resolves and works against recorded responses.
2. Add US2 for production-grade failure handling (needed before real unattended use).
3. Add US4 and US3 for contract and operator verification, then polish.
4. The Foundational refactor (T003–T005) must land green before any OpenAI code, and may go in its own commit.

## Notes

- Model ids, prices and context windows (T002) are deliberately not specified here; they must come from OpenAI's official pages on the day of implementation.
- One commit per task or logical group; work happens on the issue branch, merged through a PR referencing #30.
