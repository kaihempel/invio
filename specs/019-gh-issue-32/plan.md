# Implementation Plan: Google (Gemini) Provider

**Branch**: `gh-issue-32` (constitution naming; supersedes the issue's `issue/32-add-google-gemini-provider`; spec dir `019-gh-issue-32`) | **Date**: 2026-10-09 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/019-gh-issue-32/spec.md`

## Summary

Add `invio.llm.google`, an `LLMProvider` backed by the official `google-genai` SDK (2.x). It
self-registers as `google` and ships its own `models.d/google.yaml`, listing
`gemini-3.5-flash-lite` (fast) and `gemini-3.8-flash` (smart).

- **Requests.** Every call is one `aio.models.generate_content` request with the system
  instruction, the registered thinking level, and the temperature. The temperature is omitted for
  models flagged to keep their default, which is the case for all Gemini 3 models. Free text adds
  the model's thinking allowance to the caller's `max_tokens`.
- **Structured output.** Structured calls request `application/json` with a
  `response_json_schema`, produced by a pure converter that inlines `$defs` and drops unsupported
  keywords. The answer is then validated through the existing `structured_with_repair`.
- **Blocks.** A blocked prompt or a policy-stopped answer raises `LLMInvalidOutputError` naming
  the reason, without retry or repair.
- **Errors.** SDK and `httpx` exceptions are classified into the shared `Failure` records and
  retried by `http_retry.run_with_retries`. A Gemini-specific step maps 400 `API_KEY_INVALID` to
  auth, maps daily or zero quota to `LLMQuotaError`, and reads `RetryInfo` as the wait hint.
- **Registry.** It gains three optional fields: `thinking_level`, `thinking_allowance_tokens` and
  `keep_default_temperature`.
- **CLI.** `llm test` is unchanged.

## Technical Context

**Language/Version**: Python 3.12+ (uv-managed)

**Primary Dependencies**:
- `google-genai>=2.29,<3`: new runtime dependency, justified because the issue mandates the
  official SDK. It brings `google-auth`, `requests`, `tenacity` and `websockets` transitively.
- Its transport is `httpx` 0.28, already locked transitively, so tests use `httpx.MockTransport`
  through `tests/sdk_harness.py`.
- Existing `pydantic` and `invio.llm.*` modules.

**Storage**: N/A. The new registry file is `models.d/google.yaml`, and three optional fields are
added to the registry schema.

**Testing**:
- pytest, offline, using recorded `generateContent` JSON fixtures replayed through an injected
  `httpx.AsyncClient`.
- The shared contract suite gains a Google harness.
- The pure schema converter gets its own unit tests.
- A live test (`-m live`) is skipped without `INVIO_GOOGLE_API_KEY`.

**Target Platform**: Linux server or macOS dev, unattended under cron/systemd

**Project Type**: single project (CLI + library), `src/invio/`

**Performance Goals**: none beyond the app-wide `llm_timeout_seconds` (default 60 s) bounding every call

**Constraints**:
- **Quality gates.** mypy strict and ruff. The `google.genai` types are typed, so no
  `ignore_missing_imports` is needed (to be verified first during implementation). The `httpx2` ban applies only to
  `scheduling`/`sources`.
- **Layering.** The LLM package must not import `db`, `services` or `cli`.
- **Error hygiene.** Errors and logs contain no secrets and no prompt or answer text, and
  `str(APIError)` (which embeds the body) is never used.
- **SDK retries disabled.** `retry_options=None` gives the SDK's "never retry" stop.
- **Fixed endpoint and credentials.** The client uses `vertexai=False`, an explicit `api_key`
  and an explicit `base_url` (research R4).

**Scale/Scope**:
- one provider module (~350 lines), with the schema converter in it or in a sibling private
  module;
- one registry change and one registry file;
- test helpers, fixtures and tests;
- docs.

## Constitution Check

| Principle | Status | Note |
|---|---|---|
| I Strict contracts at boundaries | PASS | Answers are validated by Pydantic via the shared repair helper. The registry stays strictly parsed: the new fields are a `Literal`, a `StrictInt > 0` and a `StrictBool`. `schema_version` stays 1 because the change is additive. |
| II CLI-first | PASS | `invio llm test google` works through the existing auto-discovered command; no new command. |
| III Test-covered behaviour | PASS | Every FR and acceptance scenario maps to an offline test. Rejection paths are covered by error and block fixtures. |
| IV Quality gates | PASS | ruff, ruff format, mypy strict and pytest. `uv.lock` is updated and CI installs with `--locked`. |
| V Secrets / observability | PASS | The key comes from `INVIO_GOOGLE_API_KEY` via `require_api_key` and is checked when the provider is built. Errors carry the status and the sanitized provider message only. Existing log lines are reused. |
| Dependency direction | PASS | `llm` imports `config` only |
| Simplicity | PASS | No new abstraction. The retry loop, repair helper, timeout helper, `LoopClients` and `sdk_harness` are reused, and `classify_status` is unchanged (`RetryInfo` is fed in as a header). The registry fields are the minimum the clarifications require. |
| Docs in same PR | PASS (planned) | README provider list, `docs/deployment.md` (supported providers, `INVIO_GOOGLE_API_KEY`), and `models.d/README.md` (new fields) |

Post-design re-check: unchanged, all PASS.

## Project Structure

### Documentation (this feature)

```text
specs/019-gh-issue-32/
├── plan.md
├── research.md
├── data-model.md
├── quickstart.md
├── contracts/
│   ├── provider-behaviour.md
│   └── cli-llm-test.md
└── tasks.md             # /speckit-tasks
```

### Source Code (repository root)

```text
src/invio/llm/
├── google.py               # NEW: GoogleProvider (@register_provider("google")), gemini_schema, _classify, _answer
├── registry.py             # CHANGED: optional thinking_level, thinking_allowance_tokens, keep_default_temperature
└── models.d/
    ├── google.yaml         # NEW: gemini-3.5-flash-lite, gemini-3.8-flash
    └── README.md           # CHANGED: documents the new fields

tests/
├── sdk_harness.py                  # CHANGED: RecordedRequest carries request headers
├── google_helpers.py               # NEW: httpx MockTransport recorder bound to fixtures/google
├── fixtures/google/*.json          # NEW: success, structured, blocks, MAX_TOKENS, errors (401/403/400 key/429/quota/500/503/404)
├── test_llm_google.py              # NEW: request shape, thinking/temperature, usage, blocks, errors, retries, hygiene
├── test_llm_google_schema.py       # NEW: gemini_schema (inlining, cycles, keyword filter, pipeline schemas)
├── test_llm_google_live.py         # NEW: opt-in (-m live), skipped without key
├── test_llm_provider_contract.py   # CHANGED: GoogleHarness in HARNESSES
├── test_llm_registry.py            # CHANGED: new optional fields accepted/validated; old files unchanged
└── test_cli_llm.py, test_llm_factory.py  # CHANGED only where they assume google is unregistered

pyproject.toml, uv.lock             # google-genai dependency
README.md, docs/deployment.md       # provider list ("jobs can use mistral, openai, anthropic and google")
```

**Structure Decision**: single-project layout. The provider is one self-registering module and
the factory is not edited. The shared-code changes are the additive registry fields and one
extra field on the test harness's `RecordedRequest`.

## Complexity Tracking

There are no constitution violations. Two decisions are worth noting for review:
- **Registry fields.** The three fields extend a shared module with Google-specific settings.
  They were chosen in clarification and are backward compatible.
- **Prices.** The `gemini-3.8-flash` price doubles on 2027-01-01. The registry file header flags
  the date, because the registry holds a single price per model.
