# Data Model: Mistral LLM Provider (#8)

No database or persisted-schema changes. The "data" of this feature are in-process value
objects, the shared error types, one registry file and test fixture files.

## Shared errors (`src/invio/llm/base.py`) — changes

| Type | Change | Fields | Notes |
|---|---|---|---|
| `LLMError` | unchanged | `provider: str \| None`, `model: str \| None` | base of all typed errors |
| `LLMRateLimitError` | **extended** | + `retry_after: float \| None = None` (keyword-only, seconds, ≥ 0) | set when the provider sent a parseable `Retry-After`; kept on the final error after exhausted retries and on the fail-fast path (> cap) |
| `LLMInvalidRequestError` | **new**, subclass of `LLMError` | + `status: int \| None` (HTTP status) | provider rejected the request itself (400/404/422/other 4xx); never retried |
| `LLMAuthError`, `LLMUnavailableError`, `LLMInvalidOutputError` | unchanged | | |

Validation: `retry_after`, when given, must be finite and ≥ 0 (`ValueError` otherwise).
Messages never contain credentials, prompt or answer text (FR-014).

## `RetryPolicy` (`src/invio/llm/mistral.py`)

Frozen dataclass, constructor option of the provider (FR-018). Not an operator setting.

| Field | Type | Default | Rule |
|---|---|---|---|
| `max_retries` | `int` | `3` | ≥ 0; attempts = `max_retries + 1` |
| `base_delay` | `float` (s) | `1.0` | > 0; wait for retry *n* = `base_delay * 2**(n-1) * jitter_factor` |
| `jitter` | `float` | `0.25` | 0 ≤ jitter < 1; `jitter_factor ∈ [1-jitter, 1+jitter]` |
| `max_retry_after` | `float` (s) | `60.0` | > 0; a larger `Retry-After` → fail fast with `LLMRateLimitError` |

Invalid values raise `ValueError` at construction.

## Failure classification (internal)

Each failed attempt is classified into one outcome (see research R5):

| Kind | Retryable | Raised as |
|---|---|---|
| `auth` (401/403) | no | `LLMAuthError` |
| `rate_limit` (429) | yes, unless `retry_after > max_retry_after` | `LLMRateLimitError(retry_after)` |
| `server` (5xx) | yes | `LLMUnavailableError` |
| `connection` (transport error, no response) | yes | `LLMUnavailableError` |
| `timeout` | no | `LLMUnavailableError` |
| `invalid_request` (other 4xx) | no | `LLMInvalidRequestError(status)` |
| `bad_response` (2xx not parseable / no text) | no | `LLMUnavailableError` |

State per request: `attempt = 1 … max_retries+1`; on a retryable failure with attempts left →
log `llm.retry`, `await sleep(wait)`, next attempt; otherwise raise. A structured completion's
repair request starts its own attempt counter.

## `MistralProvider` (`src/invio/llm/mistral.py`)

Registered as `mistral` via `@register_provider("mistral")`.

| Attribute | Source |
|---|---|
| API key (secret, never logged/repr'd) | `require_api_key(settings, "mistral")` → `INVIO_MISTRAL_API_KEY` |
| `timeout_seconds` | `settings.llm_timeout_seconds` (default 60) |
| `retry` | `RetryPolicy()` unless given |
| `sleep`, `uniform`, `now` | injectable (defaults `asyncio.sleep`, `random.uniform`, `datetime.now(UTC)`) |
| `client_factory` | `Callable[[], httpx2.AsyncClient]`, called once per event loop (default `httpx2.AsyncClient(follow_redirects=True)`; tests return a `MockTransport` client) (FR-027, R15) |
| cached client + loop (private) | the `Mistral` client and the event loop it was built for; rebuilt when used from another loop |
| `server_url` | optional override (default: SDK default) |

## Usage mapping

`Usage(input_tokens=usage.prompt_tokens or 0, output_tokens=usage.completion_tokens or 0)`;
`requests=1` per successful HTTP response (failed attempts contribute nothing). Structured
completions sum usages via `structured_with_repair` (FR-009).

## Registry file `src/invio/llm/models.d/mistral.yaml`

#7 format (`schema_version: 1`, `provider: mistral`, `models: {id: {input_price_per_mtok,
output_price_per_mtok, context_window}}`). Rule added by this feature: ids are pinned versions
— no id ends with `-latest` (FR-004, tested). Initial content: see research R11.

## Test fixtures `tests/fixtures/mistral/*.json`

One file per recorded response:

```json
{"status": 200, "headers": {"content-type": "application/json"}, "body": { ...chat completion... }}
```

Planned set: `chat_ok.json`, `chat_ok_chunks.json` (content as chunk list),
`chat_no_usage.json`, `chat_empty_choices.json`, `structured_ok.json`,
`structured_invalid.json`, `error_401.json`, `error_403.json`, `error_429.json`,
`error_429_retry_after.json`, `error_429_retry_after_long.json`, `error_400.json`,
`error_404_model.json`, `error_422.json` (body echoes an `input` field to prove it is not
leaked), `error_500.json`, `error_503.json`, `malformed_200.json`. Bodies are recorded from
real API responses where possible and scrubbed of ids/keys; synthetic otherwise.
