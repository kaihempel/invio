# Context: GH issue #33 — Add Ollama provider for local models

Branch: `gh-issue-33` · Track `llm` · round 2 · depends on #7 (provider contract, done)

## Goal

Local models via Ollama allow cost-free runs (e.g. relevance scoring). Add an `ollama`
LLM provider next to `mistral`, `openai`, `anthropic` and `google`.

## Acceptance criteria (from the issue)

- Works against a local Ollama instance (manual test documented) and against mocked HTTP in CI.
- An unreachable server raises `LLMUnavailableError` quickly (connect timeout ≤ 5 s).
- Cost estimate is 0 for local models.

## Already in place

- `Settings.ollama_base_url` (default `http://localhost:11434`, env `INVIO_OLLAMA_BASE_URL`,
  `.env.example`) — `src/invio/config/settings.py`.
- `LLMProviderName.OLLAMA = "ollama"` in `src/invio/config/job.py`, in `docs/job.schema.json`,
  and offered by the setup wizard.
- No API key setting for Ollama: `require_api_key(settings, "ollama")` raises `LLMConfigError`
  (tested in `tests/test_llm_base.py`) — the provider must not call it.

## Architecture to follow

- Providers live in `src/invio/llm/<name>.py`, are decorated with
  `@register_provider(PROVIDER)` (`factory.py`, auto-discovered) and expose
  `from_settings(settings, *, registry=None)`.
- Model prices/context windows: `src/invio/llm/models.d/<provider>.yaml` (`schema_version: 1`,
  file stem must equal `provider`, model ids unique across all files, prices USD per Mtok).
- Shared HTTP retry loop and classification: `src/invio/llm/http_retry.py`
  (`run_with_retries`, `classify_status`, `classify_transport`, `timeout_failure`,
  `bad_response_failure`, `sanitize_detail`, `describe`, `RetryPolicy`).
- Structured output: `structured_with_repair` in `base.py` (one repair request);
  per-request deadline: `with_timeout`.
- One HTTP client per event loop: `LoopClients` (`loop_clients.py`).
- Closest reference: `mistral.py` (httpx2-based, injectable `client_factory`, `sleep`,
  `uniform`, `now`). Error hygiene rules in its docstring apply (no prompt/answer/raw body in
  messages; SDK exception not chained).
- HTTP library: `httpx2` (already a dependency; banned only in `invio.sources`/`scheduling`).

## Design decisions

1. **Native Ollama API** `POST {ollama_base_url}/api/chat` with `stream: false`,
   `messages=[system, user]`, `options={"temperature": t, "num_predict": max_tokens}`.
   Answer text: `message.content`; usage: `prompt_eval_count` / `eval_count` (default 0).
   No SDK — plain `httpx2.AsyncClient(base_url=...)`.
2. **Structured output**: send the JSON schema of the Pydantic model in `format`. If the server
   rejects a schema-valued `format` (HTTP 400 for older Ollama / models without support), fall
   back to `format: "json"` (JSON mode) for that request and remember the model so later calls
   go straight to JSON mode; `structured_with_repair` validates and repairs.
3. **Timeouts**: `httpx2.Timeout(timeout_seconds, connect=5.0)` (connect ≤ 5 s) plus
   `with_timeout` for the whole request.
4. **Errors**: connection failures (refused, connect timeout, DNS) → `LLMUnavailableError`
   *not retried* (a local server that is down will not come back within the backoff, and the
   acceptance criterion asks for a quick failure). Other transport errors/5xx/429 follow the
   shared retry loop. Status errors via `classify_status`; detail from the Ollama JSON body
   `{"error": "..."}` (sanitized). 404 "model not found" → `LLMInvalidRequestError`.
   Base URL appears in no secret; still never log prompts/answers.
5. **No API key**: `from_settings` reads `ollama_base_url` and `llm_timeout_seconds` only.
6. **Registry**: `models.d/ollama.yaml` with example local models, all prices `0`
   (first = fast role, second = smart role, as in the other files).
7. **Tests**: mocked HTTP via `httpx2.MockTransport` through `client_factory`; extend the
   provider contract/factory tests as the other providers do; opt-in `live` test against a
   real local Ollama (skipped unless `-m live` and the server is reachable).
8. **Docs**: document the manual test (pull model, set `INVIO_OLLAMA_BASE_URL`, run the live
   test / `invio llm` command) where the other providers are documented.
