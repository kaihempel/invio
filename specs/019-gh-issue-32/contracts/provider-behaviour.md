# Contract: Google provider behaviour

The provider implements `invio.llm.base.LLMProvider` unchanged:

```python
async def complete(system, user, *, model, temperature, max_tokens) -> tuple[str, Usage]
async def complete_structured(system, user, schema, *, model, temperature) -> tuple[T, Usage]
async def aclose() -> None
```

## Guarantees

1. **Resolution.** `get_provider("google")` and `resolve(llm_config, role)` with
   `provider: google` return the wrapped provider. The factory is not edited.
2. **Building the provider.**
   - Building without a key raises `LLMAuthError` naming `INVIO_GOOGLE_API_KEY`, and no request
     is made.
   - Building with a registry where a `google` model lacks `thinking_level` or
     `thinking_allowance_tokens` raises `LLMConfigError`.
3. **Free text.** `complete` makes one `generateContent` request per attempt, containing:
   - `systemInstruction` and the user text;
   - `thinkingConfig.thinkingLevel` = the registered level;
   - `maxOutputTokens` = `max_tokens` + the registered allowance;
   - `temperature`, unless the model keeps its default temperature, in which case the field is
     absent.

   It returns the joined non-thought text and `Usage(prompt, candidates + thoughts)`.
4. **Structured output.**
   - `complete_structured` sends `responseMimeType: application/json` and `responseJsonSchema`
     built by `gemini_schema`, which has no `$ref` or `$defs` and only supported keywords. It
     sends no `maxOutputTokens`.
   - The answer is validated against the original model, with at most one repair request. The
     returned usage is the sum over requests.
   - `RelevanceResult`, `ItemSummary`, and a nested model with lists and shared sub-models
     round-trip intact.
   - A recursive schema raises `LLMConfigError` before any request is sent.
5. **Blocks.** A blocked prompt (`promptFeedback.blockReason`) or a stopped answer (`finishReason`
   other than `STOP` or `MAX_TOKENS`) raises `LLMInvalidOutputError`:
   - it names the reason and any blocked safety categories;
   - it carries the call's usage;
   - it is raised after exactly one request, with no retry and no repair.
6. **Incomplete or empty answers.** `MAX_TOKENS` raises `LLMUnavailableError`, with no repair.
   Empty free text also raises `LLMUnavailableError`. Empty or non-JSON structured text goes
   through the single repair attempt.
7. **Retries.** Errors are mapped as in [research R6](../research.md):
   - Rate limits are retried by `http_retry.run_with_retries`, honouring `RetryInfo.retryDelay`
     or `Retry-After` up to `max_retry_after`. So are 5xx responses and connection failures.
   - Raised after one attempt: timeouts, auth failures (including 400 `API_KEY_INVALID`), daily
     or zero quota (`LLMQuotaError`), other 4xx responses, and unusable responses.
8. **Error hygiene.**
   - Error messages name provider and model, and include at most the status and the sanitized
     `error.message`.
   - They never contain the key, the prompt, the answer or the raw body.
   - The SDK exception is neither `__cause__` nor `__context__`.
9. **Credentials and SDK settings.**
   - Only `x-goog-api-key` is sent, to the configured base URL.
   - `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_GEMINI_BASE_URL` and
     `GOOGLE_GENAI_USE_VERTEXAI` have no effect.
   - SDK retries are disabled.
10. **Timeouts and logging.** Each call is bounded by `llm_timeout_seconds`. The `llm.call`,
    `llm.error` and `llm.retry` log lines are emitted as for other providers, and `llm.error`
    reports the usage of a blocked call.
