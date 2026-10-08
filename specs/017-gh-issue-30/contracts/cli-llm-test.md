# Contract: `invio llm test openai`

The command (`src/invio/cli/commands/llm.py`) is unchanged; its behaviour for `openai`:
- Success (exit 0), stdout: `ok provider=openai model=<id> input_tokens=<n> output_tokens=<n> duration_ms=<ms>`. The model defaults to the cheapest registered OpenAI model; `--model` overrides it (must be registered for `openai`).
- Missing key or unknown model (exit 2), stderr: `Configuration error: ...` naming `INVIO_OPENAI_API_KEY`; no network call.
- Provider failure (exit 1), stderr: `Error: <LLMErrorType>: <message>`.
- Call timeout capped at the command's existing cap.
