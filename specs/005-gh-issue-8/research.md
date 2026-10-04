# Research: Mistral LLM Provider (#8)

All findings below were checked against `mistralai==3.0.0` (installed in a scratch venv and
inspected) and the merged #7 code in `src/invio/llm/`. No NEEDS CLARIFICATION remain.

## R1 — Client library and import path

- **Decision**: Use the official `mistralai` SDK, `>=3.0,<4`, imported as
  `from mistralai.client import Mistral` (3.x moved everything under `mistralai.client`; the
  top-level `mistralai` package is now an empty namespace). Call `client.chat.complete_async(...)`.
- **Rationale**: Required by the issue; async fits the async `LLMProvider` protocol; the
  package ships `py.typed`, so `mypy --strict` works without stubs.
- **Alternatives considered**: Raw HTTP against `/v1/chat/completions` (rejected: the issue
  requires the official SDK); the sync `chat.complete` (rejected: the protocol is async).

## R2 — HTTP stack: the SDK uses `httpx2`, not `httpx`

- **Finding**: `mistralai` 3.x depends on `httpx2>=2.13` (pydantic's httpx fork) and does
  `import httpx2 as httpx` internally. `httpx2.AsyncClient is not httpx.AsyncClient`.
- **Decision**: Declare `httpx2>=2.13` as an explicit runtime dependency, because
  `invio.llm.mistral` imports its exception types (`httpx2.TimeoutException`,
  `httpx2.TransportError`) for error mapping. The provider accepts an optional
  `client_factory: Callable[[], httpx2.AsyncClient]` whose result is passed to
  `Mistral(async_client=...)`; it is called once per event loop (R15).
- **Rationale**: Importing a transitive dependency without declaring it would break when the
  SDK changes its pin; explicit is safer and is justified in the PR description.

## R3 — Offline HTTP fixtures: `httpx2.MockTransport` instead of `respx`

- **Finding**: `respx` (0.23.1) patches `httpx` only; it has no `httpx2` support, so it would
  not intercept the SDK's requests.
- **Decision**: Tests inject `client_factory=lambda: httpx2.AsyncClient(transport=httpx2.MockTransport(handler))`.
  The handler replays **recorded response fixtures** stored as JSON files in
  `tests/fixtures/mistral/` (status, headers, body) and records every request for assertions
  (method, path, JSON body, `Authorization` header present). No new test dependency.
- **Rationale**: Satisfies the issue's intent ("recorded HTTP fixtures (e.g. respx)") at the
  HTTP layer the SDK really uses; deterministic; zero network.
- **Alternatives considered**: `respx` (does not work with `httpx2`); mocking
  `chat.complete_async` (rejected: would not test SDK request serialization or status
  handling); VCR-style cassettes (extra dependency, overkill for ~10 fixtures).

## R4 — Disabling the SDK's own retries

- **Finding**: `chat.complete_async` retries only when a `RetryConfig` is set (per call or on
  the client); the default `retry_config` is `UNSET` → no retries. Retry status codes in the
  SDK would be `429, 500, 502, 503, 504`.
- **Decision**: Never set `retry_config` on the client nor `retries` per call (both stay
  `UNSET` → the SDK makes exactly one HTTP request per call), and assert in a test that a
  single 503 results in exactly one HTTP request when `max_retries=0`. All retrying is done by invio (spec Assumption "Retry
  ownership").
- **Rationale**: Uniform behaviour/logging, `Retry-After` handling with our 60 s cap,
  injectable sleep for tests.

## R5 — Error surface of the SDK and mapping

| SDK outcome | Detail | invio error | Retried |
|---|---|---|---|
| `MistralError` (base; `SDKError`, `HTTPValidationError` subclass it) with `status_code` 401/403 | | `LLMAuthError` | no |
| `status_code` 429 | `Retry-After` from `exc.headers` | `LLMRateLimitError(retry_after=…)` | yes (≤3) |
| `status_code` ≥ 500 | | `LLMUnavailableError` | yes (≤3) |
| other 4xx (400, 404, 422 → `HTTPValidationError`, …) | | `LLMInvalidRequestError(status=…)` | no |
| `httpx2.TimeoutException` (SDK `timeout_ms` backstop) or our `with_timeout` deadline | | `LLMUnavailableError` | no |
| `httpx2.TransportError` (non-timeout: `ConnectError`, `ReadError`, `RemoteProtocolError`, …) and `errors.NoResponseError` | | `LLMUnavailableError` | yes (≤3) |
| `errors.ResponseValidationError` (2xx body not a chat completion) | | `LLMUnavailableError` | no |
| 2xx with no choices / empty content | | `LLMUnavailableError` | no |

- **Order of checks**: `TimeoutException` is a subclass of `TransportError` in httpx2, so it is
  matched first.
- **Message hygiene**: `SDKError.__str__` embeds the full response body (up to 10 000 chars)
  and 422 bodies may echo request input (prompt text). invio **never** uses `str(exc)`. The
  message is built from: provider, model, HTTP status and a sanitized provider message —
  the JSON body's top-level `message` (string) or, for 422, each `detail[].msg` with its
  `loc`, ignoring `input`/`ctx`; truncated to 300 characters. Errors are raised `from None`
  so the SDK exception (with body/headers incl. request auth context) is not chained into
  logs/tracebacks.

## R6 — `Retry-After` parsing and backoff

- **Decision**: Parse `Retry-After` as non-negative integer/decimal seconds, else as an HTTP
  date (`email.utils.parsedate_to_datetime`, delta to now, floored at 0); unparseable →
  `None` (use computed backoff). Backoff for retry *n* (1-based):
  `base * 2**(n-1) * (1 + uniform(-jitter, +jitter))` → 1 s, 2 s, 4 s ±25 %. If the requested
  `Retry-After` > `max_retry_after` (60 s) → raise `LLMRateLimitError` immediately with
  `retry_after` set. After the last allowed retry fails, raise the mapped error of the last
  attempt (with `retry_after` if the last 429 carried one).
- **Testability**: `sleep` (default `asyncio.sleep`), `random` (default `random.uniform`) and
  `now` (for HTTP-date parsing) are constructor-injectable; tests record sleeps instead of
  waiting. Cancellation during `await sleep(...)` propagates naturally (`CancelledError`).

## R7 — Timeout

- **Decision**: Each attempt is wrapped in the existing `with_timeout(..., seconds=
  settings.llm_timeout_seconds)` from #7 (uniform message "did not answer within N s").
  The SDK's `timeout_ms` is set to the same value plus 5 s as a backstop only (so the asyncio
  deadline normally fires first); an `httpx2.TimeoutException` is mapped identically. Timeouts
  are not retried (clarification Q2).

## R8 — Structured output via JSON-schema `response_format`

- **Decision**: `response_format={"type": "json_schema", "json_schema": {"name":
  <schema.__name__>, "schema": schema.model_json_schema(), "strict": True}}`. The raw request
  passed to `structured_with_repair` sends this `response_format` on both the first and the
  repair request; `structured_with_repair` additionally embeds the schema in the system prompt
  (harmless, and keeps repair behaviour identical to other providers). `max_tokens` is not
  sent for structured calls (protocol has no limit there).
- **Edge**: Mistral rejecting a schema construct → 400/422 → `LLMInvalidRequestError` (not a
  repair); the schema `name` must match `^[a-zA-Z0-9_-]+$` — Pydantic class names already do.

## R9 — Reading text and usage from the response

- **Decision**: Text = `choices[0].message.content`; if it is a list of chunks, concatenate
  the `text` of `TextChunk` items (other chunk types ignored). Empty/missing → unavailable
  error (FR-007). Usage = `Usage(prompt_tokens or 0, completion_tokens or 0)` from
  `response.usage` (missing `usage` → zeros) (FR-006).

## R10 — Shared error additions in `base.py`

- **Decision**: Add `LLMInvalidRequestError(LLMError)` with `status: int | None`; extend
  `LLMRateLimitError` with keyword `retry_after: float | None = None`. Both
  backward-compatible; `_LoggedProvider._log_error` already logs `type(err).__name__`, so the
  new type is logged without factory changes. Export from `invio.llm.__init__` if that module
  re-exports the error set.

## R11 — Pinned Mistral models for the shipped registry

- **Decision**: `src/invio/llm/models.d/mistral.yaml` lists pinned, dated ids only
  (clarification Q3). Proposed initial set (to be **re-verified against Mistral's public price
  list and `/v1/models` at implementation time**; the implementer updates ids/prices if they
  have changed):

  | Model id | Role fit | Input $/1M | Output $/1M | Context |
  |---|---|---|---|---|
  | `mistral-small-2506` | fast | 0.10 | 0.30 | 131072 |
  | `mistral-medium-2508` | smart | 0.40 | 2.00 | 131072 |

- **Test**: a registry test asserts that no Mistral id ends with `-latest` and that at least
  two models exist.

## R12 — CLI `invio llm test <provider>`

- **Decision**: New module `src/invio/cli/commands/llm.py` exposing `app = typer.Typer(...)`
  with a `test` command (auto-discovered → `invio llm test`). Flow: `get_provider(provider)`
  (unknown provider / missing key → exit 2) → pick model (`--model` must be registered for that
  provider, else exit 2; default = cheapest by input+output price, tie → model id) → one
  `complete(system="Connectivity check.", user="Reply with OK.", temperature=0, max_tokens=5)`
  via `asyncio.run` → stdout one line; any `LLMError` → stderr one line, exit 1.
- **Time bound (FR-026)**: the command passes
  `settings.model_copy(update={"llm_timeout_seconds": min(settings.llm_timeout_seconds, 20.0)})`
  to `get_provider`. Each attempt is then capped at 20 s, with no change to the factory or
  provider API. Retries keep their normal policy, so a rate-limited or failing service is
  reported as it would be in a pipeline run.
- **Rationale**: Missing key is detected before any request (spec US4-2); the 2-vs-1 split
  follows the constitution (config errors → 2).

## R13 — Live test opt-in

- **Decision**: Register marker `live` in `pyproject.toml` and add `-m "not live"` to
  `addopts`; `pytest -m live` overrides it (the last `-m` wins). The live test skips with a
  reason when `INVIO_MISTRAL_API_KEY` is unset. CI runs the default selection only.
- **Note**: The autouse `isolated_settings` fixture in `tests/conftest.py` isolates env/`.env`;
  the live test reads the key from the real environment explicitly before isolation applies
  (e.g. capture `os.environ` at module import) — implementer verifies against the fixture.

## R14 — Retry logging

- **Decision**: One `logger.warning("llm.retry", extra={provider, model, attempt, status |
  failure, wait_s})` per retry (FR-019). Final errors are already logged as `llm.error` by the
  #7 wrapper; the provider does not log them again.

## R15 — Event-loop-safe client lifecycle

- **Finding**: `Mistral(...)` creates its `httpx2.AsyncClient` at construction. An httpx-style
  async connection pool is bound to the loop that first used it; reusing it from a later loop
  (a second `asyncio.run`) can fail with "Event loop is closed" or hang on stale connections.
  The CLI builds the provider outside `asyncio.run`, and the scheduler may run one loop per
  job run while caching providers.
- **Decision**: Build the SDK client lazily, once per running loop. A private
  `_client_for_loop()` compares `asyncio.get_running_loop()` with the loop of the cached client.
  On a mismatch (or on first use) it creates `Mistral(api_key=..., async_client=
  self._client_factory(), server_url=..., timeout_ms=...)`. It then tries to close the
  previous `httpx2.AsyncClient` if that client's loop is still open; otherwise it drops it
  (sockets are closed when it is garbage-collected). `client_factory` defaults to
  `lambda: httpx2.AsyncClient(follow_redirects=True)`, the same default the SDK uses.
- **Test**: a plain (sync) test builds one provider with a counting `client_factory` and calls
  `asyncio.run(provider.complete(...))` twice. Both succeed and the factory was called twice.
  A further check: two calls in the same loop reuse one client (factory called once).
- **Alternatives considered**: a new client per call (rejected: loses connection reuse,
  costing a TLS handshake per call in high-volume filtering); requiring callers to use one loop
  (rejected: fragile, and violates FR-027); `async with` provider lifecycles (rejected: the #7
  protocol has no open/close methods).
