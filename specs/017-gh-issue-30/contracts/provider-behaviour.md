# Contract: OpenAI provider behaviour

Implements `invio.llm.base.LLMProvider`:
- `complete(system, user, *, model, temperature, max_tokens) -> (str, Usage)`
- `complete_structured(system, user, schema, *, model, temperature) -> (T, Usage)`
- `aclose()`

Rules (each covered by a test):
1. Request: one stateless Responses call; system and user as input; `temperature`; `max_output_tokens` for `complete` only; `complete_structured` adds a strict `text.format` json_schema.
2. Success: non-empty text required; usage taken from the response (`0` when absent).
3. Structured: invalid answer, then exactly one repair call with the validation error appended, then `LLMInvalidOutputError` (usage = sum of calls).
4. Errors: see the table in research.md R3; same type and retry behaviour as Mistral for each category (exception: `insufficient_quota`).
5. Retries: shared loop; backoff `base_delay * 2^(n-1)` with jitter; `Retry-After` honoured up to `max_retry_after`.
6. No secret, prompt or answer text in any error, log or repr; SDK exception not chained.
7. Every call bounded by `llm_timeout_seconds`.
8. Registered via `@register_provider("openai")`, discovered without editing `factory.py`; `from_settings` raises `LLMAuthError` naming `INVIO_OPENAI_API_KEY` if unset.
