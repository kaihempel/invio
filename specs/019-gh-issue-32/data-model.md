# Data Model: Google (Gemini) Provider

## ModelInfo / registry entry (changed, `src/invio/llm/registry.py`)

| Field | Type | Rule |
|---|---|---|
| `model_id` | str | key in `models:` of the file |
| `provider` | str | equals the file name |
| `input_price_per_mtok` | Decimal | ≥ 0, USD per 1,000,000 tokens |
| `output_price_per_mtok` | Decimal | ≥ 0; covers thinking tokens for Google |
| `context_window` | int | > 0 |
| `max_output_tokens` | int \| None | existing, optional; not used by Google |
| `thinking_level` | `"minimal" \| "low" \| "medium" \| "high"` \| None | **new**, optional; the level every request sends. Required for `google` models. |
| `thinking_allowance_tokens` | int \| None | **new**, optional, strict int > 0; added to a free-text call's `max_tokens`. Required for `google` models. |
| `keep_default_temperature` | bool | **new**, optional, strict bool, default `false`. When `true` the request sends no temperature. |

Rules:
- `schema_version` stays `1`, and files without the new fields parse exactly as before.
- Other providers ignore the new fields.
- `GoogleProvider` checks, when it is built, that every `google` entry has `thinking_level` and
  `thinking_allowance_tokens`. A missing field is an `LLMConfigError` that names the models.

## `models.d/google.yaml` (new)

| Model id | Role | Input $/MTok | Output $/MTok | Context | Thinking level | Allowance | Keep default temperature |
|---|---|---|---|---|---|---|---|
| `gemini-3.5-flash-lite` | fast (cheapest; `llm test` default) | 0.30 | 2.50 | 1048576 | minimal | 1024 | true |
| `gemini-3.8-flash` | smart | 0.75 | 3.75 | 1048576 | low | 4096 | true |

The file header records the verification date (2026-10-09), the source URLs, and the
`gemini-3.8-flash` price increase to 1.50/7.50 on 2027-01-01 ([research R1](research.md)).

## GoogleProvider (new, `src/invio/llm/google.py`)

| Attribute | Source |
|---|---|
| API key | `require_api_key(settings, "google")` → `INVIO_GOOGLE_API_KEY` |
| `timeout_seconds` | `settings.llm_timeout_seconds` |
| `retry` | `RetryPolicy()` (shared defaults) |
| registry | the registry passed by the factory, else `default_registry()`; validated when the provider is built |
| base URL | `https://generativelanguage.googleapis.com/`, unless injected (tests) |
| clients | one `(genai.Client(...).aio, httpx.AsyncClient)` per running event loop |

`repr` shows the timeout and the retry policy only, never the key.

## Request mapping (`GenerateContentConfig`)

| Contract input | Gemini request |
|---|---|
| `system` | `system_instruction`, omitted when empty |
| `user` | `contents=user` (one user turn) |
| `temperature` | `temperature`, or omitted when `keep_default_temperature` is set |
| `max_tokens` (free text) | `max_output_tokens = max_tokens + thinking_allowance_tokens` |
| (structured) | no `max_output_tokens` (the model maximum applies) |
| (all calls) | `thinking_config.thinking_level = <registered level>` |
| `schema` (structured) | `response_mime_type="application/json"` and `response_json_schema = gemini_schema(schema.model_json_schema())` |

## Service-compatible shape (`gemini_schema`)

Input: a Pydantic JSON Schema. Output: a new dict, and the input is never mutated.

- Local `$ref` (`#/$defs/<Name>`) values are replaced by a copy of the definition, and `$defs` is
  removed. A reference cycle raises `LLMConfigError("schema <Name> is recursive …")`.
- Only these keywords are kept per schema node: `type`, `title`, `description`, `properties`,
  `required`, `additionalProperties`, `items`, `prefixItems`, `minItems`, `maxItems`, `enum`,
  `format`, `minimum`, `maximum` and `anyOf`. The keys inside `properties` are field names and
  are always kept.
- A reference that is not local raises `LLMConfigError`.

## Response mapping

| Response | Result |
|---|---|
| `prompt_feedback.block_reason` | `LLMInvalidOutputError` (prompt blocked: reason, blocked categories) |
| `candidates[0].finish_reason` ∉ {`STOP`, `MAX_TOKENS`, unspecified} | `LLMInvalidOutputError` (answer stopped: reason, blocked categories) |
| `finish_reason == MAX_TOKENS` | `LLMUnavailableError` |
| free text: joined non-thought text is empty | `LLMUnavailableError` |
| structured: joined non-thought text | validated by `structured_with_repair` (an empty answer is repaired once) |
| `usage_metadata` | `Usage(prompt_token_count, candidates_token_count + thoughts_token_count)`; missing counts are 0 |

The `LLMInvalidOutputError` for blocks carries `errors` (the same reason text) and the call's
`usage`. Its message never contains prompt or answer text.

## Typed errors

These are the existing errors in `invio.llm.base`. The full mapping is in
[research R6](research.md).
