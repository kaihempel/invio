# Implementation Plan: Anthropic (Claude) Provider

**Branch**: `gh-issue-31` (constitution naming; supersedes the issue's `issue/31-add-anthropic-claude-provider`; spec dir `018-gh-issue-31`) | **Date**: 2026-10-09 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/018-gh-issue-31/spec.md`

## Summary

Add `invio.llm.anthropic`, an `LLMProvider` backed by the official `anthropic` SDK (1.x),
self-registered as `anthropic`, with its own `models.d/anthropic.yaml` listing Claude Haiku 4.5
(fast) and Claude Sonnet 4.6 (smart). Free text uses one Messages API request with `system` and
`max_tokens`. Structured output declares one tool whose `input_schema` is the Pydantic JSON
schema, forces it with `tool_choice: {type: "tool"}`, serialises the tool input to JSON and
validates it through the existing `structured_with_repair`. Structured calls use the model's
registered maximum output size, which needs one new optional registry field
(`max_output_tokens`). SDK exceptions are classified into the existing `Failure` records and
retried by the shared `http_retry.run_with_retries` loop, as the OpenAI provider does. The
`llm test` command is unchanged.

## Technical Context

**Language/Version**: Python 3.12+ (uv-managed)

**Primary Dependencies**: `anthropic` official SDK (new runtime dependency, justified: the issue mandates it; pin `>=1.12,<2`). Its transport is `httpx2`, already a direct dependency, so tests use the same `httpx2.MockTransport` pattern as `tests/mistral_helpers.py`. Existing `pydantic` and `invio.llm.*` modules.

**Storage**: N/A (registry YAML file `models.d/anthropic.yaml`; one optional field added to the registry schema)

**Testing**: pytest, offline. Recorded JSON fixtures are replayed through an `httpx2.MockTransport` injected as the SDK's `http_client`, with no network and no real key. The shared contract suite gains an Anthropic harness. A live test is skipped without a key, mirroring `test_llm_openai_live.py`.

**Target Platform**: Linux server / macOS dev, unattended under cron/systemd

**Project Type**: single project (CLI + library), `src/invio/`

**Performance Goals**: none beyond the existing app-wide `llm_timeout_seconds` (default 60 s) bounding every call

**Constraints**:
- mypy strict and ruff; the `httpx2` ban applies only to `scheduling`/`sources`.
- The LLM package must not import `db`/`services`/`cli`.
- No secrets, prompt or answer text in errors or logs.
- SDK-internal retries are disabled (`max_retries=0`).
- `temperature` is passed through `extra_body`, because the 1.x SDK dropped it from `messages.create()` (research R2).
- The base URL is always explicit, and only `x-api-key` authentication is sent (research R7).

**Scale/Scope**: one new provider module (~300 lines), one small registry change, one registry file, test helpers and fixtures, tests, docs

## Constitution Check

| Principle | Status | Note |
|---|---|---|
| I Strict contracts at boundaries | PASS | Tool input validated by Pydantic via the shared repair helper. Registry stays strictly parsed; the new optional field is `StrictInt > 0`. `schema_version` stays 1 because the change is additive and existing files stay valid (research R5). |
| II CLI-first | PASS | `invio llm test anthropic` works through the existing auto-discovered command; no new command |
| III Test-covered behaviour | PASS | Every FR and acceptance scenario maps to an offline test; rejection paths are covered by error fixtures |
| IV Quality gates | PASS | ruff, ruff format, mypy strict over `src/`, pytest; `uv.lock` updated, CI `--locked` |
| V Secrets / observability | PASS | Key from `INVIO_ANTHROPIC_API_KEY` via `require_api_key`, checked when the provider is built. Errors carry the status plus the sanitized provider message only. Existing `llm.call` / `llm.error` / `llm.retry` log lines are reused. |
| Dependency direction | PASS | `llm` imports `config` only |
| Simplicity | PASS | No new abstraction. The shared retry loop, repair helper and timeout helper are reused. The registry field is the minimum needed for FR-011a/b. |
| Docs in same PR | PASS (planned) | README provider list, `docs/deployment.md` ("jobs can use ..."), `models.d/README.md` (new field) |

Post-design re-check: unchanged, all PASS.

## Project Structure

### Documentation (this feature)

```text
specs/018-gh-issue-31/
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
├── anthropic.py            # NEW: AnthropicProvider (@register_provider("anthropic"))
├── registry.py             # CHANGED: optional max_output_tokens on ModelInfo / _ModelEntry
├── __init__.py             # CHANGED: docstring lists anthropic (if it lists providers)
└── models.d/
    ├── anthropic.yaml      # NEW: claude-haiku-4-5-20251001, claude-sonnet-4-6
    └── README.md           # CHANGED: documents max_output_tokens

tests/
├── anthropic_helpers.py            # NEW: httpx2 MockTransport recorder (like openai_helpers.py)
├── fixtures/anthropic/*.json       # NEW: recorded responses (success, tool_use, errors)
├── test_llm_anthropic.py           # NEW: behaviour, error mapping, retries, structured, limits
├── test_llm_anthropic_live.py      # NEW: opt-in (-m live), skipped without key
├── test_llm_provider_contract.py   # CHANGED: adds the Anthropic harness to HARNESSES
├── test_llm_registry.py            # CHANGED: optional field accepted/validated, old files unchanged
└── test_cli_llm.py, test_llm_factory.py  # CHANGED only where they assume anthropic is unregistered

pyproject.toml, uv.lock             # anthropic dependency
README.md, docs/deployment.md       # provider list ("jobs can use mistral, openai and anthropic")
```

**Structure Decision**: single-project layout. The provider is one self-registering module and
the factory is not edited. The only shared-code change is the additive registry field.

## Complexity Tracking

No constitution violations. The registry change touches a shared module. It was chosen in
clarification (per-model maximum output size) and is backward compatible.
