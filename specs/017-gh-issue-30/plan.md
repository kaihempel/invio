# Implementation Plan: OpenAI Provider

**Branch**: `gh-issue-30` (constitution naming; supersedes the issue's `issue/30-add-openai-provider`; spec dir `017-gh-issue-30`) | **Date**: 2026-10-08 | **Spec**: [spec.md](spec.md)

**Input**: Feature specification from `/specs/017-gh-issue-30/spec.md`

## Summary

Add `invio.llm.openai`, an `LLMProvider` backed by the official `openai` SDK, self-registered as
`openai`, with its own `models.d/openai.yaml`. Free text and structured output use the
**Responses API** (stateless, `store=False`); structured output uses a strict JSON-schema
`text.format` plus the existing `structured_with_repair`. Errors map to the existing typed errors
with the same classification as Mistral. To satisfy "share retry/backoff logic", the retry policy
and request loop that currently live inside `mistral.py` are extracted into a provider-neutral
module that both providers use; Mistral behaviour is unchanged (its tests are the regression
guard). The `llm test` CLI command is already provider-generic and needs no change beyond the
provider existing.

## Technical Context

**Language/Version**: Python 3.12+ (uv-managed)

**Primary Dependencies**: `openai` official SDK (new runtime dependency, justified: the issue mandates it; pin `>=2,<3`, exact lower bound confirmed at implementation), `httpx` (the SDK's transport; used by tests via `MockTransport`), existing `pydantic` and `invio.llm.*`

**Storage**: N/A (registry YAML file `models.d/openai.yaml`)

**Testing**: pytest, offline; recorded JSON fixtures replayed through an `httpx.MockTransport` injected as the SDK's `http_client`; no network, no real key. A live test, skipped without a key, mirrors `test_llm_mistral_live.py`

**Target Platform**: Linux server / macOS dev, unattended under cron/systemd

**Project Type**: single project (CLI + library), `src/invio/`

**Performance Goals**: none beyond the existing app-wide `llm_timeout_seconds` (default 60 s) bounding every call

**Constraints**: mypy strict; ruff (including the `httpx2` ban, which does not affect `httpx`); the LLM package must not import `db`/`services`/`cli`; no secrets, prompt or answer text in errors or logs; SDK-internal retries disabled (`max_retries=0`)

**Scale/Scope**: one new provider module (~300 lines), one extracted shared module, one registry file, tests, docs

## Constitution Check

| Principle | Status | Note |
|---|---|---|
| I Strict contracts at boundaries | PASS | Structured answers validated by Pydantic via the shared repair helper; registry file parsed strictly by the existing loader |
| II CLI-first | PASS | `invio llm test openai` works through the existing auto-discovered command; no new command |
| III Test-covered behaviour | PASS | Every FR/acceptance criterion has an offline test; rejection paths covered by error fixtures |
| IV Quality gates | PASS | ruff, ruff format, mypy strict over `src/`, pytest; `uv.lock` updated, CI `--locked` |
| V Secrets / observability | PASS | Key from `INVIO_OPENAI_API_KEY` via `require_api_key`, checked when the provider is used; errors carry status + sanitized provider message only; existing `llm.call` / `llm.error` / `llm.retry` log lines |
| Dependency direction | PASS | `llm` imports `config` only |
| Simplicity | PASS with note | Extracting the retry loop refactors Mistral, justified by the issue's explicit "share retry/backoff logic"; no abstraction beyond what two providers need |

Post-design re-check: unchanged, all PASS.

## Project Structure

### Documentation (this feature)

```text
specs/017-gh-issue-30/
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
├── openai.py            # NEW: OpenAIProvider (@register_provider("openai"))
├── http_retry.py        # NEW: RetryPolicy, retry loop, Retry-After parsing (extracted from mistral.py)
├── mistral.py           # CHANGED: uses http_retry; re-exports RetryPolicy for existing imports
├── __init__.py          # CHANGED: docstring lists openai
└── models.d/openai.yaml # NEW

tests/
├── openai_helpers.py             # NEW: MockTransport recorder (like mistral_helpers.py)
├── fixtures/openai/*.json        # NEW: recorded responses
├── test_llm_openai.py            # NEW: provider behaviour, errors, retries, structured
├── test_llm_provider_contract.py # NEW: shared contract suite over fake / mistral / openai
├── test_llm_openai_live.py       # NEW: skipped without key
├── test_llm_mistral.py           # unchanged expectations (regression guard for the refactor)
└── test_cli_llm.py, test_llm_factory.py  # CHANGED only where they assume openai is unregistered

pyproject.toml, uv.lock           # openai dependency
README.md, docs/deployment.md     # provider list / key variable
```

**Structure Decision**: single-project layout. The provider is one self-registering module, so
no shared file (the factory) is edited, per spec FR-011. The only shared-code change is the
extraction of the retry loop.

## Complexity Tracking

No constitution violations. The `mistral.py` refactor is a deliberate scope item (shared retry),
not a violation.
