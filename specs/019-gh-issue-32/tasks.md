---

description: "Task list for the Google (Gemini) provider (issue #32)"
---

# Tasks: Google (Gemini) Provider

**Input**: Design documents from `/specs/019-gh-issue-32/`

**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/, quickstart.md

**Tests**: Requested (spec FR-016, constitution principle III). Tests use recorded HTTP fixtures only, with no network and no real key.

**Organization**: Tasks are grouped by user story. Paths are relative to the repository root.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: can run in parallel (different files, no dependency on an incomplete task)
- **[Story]**:
  - US1: run a job on Gemini
  - US2: safety blocks and failures
  - US3: `llm test`
  - US4: shared contract

---

## Phase 1: Setup

- [X] T001 Create the branch `gh-issue-32` from an up-to-date `main` (`git fetch origin && git switch -c gh-issue-32 origin/main`) and commit the Spec Kit artifacts in `specs/019-gh-issue-32/` (spec, plan, research, data model, contracts, quickstart, tasks, checklists) as the first commit. All later work is committed on this branch (constitution, Development Workflow).
- [X] T002 Add `google-genai>=2.29,<3` to `dependencies` in `pyproject.toml`, run `uv lock` and commit the updated `uv.lock`. Against the installed SDK, verify the following and record the results in `specs/019-gh-issue-32/research.md` (new section "R8 – SDK verification"):
  - (a) `mypy --strict` accepts `from google import genai` / `from google.genai import errors, types` without `ignore_missing_imports`. If not, add a narrow `[[tool.mypy.overrides]]` with a justification comment.
  - (b) With `genai.Client(vertexai=False, api_key=..., http_options=types.HttpOptions(base_url=..., api_version="v1beta", timeout=<ms>, httpx_async_client=<httpx.AsyncClient>))`, a request:
    - is sent to `<base_url>/v1beta/models/<model>:generateContent`;
    - carries `x-goog-api-key` and no `authorization` header;
    - ignores `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_GEMINI_BASE_URL` and `GOOGLE_GENAI_USE_VERTEXAI`;
    - is made exactly once on a 503 when `retry_options` is unset.
  - (c) The JSON body field names produced by `GenerateContentConfig`: `systemInstruction`, `generationConfig.temperature`, `maxOutputTokens`, `thinkingConfig.thinkingLevel`, `responseMimeType`, `responseJsonSchema`. Also whether an unset `temperature` is omitted from the body.
  - (d) For 400, 401, 403, 404, 429, 500 and 503: the exception class and its `.code`, `.status`, `.message`, `.details` and `.response.headers`.
  - (e) The raw `httpx` exceptions raised for a connect error and for a timeout.
  - (f) The exception raised for a non-JSON 200 body (`errors.UnknownApiResponseError`?).
  - (g) The attribute names `response.prompt_feedback.block_reason`, `block_reason_message`, `safety_ratings[].blocked` / `.category`, `candidates[0].finish_reason`, `content.parts[].text` / `.thought` and `usage_metadata.*_token_count`. Also whether enum values arrive as `types.FinishReason` / `types.BlockedReason` members or as plain strings.
  - (h) `Client.aio` does not need closing when the `httpx.AsyncClient` is owned and closed by the caller.
---

## Phase 2: Foundational (blocks all user stories)

**Purpose**: the registry fields required by FR-013a/c, the Google registry file that uses them, and the harness change used by all provider tests.

- [X] T003 In `src/invio/llm/registry.py`, add three optional fields to `ModelInfo` and `_ModelEntry` and copy them in `load_registry`:
  - `thinking_level: Literal["minimal", "low", "medium", "high"] | None = None`.
  - `thinking_allowance_tokens: StrictInt | None = Field(default=None, gt=0)` (`ModelInfo`: `int | None = None`).
  - `keep_default_temperature: StrictBool = False` (`ModelInfo`: `bool = False`).

  `schema_version` stays `Literal[1]`. Update the module docstring. In `src/invio/llm/models.d/README.md`, document the fields as:
  - `thinking_level: <minimal|low|medium|high>  # optional; required for google models`
  - `thinking_allowance_tokens: <integer > 0>  # optional; required for google models`
  - `keep_default_temperature: <true|false>  # optional, default false; the request sends no temperature`
- [X] T004 Create `src/invio/llm/models.d/google.yaml` (`schema_version: 1`, `provider: google`) with exactly two models, first = fast and second = smart:
  - `gemini-3.5-flash-lite`: `input_price_per_mtok: 0.30`, `output_price_per_mtok: 2.50`, `context_window: 1048576`, `thinking_level: minimal`, `thinking_allowance_tokens: 1024`, `keep_default_temperature: true`.
  - `gemini-3.8-flash`: `input_price_per_mtok: 0.75`, `output_price_per_mtok: 3.75`, `context_window: 1048576`, `thinking_level: low`, `thinking_allowance_tokens: 4096`, `keep_default_temperature: true`.
  - Header comment, modelled on `anthropic.yaml`. It records:
    - the verification date and the source URLs (models, pricing, thinking, Gemini 3 developer guide);
    - "Order matters to the tests: first = fast role, second = smart role";
    - that every `google` entry must define `thinking_level` and `thinking_allowance_tokens`;
    - that `keep_default_temperature` follows Google's Gemini 3 guidance;
    - that `gemini-3.8-flash` costs 1.50/7.50 from 2027-01-01, so the file must be updated then;
    - that output prices include thinking tokens.
  - Re-verify IDs, prices and thinking levels against Google's pages on the day of implementation.
- [X] T005 Extend `tests/test_llm_registry.py`:
  - The three fields are accepted and exposed on `ModelInfo`. They default to `None` / `None` / `False` when absent, and the existing `mistral.yaml`, `openai.yaml` and `anthropic.yaml` still load unchanged.
  - These values are rejected with a `ModelRegistryError` naming the file and the field:
    - an unknown `thinking_level` (`"off"`, `"none"`, `1`);
    - a `thinking_allowance_tokens` of `0`, a negative number, a float or a string;
    - a `keep_default_temperature` given as `"yes"` or `1`.
  - The real `google.yaml` defines `thinking_level` and `thinking_allowance_tokens` for every entry, and sets `keep_default_temperature: true` for both models.
- [X] T006 [P] In `tests/sdk_harness.py`, add `headers: dict[str, str]` (lower-cased names) to `RecordedRequest` and fill it in `Recorder.__call__`. Keep the existing fields, so `tests/test_llm_anthropic.py`, `test_llm_openai.py` and `test_llm_mistral.py` are unchanged. Run those three modules to confirm.

**Checkpoint**: the registry suite and the existing provider suites are green.

---

## Phase 3: User Story 1 - Run a job on Gemini models (Priority: P1) 🎯 MVP

**Goal**: `llm.provider: google` resolves and returns text and validated structured data (including `RelevanceResult` and `ItemSummary`) with usage, applying the registered thinking level, allowance and temperature rule.

**Independent Test**: with recorded responses, resolve `fast`/`smart` for a Google job and check:
- text, validated structured values and token usage are returned;
- the requests carry the thinking level, the allowance-raised `maxOutputTokens` and no temperature for flagged models;
- a bad answer triggers exactly one repair, then `LLMInvalidOutputError` with summed usage.

### Tests for User Story 1

- [X] T007 [P] [US1] Create the offline harness `tests/google_helpers.py`, modelled on `tests/anthropic_helpers.py` but binding `sdk_harness` to `httpx` (not `httpx2`):
  - `FIXTURE_DIR = tests/fixtures/google`, `API_KEY = "AIza-test-SECRET123"`, `BASE_URL = "https://generativelanguage.googleapis.test/"`.
  - `load_fixture(name)` returning an `httpx.Response`.
  - `Recorder` and `recording_options`.
  - `make_provider(*replies, retry=None, timeout_seconds=60.0, registry=None, uniform=None, sleep=None, now=None)` returning `(GoogleProvider, Recorder, waits)`, with `registry` defaulting to `default_registry()`.
- [X] T008 [P] [US1] Create recorded success fixtures in `tests/fixtures/google/`. Each file has the `{"status", "headers", "body"}` shape used by `sdk_harness.load_fixture`, and the body uses the REST `generateContent` shape (`{"candidates": [{"content": {"role": "model", "parts": [...]}, "finishReason": "STOP", "index": 0}], "usageMetadata": {...}, "modelVersion": ...}`):
  - `text_ok.json`: one text part `"OK"`, usage `promptTokenCount: 12`, `candidatesTokenCount: 3`, no thoughts.
  - `text_with_thoughts.json`: one `{"text": "...", "thought": true}` part plus one answer part, usage `promptTokenCount: 12`, `candidatesTokenCount: 3`, `thoughtsTokenCount: 40`.
  - `text_no_usage.json`: `usageMetadata` omitted.
  - `text_empty.json`: a candidate with no parts and `finishReason: "STOP"`.
  - `no_candidates.json`: `candidates` omitted, `promptFeedback` without `blockReason`.
  - `json_ok.json`: text part `{"score": 0.8, "reason": "relevant"}`, usage 12/3.
  - `json_invalid.json`: text part `{"score": 7}`, usage 12/3.
  - `json_fenced.json`: the `json_ok` answer wrapped in a ```json fence.
  - `json_relevance.json`: a valid `RelevanceResult` (`score` 0.75, `reason`, 2 `key_points`).
  - `json_item_summary.json`: a valid `ItemSummary` (single-line `headline`, 4 `bullets`, `why_relevant`).
  - `json_item_summary_too_few.json`: an `ItemSummary` with 2 bullets, which violates `minItems: 3` and is enforced locally.
  - `json_nested.json`: an answer for the test model of T009 (nested sub-model, list of sub-models).
- [X] T009 [P] [US1] Create `tests/test_llm_google_schema.py` for the pure converter `invio.llm.google.gemini_schema` ([data-model](data-model.md), "Service-compatible shape"):
  - The `RelevanceResult` schema loses `minLength` but keeps `minimum: 0` / `maximum: 1`, `additionalProperties: false` and `required`.
  - The `ItemSummary` schema keeps `minItems: 3` / `maxItems: 6` and loses `minLength`.
  - A model with a nested sub-model used twice and a `list[SubModel]` has every `$ref` inlined and no `$defs` left. The result validates the same sample under `jsonschema`, which is in the dev group.
  - `Optional[...]` / `anyOf` with `{"type": "null"}`, `Literal`/`Enum` → `enum`, and `Field(description=...)` survive.
  - `pattern`, `const`, `default`, `exclusiveMinimum` and `exclusiveMaximum` are dropped.
  - A property named `default` or `pattern` is kept.
  - A recursive model (self-reference) raises `LLMConfigError` naming the schema.
  - A non-local `$ref` raises `LLMConfigError`.
  - The input dict is not mutated.
- [X] T010 [P] [US1] Create `tests/test_llm_google.py` with the US1 cases (`make_provider` from T007):
  - **Free text.** `complete` returns `("OK", Usage(12, 3))`. The request goes to `.../v1beta/models/<model>:generateContent`. Its body has:
    - `systemInstruction` with the system text;
    - `contents` with the user text;
    - `generationConfig.maxOutputTokens == max_tokens + thinking_allowance_tokens`;
    - `generationConfig.thinkingConfig.thinkingLevel` equal to the registered level (in the API's casing, as found in T002c).
  - **Empty system text.** An empty system text omits `systemInstruction`.
  - **Temperature.** Using a fixture registry (`tests.llm_helpers.write_registry` + `load_registry`) with one flagged and one unflagged `google` model:
    - flagged model: `temperature=0.0` is absent from `generationConfig`;
    - unflagged model: `temperature: 0.0` is present.

    Both for `complete` and `complete_structured`.
  - **Thinking tokens.** `text_with_thoughts` returns only the answer text and `Usage(12, 43)`.
  - **Missing usage.** `text_no_usage` gives `Usage(0, 0)` without crashing.
  - **Structured.** `complete_structured(..., Score, ...)`:
    - sends `responseMimeType: "application/json"` and `responseJsonSchema` equal to `gemini_schema(Score.model_json_schema())`;
    - sends no `maxOutputTokens`;
    - returns the validated `Score` and `Usage(12, 3)`.
  - **Fenced answer.** `json_fenced` validates.
  - **Pipeline schemas.** `RelevanceResult` and `ItemSummary` (imported from `invio.graph.nodes.relevance` / `summarize_item`) round-trip from `json_relevance` / `json_item_summary`.
  - **Repair.** `json_item_summary_too_few` followed by `json_item_summary` repairs once: `usage.requests == 2`, and the second request's user text contains the problem summary.
  - **Nested.** The nested model of T009 round-trips from `json_nested`.
  - **Invalid twice.** `json_invalid` twice raises `LLMInvalidOutputError` with `Usage(24, 6)`, `provider="google"` and the model.
  - **Empty structured answer.** `text_empty` or `no_candidates` as a structured answer goes through repair, not a crash.
  - **Recursive schema.** A recursive schema raises `LLMConfigError` and makes no request.
  - **Building with a key.** `GoogleProvider.from_settings(make_settings(google_api_key=...))` builds and its `repr` does not contain the key.
  - **Building without a key.** A missing key raises `LLMAuthError` naming `INVIO_GOOGLE_API_KEY`.
  - **Incomplete registry.** A registry whose `google` entry lacks `thinking_level` or `thinking_allowance_tokens` raises `LLMConfigError` listing the models when the provider is built.
  - **Unregistered model.** Calling with a model that is not in the registry raises `LLMConfigError` before any request.
  - **Credentials.** The key is sent only as `x-goog-api-key`, to `BASE_URL`, with no `authorization` header. This holds even with `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_GEMINI_BASE_URL` and `GOOGLE_GENAI_USE_VERTEXAI=true` set via `monkeypatch.setenv`.
  - **Clients.** `aclose()` closes the client of the running loop, and the next call builds a new one (`recorder.clients_created == 2`).
- [X] T011 [P] [US1] Extend `tests/test_llm_factory.py` with `test_google_resolves_from_its_module_and_registry_file_only`, modelled on the Anthropic test at `tests/test_llm_factory.py:499`:
  - `google.py` and `models.d/google.yaml` exist, and `factory.py` does not mention `google`.
  - `resolve()` of a job with `provider: google` and the first/second registry models returns a wrapped `GoogleProvider` and the model id.

### Implementation for User Story 1

- [X] T012 [US1] Create `src/invio/llm/google.py`. It is the success path only; error classification is T021.
  - **Module docstring**, in the style of `anthropic.py`, covering:
    - the API choice;
    - the thinking/allowance/temperature rules ([research R2/R3](research.md));
    - the schema conversion;
    - error hygiene;
    - the explicit `vertexai=False` / `api_key` / `base_url`.
  - **Constants**: `PROVIDER = "google"`, `_LABEL = "Google"`, `_ENV_VAR = "INVIO_GOOGLE_API_KEY"`, `_DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/"`, `_API_VERSION = "v1beta"`, `_SDK_TIMEOUT_MARGIN_S = 5`.
  - **`gemini_schema(schema: dict[str, Any]) -> dict[str, Any]`**, per [data-model](data-model.md):
    - inline local `#/$defs/<Name>` refs (cycle → `LLMConfigError(f"schema {title} is recursive; Google structured output cannot express it")`; non-local ref → `LLMConfigError`);
    - drop `$defs`;
    - keep only `type`, `title`, `description`, `properties`, `required`, `additionalProperties`, `items`, `prefixItems`, `minItems`, `maxItems`, `enum`, `format`, `minimum`, `maximum`, `anyOf`;
    - never filter the keys inside `properties`;
    - return a new dict.
  - **`@register_provider(PROVIDER) class GoogleProvider`**, with the same constructor signature as `AnthropicProvider` but `client_factory: Callable[[], httpx.AsyncClient] | None` (default `httpx.AsyncClient`).
    - The constructor validates that every `registry.models_for("google")` entry has `thinking_level` and `thinking_allowance_tokens`, else `LLMConfigError(f"registry entries of provider 'google' must define thinking_level and thinking_allowance_tokens: {ids}")`.
    - `from_settings(settings, *, registry=None)` uses `require_api_key(settings, PROVIDER)`.
    - `__repr__` shows `timeout_seconds` and `retry` only.
    - `_build_client()` returns `(genai.Client(vertexai=False, api_key=..., http_options=types.HttpOptions(base_url=self._base_url, api_version=_API_VERSION, timeout=int((self.timeout_seconds + _SDK_TIMEOUT_MARGIN_S) * 1000), httpx_async_client=http_client)).aio, http_client)`, used via `LoopClients`.
    - `aclose()` delegates to `LoopClients`.
  - **`_model_info(model)`**: `self._registry.require(model, PROVIDER)`, so an unregistered model raises `LLMConfigError`.
  - **`_config(info, system, temperature, max_output_tokens, schema_json)`** builds `types.GenerateContentConfig` with:
    - `system_instruction` only if `system`;
    - `temperature` only if not `info.keep_default_temperature`;
    - `max_output_tokens` only for free text;
    - `thinking_config=types.ThinkingConfig(thinking_level=...)`;
    - `response_mime_type="application/json"` and `response_json_schema=` for structured calls.
  - **`_attempt`** awaits `with_timeout(aio.models.generate_content(model=model, contents=user, config=config), seconds=self.timeout_seconds, provider=PROVIDER, model=model)` and passes the response to `_answer`.
  - **`_answer(response, model, *, structured)`** for now:
    - joins the non-thought text parts of `candidates[0]`;
    - empty free text → `LLMUnavailableError(describe("Google returned no answer text", model, None))`;
    - `finish_reason == MAX_TOKENS` → `LLMUnavailableError(describe("Google answer is incomplete: MAX_TOKENS", model, None))`;
    - usage = `Usage(prompt_token_count or 0, (candidates_token_count or 0) + (thoughts_token_count or 0))`.
  - **`_request`** wraps `_attempt` in `run_with_retries`, with `classify` initially returning `None`.
  - **`complete`** passes `max_output_tokens = max_tokens + info.thinking_allowance_tokens`.
  - **`complete_structured`** computes `gemini_schema(schema.model_json_schema())` once, before any request, and calls `structured_with_repair(request, system, user, schema, provider=PROVIDER, model=model)`.
  - End with `if TYPE_CHECKING: _check: type[LLMProvider] = GoogleProvider`.
  - Depends on T002–T004 and T007–T011.

**Checkpoint**: T009–T011 pass. A Google job can obtain text and validated structured output offline.

---

## Phase 4: User Story 2 - Safety blocks and failures behave predictably (Priority: P1)

**Goal**: blocks raise a descriptive `LLMInvalidOutputError` without retry or repair, and every failure category maps to the same typed error and retry behaviour as the other providers.

**Independent Test**: replay the block and error fixtures. Each yields the expected typed error, the expected number of requests and waits, and a message free of secrets, prompt and answer text.

### Tests for User Story 2

- [X] T013 [P] [US2] Create the block fixtures in `tests/fixtures/google/` (status 200):
  - `blocked_prompt.json`: no `candidates`, `promptFeedback: {"blockReason": "SAFETY", "blockReasonMessage": "...", "safetyRatings": [{"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "probability": "HIGH", "blocked": true}]}`, usage `promptTokenCount: 12`.
  - `blocked_prompt_other.json`: `blockReason: "PROHIBITED_CONTENT"`, no ratings.
  - `stopped_safety.json`: a candidate with a partial text part `"PARTIAL-ANSWER-TEXT"`, `finishReason: "SAFETY"`, `safetyRatings` with one `blocked: true` category, usage 12/3.
  - `stopped_recitation.json`: `finishReason: "RECITATION"`.
  - `stopped_spii.json`: `finishReason: "SPII"`.
  - `text_max_tokens.json`: partial text, `finishReason: "MAX_TOKENS"`, usage 12/5 plus thoughts 30.
- [X] T014 [P] [US2] Create the error fixtures in `tests/fixtures/google/`, using Google's error body `{"error": {"code", "message", "status", "details": [...]}}`:
  - `error_400.json`: `INVALID_ARGUMENT`, message `"Invalid JSON payload received."`.
  - `error_400_api_key_invalid.json`: `INVALID_ARGUMENT`, `"API key not valid. Please pass a valid API key."`, details `[{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID", "domain": "googleapis.com"}]`.
  - `error_401.json`: `UNAUTHENTICATED`.
  - `error_403.json`: `PERMISSION_DENIED`.
  - `error_404_model.json`: `NOT_FOUND`, `"models/gemini-x is not found for API version v1beta"`.
  - `error_429_retry_delay.json`: `RESOURCE_EXHAUSTED`, details with a `QuotaFailure` whose `quotaId` is `"GenerateRequestsPerMinutePerProjectPerModel"` and a `RetryInfo` with `"retryDelay": "7s"`.
  - `error_429_retry_delay_long.json`: same, `"retryDelay": "3600s"`.
  - `error_429_bare.json`: no details, no headers.
  - `error_429_retry_after_header.json`: no details, header `retry-after: 3`.
  - `error_429_daily_quota.json`: `quotaId` `"GenerateRequestsPerDayPerProjectPerModel"` and `RetryInfo` `"27s"`.
  - `error_429_zero_quota.json`: `quotaValue: "0"`.
  - `error_500.json`: `INTERNAL`.
  - `error_503.json`: `UNAVAILABLE`, `"The model is overloaded. Please try again later."`.
  - `error_504.json`: `DEADLINE_EXCEEDED`.
  - `malformed_200.json`: body `"<html>not json</html>"` with `content-type: text/html`.

  Error messages must not contain the test key, so the hygiene checks are meaningful. Add one fixture whose `message` echoes a fake prompt `"PROMPT-ECHO"`, to show that only the sanitized `error.message` is used and that it is truncated.
- [X] T015 [P] [US2] Add the block tests to `tests/test_llm_google.py`:
  - **Blocked prompt.** For `complete` and `complete_structured`, `blocked_prompt` raises `LLMInvalidOutputError`:
    - the message contains `SAFETY` and `HARM_CATEGORY_DANGEROUS_CONTENT`, names `provider="google"` and the model, and does not contain the prompt or the user text;
    - `errors` contains the reason;
    - `usage == Usage(12, 0)`;
    - exactly one request is made: no retry, and no repair for structured.
  - **Other block reasons.** `blocked_prompt_other` names `PROHIBITED_CONTENT`.
  - **Stopped answers.** `stopped_safety`, `stopped_recitation` and `stopped_spii` raise `LLMInvalidOutputError` naming the reason. The message and `errors` do not contain `"PARTIAL-ANSWER-TEXT"`, and exactly one request is made.
  - **Cut-off answer.** `text_max_tokens` raises `LLMUnavailableError` (not retried, no repair) for both operations.
  - **Empty free text.** `text_empty` and `no_candidates` raise `LLMUnavailableError` for `complete`.
  - **Log line.** The `llm.error` record, via `get_provider` with a test registry or the `_LoggedProvider` path, carries `input_tokens=12` for a blocked prompt.
- [X] T016 [US2] Add the error-mapping and retry tests to `tests/test_llm_google.py`, using `RetryPolicy(max_retries=2)` and the recorded `waits`:
  - **Auth.**
    - `error_401`, `error_403` and `error_400_api_key_invalid` raise `LLMAuthError` naming `INVIO_GOOGLE_API_KEY`, after 1 request.
    - Plain `error_400` raises `LLMInvalidRequestError` with `status == 400`, after 1 request.
    - `error_404_model` raises `LLMInvalidRequestError`, after 1 request.
  - **Rate limits.**
    - `error_429_retry_delay` followed by `text_ok` succeeds, with `waits == [7.0]`.
    - `error_429_retry_after_header` followed by `text_ok` succeeds, with `waits == [3.0]`.
    - `error_429_bare` followed by `text_ok` succeeds, with an exponential-backoff wait.
    - Three times `error_429_retry_delay` raises `LLMRateLimitError` with `retry_after == 7.0`, after 3 requests.
    - `error_429_retry_delay_long` raises `LLMRateLimitError` at once, because the wait exceeds `max_retry_after`.
  - **Quota.** `error_429_daily_quota` and `error_429_zero_quota` raise `LLMQuotaError`, a subclass of `LLMRateLimitError`, with `retry_after is None`, after 1 request.
  - **Server errors.** Three times `error_500`, `error_503` or `error_504` raises `LLMUnavailableError` after 3 requests. `error_503` followed by `text_ok` succeeds.
  - **Transport errors.**
    - `httpx.ConnectError` is retried, then `LLMUnavailableError`.
    - `httpx.ReadTimeout` and `HANG` with `timeout_seconds=0.05` raise `LLMUnavailableError` without retry.
    - `httpx.InvalidURL` / `httpx.LocalProtocolError` raise `LLMUnavailableError` without retry ("could not be sent").
    - `malformed_200` raises `LLMUnavailableError` without retry.
  - **Hygiene**, for every error above:
    - `str(err)` names the model and does not contain the API key, the system or user text, the raw body, or `"PROMPT-ECHO"` beyond the sanitized and truncated detail;
    - `err.__cause__` and `err.__context__` are `None` or not SDK exceptions;
    - `caplog` text does not contain the key or the prompt.
  - **Recorded `llm.retry` log.** It carries provider, model, attempt, status, kind and wait only.

### Implementation for User Story 2

- [X] T017 [US2] In `src/invio/llm/google.py`, add the block handling to `_answer` (before the empty-text and `MAX_TOKENS` checks):
  - **Blocked prompt.** If `response.prompt_feedback` has a `block_reason`, raise `LLMInvalidOutputError` with:
    - message `describe(f"Google blocked the prompt: {reason}{categories}", model, None, sanitize_detail(block_reason_message or ""))`;
    - `errors=<same reason text>`;
    - `usage=<call usage>`, `provider=PROVIDER`, `model=model`.
  - **Stopped answer.** If `candidates[0].finish_reason` is set and not in {`STOP`, `MAX_TOKENS`, `FINISH_REASON_UNSPECIFIED`}, raise the same error with `"Google stopped the answer: <REASON>"`. Never include part text.
  - `<categories>` is `" (HARM_CATEGORY_X, ...)"`, built from the `safety_ratings` with `blocked` true, or empty.
  - Enum values are rendered by their `.name` (or the string value, per T002g).
  - Usage is computed first, so blocked calls report it.
- [X] T018 [US2] In `src/invio/llm/google.py`, add `_retry_delay(details) -> float | None`, which reads the first `RetryInfo.retryDelay` (`"<seconds>s"`, decimal allowed) from `error.details`. Add `_is_quota_exhausted(details) -> bool`, true if any `QuotaFailure` violation has `"PerDay"` in its `quotaId` or a `quotaValue` of `"0"`. Add `_is_api_key_invalid(details) -> bool` (`ErrorInfo.reason` starting with `"API_KEY_"`). Malformed or missing `details` give `None` / `False`, never an exception.
- [X] T019 [US2] In `src/invio/llm/google.py`, add `_safe_detail(exc: errors.APIError) -> str`. It returns `sanitize_detail(exc.message)` if `exc.message` is a `str`, else `""`. It never uses `str(exc)` or `exc.details` text.
- [X] T020 [US2] In `src/invio/llm/google.py`, add `_classify_status(exc: errors.APIError, model, now) -> Failure`:
  - `exc.code` 400 with `_is_api_key_invalid` → `Failure("auth", False, LLMAuthError(describe(f"Google rejected the API key; check {_ENV_VAR}", model, 400), ...), 400)`.
  - 429 with `_is_quota_exhausted` → `Failure("quota", False, LLMQuotaError(describe("Google quota exhausted; check plan, billing and daily limits", model, 429), ...), 429)`.
  - Otherwise delegate to the shared `classify_status(...)` with `detail=_safe_detail(exc)`. Pass `headers`: the response headers, plus `{"retry-after": str(delay)}` when `_retry_delay` gives a value and no `Retry-After` header is present.
- [X] T021 [US2] In `src/invio/llm/google.py`, add `_classify(exc, model, now) -> Failure | None`, checked in this order:
  1. `httpx.TimeoutException` → `timeout_failure`.
  2. `errors.APIError` → `_classify_status`.
  3. `errors.UnknownApiResponseError | json.JSONDecodeError | httpx.DecodingError` → `bad_response_failure`.
  4. `httpx.HTTPError | httpx.InvalidURL` → `classify_transport(..., unsendable=(httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError), bad_response=(httpx.DecodingError, httpx.TooManyRedirects, httpx.StreamError))`.
  5. Anything else → `None`.

  Wire it into `_request`'s `run_with_retries`. Make sure no SDK exception becomes `__cause__` or `__context__`. `run_with_retries` raises outside the handler; verify that with the T016 test.

**Checkpoint**: T015 and T016 pass. Blocks and failures are typed, descriptive and hygienic.

---

## Phase 5: User Story 4 - Provider passes the shared contract (Priority: P2)

**Goal**: Google is substitutable wherever the contract suite is valid.

**Independent Test**: `uv run pytest tests/test_llm_provider_contract.py -k google` passes.

- [X] T022 [US4] In `tests/test_llm_provider_contract.py`:
  - Add `GoogleHarness(ProviderHarness)` with `name = "google"` and `model = "gemini-3.5-flash-lite"`, mapping outcomes to fixtures: `TEXT: "text_ok"`, `STRUCTURED: "json_ok"`, `STRUCTURED_INVALID: "json_invalid"`, `AUTH: "error_401"`, `RATE_LIMIT: "error_429_bare"`, `UNAVAILABLE: "error_503"`, `INVALID_REQUEST: "error_400"`.
  - `build()` uses `google_helpers.make_provider(..., retry=NO_RETRIES)` and sets `self.requests_made`.
  - Add it to `HARNESSES` and to the module docstring list, and import `google_helpers`.
  - Make sure the success fixtures report exactly `RECORDED_USAGE` (12, 3).

**Checkpoint**: the contract suite is green for all five harnesses.

---

## Phase 6: User Story 3 - Verify the setup with `llm test` (Priority: P2)

**Goal**: `invio llm test google` gives a pass/fail answer with the existing exit codes. The command code is unchanged.

**Independent Test**: CLI runs against recorded responses give exit 0, 1 and 2 with the documented stdout and stderr.

- [X] T023 [P] [US3] Extend `tests/test_cli_llm.py`, modelled on the existing Anthropic CLI tests (patch `GoogleProvider` construction to use `google_helpers.make_provider` or a recorded `client_factory`):
  - **Success.** `invio llm test google` with `INVIO_GOOGLE_API_KEY` set and `text_ok` exits 0, and stdout starts with `ok provider=google model=gemini-3.5-flash-lite input_tokens=12 output_tokens=3`. The recorded request has `maxOutputTokens == 5 + 1024` and no temperature.
  - **`--model`.** `--model gemini-3.8-flash` uses that model and `maxOutputTokens == 5 + 4096`.
  - **Missing key.** It exits 2 with `Configuration error` naming `INVIO_GOOGLE_API_KEY`, and no request is made.
  - **Unknown model.** `--model gpt-x` exits 2.
  - **Provider failures.** `error_401` exits 1 with `Error: LLMAuthError` on stderr. Three times `error_503` exits 1 with `LLMUnavailableError`. `blocked_prompt` exits 1 with `LLMInvalidOutputError`. stdout is empty, and stderr does not contain the key.
- [X] T024 [US3] Run `uv run pytest tests/test_cli_llm.py tests/test_llm_factory.py`. Fix any existing test that assumes `google` is unregistered or lists the registered providers, for example `"registered: ..."` strings in tests that do not patch the provider table and the `fallback_provider: google` job tests. Change only the expectation, not `src/invio/cli/commands/llm.py` (FR-014).

**Checkpoint**: CLI behaviour matches [contracts/cli-llm-test.md](contracts/cli-llm-test.md).

---

## Phase 7: Polish & cross-cutting

- [ ] T025 [P] Create `tests/test_llm_google_live.py`, modelled on `tests/test_llm_anthropic_live.py`:
  - It is marked `live` and captures `INVIO_GOOGLE_API_KEY` at import, skipping without it.
  - (a) Connectivity check on the cheapest model with `max_tokens=5`, `temperature=0`: the text is non-empty and `input_tokens > 0`.
  - (b) `complete_structured` with `RelevanceResult` on the cheapest model.
  - (c) `complete_structured` with `ItemSummary` on `gemini-3.8-flash`, which proves the converted schemas are accepted.
- [ ] T026 [P] Update the docs:
  - `README.md`: a Google section next to the Anthropic one at `README.md:489`, covering:
    - `INVIO_GOOGLE_API_KEY` and `provider: google`;
    - the shipped models in `models.d/google.yaml`;
    - the thinking level, the allowance and the default-temperature behaviour;
    - that blocks raise `LLMInvalidOutputError`;
    - that only the Gemini Developer API is used (`GOOGLE_*` / `GEMINI_*` environment variables are ignored);
    - `invio llm test google`.

    Also update the registry example near `README.md:540` with the three new fields.
  - `docs/deployment.md:81`: replace "`google` has no provider implementation yet" with "jobs can use `mistral`, `openai`, `anthropic` and `google`".
  - `docs/deployment.md:230`: add `google` to the `llm test` provider list.
  - `.env.example`: make sure `INVIO_GOOGLE_API_KEY` is present.
- [ ] T027 Run the CI gates locally: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy`, and `uv run pytest` (coverage `fail_under = 95` must hold). Then run the offline steps of `specs/019-gh-issue-32/quickstart.md`. Fix any issue in the files touched by this feature.
- [ ] T028 Confirm the PR description, filed on branch `gh-issue-32` and referencing #32:
  - justifies the new runtime dependency `google-genai` and its transitive packages (`google-auth`, `requests`, `tenacity`, `websockets`);
  - notes the `gemini-3.8-flash` price change on 2027-01-01;
  - lists the three new optional registry fields.

---

## Dependencies & Execution Order

- **Setup**:
  - T001 comes first: every later commit goes to `gh-issue-32`.
  - T002 next; its SDK findings may adjust the field names used in T010, T012 and T017.
- **Foundational**: T003 → T004 (the registry file parses only with the new fields) → T005. T006 is independent. Phase 2 blocks all stories.
- **US1 (P1)**: T007–T011 (tests and fixtures, parallel) → T012.
- **US2 (P1)**: depends on T012. T013–T016 (parallel) → T017 → T018 → T019 → T020 → T021. T017 can run in parallel with T018–T019, since they are separate functions in the same file; keep them sequential to avoid edit conflicts.
- **US4 (P2)**: depends on T012 (success) and T021 (error outcomes) → T022.
- **US3 (P2)**: depends on T012 and T021 → T023 → T024.
- **Polish**: T025 and T026 after T012; T027 after everything; T028 last.

### Parallel opportunities

- T006 can run in parallel with T003–T005.
- T007, T008, T009, T010 and T011 can run in parallel (different files).
- T013, T014 and T015 can run in parallel; T016 follows T015 because both edit `tests/test_llm_google.py`.
- T023, T025 and T026 can run in parallel.

### Parallel example: User Story 1

```text
T007 tests/google_helpers.py
T008 tests/fixtures/google/*.json (success)
T009 tests/test_llm_google_schema.py
T010 tests/test_llm_google.py (US1 cases)
T011 tests/test_llm_factory.py
→ then T012 src/invio/llm/google.py
```

## Implementation Strategy

1. **MVP**: Phases 1–3 (US1). A Google job runs end to end offline, with the pipeline schemas.
2. **Safety**: Phase 4 (US2). Blocks and errors are typed. This is required before any unattended use, because both P1 stories are needed to ship.
3. Phase 5 (US4) contract, then Phase 6 (US3) CLI.
4. Polish: live test, docs, gates, PR.

## Notes

- Never use `str(errors.APIError)`; it embeds the full response body.
- Keep `src/invio/llm/factory.py`, `base.py`, `http_retry.py` and `cli/commands/llm.py` unchanged. A need to change them means the design is off: re-check research R6.
- Every task ends with its own tests green. Commit after each phase.
