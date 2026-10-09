# Data Model: Anthropic (Claude) Provider

## ModelInfo / registry entry (changed, `src/invio/llm/registry.py`)

| Field | Type | Rule |
|---|---|---|
| `model_id` | str | key in `models:` of the file |
| `provider` | str | equals the file name |
| `input_price_per_mtok` | Decimal | ≥ 0, USD per 1,000,000 tokens |
| `output_price_per_mtok` | Decimal | ≥ 0 |
| `context_window` | int | > 0 |
| `max_output_tokens` | int \| None | **new**, optional, strict int > 0. Required for every `anthropic` model, which the provider checks when it is built. |

`schema_version` stays `1`. Files without the field parse exactly as before.

## `models.d/anthropic.yaml` (new)

| Model id | Role | Input $/MTok | Output $/MTok | Context | Max output |
|---|---|---|---|---|---|
| `claude-haiku-4-5-20251001` | fast (cheapest; `llm test` default) | 1.00 | 5.00 | 200000 | 64000 |
| `claude-sonnet-4-6` | smart | 3.00 | 15.00 | 1000000 | 128000 |

Prices, IDs and limits must be re-verified against Anthropic's model and pricing pages when the
file is written, and the date recorded in the file header (as in `openai.yaml`).

## AnthropicProvider (new, `src/invio/llm/anthropic.py`)

| Attribute | Source |
|---|---|
| API key | `require_api_key(settings, "anthropic")` → `INVIO_ANTHROPIC_API_KEY` |
| `timeout_seconds` | `settings.llm_timeout_seconds` |
| `retry` | `RetryPolicy()` (shared defaults) |
| registry | `default_registry()` unless injected through the constructor (the factory never injects; research R5); it is validated when the provider is built |
| base URL | `https://api.anthropic.com` unless injected (tests) |
| clients | one `(AsyncAnthropic, httpx2.AsyncClient)` per running event loop |

`repr` shows the timeout and retry policy only, never the key.

## Request and response mapping

| Contract input | Messages API |
|---|---|
| `system` | `system` |
| `user` | `messages=[{"role": "user", "content": user}]` |
| `temperature` | `extra_body={"temperature": t}` |
| `max_tokens` (free text) | `max_tokens`, passed as given; a value above the model's limit is rejected by the service as `LLMInvalidRequestError` |
| (structured) | `max_tokens = ModelInfo.max_output_tokens` |
| `schema` (structured) | `tools=[{"name", "description", "input_schema"}]`, `tool_choice={"type": "tool", "name", "disable_parallel_tool_use": true}` |

| Response | Result |
|---|---|
| `stop_reason` `max_tokens` or `refusal` | `LLMUnavailableError` |
| free text: joined `text` blocks, empty | `LLMUnavailableError` |
| structured: first `tool_use.input` | `json.dumps(input)`, validated by `structured_with_repair` |
| structured: no `tool_use` block | joined text, which fails validation and triggers one repair |
| `usage.input_tokens` / `output_tokens` | `Usage(in, out)`; missing usage gives `Usage(0, 0)` |

## Typed errors (existing, `invio.llm.base`)

See [research.md R6](research.md) for the full mapping. `LLMQuotaError` is reused for exhausted
credit.
