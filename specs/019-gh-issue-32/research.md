# Research: Google (Gemini) Provider

Sources:
- `google-genai` 2.29.0, the latest on PyPI on 2026-10-09. It was installed in a scratch venv and its `_api_client.py`, `errors.py`, `types.py` and `models.py` were read.
- Gemini API docs (fetched 2026-10-09): models overview, pricing ("Last updated 2026-10-09"), thinking, structured output, the Gemini 3 developer guide, and the model pages for `gemini-3.8-flash` and `gemini-3.5-flash-lite`.
- Community reports of 429 and invalid-key response bodies.
- The existing providers `src/invio/llm/anthropic.py` and `openai.py`.

## R1 – Model selection

- **Decision**: Register `gemini-3.5-flash-lite` (fast) and `gemini-3.8-flash` (smart). Both are
  stable, have a 1,048,576-token input limit and a 65,536-token output limit, and support
  structured output and thinking.
- **Rationale**:
  - The models page recommends "3.5 Flash-Lite or 3.8 Flash" for new projects.
  - The only Pro-tier text model is `gemini-3.1-pro-preview`, which is a preview. Its predecessor
    `gemini-3-pro-preview` has already been shut down, so a preview model is too short-lived for
    unattended jobs.
  - Access to the 2.5 family is limited to past users.
  - Flash-Lite is the cheapest model, so it is the `llm test` default (`registry.cheapest`).
- **Prices** (paid tier, standard, USD per 1M tokens, thinking billed as output):

  | Model | Input | Output |
  |---|---|---|
  | `gemini-3.5-flash-lite` | 0.30 | 2.50 |
  | `gemini-3.8-flash` | 0.75 (1.50 from 2027-01-01) | 3.75 (7.50 from 2027-01-01) |

  The registry holds one price per model, so it records the price valid today. The file header
  states the 2027-01-01 increase, which needs a registry update on that date.
- **Alternatives considered**:
  - `gemini-3.1-pro-preview` as smart: preview status with a shutdown risk, and it costs more.
  - `gemini-3.5-flash` as smart: allows `minimal` thinking, but at 1.50/9.00 it costs more than
    3.8 Flash and is older.
  - The 2.5 family: restricted access.

## R2 – Thinking (spec Clarification 1)

- **Decision**:
  - Each Google registry entry carries `thinking_level` (the lowest level the model accepts) and
    `thinking_allowance_tokens`.
  - Registered values: `gemini-3.5-flash-lite` uses `minimal` with an allowance of 1024;
    `gemini-3.8-flash` uses `low` with an allowance of 4096, because it rejects `minimal`.
  - Every request sends `thinking_config=ThinkingConfig(thinking_level=<level>)`.
  - Free text sends `max_output_tokens = max_tokens + thinking_allowance_tokens`.
  - Structured calls send no `max_output_tokens`, so the model's own maximum (65,536) applies,
    matching the "model's default limit" in FR-013b.
- **Rationale**:
  - The thinking docs say `max_output_tokens` "sets the maximum number of tokens a response can
    generate, including thought tokens". Reaching it while thinking gives an empty or truncated
    answer that is still billed.
  - The thinking table lists no way to switch thinking off on a Gemini 3 model. `minimal` is the
    lowest level where offered, and the docs do not promise that it means zero thinking.
    3.8 Flash and 3.7 Flash accept only low, medium and high, and `minimal` returns an error.
  - An allowance per model therefore covers both cases. The spec now requires the allowance for
    every Google entry.
  - `thinking_level` is the documented control for Gemini 3. `thinking_budget` is the 2.5-era
    control and is not used.
- **Usage**: `output_tokens = candidates_token_count + thoughts_token_count` and
  `input_tokens = prompt_token_count`. `prompt_token_count` already includes cached tokens.
  Missing counts are treated as 0.
- **Alternatives considered**: leaving the default thinking level (rejected in clarification);
  sending `thinking_budget=0` (rejected by Gemini 3 models).

## R3 – Temperature (spec Clarification 2)

- **Decision**:
  - A registry entry may carry `keep_default_temperature: true`. For such a model the request
    omits `temperature`; otherwise the caller's value is sent.
  - Both registered models set the flag.
- **Rationale**: The Gemini 3 developer guide says: "For all Gemini 3 models, we strongly
  recommend keeping the temperature parameter at its default value of 1.0 … setting it to less
  than 1.0 may lead to unexpected behavior, looping, or degraded performance". The pipeline
  always sends 0.0 (`graph/nodes/llm_calls.py`), and `llm test` sends 0.
- **Consequence**: For Google, answers are not deterministic across runs. Structured answers are
  still validated and repaired, and the offline tests are deterministic because they use
  recorded responses.
- **Alternatives considered**: always sending the caller's temperature (quality risk); clamping
  to 1.0 (equivalent, but less explicit than omitting the parameter).

## R4 – SDK version, client construction and call shape

- **Decision**:
  - Pin `google-genai>=2.29,<3`.
  - Build one client per event loop:

    ```python
    genai.Client(
        vertexai=False,
        api_key=<key>,
        http_options=HttpOptions(
            base_url=<explicit>,
            api_version="v1beta",
            timeout=(llm_timeout + 5) * 1000,  # milliseconds
            httpx_async_client=<httpx.AsyncClient>,
            retry_options=None,
        ),
    ).aio
    ```

  - Each attempt is one `await aio.models.generate_content(model=<id>, contents=<user>,
    config=GenerateContentConfig(...))`. The config sets `system_instruction` (when given),
    `temperature` (unless the model keeps its default), `max_output_tokens` (free text only),
    `thinking_config`, and, for structured calls, `response_mime_type="application/json"` and
    `response_json_schema=<converted schema>`.
- **Rationale**:
  - **Retries**: `retry_args(None)` returns `stop_after_attempt(1)`, so the SDK never retries
    unless `retry_options` is set. The shared loop in `http_retry` does all retrying.
  - **Environment overrides**: `vertexai=False` stops `GOOGLE_GENAI_USE_VERTEXAI` and
    `GOOGLE_GENAI_USE_ENTERPRISE` from switching the backend. The explicit `api_key` takes
    precedence over `GOOGLE_API_KEY` and `GEMINI_API_KEY`. The explicit `base_url` stops
    `GOOGLE_GEMINI_BASE_URL` from redirecting the key. The key is sent only as the
    `x-goog-api-key` header.
  - **HTTP client**: when `httpx_async_client` is given, the SDK uses it even if aiohttp is
    installed, and tests inject an `httpx.MockTransport`. The provider owns that client, so
    `LoopClients.aclose()` closes it.
  - **Transport**: the SDK uses `httpx` 0.28, which is already in `uv.lock` as a transitive
    dependency, not `httpx2`. The provider imports `httpx` only to classify transport
    exceptions. The ruff `httpx2` ban applies to `scheduling` and `sources` only.
  - New transitive packages: `google-auth`, `requests`, `tenacity` and `websockets`. The PR
    description must justify the new dependency (constitution).
- **Alternatives considered**:
  - Raw REST calls through `httpx2`: the issue mandates the SDK.
  - `response_schema` (the OpenAPI subset, which also takes Pydantic classes):
    `response_json_schema` takes standard JSON Schema, which `t_json_schema` passes through
    unchanged, and it is what the structured-output docs describe.
  - The Interactions API: newer, but the issue names `models.generate_content`.

## R5 – Schema conversion (FR-005)

- **Decision**: A pure function `gemini_schema(schema_json) -> dict` that does three things:
  1. Inlines every local `$ref` into `#/$defs/...` and drops `$defs`. A cycle raises
     `LLMConfigError` before any call.
  2. Keeps only the documented keywords: `type`, `title`, `description`, `properties`,
     `required`, `additionalProperties`, `items`, `prefixItems`, `minItems`, `maxItems`, `enum`,
     `format`, `minimum`, `maximum`, `anyOf`, and `type` arrays including `"null"`. Everything
     else is dropped, for example `minLength`, `maxLength`, `pattern`, `const`, `default`,
     `exclusiveMinimum` and `exclusiveMaximum`.
  3. Leaves `properties` names untouched, so a field called `default` or `pattern` survives.
- **Validation**: Validation always uses the original Pydantic model through
  `structured_with_repair`, so dropped constraints are still enforced. The repair instruction
  still embeds the full original JSON Schema, as for the other providers.
- **Rationale**: The structured-output docs say "Not all JSON Schema features are supported" and
  "very large or deeply nested schemas may be rejected", and do not document `$defs`. Inlining
  and dropping is the safe common subset.
- **Pipeline schemas**:
  - `RelevanceResult` has no `$defs`. Its `score` keeps `minimum`/`maximum`, `reason` loses
    `minLength`, and `key_points` is `array<string>`.
  - `ItemSummary` keeps `minItems: 3` and `maxItems: 6`, and loses `minLength`. The headline
    validator is local only.
  - Both keep `additionalProperties: false`.
- **Alternatives considered**: sending the Pydantic schema unchanged (risks a 400 on `$defs` or
  unknown keywords); reusing `http_retry.strict_schema` (it closes objects but does not inline
  or drop keywords).

## R6 – Error and block mapping

SDK behaviour:
- Non-200 responses raise `errors.ClientError` (4xx) or `errors.ServerError` (5xx), both
  subclasses of `APIError`, with `.code`, `.status` (gRPC status), `.message`, `.details` (the
  parsed body) and `.response` (`httpx.Response`).
- Transport failures propagate as raw `httpx` exceptions.
- A non-JSON 200 body raises `errors.UnknownApiResponseError`.
- `str(APIError)` embeds the whole body, so it is never used.

Classification for Gemini:

| Case | Typed error | Retried |
|---|---|---|
| 401, 403 (`PERMISSION_DENIED`, `UNAUTHENTICATED`) | `LLMAuthError` naming `INVIO_GOOGLE_API_KEY` | no |
| 400 with `ErrorInfo.reason` `API_KEY_INVALID`, or `API_KEY_*` (Gemini reports a bad key as 400 `INVALID_ARGUMENT`) | `LLMAuthError` | no |
| 429 whose `QuotaFailure` violations include a `quotaId` containing `PerDay`, or a `quotaValue` of `"0"` (no quota on this tier) | `LLMQuotaError` | no |
| Other 429 | `LLMRateLimitError`; the wait hint is `RetryInfo.retryDelay` (`"27s"` / `"1.5s"`), else `Retry-After` | yes |
| 5xx (500 `INTERNAL`, 503 `UNAVAILABLE`/overloaded, 504 `DEADLINE_EXCEEDED`) | `LLMUnavailableError` | yes |
| Other 4xx (400 `INVALID_ARGUMENT` / `FAILED_PRECONDITION`, 404 unknown model, 413) | `LLMInvalidRequestError` | no |
| `httpx.TimeoutException` | `LLMUnavailableError` | no |
| `httpx.ConnectError` and other transport errors | `LLMUnavailableError` | yes |
| `httpx.InvalidURL`, `UnsupportedProtocol`, `LocalProtocolError` (could not be sent) | `LLMUnavailableError` | no |
| `UnknownApiResponseError`, `httpx.DecodingError`, `json.JSONDecodeError` (unusable response) | `LLMUnavailableError` | no |

Implementation notes:
- `RetryInfo.retryDelay` is passed to the shared `classify_status` as a synthetic `retry-after`
  header when the response has none. `classify_status` therefore stays unchanged and still caps
  the wait at `max_retry_after`.
- The error message detail is the sanitized `error.message` only (`sanitize_detail`).

Every 429 message says "You exceeded your current quota", so the message cannot tell a daily
quota from a per-minute limit. The `quotaId` can, so a daily quota fails at once instead of
waiting out a misleading `retryDelay`.

Answers that come back with HTTP 200 (handled in `_answer`):

| Response | Result |
|---|---|
| `prompt_feedback.block_reason` set | `LLMInvalidOutputError`: "Google blocked the prompt: `<REASON>`" plus the categories of ratings with `blocked=true` and the sanitized `block_reason_message`. `errors` is the same text and `usage` is the call's usage. |
| First candidate's `finish_reason` neither `STOP`, `MAX_TOKENS` nor unspecified (`SAFETY`, `RECITATION`, `BLOCKLIST`, `PROHIBITED_CONTENT`, `SPII`, `LANGUAGE`, `OTHER`, `IMAGE_*`, …) | `LLMInvalidOutputError`: "Google stopped the answer: `<REASON>`" plus blocked categories. Partial text is discarded. |
| `finish_reason` `MAX_TOKENS` | `LLMUnavailableError` ("answer is incomplete") |
| No candidates or no text parts, free text | `LLMUnavailableError` |
| No candidates or no text parts, structured | `""`, which fails validation and is repaired once |

- The answer text joins the non-thought `text` parts. Parts with `thought=True` are skipped,
  because `response.text` would warn on mixed parts.
- An `LLMInvalidOutputError` raised inside an attempt is an `LLMError`. `run_with_retries`
  therefore re-raises it without retrying, and `structured_with_repair` lets it propagate without
  a repair attempt, which is the FR-007 behaviour without changes to shared code.
- The `llm.error` log line already reports the usage of `LLMInvalidOutputError`.

## R7 – Test harness

- **Decision**:
  - `tests/google_helpers.py` reuses `tests/sdk_harness.py` with `http=httpx` and fixtures in
    `tests/fixtures/google/*.json` (REST `generateContent` bodies: `candidates`,
    `usageMetadata`, `promptFeedback`).
  - `sdk_harness.RecordedRequest` gains the request headers, so tests can assert that
    `x-goog-api-key` is sent and that no `authorization` header is sent.
  - `GoogleHarness` is added to the shared contract `HARNESSES`.
  - Contract success fixtures report 12 prompt and 3 candidate tokens, with no thoughts.
- **Rationale**: same pattern as Anthropic and OpenAI. `sdk_harness` already takes the HTTP
  module as a parameter.
- Live test `tests/test_llm_google_live.py`, marked `live` and skipped without
  `INVIO_GOOGLE_API_KEY`: a connectivity check on the cheapest model, a structured check with
  `RelevanceResult`, and one with `ItemSummary`. The structured checks confirm the converted
  schema is accepted.
