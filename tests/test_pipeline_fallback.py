"""``run_job`` with per-role providers and a provider fallback (#34), wired by ``default_deps``.

The real ``provider_for`` binds the roles; ``get_provider`` is replaced so every provider name
gets its own :class:`RoutedFakeProvider`.
"""

import dataclasses
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, select

from invio.config.job import ScheduleConfig
from invio.config.settings import Settings
from invio.db.models import LlmUsage, Run
from invio.db.session import session_factory, session_scope
from invio.domain import RunStatus
from invio.graph.ports import RunDeps
from invio.llm.base import LLMAuthError, LLMUnavailableError
from invio.llm.registry import ModelRegistry, load_registry
from invio.pipeline import deps as deps_module
from invio.pipeline.deps import default_deps
from invio.pipeline.run import RunResult, run_job
from tests.conftest import FakeClock
from tests.llm_helpers import write_registry
from tests.pipeline_helpers import (
    DEFAULT_USAGE,
    Env,
    RoutedFakeProvider,
    build_env,
    make_job_config,
)

pytestmark = pytest.mark.usefixtures("clean_jobs")

NextRun = Callable[[ScheduleConfig, datetime], datetime]
CHEAP: dict[str, object] = {
    "input_price_per_mtok": 1,
    "output_price_per_mtok": 2,
    "context_window": 128000,
}
PRICEY: dict[str, object] = {
    "input_price_per_mtok": 10,
    "output_price_per_mtok": 20,
    "context_window": 128000,
}
PURPOSES = {"relevance", "summarize", "synthesize"}


@pytest.fixture
def registry(tmp_path: Path) -> ModelRegistry:
    write_registry(tmp_path, "mistral", {"m-fast": CHEAP, "m-smart": CHEAP})
    return load_registry(
        [write_registry(tmp_path, "openai", {"o-fast": PRICEY, "o-smart": PRICEY})]
    )


@dataclasses.dataclass
class Providers:
    """The fake behind each provider name and the names ``get_provider`` was asked for."""

    by_name: dict[str, RoutedFakeProvider]
    built: list[str] = dataclasses.field(default_factory=list)


@pytest.fixture
def providers(monkeypatch: pytest.MonkeyPatch) -> Providers:
    fakes = Providers({"mistral": RoutedFakeProvider(), "openai": RoutedFakeProvider()})

    def fake_get_provider(name: str, settings: Settings, **kw: Any) -> RoutedFakeProvider:
        fakes.built.append(name)
        return fakes.by_name[name]

    monkeypatch.setattr(deps_module, "get_provider", fake_get_provider)
    return fakes


@asynccontextmanager
async def _real_binding(env: Env, registry: ModelRegistry) -> AsyncIterator[RunDeps]:
    """``env.deps`` with the ``provider_for`` of ``default_deps``."""
    settings = Settings(_env_file=None, database_url="sqlite+pysqlite://")
    async with default_deps(settings, registry=registry) as production:
        yield dataclasses.replace(env.deps, provider_for=production.provider_for)


async def _run(env: Env, registry: ModelRegistry) -> RunResult:
    async with _real_binding(env, registry) as deps:
        return await run_job(env.job_id, deps=deps)


def _usage(engine: Engine) -> list[tuple[str | None, str, str, Decimal | None]]:
    with session_scope(session_factory(engine)) as session:
        rows = session.scalars(select(LlmUsage).order_by(LlmUsage.id))
        return [(row.purpose, row.provider, row.model, row.cost_usd) for row in rows]


def _env(
    engine: Engine, clock: FakeClock, next_run: NextRun, providers: Providers, llm: dict[str, Any]
) -> Env:
    return build_env(
        engine,
        clock,
        next_run,
        config=make_job_config(llm=llm),
        provider=providers.by_name["mistral"],
    )


async def test_fast_and_smart_on_different_providers_write_their_own_usage_rows(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    providers: Providers,
    registry: ModelRegistry,
) -> None:
    llm = {
        "provider": "mistral",
        "models": {"fast": "m-fast", "smart": {"provider": "openai", "model": "o-smart"}},
    }
    env = _env(db_engine, fake_clock, recording_next_run, providers, llm)

    result = await _run(env, registry)

    assert result.status == RunStatus.SUCCEEDED
    mistral, openai = providers.by_name["mistral"], providers.by_name["openai"]
    assert {call.purpose for call in mistral.calls} == {"relevance", "summarize"}
    assert {call.purpose for call in openai.calls} == {"synthesize"}
    rows = _usage(db_engine)
    assert {purpose for purpose, *_ in rows} == PURPOSES
    for purpose, provider, model, cost in rows:
        if purpose == "synthesize":
            assert (provider, model) == ("openai", "o-smart")
            assert cost == registry.cost("o-smart", DEFAULT_USAGE)
        else:
            assert (provider, model) == ("mistral", "m-fast")
            assert cost == registry.cost("m-fast", DEFAULT_USAGE)
    assert sorted(providers.built) == ["mistral", "openai"]
    assert mistral.closed and openai.closed


async def test_a_primary_outage_falls_back_and_records_the_fallback(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    providers: Providers,
    registry: ModelRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    down = LLMUnavailableError("down", provider="mistral")
    mistral = providers.by_name["mistral"]
    for route in PURPOSES:
        mistral.route(route, down)
    llm = {
        "provider": "mistral",
        "models": {"fast": "m-fast", "smart": "m-smart"},
        "fallback_provider": "openai",
        "fallback_models": {"fast": "o-fast", "smart": "o-smart"},
    }
    env = _env(db_engine, fake_clock, recording_next_run, providers, llm)

    with caplog.at_level(logging.WARNING, logger="invio.llm"):
        result = await _run(env, registry)

    assert result.status == RunStatus.SUCCEEDED
    rows = _usage(db_engine)
    assert {purpose for purpose, *_ in rows} == PURPOSES
    for purpose, provider, model, cost in rows:
        expected = "o-smart" if purpose == "synthesize" else "o-fast"
        assert (provider, model) == ("openai", expected)
        assert cost == registry.cost(expected, DEFAULT_USAGE)
        assert cost != registry.cost("m-fast", DEFAULT_USAGE)  # priced with the fallback model
    # The primary was retried (3 attempts) before every fallback.
    assert len(mistral.calls) == 3 * len(providers.by_name["openai"].calls)
    fallbacks = [r for r in caplog.records if r.getMessage() == "llm.fallback"]
    assert len(fallbacks) == len(rows)
    assert {(r.__dict__["provider"], r.__dict__["fallback_provider"]) for r in fallbacks} == {
        ("mistral", "openai")
    }
    assert {r.__dict__["error"] for r in fallbacks} == {"LLMUnavailableError"}


async def test_an_auth_error_of_the_primary_fails_the_run_without_the_fallback(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    providers: Providers,
    registry: ModelRegistry,
) -> None:
    providers.by_name["mistral"].route("relevance", LLMAuthError("bad key", provider="mistral"))
    llm = {
        "provider": "mistral",
        "models": {"fast": "m-fast", "smart": "m-smart"},
        "fallback_provider": "openai",
        "fallback_models": {"fast": "o-fast", "smart": "o-smart"},
    }
    env = _env(db_engine, fake_clock, recording_next_run, providers, llm)

    result = await _run(env, registry)

    assert result.status == RunStatus.FAILED
    assert {e.error_class for e in result.errors} == {"LLMAuthError"}
    with session_scope(session_factory(db_engine)) as session:
        (run,) = session.scalars(select(Run))
        assert (run.error or "").startswith("LLMAuthError:")
    assert providers.by_name["openai"].calls == []
    assert _usage(db_engine) == []


async def test_an_old_config_with_only_fallback_provider_warns_and_runs(
    db_engine: Engine,
    fake_clock: FakeClock,
    recording_next_run: NextRun,
    providers: Providers,
    registry: ModelRegistry,
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm = {
        "provider": "mistral",
        "models": {"fast": "m-fast", "smart": "m-smart"},
        "fallback_provider": "openai",
    }
    env = _env(db_engine, fake_clock, recording_next_run, providers, llm)

    with caplog.at_level(logging.WARNING):
        result = await _run(env, registry)

    assert result.status == RunStatus.SUCCEEDED
    warnings = [r for r in caplog.records if r.getMessage() == "llm.fallback_unconfigured"]
    assert len(warnings) == 1
    assert {provider for _, provider, _, _ in _usage(db_engine)} == {"mistral"}
    assert providers.built == ["mistral"]
    assert providers.by_name["openai"].calls == []
