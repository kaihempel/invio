# Data Model: LLM Provider Abstraction, Usage Tracking and Model Registry

**Feature**: [spec.md](./spec.md) · **Plan**: [plan.md](./plan.md) · **Research**: [research.md](./research.md)

No database changes: the existing `llm_usage` table (#4) already has `provider`, `model`,
`input_tokens`, `output_tokens` and nullable `cost_usd Numeric(12, 6)`; this feature only
produces the values (persisting them is a later issue). All entities below are in-memory
value objects or file formats.

---

## Usage (`invio.llm.base.Usage`)

Frozen, slotted dataclass — token usage behind one result.

| Field | Type | Rule |
|---|---|---|
| `input_tokens` | `int` | `>= 0` |
| `output_tokens` | `int` | `>= 0` |
| `requests` | `int` | `>= 1`, default `1`; number of provider requests summed into this value |

- `a + b` → field-wise sum (used to combine original + repair request, FR-007).
- Derived: `repaired` (in logs) ⇔ `requests > 1`.
- Missing token counts from a provider are reported as `0` (edge case).

## Role (`invio.llm.factory.Role`)

`Literal["fast", "smart"]` — maps to `LLMConfig.models.fast` / `.smart` (existing job schema).
Any other value → `LLMConfigError` listing `fast, smart`.

## ModelInfo (`invio.llm.registry.ModelInfo`)

Frozen value object — one registry entry.

| Field | Type | Rule |
|---|---|---|
| `model_id` | `str` | non-empty; unique across all registry files |
| `provider` | `str` | equals the file's `provider` |
| `input_price_per_mtok` | `Decimal` | `>= 0`, USD per 1,000,000 input tokens |
| `output_price_per_mtok` | `Decimal` | `>= 0`, USD per 1,000,000 output tokens |
| `context_window` | `int` | `> 0`, tokens |

## ModelRegistry (`invio.llm.registry.ModelRegistry`)

Immutable mapping `model_id → ModelInfo`, merged from all registry files.

- `get(model_id) -> ModelInfo | None`
- `cost(model_id, usage) -> Decimal | None` —
  `(usage.input_tokens × input_price + usage.output_tokens × output_price) / 1,000,000`,
  quantized to `0.000001` (`ROUND_HALF_UP`); `None` if `model_id` unknown; `0.000000` for
  zero-priced models.
- `model_ids() -> frozenset[str]` (used by the SC-005 guard test).

## Registry file (`src/invio/llm/models.d/<provider>.yaml`)

Versioned file format (constitution I). Parsed strictly: unknown keys rejected, all fields
required.

```yaml
schema_version: 1          # must be 1
provider: <provider>       # must equal the file stem
models:                    # mapping, may be empty
  <model-id>:
    input_price_per_mtok: <number >= 0>
    output_price_per_mtok: <number >= 0>
    context_window: <integer > 0>
```

Load rules (FR-014/015):

| Condition | Result |
|---|---|
| directory missing or no `*.yaml` | empty registry (non-YAML files ignored) |
| invalid YAML, unknown/missing key, invalid value, wrong `schema_version` | `ModelRegistryError` naming file (and model, field) |
| `provider` ≠ file stem | `ModelRegistryError` naming file and provider |
| same model id in two files | `ModelRegistryError` naming model id and both files |

Merged from `<p>/models.d` for each `p` in `invio.llm.__path__` (research R8).

## Provider registration (`invio.llm.factory`)

| Element | Description |
|---|---|
| `_REGISTRY: dict[str, type[RegisteredProvider]]` | provider name → class, filled by `@register_provider(name)` |
| `RegisteredProvider` | Protocol = `LLMProvider` + `@classmethod from_settings(settings) -> Self` |
| discovery state | flag; set after importing all non-`_` modules of `invio.llm.__path__` once |

Rules: duplicate name → `LLMConfigError` (FR-009); unknown name in `get_provider` →
`LLMConfigError` naming it and listing registered names sorted (FR-010).

## Errors (`invio.llm.base`)

| Error | Base | Attributes | Raised when |
|---|---|---|---|
| `LLMError` | `Exception` | `provider: str \| None`, `model: str \| None` | base of call failures |
| `LLMRateLimitError` | `LLMError` | — | provider reports rate limiting |
| `LLMAuthError` | `LLMError` | — | key missing/blank at `get_provider` (message names env var) or rejected by provider |
| `LLMUnavailableError` | `LLMError` | — | provider unreachable / 5xx / call timeout exceeded |
| `LLMInvalidOutputError` | `LLMError` | `errors: str`, `usage: Usage` | structured answer invalid after one repair |
| `LLMConfigError` | `ValueError` | — | unknown provider/role, model not in registry or wrong provider, duplicate registration |
| `ModelRegistryError` | `LLMConfigError` | — | invalid/conflicting registry files |

No message or attribute contains a credential, prompt or answer text (FR-004, FR-021).

## Structured completion lifecycle

```text
request #1 ──valid──────────────────────────────▶ return (value, usage1)            requests=1
     │invalid / not JSON
     ▼ log warning llm.repair
request #2 (user + previous answer + problems) ──valid──▶ return (value, usage1+usage2)  requests=2
     │invalid
     ▼
raise LLMInvalidOutputError(errors=<final problems>, usage=usage1+usage2)
```

A provider error (`LLMRateLimitError`, `LLMAuthError`, `LLMUnavailableError` incl. timeout) in
either request propagates unchanged.

## FakeProvider (`invio.llm.fake`)

| Element | Description |
|---|---|
| `FakeReply(text, usage=Usage(10, 5))` | scripted answer |
| `FakeDelay(seconds, then: FakeReply)` | sleeps before answering (timeout tests) |
| `LLMError` instance | raised when reached |
| `FakeRequest(system, user, model, temperature, max_tokens)` | recorded per request (`max_tokens` is `None` for structured requests) |
| `FakeScriptExhaustedError(AssertionError)` | raised when the script has no step left |

## Settings change (`invio.config.settings.Settings`)

| Field | Env var | Type | Default | Rule |
|---|---|---|---|---|
| `llm_timeout_seconds` | `INVIO_LLM_TIMEOUT_SECONDS` | `float` | `60.0` | `> 0` |

## Log events (logger `invio.llm`)

| Event (`message`) | Level | Extra fields |
|---|---|---|
| `llm.call` | INFO | `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd` (string or `null`), `duration_ms`, `repaired` |
| `llm.repair` | WARNING | `schema` (class name), `errors` (validation summary, no input values) — provider and model appear on the `llm.call` / `llm.error` line of the same call |
| `llm.error` | WARNING | `error` (class name), `provider`, `model`, `duration_ms`; for `LLMInvalidOutputError` also `input_tokens`, `output_tokens`, `cost_usd`, `repaired` taken from `err.usage` (the failed call still consumed tokens) |

`job` and `run_id` are added by the existing JSON formatter inside a run context.
