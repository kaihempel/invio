# Quickstart: Validating the LLM Provider Layer

**Feature**: [spec.md](./spec.md) · **Contract**: [contracts/python-api.md](./contracts/python-api.md)

Everything here runs offline; no API keys and no database are needed.

## Prerequisites

```bash
uv sync --locked
```

## 1. Quality gates (same as CI)

```bash
uv run ruff check
uv run ruff format --check
uv run mypy            # strict over src/ — includes the FakeProvider protocol check
uv run pytest
```

Expected: all green, coverage ≥ 95 %.

## 2. Feature tests only

```bash
uv run pytest tests/test_llm_*.py -v
```

## 3. Acceptance criteria → tests

| Acceptance criterion (issue #7) / spec item | Where it is proven |
|---|---|
| `FakeProvider` satisfies the protocol (mypy) | `uv run mypy` (`TYPE_CHECKING` assignment in `invio/llm/fake.py`); `tests/test_llm_fake.py` (scripting, recording, exhausted script) |
| Invalid structured output → exactly one repair, then `LLMInvalidOutputError` | `tests/test_llm_repair.py` (valid first, invalid→valid, invalid→invalid, prose answer, repair request contains problems, usage summed, provider error during repair) |
| Missing key → `LLMAuthError` with clear message | `tests/test_llm_factory.py` (message names `INVIO_<P>_API_KEY`, blank key, key never in message) |
| Cost calculation from registry unit-tested | `tests/test_llm_registry.py` (formula, zero/large counts, zero price, unknown model → `None`, quantization) |
| Model ids never hard-coded outside registry/job configs | `tests/test_llm_registry.py::test_no_model_ids_in_source` (SC-005) |
| New provider module + `models.d/<p>.yaml` resolvable without editing `factory.py` | `tests/test_llm_factory.py::test_new_provider_module_is_discovered` (temp dir prepended to `invio.llm.__path__`) |
| Role resolution, unknown role/provider, model not in registry / wrong provider | `tests/test_llm_factory.py` |
| Registry load errors (unknown key, negative price, provider ≠ file, duplicate id) | `tests/test_llm_registry.py` |
| Typed errors propagate; timeout → `LLMUnavailableError` (FR-022) | `tests/test_llm_base.py`, `tests/test_llm_fake.py` (`FakeDelay` with tiny timeout, also during repair) |
| Logging: one `llm.call` per call, warnings for repair/error, no prompt/answer/secret (FR-019–021) | `tests/test_llm_logging.py` |
| `INVIO_LLM_TIMEOUT_SECONDS` default 60, rejects ≤ 0 | `tests/test_settings.py` |

## 4. Manual smoke check (optional)

```bash
uv run python - <<'EOF'
import asyncio
from pydantic import BaseModel
from invio.llm.base import Usage
from invio.llm.fake import FakeProvider, FakeReply

class Score(BaseModel):
    score: float

fake = FakeProvider([FakeReply("not json"), FakeReply('{"score": 0.8}', Usage(12, 3))])
value, usage = asyncio.run(
    fake.complete_structured("rate", "item", Score, model="any", temperature=0)
)
print(value, usage, len(fake.requests))
EOF
```

Expected output: `score=0.8 Usage(input_tokens=22, output_tokens=8, requests=2) 2`.

## 5. Missing-key message check (optional)

Once a real provider (e.g. Mistral, later issue) exists:

```bash
INVIO_MISTRAL_API_KEY= uv run python -c "from invio.llm.factory import get_provider; get_provider('mistral')"
```

Expected: `LLMAuthError: LLM provider 'mistral' needs an API key: set INVIO_MISTRAL_API_KEY`.
