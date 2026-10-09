# Contract: Anthropic provider behaviour

The provider implements `invio.llm.base.LLMProvider` unchanged:

```python
async def complete(system, user, *, model, temperature, max_tokens) -> tuple[str, Usage]
async def complete_structured(system, user, schema, *, model, temperature) -> tuple[T, Usage]
async def aclose() -> None
```

## Guarantees

1. `get_provider("anthropic")` and `resolve(llm_config, role)` with `provider: anthropic`
   return the wrapped provider. The factory is not edited.
2. Building without a key raises `LLMAuthError` naming `INVIO_ANTHROPIC_API_KEY`, and no
   request is made. Building with a registry where an `anthropic` model lacks
   `max_output_tokens` raises `LLMConfigError`.
3. `complete` makes one Messages request per attempt with `system`, the user message,
   `max_tokens` and `temperature`, and returns the joined text and `Usage` from `usage`.
4. `complete_structured` sends one tool built from `schema.model_json_schema()` and forces it.
   The request's `max_tokens` is the model's registered `max_output_tokens`. The tool input is
   validated, with at most one repair request; the returned usage is the sum over requests
   (`requests` = 1 or 2). Nested models and lists of models round-trip intact.
5. An answer cut off by `max_tokens`, a refusal, or empty free text raises
   `LLMUnavailableError`, with no repair and no retry.
6. A missing `tool_use` block or invalid tool input goes through the repair attempt, then
   `LLMInvalidOutputError` carrying the summed usage.
7. Errors are mapped as in [research.md R6](../research.md). 429, 5xx, 529 and connection
   failures are retried by `http_retry.run_with_retries`, which honours `Retry-After` up to
   `max_retry_after`. Timeouts, auth, quota/billing and other 4xx failures are raised after one
   attempt.
8. Error messages name provider and model, and include at most the status and the sanitized
   provider message. They never contain the key, the prompt, the answer, the refusal text or
   the raw body. The SDK exception is neither `__cause__` nor `__context__`.
9. Only `x-api-key` is sent, to the configured base URL, regardless of `ANTHROPIC_*`
   environment variables. SDK retries are disabled.
10. Each call is bounded by `llm_timeout_seconds`, and the `llm.call` / `llm.error` /
    `llm.retry` log lines are emitted as for other providers.
