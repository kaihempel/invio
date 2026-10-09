# Contract: `invio llm test google`

The command (`src/invio/cli/commands/llm.py`) is unchanged (FR-014). Its behaviour for `google`:

- **Success** (exit 0): stdout is `ok provider=google model=<id> input_tokens=<n> output_tokens=<n> duration_ms=<ms>`.
  - The model defaults to the cheapest registered Google model (`gemini-3.5-flash-lite`).
    `--model` overrides it, and the model must be registered for `google`.
  - The check is free text only (`max_tokens=5`, `temperature=0`).
  - The provider adds the model's thinking allowance to the 5 tokens and omits the temperature
    for flagged models. `output_tokens` includes thinking tokens.
- **Missing key or unknown model** (exit 2): stderr is `Configuration error: ...`, naming
  `INVIO_GOOGLE_API_KEY` for a missing key. No network call is made.
- **Provider failure** (exit 1): stderr is `Error: <LLMErrorType>: <message>`. Examples:
  - `LLMAuthError` for a rejected key;
  - `LLMUnavailableError` when the service is overloaded;
  - `LLMInvalidOutputError` when the check prompt is blocked.
- **Timeout**: the call timeout is capped at the command's existing cap.
