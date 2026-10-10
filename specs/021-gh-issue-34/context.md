# Context: GH issue #34 — Add provider fallback and per-role provider selection

Branch: `gh-issue-34` · Track `llm` · round 2 · depends on #7, #8 (done)

## Goal

Jobs survive provider outages, and `fast` and `smart` can use different providers (for example
a local model for `fast`, a stronger hosted one for `smart`).

## Acceptance criteria (from the issue)

- `fast` and `smart` can use different providers in one job.
- A primary outage triggers the fallback and shows up in the logs and in the `llm_usage` rows.
- Authentication errors do not trigger the fallback.
- Old simple configs still validate.

## Already in place

- `LLMConfig` (`src/invio/config/job.py`): `provider`, `models: LLMModels{fast, smart}` (strings),
  `fallback_provider` (validated to differ from `provider`, **never read at runtime**).
- `docs/job.example.yaml` sets `fallback_provider: anthropic` with no fallback model.
- `docs/job.schema.json` is generated from the models (see `tests/test_job_schema.py`).
- Runtime wiring: `pipeline/deps.py::provider_for` builds **one** provider through
  `llm/factory.py::resolve` → `graph/ports.py::ProviderBinding(provider, provider_name,
  fast_model, smart_model, registry)`. `graph/stages.py::load_job` wraps it in
  `RetryingProvider` (`llm/retry.py`); the stage contexts (`_scoring_context`,
  `_summary_context`, `synthesize_digest`) copy `provider`/`provider_name`/models.
- `graph/nodes/llm_calls.py::_record_usage` writes `llm_usage.provider = ctx.provider_name` and
  prices with the requested model.
- `factory._LoggedProvider` logs `llm.call`/`llm.error` for every call.

## Design decisions (confirmed by the user)

### 1. Config: per-role long form

```yaml
llm:
  provider: ollama              # default provider of every role
  models:
    fast: llama3.2:3b           # shorthand: model id of `provider`
    smart:                      # long form
      provider: anthropic       # optional, defaults to llm.provider
      model: claude-sonnet-4-6
      fallback:                 # optional, this role only
        provider: openai
        model: gpt-5
  fallback_provider: mistral    # job-wide fallback for roles without their own...
  fallback_models:              # ...takes effect only together with fallback_models
    fast: mistral-small-latest
    smart: mistral-large-latest
```

- New strict models: `LLMTarget(provider: LLMProvider, model: str)` and
  `LLMRoleModel(provider: LLMProvider | None = None, model: str, fallback: LLMTarget | None = None)`.
  `LLMModels.fast`/`smart` become `str | LLMRoleModel`.
- `LLMConfig.fallback_models: LLMModels-like (strings only) | None = None`;
  `fallback_models` without `fallback_provider` is a validation error.
- Keep the existing rule `fallback_provider != provider` while some role is served by
  `provider`; additionally a role's effective fallback provider must differ from the role's
  effective provider.
- `LLMConfig.role(name)` resolves the shorthand to a frozen `RoleSelection(provider, model,
  fallback: LLMTarget | None)`: role fallback first, else `fallback_provider` + the
  `fallback_models` entry, else none.
- Regenerate `docs/job.schema.json`; update `docs/job.example.yaml` and the README LLM section.

### 2. Old configs: warn and run without a fallback

`fallback_provider` without `fallback_models` (and no role fallback) still validates; at run
start (`provider_for`) log a warning `llm.fallback_unconfigured` once and run without a fallback,
as today. `fallback_models` that no role uses (every role has its own fallback) logs
`llm.fallback_models_unused`.

### 3. Runtime

- `invio/llm/fallback.py::FallbackProvider(primary, fallback, *, primary_name, fallback_name,
  fallback_model)`: tries `primary` with the requested model; on `LLMUnavailableError` or
  `LLMRateLimitError` (incl. `LLMQuotaError`) it logs `llm.fallback` (warning: primary provider,
  primary model, fallback provider, fallback model, error class — never the message) and calls
  `fallback` with `fallback_model`. `LLMAuthError`, `LLMInvalidRequestError`,
  `LLMInvalidOutputError` and non-LLM errors propagate untouched. Fallback errors propagate
  after an `llm.fallback_failed` warning (same fields).
  Stateless per call (safe under concurrency). Module must not import `invio.graph`.
- Retries happen **before** the fallback: compose `FallbackProvider(RetryingProvider(primary),
  RetryingProvider(fallback))`, so `load_job` builds the per-role providers instead of wrapping
  a single one.
- Actually used provider/model reach `llm_usage`: `Usage` gets optional `provider`/`model`
  fields (default `None`, ignored by `__add__` except kept from the left operand) that
  `FallbackProvider` stamps on every result (and on `LLMInvalidOutputError.usage`);
  `_record_usage` uses `usage.provider or ctx.provider_name` and `usage.model or model`, and
  prices with the model actually used.
- `ProviderBinding` describes each role (`RoleBinding` for `fast`, `smart`: provider, provider
  name, model and an optional `FallbackBinding` of provider, provider name and model) so the
  stage contexts get the right provider per role.
  Providers for the same provider name are built once per run and all are closed on exit.
- Every model (primary and fallback) is checked with `registry.require(model, provider)` when
  binding, before any call.
