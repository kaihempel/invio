# Contract: `invio llm test anthropic`

The command (`src/invio/cli/commands/llm.py`) is unchanged (clarification Q3). Its behaviour for
`anthropic`:

- **Success** (exit 0), stdout: `ok provider=anthropic model=<id> input_tokens=<n> output_tokens=<n> duration_ms=<ms>`. The model defaults to the cheapest registered Anthropic model (`claude-haiku-4-5-20251001`); `--model` overrides it, and the model must be registered for `anthropic`. The check is free text only (`max_tokens=5`, `temperature=0`).
- **Missing key or unknown model** (exit 2), stderr: `Configuration error: ...`, naming `INVIO_ANTHROPIC_API_KEY` for a missing key. No network call is made.
- **Provider failure** (exit 1), stderr: `Error: <LLMErrorType>: <message>`, for example `LLMAuthError` for a rejected key or `LLMUnavailableError` when the service is overloaded.
- **Timeout**: the call timeout is capped at the command's existing cap.
