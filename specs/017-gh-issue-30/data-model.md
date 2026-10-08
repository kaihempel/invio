# Data Model: OpenAI Provider

No new persisted entities. Shapes used:

- **OpenAIProvider** (class, registered as `openai`): `api_key` (secret, from `INVIO_OPENAI_API_KEY`), `timeout_seconds` (from `llm_timeout_seconds`), `retry: RetryPolicy`, injectable `client_factory`, `base_url`, `sleep`, `uniform`, `now` (for tests). One SDK client per event loop. `repr` shows timeout and retry only.
- **RetryPolicy** (frozen dataclass, moved to `llm/http_retry.py`): `max_retries=3`, `base_delay=1.0`, `jitter=0.25`, `max_retry_after=60.0`; validation unchanged.
- **Failure** (internal): `kind`, `retryable`, `error: LLMError`, `status`, `retry_after`.
- **Registry entry** (`models.d/openai.yaml`, existing schema v1): `provider: openai`; per model `input_price_per_mtok`, `output_price_per_mtok`, `context_window`.
- **Usage / LLMError family**: unchanged (`invio.llm.base`). `Usage(input_tokens, output_tokens)` comes from the response's usage; missing usage gives `0, 0`.
