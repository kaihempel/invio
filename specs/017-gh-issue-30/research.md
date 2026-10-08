# Research: OpenAI Provider

## R1 – Responses API vs Chat Completions
- **Decision**: Responses API, stateless (`store=False`), with system and user message as `input`.
- **Rationale**: It is OpenAI's recommended API for new work. Strict JSON schema is `text.format` with `type: json_schema, strict: true`. Usage is `input_tokens`/`output_tokens`, which map 1:1 to `Usage`. `max_output_tokens` is the single limit parameter for all models.
- **Alternatives**: Chat Completions is closest to the Mistral code (less new surface), but `max_tokens` vs `max_completion_tokens` differs by model family. Rejected as the long-term path; the `LLMProvider` protocol hides the choice, so switching later is local to one module.
- **Verify at implementation**: exact SDK field names against the pinned SDK version (`output_text`, `usage`, `status` / `incomplete_details`, refusal items).

## R2 – Retry/backoff sharing
- **Decision**: Extract `RetryPolicy`, the failure record, `Retry-After` parsing and the attempt loop from `mistral.py` into `llm/http_retry.py`. The loop is parametrized by a provider-specific `classify(exc, model, now)`. Both providers use it. SDK retries are disabled (`max_retries=0`) so only one layer retries.
- **Rationale**: The issue requires shared logic; copying about 120 lines would let the two providers drift. Mistral's tests pin the behaviour. The graph-level `RetryingProvider` (`llm/retry.py`) is a separate outer layer and stays untouched.
- **Alternatives**: rely on the SDK's built-in retry (different semantics, cannot honour `max_retry_after`; rejected); copy the code (drift; rejected).
- `RetryPolicy` stays importable from `invio.llm.mistral` (re-export) so existing imports keep working.

## R3 – Error mapping (parity with Mistral)
| Situation | Typed error | Retried |
|---|---|---|
| Timeout (SDK timeout, `with_timeout` deadline) | `LLMUnavailableError` | no |
| Connection failure | `LLMUnavailableError` | yes |
| 401 / 403 | `LLMAuthError` (names `INVIO_OPENAI_API_KEY`) | no |
| 429 (+ finite `Retry-After`) | `LLMRateLimitError(retry_after)` | yes, wait ≤ `max_retry_after` |
| 5xx | `LLMUnavailableError` | yes |
| Other 4xx (400, 404 unknown model, 422, …) | `LLMInvalidRequestError(status)` | no |
| Undecodable / unexpected body or status | `LLMUnavailableError` | no |
| Empty text, refusal, incomplete (length cut-off) | `LLMUnavailableError`; never returned as valid | no |

Messages: status + model + sanitized provider message from the JSON body's `error.message` (whitespace collapsed, ≤300 chars). Never the key, prompt or answer. The SDK exception is not chained.

- **OpenAI-specific case**: a 429 with code `insufficient_quota` means exhausted billing, not a transient limit. **Decision**: raise `LLMRateLimitError` without a wait hint and mark it non-retryable, so it is not retried pointlessly. Mistral has no equivalent, so this is the one deliberate divergence.

## R4 – Strict structured output
- **Decision**: Send `schema.model_json_schema()` after a transform: close all objects (`additionalProperties: false`, reusing Mistral's `_strict_schema`, moved to the shared module) and list every property in `required` (OpenAI strict-mode requirement). The schema `name` is sanitized to `[a-zA-Z0-9_-]{1,64}`.
- **Unsupported schema / model**: a 400 that rejects the schema surfaces as `LLMInvalidRequestError`; it is not silently retried non-strict, which would hide bugs. Only models that support `json_schema` output are registered, and the repair helper is the safety net. This satisfies the spec's "fallback where unsupported".
- Pydantic validation, the real contract, stays in `structured_with_repair`.
- Refusal and length cut-off answers raise `LLMUnavailableError` (Mistral has no equivalent handling; spec edge case states this explicitly).

## R5 – Model registry content
- **Decision**: `models.d/openai.yaml` with two pinned non-reasoning models (first = fast, second = smart, as in `mistral.yaml`), no `-latest` aliases, and a header comment with verification date and source. Non-reasoning models accept `temperature`; reasoning models reject it, so they are excluded for now.
- **Open item for implementation**: ids, prices and context windows MUST be copied from OpenAI's official model and pricing pages on the day of implementation. They are deliberately not guessed here.

## R6 – Client lifecycle and tests
- One `AsyncOpenAI` per running event loop, built lazily and lock-guarded (same pattern as Mistral). Tests inject `http_client=httpx.AsyncClient(transport=MockTransport(recorder))` and a `base_url`, plus fake `sleep` / `uniform` / `now` as in the Mistral tests.
- `httpx` is already a transitive dependency of `openai`; declare it explicitly only if tests import it directly and the tooling requires it.

## R7 – Shared contract tests
No parametrized cross-provider suite exists yet (only per-provider files). **Decision**: add `tests/test_llm_provider_contract.py`, parametrized over `FakeProvider`, `MistralProvider` and `OpenAIProvider` (the latter two on recorded HTTP). It covers text + usage, structured value + usage, one repair then `LLMInvalidOutputError` with summed usage, and the typed errors. This realizes the acceptance criterion "pass the shared provider contract tests".

## R8 – Existing tests that assume `openai` is unregistered
`test_llm_factory.py::test_schema_provider_without_module_is_unknown` and `test_cli_llm.py::test_unknown_provider_is_rejected_before_reading_the_key` use `patched_providers`, which replaces the registry, so they are probably unaffected. If real discovery leaks in, switch their provider name to `anthropic` or `google`.

## R9 – SDK verification (T001, openai 2.54.0, 2026-10-08)
Confirmed against the installed SDK (`uv add "openai>=2,<3"` resolved 2.54.0):
- `Response` has `status` (`completed|failed|in_progress|cancelled|queued|incomplete`), `incomplete_details`, `usage`, `output`, and the `output_text` property. `ResponseUsage` has `input_tokens`/`output_tokens`. A refusal is an `output[*].content[*]` item with `type == "refusal"` and a `refusal` text field.
- Exceptions: `APITimeoutError` is a **subclass** of `APIConnectionError` (check it first); `APIConnectionError`, `APIStatusError`, `APIResponseValidationError` derive from `APIError`. The SDK raises timeouts/connection errors `from` the underlying `httpx` exception (`__cause__`), which the classifier inspects.
- Differences from the design assumptions:
  - `APIStatusError.body` is already unwrapped (`body["error"]` when the payload has an `error` key), so `error.message` and `error.code` are read from `exc.body` directly. `exc.message` is the SDK's own text and embeds the raw body (`Error code: 400 - {...}`): it must never be used, nor `str(exc)`.
  - `AsyncOpenAI` accepts an `httpx.AsyncClient` (or `httpx2.AsyncClient`); `httpx` is a hard dependency of `openai` and is imported only for exception types in `openai.py`. It is not declared separately.
  - `AsyncOpenAI.close()` closes the underlying HTTP client (injected or its own), so `aclose` needs only the SDK client.
