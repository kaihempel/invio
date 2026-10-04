# Implementation Plan: LLM Provider Abstraction, Usage Tracking and Model Registry

**Branch**: `gh-issue-7` | **Date**: 2026-10-04 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `specs/004-gh-issue-7/spec.md` (GitHub issue #7)

## Summary

Build the provider-neutral LLM layer in `src/invio/llm/`: an async `LLMProvider` protocol
(`complete`, `complete_structured`), a `Usage` value object (with a `requests` count that marks
repairs), typed errors, a shared `structured_with_repair()` helper (exactly one repair request,
then `LLMInvalidOutputError`), a per-request `with_timeout()` helper backed by the new
`INVIO_LLM_TIMEOUT_SECONDS` setting, and `require_api_key()` that turns a missing key into an
`LLMAuthError` naming the env var. `factory.py` provides `@register_provider`, `pkgutil`
discovery over `invio.llm.__path__`, `get_provider()` (returns the provider wrapped in a
logging/validation wrapper that writes one `llm.call` line per call) and
`resolve(llm_config, role)`, which checks the model against the registry. `registry.py` merges
`models.d/<provider>.yaml` files (versioned, strict) and computes costs with `Decimal`,
quantized to the `llm_usage.cost_usd` precision. `fake.py` ships a scripted `FakeProvider`
that mypy verifies against the protocol. No concrete provider and no database change are part
of this issue.

## Technical Context

**Language/Version**: Python 3.12 (uv-managed)

**Primary Dependencies**: Existing only — Pydantic v2 (schema validation, registry files),
pydantic-settings (new setting), PyYAML (registry files), standard library `asyncio`,
`pkgutil`, `importlib`, `decimal`. No new runtime or dev dependencies (pytest-asyncio already
present, `asyncio_mode = "auto"`).

**Storage**: N/A — registry files shipped inside the package (`src/invio/llm/models.d/*.yaml`);
existing `llm_usage` table untouched

**Testing**: pytest (+ pytest-asyncio auto mode); `FakeProvider`; fixture registries in
`tests/fixtures/llm/models.d/`; temp provider package for the extensibility test; `caplog` for
log assertions

**Target Platform**: Linux (cron/systemd) and macOS dev

**Project Type**: CLI application / library modules (single project)

**Performance Goals**: Negligible overhead per call (registry loaded once and cached;
discovery once per process); feature tests run in < 5 s

**Constraints**: `mypy --strict` clean (Protocol conformance of `FakeProvider` checked
statically); no network in tests; no credential, prompt or answer text in errors/logs;
`invio.llm` imports only `invio.config` (no `db`, `graph`, `cli`); coverage ≥ 95 %; no file
overlap with parallel tracks besides `config/settings.py`, `.env.example`, `README.md`,
`pyproject.toml` (one-line coverage exclusion)

**Scale/Scope**: ~450 LOC source (base ~170, registry ~110, factory ~130, fake ~70),
~600 LOC tests

All unknowns resolved in [research.md](./research.md) (R1–R16); no NEEDS CLARIFICATION remain.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Gate | Pre-design | Post-design |
|---|---|---|---|
| I. Strict Contracts at Boundaries | LLM responses and registry files validated into typed models; fail fast with field-level messages; versioned formats | ✅ FR-005, FR-015 | ✅ structured answers via `schema.model_validate_json` (R12); registry files strict Pydantic, `extra="forbid"`, `schema_version: 1` (R8); contract defined once in `base.py` |
| II. CLI-First Operation | User functionality via CLI | ⚠️ Library layer only — no user-visible function | ⚠️ No command added; the only user-facing change is a new setting (documented). Recorded in Complexity Tracking |
| III. Test-Covered Behaviour | Every acceptance criterion tested incl. rejection paths; no network/real providers | ✅ FR-018 | ✅ mapping in [quickstart.md](./quickstart.md); `FakeProvider` + temp provider package; all offline (R15, R16) |
| IV. Quality Gates Mirror CI | ruff, format, mypy strict, pytest; narrow suppressions; locked deps | ✅ | ✅ no new deps; no `Any` needed (YAML loaded as `object` and validated); one `TYPE_CHECKING` coverage exclusion added to `pyproject.toml` (justified in R15) |
| V. Secrets / Observability | Keys via settings as secrets, checked at use; structured logs; no secrets in logs/repr | ✅ FR-004, FR-011, FR-019–021 | ✅ `require_api_key` reuses `require_secret` (R6); `llm.call`/`llm.repair`/`llm.error` via existing JSON logger, validation summaries exclude input values (R12, R14) |
| Architecture: dependency direction | adapters → config/domain only | ✅ | ✅ `invio.llm` → `invio.config` only (R1) |
| Architecture: simplicity | No speculative abstractions | ✅ | ✅ Protocol instead of base class; no provider implementations, no retry/fallback, no shipped price files (R9); one wrapper class for cross-cutting concerns (R14) |
| Workflow: branch `gh-issue-<N>`; docs for config changes | | ✅ `gh-issue-7` | ✅ README: "LLM layer" developer section, new `INVIO_LLM_TIMEOUT_SECONDS`, layout (`llm/models.d`); `.env.example` updated |

Result: **PASS with one recorded deviation** (Principle II, see Complexity Tracking).

## Project Structure

### Documentation (this feature)

```text
specs/004-gh-issue-7/
├── spec.md
├── plan.md              # this file
├── research.md          # Phase 0
├── data-model.md        # Phase 1
├── quickstart.md        # Phase 1
├── contracts/
│   └── python-api.md    # base, registry, factory, fake public API
├── checklists/
│   └── requirements.md
└── tasks.md             # Phase 2 (/speckit-tasks — not created here)
```

### Source Code (repository root)

```text
src/invio/
├── config/
│   └── settings.py              # + llm_timeout_seconds (INVIO_LLM_TIMEOUT_SECONDS, > 0, default 60)
└── llm/
    ├── __init__.py              # docstring only (no imports → discovery stays lazy)
    ├── base.py                  # NEW: Usage, LLMProvider, errors, structured_with_repair,
    │                            #      with_timeout, require_api_key
    ├── registry.py              # NEW: ModelInfo, ModelRegistry, load_registry, default_registry
    ├── factory.py               # NEW: Role, RegisteredProvider, register_provider, discovery,
    │                            #      get_provider, resolve, _LoggedProvider
    ├── fake.py                  # NEW: FakeProvider, FakeReply, FakeDelay, FakeRequest
    └── models.d/
        └── README.md            # NEW: registry file format (only *.yaml is loaded)

tests/
├── llm_helpers.py               # NEW: Score schema, registry/fake fixtures helpers
├── conftest.py                  # + fixtures: fake_registry, registered_fake (patches _REGISTRY,
│                                #   resets discovery and default_registry cache)
├── fixtures/llm/models.d/
│   ├── fakeco.yaml              # NEW: test registry (priced + zero-priced models)
│   └── other.yaml               # NEW: second provider for merge/mismatch tests
├── test_llm_base.py             # NEW: Usage, errors, with_timeout, require_api_key
├── test_llm_repair.py           # NEW: US2 (structured_with_repair)
├── test_llm_registry.py         # NEW: US4, SC-005 guard
├── test_llm_factory.py          # NEW: US1, US3, US5 (discovery, resolve, auth, extensibility)
├── test_llm_fake.py             # NEW: US6, timeout via FakeDelay
├── test_llm_logging.py          # NEW: FR-019–021
└── test_settings.py             # + llm_timeout_seconds default / rejection

.env.example                     # + INVIO_LLM_TIMEOUT_SECONDS=60
pyproject.toml                   # coverage exclude_also += "if TYPE_CHECKING:"
README.md                        # + LLM layer section, setting, layout
```

**Structure Decision**: Single project, existing `src/invio/` layout. The issue's
`scout/llm/base.py`, `scout/llm/factory.py` and `scout/llm/models.d/` map to
`src/invio/llm/base.py`, `src/invio/llm/factory.py` and `src/invio/llm/models.d/`;
`registry.py` and `fake.py` are split out for single responsibility (R1).

## Implementation Notes (for /speckit-tasks)

- Order: setting (+ test) → `base.py` (`Usage`, errors, `with_timeout`, `require_api_key`) →
  `structured_with_repair` → `fake.py` → `registry.py` → `factory.py` (registration, discovery,
  `get_provider`, `resolve`) → logging wrapper → extensibility test → README/.env.example.
- `invio/llm/__init__.py` must not import `factory`: discovery imports every module of the
  package, and an eager import from `__init__` would create import cycles.
- Discovery skips modules starting with `_`; it imports `base`, `registry`, `factory`, `fake`
  too (harmless — none of them registers). Keep a `_reset_for_tests()` (or fixture-level
  `monkeypatch` of `_REGISTRY` and the discovery flag) so tests do not leak registrations.
- Extensibility test: write `acme.py` + `models.d/acme.yaml` into `tmp_path`,
  `monkeypatch.setattr(invio.llm, "__path__", [str(tmp_path), *invio.llm.__path__])`, reset
  discovery and `default_registry.cache_clear()`, remove `invio.llm.acme` from `sys.modules`
  at teardown. `acme` is not in the job schema's provider enum, so this test uses
  `get_provider("acme")` + registry lookup rather than `resolve()` (clarification 1).
- `resolve()` tests use a fixture that registers `FakeProvider` under `mistral` in a patched
  `_REGISTRY` and a fixture registry declaring `mistral` models (file `mistral.yaml` under a
  temp dir) — job configs from `tests/job_helpers.py`.
- Pydantic `ValidationError.errors(include_input=False, include_url=False)` → format
  `".".join(map(str, loc)) + ": " + msg`; for JSON syntax errors `loc` is empty → use `"(root)"`.
- Code-fence stripping: only a single leading ```` ```json ```` / ```` ``` ```` line and trailing
  ```` ``` ```` — no other heuristics.
- Log `cost_usd` as `str(Decimal)` (JSON-safe, exact) or `None`.
- `FakeProvider.complete_structured` records `max_tokens=None`.
- The `TYPE_CHECKING` check: `_check: type[LLMProvider] = FakeProvider` inside
  `if TYPE_CHECKING:` at the end of `fake.py`.
- SC-005 guard scans `src/invio/**/*.py` for each id of the shipped registry; it passes
  vacuously until a provider issue ships a registry file — keep it anyway (it guards those).

## Complexity Tracking

| Violation | Why Needed | Simpler Alternative Rejected Because |
|-----------|------------|-------------------------------------|
| Principle II (CLI-First): the LLM layer ships as a Python API without an `invio` command | Issue #7 is an internal adapter contract consumed by pipeline nodes (graph track) and provider modules; it has no operator-facing function of its own. The only user-visible change, `INVIO_LLM_TIMEOUT_SECONDS`, is a setting documented in README and `.env.example` | An `invio llm ...` command (e.g. listing registry models/prices) is not requested by the issue, would pre-empt CLI design owned by the `cli-sched` track, and would add surface without a current user need ("simplicity first") |
