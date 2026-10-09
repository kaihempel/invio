# Research: Anthropic (Claude) Provider

Sources: the bundled Claude API reference (cached 2026-10-06: models, error codes, tool use,
Python SDK 1.x upgrade notes), PyPI (`anthropic` 1.12.1 is current, requires `httpx2>=2,<3`), and
the existing providers `src/invio/llm/openai.py` and `mistral.py`.

## R1 – Model selection (forced tool use and temperature)

- **Decision**: Register `claude-haiku-4-5-20251001` (fast, pinned snapshot) and
  `claude-sonnet-4-6` (smart; no dated snapshot exists, so the ID is the bare model name).
- **Rationale**: The issue requires forced tool use (`tool_choice: {type: "tool"}`), and the
  pipeline always sends `temperature=0.0` (`graph/nodes/llm_calls.py`).
  - Claude Fable 5.1, Opus 5.5 and Sonnet 5.5 return a 400 for forced tool choice.
  - Opus 4.7 and later reject any `temperature`.
  - Sonnet 5/5.5 and Haiku 5.5 reject non-default `temperature`.
  - Haiku 4.5 and Sonnet 4.6 accept both and are still served. When `thinking` is omitted, they
    run without thinking, which forced tool choice requires.
  - The user confirmed this choice during planning (spec, Clarifications).
- **Alternatives considered**:
  - Current models with `tool_choice: auto` plus a strict tool and no temperature: departs from
    the issue, and `auto` does not guarantee a call.
  - Per-model capability flags with a fallback path: most complex. Deferred until the provider
    contract drops `temperature`.

## R2 – SDK version and call shape

- **Decision**: `anthropic>=1.12,<2`, `AsyncAnthropic(api_key=..., base_url=..., http_client=<httpx2.AsyncClient>, max_retries=0, timeout=llm_timeout + 5)`, with one `messages.create` call per attempt:
  - `model`, `max_tokens`, `system`, and a single user message.
  - `extra_body={"temperature": t}`, because 1.x removed `temperature` from `messages.create()`.
    The parameter still exists in the API and is honoured by the registered models.
  - Structured calls only: `tools=[tool]` and
    `tool_choice={"type": "tool", "name": <tool name>, "disable_parallel_tool_use": True}`.
- **Rationale**:
  - 1.x is the maintained major version.
  - Its transport is `httpx2`, which is already a direct dependency and is used by the Mistral
    tests' `MockTransport` harness.
  - Retries come from the shared loop, as in OpenAI and Mistral.
  - The explicit client timeout also bypasses the SDK's "use streaming for large `max_tokens`"
    guard, which only applies when the default timeout is in use. The plan verifies this in task
    T001 (SDK check).
- **Alternatives considered**: `anthropic<1` (httpx-based; superseded); streaming (not needed
  under a 60 s call timeout; adds complexity).

## R3 – Structured output via forced tool

- **Decision**:
  - The tool name is the schema class name sanitised to `[a-zA-Z0-9_-]`, max 64 characters.
  - `input_schema` is `schema.model_json_schema()` unchanged, so `$defs`/`$ref`, optional
    fields and enums pass through.
  - No `strict: true`, so the schema features accepted stay unrestricted. Validation and the
    single repair attempt remain the safety net (FR-006).
  - From the response, the first `tool_use` block's `input` is serialised with `json.dumps` and
    returned as the text that `structured_with_repair` validates. If there is no `tool_use`
    block, the joined text blocks are returned instead; they fail validation and trigger the
    single repair attempt, as the spec's plain-text edge case requires.
- **Rationale**: Reuses the shared repair helper unchanged, and covers nested models and lists
  (FR-005) because Pydantic emits `$defs` for nested models.
- **Alternatives considered**: `strict: true` (restricts schema constructs; the spec's edge
  case requires unrestricted shapes); `output_config.format` (not the issue's technique).

## R4 – Stop reasons and unusable answers

- **Decision**:
  - `stop_reason == "max_tokens"` raises `LLMUnavailableError` for both operations with no
    repair (clarification Q1).
  - `"refusal"` raises `LLMUnavailableError`; the refusal text is never put into the message.
  - For free text, no text gives `LLMUnavailableError`.
  - Missing `usage` gives `Usage(0, 0)`. Only `input_tokens` and `output_tokens` are read; cache
    token fields are unused because no caching is requested.
- **Rationale**: Matches the OpenAI provider's `_answer` semantics, so behaviour is the same
  across providers (SC-004).

## R5 – Registry field `max_output_tokens`

- **Decision**:
  - `ModelInfo.max_output_tokens: int | None = None`.
  - `_ModelEntry.max_output_tokens: StrictInt | None = Field(default=None, gt=0)`.
  - `schema_version` stays `1`, and `models.d/README.md` documents the field.
  - The Anthropic provider takes a `ModelRegistry` (default `default_registry()` in
    `from_settings`). When it is built, it raises `LLMConfigError` if any `anthropic` model lacks
    the field (FR-011b: before any call).
  - A structured call for a model that is not in the registry raises `LLMConfigError`.
    `resolve()` already prevents this for job runs.
  - Values: Haiku 4.5 → 64000, Sonnet 4.6 → 128000.
- **Rationale**:
  - The change is additive and optional, so existing files are untouched and no version bump is
    needed (constitution I: the format stays versioned).
  - `from_settings(settings)` is the factory's only hook, and the registry is otherwise reachable
    only through the logging wrapper. Injecting it keeps the factory unchanged.
- **Registry source** (revised in review): `get_provider(name, settings, registry=...)`
  forwards its registry to `from_settings(settings, *, registry=None)`, so the provider reads
  limits from the same registry the logging wrapper prices calls with. Without one,
  `from_settings` falls back to `default_registry()`. Providers that need no registry ignore the
  argument. Tests pin both the forwarding and the fallback.
- **Risk noted**: Anthropic's output-tokens-per-minute limit is estimated from `max_tokens` when
  a request starts, so large limits may trigger 429s on low usage tiers. These are retried, and
  surfaced as "rate limit" once retries are exhausted.
- **Alternatives considered**: a fixed provider constant or a new setting (rejected in
  clarification Q2); passing the registry through `from_settings` (changes the factory protocol
  for every provider).

## R6 – Error mapping

The SDK raises `APIStatusError` subclasses exposing `status_code`, `.type` and `body`; transport
failures come as `APIConnectionError` / `APITimeoutError` wrapping `httpx2` exceptions.

| Situation | Typed error | Retried |
|---|---|---|
| `APITimeoutError`, `httpx2.TimeoutException`, deadline from `with_timeout` | `LLMUnavailableError` | no |
| Connection failure (`APIConnectionError` caused by a network error) | `LLMUnavailableError` | yes |
| Request could not be sent (`InvalidURL`, `UnsupportedProtocol`, `LocalProtocolError`) | `LLMUnavailableError` | no |
| Undecodable response, `APIResponseValidationError` | `LLMUnavailableError` | no |
| 401 `authentication_error`, 403 `permission_error` | `LLMAuthError` (message names `INVIO_ANTHROPIC_API_KEY`) | no |
| 402 `billing_error`; 400 whose message mentions the credit balance (legacy form) | `LLMQuotaError` (no wait hint) | no |
| 429 `rate_limit_error` | `LLMRateLimitError` with `Retry-After` | yes (cap `max_retry_after`) |
| 500–599 including 529 `overloaded_error` | `LLMUnavailableError` | yes, then raised |
| Other 4xx (400, 404 unknown model, 413, 422) | `LLMInvalidRequestError` (status kept) | no |
| Other status | `LLMUnavailableError` | no |

Messages are built with `http_retry.describe` from the status and the sanitized
`body["error"]["message"]`. Note that Anthropic nests the message under `error`, unlike
OpenAI's unwrapped body; T001 confirms what the SDK passes as `exc.body`. `str(exc)` is never
used, and the typed error is raised outside the `except` block (no `__cause__`/`__context__`).

## R7 – Credentials and base URL

- **Decision**: Always pass `base_url="https://api.anthropic.com"` explicitly, so that
  `ANTHROPIC_BASE_URL` cannot redirect the key. Make sure no `Authorization` header is sent,
  even if `ANTHROPIC_AUTH_TOKEN` or an `ant` profile exists in the environment.
- **Rationale**: The 1.x SDK resolves extra credential sources (`ANTHROPIC_AUTH_TOKEN`, OAuth
  profiles). invio must only ever use `INVIO_ANTHROPIC_API_KEY` (constitution V).
- **To verify in T001**: how to suppress the auth-token lookup (for example
  `auth_token=None` handling, or overriding the header with the SDK's `Omit`). A test sets
  `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` and asserts that only `x-api-key` reaches the
  recorded host.

## R8 – Client lifecycle

- **Decision**: Copy the OpenAI provider's per-event-loop client table (a lock, lazy creation
  from `client_factory`, `aclose` for the running loop, and dropping clients of closed loops).
  The default factory is `anthropic.DefaultAsyncHttpxClient`.
- **Rationale**: The same constraints apply (pools bound to their loop, threads with their own
  loops), and the code is proven.

## R9 – Tests

- **Decision**:
  - `tests/anthropic_helpers.py` mirrors `openai_helpers.py` on `httpx2`, with the same queue
    semantics (fixture name, response, exception, `HANG`).
  - Fixtures under `tests/fixtures/anthropic/`. Success fixtures report 12 input and 3 output
    tokens, as the contract suite expects.
  - `test_llm_provider_contract.py` gains an `AnthropicHarness`.
  - The live test (`-m live`) runs one free-text and one structured call on the cheapest model.
- **Rationale**: Constitution III (offline, deterministic) and the spec's SC-002/SC-006.

## R10 – SDK verification (T001, 2026-10-09)

Exercised against the installed `anthropic` 1.12.1 with an `httpx2.MockTransport` (no network).

- **Transport: `httpx2`, confirmed.** `anthropic.DefaultAsyncHttpxClient` subclasses
  `httpx2.AsyncClient`; the SDK raises `APIConnectionError` / `APITimeoutError` from `httpx2`
  exceptions (`__cause__` is the `httpx2` exception, e.g. `ConnectError`, `ReadTimeout`,
  `UnsupportedProtocol`). The plan's assumption holds: tests and `_classify` use `httpx2`.
  No deviation.
- **(a) `extra_body`.** `messages.create(..., extra_body={"temperature": 0.0})` puts
  `temperature` at the top level of the JSON body.
- **(b) Large `max_tokens` guard.** With the default timeout, `max_tokens=128000` raises
  `ValueError("Streaming is required for operations that may take longer than 10 minutes...")`.
  With an explicit client `timeout` (as the provider always sets), the call is sent normally.
- **(c) `APIStatusError.body`.** The full wrapped document is passed:
  `{"type": "error", "error": {"type": ..., "message": ...}}`, so the message is
  `body["error"]["message"]`. `.type` is the inner error type (`invalid_request_error`,
  `billing_error`, `overloaded_error`, ...) and `.status_code` the HTTP status. A non-JSON error
  body gives `body` as the raw string (no `error` key). `_safe_detail` accepts the wrapped shape
  and a flat `{"message": ...}`, anything else gives `""`.
- **(d) Auth headers.** With `ANTHROPIC_AUTH_TOKEN` and `ANTHROPIC_BASE_URL` set in the
  environment and an explicit `api_key` and `base_url`, only `x-api-key` is sent (no
  `Authorization`) and the request goes to the explicit host. `auth_token=None` and a
  `default_headers={"Authorization": anthropic.Omit()}` override give the same result, so no
  extra suppression is needed; the behaviour is pinned by a test that sets both variables.
- **(e) Exception types.** 400 `BadRequestError`, 413 `RequestTooLargeError`, 429
  `RateLimitError`, 529 `OverloadedError` (all `APIStatusError` subclasses). 402 has no
  dedicated class: it is a plain `APIStatusError` with `.type == "billing_error"`. Timeouts are
  `APITimeoutError` (a subclass of `APIConnectionError`, so it is checked first).
- **(f) Response attributes.** `Message.stop_reason`, `Message.usage.input_tokens` /
  `.output_tokens`, `TextBlock.text`, `ToolUseBlock.name` / `.input` (a dict).
- **Surprise: malformed 200.** A 200 response with a non-JSON body does not raise: `create()`
  returns the raw `str`. `_answer` therefore treats any result that is not a `Message` as an
  unexpected response (`LLMUnavailableError`, not retried).
