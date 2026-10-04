# Implementation Plan: Mistral LLM Provider

**Branch**: `gh-issue-8` (create from `main`, see note 7) | **Date**: 2026-10-04 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/005-gh-issue-8/spec.md` (GitHub issue #8)

## Summary

Add the first production LLM provider. `src/invio/llm/mistral.py` defines `MistralProvider`,
registered as `mistral`. It uses the official `mistralai` 3.x SDK (`chat.complete_async`) and
wraps every attempt in the #7 `with_timeout`. invio does its own retrying: 429, 5xx and
connection failures get up to 3 retries with exponential backoff (1/2/4 s ±25 %). A
`Retry-After` of up to 60 s is honoured; a longer one fails fast. Timeouts and other 4xx are
never retried. Statuses map to typed errors: 401/403 → `LLMAuthError`, 429 →
`LLMRateLimitError`, 5xx/timeout/connection → `LLMUnavailableError`, other 4xx → the new
`LLMInvalidRequestError`. Error messages are built from sanitized fields, never from
`str(sdk_exc)`. Structured output sends a strict JSON-schema `response_format` through the
shared `structured_with_repair`.

`base.py` gains two provider-neutral additions: `LLMInvalidRequestError` and
`LLMRateLimitError.retry_after`. `models.d/mistral.yaml` ships pinned model ids.
`invio llm test <provider>` is a new auto-discovered CLI module. Tests replay recorded
responses through an injected `httpx2.MockTransport`, because `respx` cannot intercept the
SDK's `httpx2` stack. One live test is opt-in via the `live` marker.

## Technical Context

**Language/Version**: Python 3.12 (uv-managed)

**Primary Dependencies**: New runtime: `mistralai>=3.0,<4` (required by the issue) and
`httpx2>=2.13`. `httpx2` is already the SDK's transport; it is declared explicitly because
invio imports its exception types and `MockTransport`. Existing: Pydantic v2, Typer, stdlib
`asyncio`/`random`/`email.utils`. No new dev dependency (`respx` rejected, research R3).

**Storage**: N/A. One package data file, `src/invio/llm/models.d/mistral.yaml`. No DB change.

**Testing**: pytest + pytest-asyncio (auto mode). `httpx2.MockTransport` replaying JSON fixtures
from `tests/fixtures/mistral/`. Injected `sleep`/`uniform`/`now`, so tests never wait for
real. `caplog` for `llm.retry`. Typer `CliRunner` for the CLI. A `live` marker that is
deselected by default.

**Target Platform**: Linux (cron/systemd) and macOS dev

**Project Type**: CLI application / library modules (single project)

**Performance Goals**: The provider adds negligible overhead around the HTTP call.
Worst-case duration of a free-text call is bounded: about 4 × timeout plus 3 × 60 s (backoff
is 7 s without `Retry-After`, at most 3 × 60 s when it is honoured). A structured call can
take twice that, because the repair request has its own retry budget. The Mistral tests run in < 10 s.

**Constraints**: `mypy --strict` clean (`MistralProvider` checked against `LLMProvider`). No
network in the default test run. No key, prompt, answer or raw response body in errors or
logs. `invio.llm` imports only `invio.config` plus the SDK and `httpx2`. `cli` → `llm` is
allowed. Coverage ≥ 95 % for new code. Shared-file edits are limited to `llm/base.py` (two
additive changes), `pyproject.toml`/`uv.lock` (dependencies, marker), `.env.example`/`README.md`
(docs).

**Scale/Scope**: About 300 LOC of source (mistral ~220, CLI ~60, base ~20), about 600 LOC of
tests plus about 17 fixture files.

All unknowns are resolved in [research.md](./research.md) (R1–R14). No NEEDS CLARIFICATION
remain.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Assessment | Status |
|---|---|---|
| I. Strict contracts at boundaries | LLM responses are parsed by the SDK's Pydantic models. Structured answers are validated against the caller's Pydantic schema via `structured_with_repair`. `RetryPolicy` validates its fields. The registry file uses the strict #7 format (`schema_version: 1`). The error types are defined once in `base.py` and imported. | ✅ |
| II. CLI-first | `invio llm test` lives in `src/invio/cli/commands/llm.py` and is auto-discovered, with no edits to other CLI files. Results go to stdout, diagnostics to stderr. Exit codes: 0 / 1 provider error / 2 config error. | ✅ |
| III. Test-covered behaviour | Every acceptance scenario of US1–US5 maps to tests, rejection paths included. The default run is offline and deterministic (no real sleeps, seeded/injected jitter). The live test is opt-in and excluded by default. | ✅ |
| IV. Quality gates mirror CI | ruff, ruff format, mypy strict and pytest. Both SDK and `httpx2` ship `py.typed`, so no `type: ignore` is expected. `uv.lock` is updated and CI installs with `--locked`. | ✅ |
| V. Secrets stay secret, runs observable | The key is read only via `require_api_key`, at provider build time, and never stored in repr or messages. Messages come from sanitized fields; `str(sdk_exc)` is never used and SDK exceptions are raised `from None`. Retries are logged as structured `llm.retry` warnings, and final errors through the #7 `llm.error` line. | ✅ |
| Architecture: dependency direction | `cli` → `llm` → `config`. `llm` does not import `db`, `graph` or `cli`. | ✅ |
| Architecture: new runtime deps justified | `mistralai` is required by the issue. `httpx2` is the SDK's own transport, declared because invio imports it. Both are justified in the PR description. | ✅ |
| Simplicity first | No generic retry framework: the retry loop is private to `mistral.py`. If later providers need the same loop, it moves to `base.py` then, not speculatively now. | ✅ |

**Post-design re-check (after Phase 1)**: still ✅ for all rows. The `base.py` additions are
backward-compatible and provider-neutral (clarifications Q4/Q5). No violations, so Complexity
Tracking is empty.

## Project Structure

### Documentation (this feature)

```text
specs/005-gh-issue-8/
├── plan.md              # This file
├── research.md          # Phase 0 (R1–R14)
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   ├── python-api.md    # MistralProvider, RetryPolicy, base.py additions, pyproject changes
│   └── cli.md           # invio llm test
├── checklists/requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks, not created here)
```

### Source Code (repository root)

```text
src/invio/
├── llm/
│   ├── base.py              # EDIT: + LLMInvalidRequestError, + LLMRateLimitError.retry_after
│   ├── mistral.py           # NEW: RetryPolicy, MistralProvider (@register_provider("mistral"))
│   ├── __init__.py          # EDIT (docstring only): list the mistral module
│   └── models.d/
│       └── mistral.yaml     # NEW: pinned Mistral models + prices
└── cli/commands/
    └── llm.py               # NEW: `invio llm test PROVIDER [--model]`

tests/
├── fixtures/mistral/*.json  # NEW: recorded/synthetic responses (status, headers, body)
├── mistral_helpers.py       # NEW: fixture loader, MockTransport recorder, provider builder
├── test_llm_mistral.py      # NEW: US1–US3, US5 (offline)
├── test_llm_mistral_live.py # NEW: @pytest.mark.live, skipped without key
├── test_cli_llm.py          # NEW: US4 (offline, MockTransport / patched provider)
└── test_llm_base.py         # EDIT: new error type + retry_after validation

pyproject.toml               # EDIT: deps, `live` marker, addopts -m "not live"
uv.lock                      # EDIT: lock new deps
.env.example, README.md      # EDIT: Mistral setup + `invio llm test` + `pytest -m live`
```

**Structure Decision**: Single project, following the existing `src/invio/` layout. The
provider is one new module in `invio.llm`, discovered by the #7 factory with no factory edit.
The CLI command is one new module, discovered by `discover_commands`.

## Implementation Notes (for /speckit-tasks)

1. **Order**: base.py additions → `mistral.py` free-text path plus error mapping → retry loop →
   structured path → registry file → CLI → live test, marker and docs.
2. **Error message builder**: one helper turns an SDK exception into
   `(kind, status, safe_detail, retry_after)`. Its tests cover the 422 `input` echo and the
   10 000-char `SDKError` body, proving neither appears in the message.
3. **Existing tests**: once `mistral` is a real registered provider, factory tests that
   register `"mistral"` must keep using the `patched_providers` fixture (they do today).
   Run the whole suite to catch any test that assumed no real provider exists.
4. **Registry prices**: re-verify ids and prices against Mistral's price list during
   implementation (research R11).
5. **Client lifecycle (FR-027, research R15)**: an `httpx2.AsyncClient`'s connection pool is
   tied to the event loop that first used it. The CLI builds the provider outside
   `asyncio.run`, and the scheduler may run several loops in one process. So the provider
   creates its `Mistral` client **lazily, once per running event loop**: it remembers the loop
   the cached client belongs to, and when called from a different loop it builds a new client
   from `client_factory` (default: `httpx2.AsyncClient(follow_redirects=True)`) and closes the
   old one with best effort. Tests inject `client_factory` returning a `MockTransport` client.
6. **CLI time bound (FR-026)**: `invio llm test` passes
   `settings.model_copy(update={"llm_timeout_seconds": min(settings.llm_timeout_seconds, 20.0)})`
   to `get_provider`, so each attempt is capped at 20 s without any factory change.
7. **Branch**: create `gh-issue-8` from `main` before the first task (this worktree is on
   `worktree-track-llm`). The issue's `issue/08-add-mistral-provider` is not used, matching the
   `gh-issue-6`/`gh-issue-7` PRs.

## Complexity Tracking

No constitution violations, so this section is empty.
