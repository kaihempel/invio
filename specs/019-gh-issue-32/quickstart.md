# Quickstart: validating the Google provider

## Prerequisites

- `uv sync --locked` installs `google-genai` 2.x.
- For the live part only, you need a Gemini API key with paid-tier quota. Free-tier daily limits
  end in `LLMQuotaError`.

## Offline validation (no key, no network)

```bash
uv run pytest tests/test_llm_google.py tests/test_llm_google_schema.py tests/test_llm_provider_contract.py tests/test_llm_registry.py tests/test_cli_llm.py
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Expected result: all pass, and no request leaves the process.

Coverage of the success criteria:

| Tests | Success criterion |
|---|---|
| Contract suite | SC-002 |
| `RelevanceResult`, `ItemSummary` and nested/`$defs` structured cases | SC-003 |
| Blocked-prompt and stopped-answer fixtures | SC-004 |
| Error-category tests (401/403, 400 `API_KEY_INVALID`, 429 with `retryDelay`, daily quota, 5xx, timeout, 400/404, `MAX_TOKENS`) | SC-005 |
| Checks that no secret, prompt or answer appears in errors and logs | SC-008 |

## Select Google in a job

Set the job's LLM configuration as follows:

```yaml
llm:
  provider: google
  models:
    fast: gemini-3.5-flash-lite
    smart: gemini-3.8-flash
```

Then load the job with `invio job create --from-file <file>`. A model that is not in
`models.d/google.yaml` fails before any call.

## Live check (needs a real key)

```bash
export INVIO_GOOGLE_API_KEY=...
uv run invio llm test google                          # exit 0: "ok provider=google model=gemini-3.5-flash-lite ..."
uv run invio llm test google --model gemini-3.8-flash
uv run pytest -m live tests/test_llm_google_live.py   # connectivity check, RelevanceResult, ItemSummary
```

Without the key, `invio llm test google` exits 2 and names `INVIO_GOOGLE_API_KEY`. See the
[contract](contracts/cli-llm-test.md).
