# Contract: `invio llm test` (#8)

Module: `src/invio/cli/commands/llm.py` (auto-discovered; exposes `app = typer.Typer(...)`).

```text
invio llm test PROVIDER [--model MODEL]
```

| Argument / option | Meaning |
|---|---|
| `PROVIDER` | registered provider name (e.g. `mistral`) |
| `--model MODEL` | model id; must be in the model registry for `PROVIDER`. Default: the cheapest registered model of that provider (input + output price per 1M; tie → lowest id) |

## Behaviour

1. `get_provider(PROVIDER, settings')`, where `settings'` is the application settings with
   `llm_timeout_seconds` capped at 20 s (FR-026). Unknown provider or missing/blank API key →
   exit 2, no request.
2. Model selection/validation — unknown model, model of another provider, or provider has no
   registry models → exit 2, no request.
3. One `complete(system="Connectivity check.", user="Reply with OK.", temperature=0,
   max_tokens=5)`.

## Output

Success (stdout, exit 0), one line:

```text
ok provider=mistral model=mistral-small-2506 input_tokens=12 output_tokens=2 duration_ms=431.7
```

Failure (no traceback; the last stderr line is the message, because the root callback's JSON
log lines such as `llm.error` or `llm.retry` may precede it):

| Case | Message prefix | Exit |
|---|---|---|
| unknown provider | `Configuration error: unknown LLM provider 'x'; registered: mistral` | 2 |
| missing key | `Configuration error: LLM provider 'mistral' needs an API key: set INVIO_MISTRAL_API_KEY` | 2 |
| bad model | `Configuration error: model 'x' is not registered for LLM provider 'mistral'` | 2 |
| rejected key | `Error: LLMAuthError: …` | 1 |
| rate limited | `Error: LLMRateLimitError: …` | 1 |
| unavailable / timeout | `Error: LLMUnavailableError: …` | 1 |
| invalid request | `Error: LLMInvalidRequestError: …` | 1 |

The answer text itself is not printed (only metadata), and the key never appears.
