# Quickstart: validating the OpenAI provider

## Prerequisites
`uv sync --locked` (after the dependency is added and `uv.lock` updated).

## Offline validation (no key, no network)
```bash
uv run pytest tests/test_llm_openai.py tests/test_llm_provider_contract.py tests/test_llm_mistral.py tests/test_cli_llm.py tests/test_llm_factory.py -q
uv run ruff check . && uv run ruff format --check . && uv run mypy
```
Expected: all green. The Mistral tests are unchanged and pass, which shows the retry extraction did not change behaviour.

## Select OpenAI in a job
In a job file set `llm.provider: openai` and `llm.models.fast` / `smart` to ids from `src/invio/llm/models.d/openai.yaml`; job validation must accept it.

## Live check (needs a real key)
```bash
export INVIO_OPENAI_API_KEY=...
uv run invio llm test openai                          # expect: ok provider=openai model=... , exit 0
INVIO_OPENAI_API_KEY= uv run invio llm test openai    # expect: exit 2, names the variable
uv run pytest tests/test_llm_openai_live.py           # runs only with the key set
```
