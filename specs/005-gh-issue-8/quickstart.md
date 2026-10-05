# Quickstart: Validate the Mistral Provider (#8)

## Prerequisites

- `uv sync --locked` (pulls `mistralai` and `httpx2`)
- Optional, for live checks only: `export INVIO_MISTRAL_API_KEY=...`

## 1. Offline test suite (no key, no network)

```bash
uv run pytest tests/test_llm_mistral.py tests/test_cli_llm.py tests/test_llm_base.py -q
```

Expected: all pass in < 10 s; the live tests are skipped. Covers spec US1–US5 and the
behaviour table in [contracts/python-api.md](./contracts/python-api.md).

Prove "tests run offline" even with a key set (SC-005):

```bash
INVIO_MISTRAL_API_KEY=dummy uv run pytest -q   # still zero requests to Mistral
```

## 2. Quality gates (as CI)

```bash
uv run ruff check && uv run ruff format --check && uv run mypy && uv run pytest
```

`mypy` must confirm `MistralProvider` satisfies `LLMProvider` (the `TYPE_CHECKING` assignment).

## 3. CLI check ([contracts/cli.md](./contracts/cli.md))

```bash
env -u INVIO_MISTRAL_API_KEY uv run invio llm test mistral; echo "exit=$?"   # → missing-key message, exit=2
uv run invio llm test nope; echo "exit=$?"                                   # → unknown provider, exit=2
uv run invio llm test mistral --model mistral-small-latest; echo "exit=$?"   # → not registered, exit=2
INVIO_MISTRAL_API_KEY=invalid uv run invio llm test mistral; echo "exit=$?"  # → LLMAuthError, exit=1 (network)
uv run invio llm test mistral; echo "exit=$?"                                # → "ok provider=mistral ...", exit=0 (real key)
```

## 4. Opt-in live test (real key, costs a few tokens)

```bash
uv run pytest -m live -q
```

Expected: 1 passed with a key; 1 skipped ("INVIO_MISTRAL_API_KEY not set") without.

## 5. Registry sanity

```bash
uv run python -c "from invio.llm.registry import default_registry as r; \
print(sorted(m for m in r().model_ids() if r().get(m).provider == 'mistral'))"
```

Expected: pinned ids only (no `-latest`), matching Mistral's current price list.
