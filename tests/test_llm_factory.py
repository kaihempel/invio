"""Tests for provider registration, discovery, role resolution and credentials."""

import pkgutil
import sys
import textwrap
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any, Self

import pytest

import invio.llm
from invio.config.job import JobConfig
from invio.config.settings import Settings
from invio.llm import factory
from invio.llm.base import (
    LLMAuthError,
    LLMConfigError,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
    require_api_key,
)
from invio.llm.factory import get_provider, register_provider, resolve
from invio.llm.fake import FakeDelay, FakeProvider, FakeReply
from invio.llm.registry import default_registry, load_registry
from tests.llm_helpers import (
    FIXTURE_REGISTRY_DIR,
    VALID_SCORE_JSON,
    Score,
    make_settings,
    write_registry,
)

Register = Callable[[str, Any], None]


@pytest.fixture
def job_config(job_data: dict[str, Any], llm_registry_dir: Path) -> JobConfig:
    return JobConfig.model_validate(job_data)


@pytest.fixture
def registry(llm_registry_dir: Path) -> Any:
    return load_registry([llm_registry_dir])


# --- Role resolution (US1) ---------------------------------------------------------------


async def test_roles_resolve_to_their_models(
    job_config: JobConfig, registry: Any, patched_providers: Register
) -> None:
    fake = FakeProvider([FakeReply("hello", Usage(3, 4)), FakeReply(VALID_SCORE_JSON)])
    patched_providers("mistral", fake)

    provider, fast_model = resolve(job_config.llm, "fast", make_settings(), registry=registry)
    same_provider, smart_model = resolve(
        job_config.llm, "smart", make_settings(), registry=registry
    )

    assert fast_model == job_config.llm.models.fast
    assert smart_model == job_config.llm.models.smart
    text, usage = await provider.complete("s", "u", model=fast_model, temperature=0, max_tokens=5)
    assert (text, usage) == ("hello", Usage(3, 4))
    value, usage = await same_provider.complete_structured(
        "s", "u", Score, model=smart_model, temperature=0
    )
    assert value == Score(score=0.8, reason="relevant")
    assert usage == Usage(10, 5)
    assert [r.model for r in fake.requests] == [fast_model, smart_model]


def test_unknown_role_lists_valid_roles(job_config: JobConfig, registry: Any) -> None:
    with pytest.raises(LLMConfigError) as info:
        resolve(job_config.llm, "medium", make_settings(), registry=registry)  # type: ignore[arg-type]

    assert "medium" in str(info.value)
    assert "fast" in str(info.value)
    assert "smart" in str(info.value)


def test_model_missing_from_registry_names_model_and_provider(
    job_config: JobConfig, tmp_path: Path
) -> None:
    empty = load_registry([tmp_path / "none"])

    with pytest.raises(LLMConfigError) as info:
        resolve(job_config.llm, "fast", make_settings(), registry=empty)

    assert job_config.llm.models.fast in str(info.value)
    assert "mistral" in str(info.value)


def test_model_registered_for_another_provider_is_rejected(
    job_data: dict[str, Any], tmp_path: Path
) -> None:
    job_data["llm"]["provider"] = "mistral"
    job_data["llm"]["models"]["fast"] = "other-model"
    config = JobConfig.model_validate(job_data)
    registry = load_registry([FIXTURE_REGISTRY_DIR])

    with pytest.raises(LLMConfigError) as info:
        resolve(config.llm, "fast", make_settings(), registry=registry)

    assert "other-model" in str(info.value)
    assert "mistral" in str(info.value)


def test_registry_error_wins_over_missing_key(
    job_config: JobConfig, tmp_path: Path, patched_providers: Register
) -> None:
    class Keyed:
        @classmethod
        def from_settings(cls, settings: Settings, *, registry: Any = None) -> Self:
            require_api_key(settings, "mistral")
            return cls()

    factory.register_provider("mistral")(Keyed)  # type: ignore[type-var]
    empty = load_registry([tmp_path / "none"])

    with pytest.raises(LLMConfigError, match="not registered"):
        resolve(job_config.llm, "fast", make_settings(), registry=empty)


def test_resolve_uses_default_registry_and_settings_when_omitted(
    job_data: dict[str, Any],
    tmp_path: Path,
    patched_providers: Register,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    models_dir = write_registry(
        tmp_path / "pkg",
        "mistral",
        {
            "m-fast": {"input_price_per_mtok": 1, "output_price_per_mtok": 1, "context_window": 9},
            "m-smart": {"input_price_per_mtok": 1, "output_price_per_mtok": 1, "context_window": 9},
        },
    )
    monkeypatch.setattr(invio.llm, "__path__", [str(models_dir.parent)])
    default_registry.cache_clear()
    try:
        patched_providers("mistral", FakeProvider([]))
        job_data["llm"] = {"provider": "mistral", "models": {"fast": "m-fast", "smart": "m-smart"}}

        _, model = resolve(JobConfig.model_validate(job_data).llm, "smart")
    finally:
        default_registry.cache_clear()

    assert model == "m-smart"


# --- Credentials and typed errors (US3) --------------------------------------------------


class _KeyedProvider(FakeProvider):
    @classmethod
    def from_settings(cls, settings: Settings, *, registry: Any = None) -> Self:
        require_api_key(settings, "mistral")
        return super().from_settings(settings, registry=registry)


def test_missing_key_surfaces_at_get_provider(patched_providers: Register) -> None:
    patched_providers("unused", FakeProvider([]))
    factory.register_provider("mistral")(_KeyedProvider)

    with pytest.raises(LLMAuthError, match="INVIO_MISTRAL_API_KEY"):
        get_provider("mistral", make_settings())


def test_present_key_returns_provider(patched_providers: Register) -> None:
    patched_providers("unused", FakeProvider([]))
    factory.register_provider("mistral")(_KeyedProvider)

    assert get_provider("mistral", make_settings(mistral_api_key="sk-test-SECRET123")) is not None


def test_keyless_provider_needs_no_key(patched_providers: Register) -> None:
    patched_providers("ollama", FakeProvider([]))

    assert get_provider("ollama", make_settings()) is not None


def test_settings_without_keys_are_valid() -> None:
    assert make_settings().mistral_api_key is None


@pytest.mark.parametrize(
    "error",
    [LLMRateLimitError("rl"), LLMAuthError("auth"), LLMUnavailableError("down")],
    ids=lambda e: type(e).__name__,
)
async def test_provider_errors_reach_the_caller(
    patched_providers: Register, error: Exception
) -> None:
    patched_providers("fakeco", FakeProvider([error]))  # type: ignore[list-item]
    provider = get_provider("fakeco", make_settings())

    with pytest.raises(type(error)):
        await provider.complete("s", "u", model="m", temperature=0, max_tokens=1)


async def test_timeout_surfaces_as_unavailable(patched_providers: Register) -> None:
    patched_providers(
        "fakeco", FakeProvider([FakeDelay(1, FakeReply("late"))], timeout_seconds=0.01)
    )
    provider = get_provider("fakeco", make_settings())

    with pytest.raises(LLMUnavailableError):
        await provider.complete("s", "u", model="m", temperature=0, max_tokens=1)


# --- Registration and discovery (US5) ----------------------------------------------------


def test_duplicate_registration_names_the_provider(patched_providers: Register) -> None:
    patched_providers("dup", FakeProvider([]))

    with pytest.raises(LLMConfigError, match="'dup' is registered twice"):
        patched_providers("dup", FakeProvider([]))


def test_unknown_provider_lists_registered_names_sorted(patched_providers: Register) -> None:
    patched_providers("zeta", FakeProvider([]))
    patched_providers("alpha", FakeProvider([]))

    with pytest.raises(LLMConfigError) as info:
        get_provider("nope", make_settings())

    assert "nope" in str(info.value)
    assert "alpha, zeta" in str(info.value)


def test_unknown_provider_with_nothing_registered(patched_providers: Register) -> None:
    with pytest.raises(LLMConfigError, match="registered: none"):
        get_provider("nope", make_settings())


def test_real_discovery_imports_package_modules() -> None:
    expected = {
        f"invio.llm.{module.name}"
        for module in pkgutil.iter_modules(invio.llm.__path__)
        if not module.name.startswith("_")
    }
    imported: list[str] = []
    real_import = factory.importlib.import_module

    def spy(name: str, package: str | None = None) -> Any:
        imported.append(name)
        return real_import(name, package)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(factory, "_REGISTRY", {})
        mp.setattr(factory, "_discovered", False)
        mp.setattr(factory.importlib, "import_module", spy)

        with pytest.raises(LLMConfigError, match="unknown LLM provider 'nope'"):
            get_provider("nope", make_settings())

        assert factory._discovered is True
    assert set(imported) == expected
    assert "invio.llm.mistral" in expected


@pytest.fixture
def provider_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A temporary directory prepended to ``invio.llm.__path__`` with fresh discovery state."""
    package = tmp_path / "pkg"
    package.mkdir()
    monkeypatch.setattr(invio.llm, "__path__", [str(package), *invio.llm.__path__])
    monkeypatch.setattr(factory, "_REGISTRY", {})
    monkeypatch.setattr(factory, "_discovered", False)
    default_registry.cache_clear()
    yield package
    # Forget every module imported from the temporary package, whatever its name.
    for qualified, module in list(sys.modules.items()):
        origin = getattr(module, "__file__", None) or ""
        if qualified.startswith("invio.llm.") and origin.startswith(str(package)):
            sys.modules.pop(qualified)
            short = qualified.removeprefix("invio.llm.")
            if hasattr(invio.llm, short):
                delattr(invio.llm, short)
    default_registry.cache_clear()


ACME_MODULE = textwrap.dedent(
    """
    from typing import Self

    from invio.llm.base import Usage
    from invio.llm.factory import register_provider


    @register_provider("acme")
    class AcmeProvider:
        @classmethod
        def from_settings(cls, settings: object, *, registry: object = None) -> Self:
            return cls()

        async def complete(self, system, user, *, model, temperature, max_tokens):
            return "acme says hi", Usage(1, 1)

        async def complete_structured(self, system, user, schema, *, model, temperature):
            raise NotImplementedError
    """
)


async def test_new_provider_module_is_discovered(provider_package: Path) -> None:
    (provider_package / "acme.py").write_text(ACME_MODULE, encoding="utf-8")
    write_registry(
        provider_package,
        "acme",
        {"acme-1": {"input_price_per_mtok": 1, "output_price_per_mtok": 2, "context_window": 5}},
    )

    provider = get_provider("acme", make_settings())

    text, _ = await provider.complete("s", "u", model="acme-1", temperature=0, max_tokens=1)
    assert text == "acme says hi"
    assert default_registry().cost("acme-1", Usage(1000, 1000)) == Decimal("0.003000")


def test_private_modules_are_not_imported(provider_package: Path) -> None:
    (provider_package / "_hidden.py").write_text(
        ACME_MODULE.replace('"acme"', '"hidden"'), encoding="utf-8"
    )

    with pytest.raises(LLMConfigError, match="'hidden'"):
        get_provider("hidden", make_settings())

    assert "invio.llm._hidden" not in sys.modules


def test_discovery_runs_only_once(provider_package: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (provider_package / "acme.py").write_text(ACME_MODULE, encoding="utf-8")
    get_provider("acme", make_settings())

    def boom(path: object) -> None:
        raise AssertionError("discovery ran twice")

    monkeypatch.setattr(pkgutil, "iter_modules", boom)

    assert get_provider("acme", make_settings()) is not None


def test_register_provider_returns_the_class(patched_providers: Register) -> None:
    patched_providers("x", FakeProvider([]))

    assert register_provider("y")(FakeProvider) is FakeProvider


def test_provider_import_failure_is_a_config_error(provider_package: Path) -> None:
    (provider_package / "acme.py").write_text("raise RuntimeError('SECRET detail')\n")

    with pytest.raises(LLMConfigError) as info:
        get_provider("acme", make_settings())

    assert str(info.value) == "cannot import LLM provider module 'acme': RuntimeError"
    assert isinstance(info.value.__cause__, RuntimeError)
    assert factory._discovered is False


OLLAMA_MODULE = ACME_MODULE.replace('"acme"', '"ollama"').replace("acme says hi", "local says hi")


async def test_new_provider_module_resolves_by_role_without_shared_edits(
    provider_package: Path, job_data: dict[str, Any]
) -> None:
    """SC-001: one module + one registry file make a job's roles resolvable and priced."""
    (provider_package / "ollama.py").write_text(OLLAMA_MODULE, encoding="utf-8")
    entry: dict[str, object] = {
        "input_price_per_mtok": 0,
        "output_price_per_mtok": 0,
        "context_window": 4096,
    }
    write_registry(provider_package, "ollama", {"local-fast": entry, "local-smart": entry})
    job_data["llm"] = {
        "provider": "ollama",
        "models": {"fast": "local-fast", "smart": "local-smart"},
    }
    llm = JobConfig.model_validate(job_data).llm

    provider, fast_model = resolve(llm, "fast", make_settings())
    _, smart_model = resolve(llm, "smart", make_settings())

    assert (fast_model, smart_model) == ("local-fast", "local-smart")
    text, usage = await provider.complete("s", "u", model=fast_model, temperature=0, max_tokens=1)
    assert text == "local says hi"
    assert default_registry().cost(fast_model, usage) == Decimal("0.000000")


def test_duplicate_name_in_discovered_modules_fails_discovery(provider_package: Path) -> None:
    (provider_package / "acme.py").write_text(ACME_MODULE, encoding="utf-8")
    (provider_package / "acme_copy.py").write_text(ACME_MODULE, encoding="utf-8")

    with pytest.raises(LLMConfigError) as info:
        get_provider("acme", make_settings())

    assert "'acme' is registered twice" in str(info.value)
    assert "invio.llm.acme_copy" in str(info.value)


def test_duplicate_name_in_discovered_modules_names_the_provider(provider_package: Path) -> None:
    (provider_package / "acme.py").write_text(ACME_MODULE, encoding="utf-8")
    (provider_package / "acme_copy.py").write_text(ACME_MODULE, encoding="utf-8")

    with pytest.raises(LLMConfigError, match="'acme'"):
        get_provider("acme", make_settings())


def test_schema_provider_without_module_is_unknown(
    job_data: dict[str, Any], tmp_path: Path, patched_providers: Register
) -> None:
    """Edge case: ``openai`` is valid in job files but has no provider module yet."""
    patched_providers("mistral", FakeProvider([]))
    entry: dict[str, object] = {
        "input_price_per_mtok": 1,
        "output_price_per_mtok": 1,
        "context_window": 9,
    }
    models = {model: entry for model in job_data["llm"]["models"].values()}
    registry = load_registry([write_registry(tmp_path, "openai", models)])

    with pytest.raises(LLMConfigError) as info:
        resolve(JobConfig.model_validate(job_data).llm, "fast", make_settings(), registry=registry)

    assert "unknown LLM provider 'openai'" in str(info.value)
    assert "registered: mistral" in str(info.value)


def test_missing_key_surfaces_at_resolve_naming_provider_and_setting(
    job_config: JobConfig, registry: Any, patched_providers: Register
) -> None:
    factory.register_provider("mistral")(_KeyedProvider)

    with pytest.raises(LLMAuthError) as info:
        resolve(job_config.llm, "smart", make_settings(), registry=registry)

    assert "'mistral'" in str(info.value)
    assert "INVIO_MISTRAL_API_KEY" in str(info.value)


@pytest.mark.parametrize(
    "error",
    [LLMRateLimitError("rl"), LLMAuthError("auth"), LLMUnavailableError("down")],
    ids=lambda e: type(e).__name__,
)
async def test_provider_errors_reach_the_caller_of_structured_completion(
    patched_providers: Register, error: Exception
) -> None:
    patched_providers("fakeco", FakeProvider([error]))  # type: ignore[list-item]
    provider = get_provider("fakeco", make_settings())

    with pytest.raises(type(error)) as info:
        await provider.complete_structured("s", "u", Score, model="m", temperature=0)

    assert info.value is error


# --- OpenAI provider (issue #30, US4) ----------------------------------------------------


def test_openai_resolves_from_its_module_and_registry_file_only(job_data: dict[str, Any]) -> None:
    """SC-001 for OpenAI: ``openai.py`` + ``models.d/openai.yaml``, no edit of ``factory.py``."""
    from invio.llm.openai import OpenAIProvider

    package = Path(invio.llm.__file__).parent
    assert (package / "openai.py").is_file()
    assert (package / "models.d" / "openai.yaml").is_file()
    assert "openai" not in Path(factory.__file__).read_text(encoding="utf-8").lower()
    default_registry.cache_clear()
    first, second = (info.model_id for info in default_registry().models_for("openai"))
    job_data["llm"] = {"provider": "openai", "models": {"fast": first, "smart": second}}
    llm = JobConfig.model_validate(job_data).llm
    settings = make_settings(openai_api_key="sk-test-SECRET123")

    provider = get_provider("openai", settings)
    resolved, model = resolve(llm, "smart", settings)

    assert factory._REGISTRY["openai"] is OpenAIProvider
    assert provider is not None
    assert resolved is not None
    assert model == second


def test_openai_without_key_names_the_setting() -> None:
    with pytest.raises(LLMAuthError) as info:
        get_provider("openai", make_settings())

    assert "'openai'" in str(info.value)
    assert "INVIO_OPENAI_API_KEY" in str(info.value)


# --- Anthropic provider (issue #31, US4) -------------------------------------------------


def test_anthropic_resolves_from_its_module_and_registry_file_only(
    job_data: dict[str, Any],
) -> None:
    """SC-001 for Anthropic: ``anthropic.py`` + ``models.d/anthropic.yaml``, no ``factory.py``."""
    from invio.llm.anthropic import AnthropicProvider

    package = Path(invio.llm.__file__).parent
    assert (package / "anthropic.py").is_file()
    assert (package / "models.d" / "anthropic.yaml").is_file()
    assert "anthropic" not in Path(factory.__file__).read_text(encoding="utf-8").lower()
    default_registry.cache_clear()
    first, second = (info.model_id for info in default_registry().models_for("anthropic"))
    job_data["llm"] = {"provider": "anthropic", "models": {"fast": first, "smart": second}}
    llm = JobConfig.model_validate(job_data).llm
    settings = make_settings(anthropic_api_key="sk-ant-test-SECRET123")

    provider = get_provider("anthropic", settings)
    resolved, model = resolve(llm, "smart", settings)

    assert factory._REGISTRY["anthropic"] is AnthropicProvider
    assert provider is not None
    assert resolved is not None
    assert model == second


def test_anthropic_without_key_names_the_setting() -> None:
    with pytest.raises(LLMAuthError) as info:
        get_provider("anthropic", make_settings())

    assert "'anthropic'" in str(info.value)
    assert "INVIO_ANTHROPIC_API_KEY" in str(info.value)


# --- Google provider (issue #32) -----------------------------------------------------------


def test_google_resolves_from_its_module_and_registry_file_only(
    job_data: dict[str, Any],
) -> None:
    """SC-001 for Google: ``google.py`` + ``models.d/google.yaml``, no ``factory.py``."""
    from invio.llm.google import GoogleProvider

    package = Path(invio.llm.__file__).parent
    assert (package / "google.py").is_file()
    assert (package / "models.d" / "google.yaml").is_file()
    assert "google" not in Path(factory.__file__).read_text(encoding="utf-8").lower()
    default_registry.cache_clear()
    first, second = (info.model_id for info in default_registry().models_for("google"))
    job_data["llm"] = {"provider": "google", "models": {"fast": first, "smart": second}}
    llm = JobConfig.model_validate(job_data).llm
    settings = make_settings(google_api_key="AIza-test-SECRET123")

    provider = get_provider("google", settings)
    resolved, model = resolve(llm, "smart", settings)

    assert factory._REGISTRY["google"] is GoogleProvider
    assert provider is not None
    assert resolved is not None
    assert model == second
