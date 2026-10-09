# Quickstart: validating the Anthropic provider

## Prerequisites

- `uv sync --locked` (installs `anthropic` 1.x)
- For the live part only: an Anthropic API key with credit

## Offline validation (no key, no network)

```bash
uv run pytest tests/test_llm_anthropic.py tests/test_llm_provider_contract.py tests/test_llm_registry.py tests/test_cli_llm.py
uv run ruff check . && uv run ruff format --check . && uv run mypy
```

Expected: all pass, with no request leaving the process.

Coverage maps to the spec as follows:
- contract suite → SC-002
- nested and list structured cases → SC-003
- error-category tests (auth, rate limit, 5xx, 529, timeout, 400/404, billing) → SC-004
- secret and prompt absence checks → SC-007

## Select Anthropic in a job

Set `llm.provider: anthropic` with `models.fast: claude-haiku-4-5-20251001` and
`models.smart: claude-sonnet-4-6`. Then load the job with `invio job create --from-file <file>`. It is validated against the job
schema and the model registry, and a model not in `models.d/anthropic.yaml` fails before any
call.

## Live check (needs a real key)

```bash
export INVIO_ANTHROPIC_API_KEY=...
uv run invio llm test anthropic               # exit 0, "ok provider=anthropic model=claude-haiku-4-5-20251001 ..."
uv run invio llm test anthropic --model claude-sonnet-4-6
uv run pytest -m live tests/test_llm_anthropic_live.py
```

Without the key, `invio llm test anthropic` exits 2 and names `INVIO_ANTHROPIC_API_KEY`
([contract](contracts/cli-llm-test.md)).
