"""Per-role LLM selection and fallback configuration (#34): shorthand, long form, resolution."""

from typing import Any

import pytest
import yaml

from invio.config.job import (
    JobConfig,
    JobConfigError,
    JobYamlLoader,
    LLMProvider,
    LLMRoleModel,
    LLMTarget,
    RoleSelection,
    dump_yaml,
    load_yaml,
    loads_yaml,
    validate_job,
)
from tests.job_helpers import EXAMPLE, validation_errors

OPENAI, ANTHROPIC, MISTRAL, OLLAMA = (
    LLMProvider.OPENAI,
    LLMProvider.ANTHROPIC,
    LLMProvider.MISTRAL,
    LLMProvider.OLLAMA,
)


def _llm(job_data: dict[str, Any], **llm: Any) -> dict[str, Any]:
    job_data["llm"] = {"provider": "openai", **llm}
    return job_data


def test_the_shorthand_resolves_to_the_job_provider_without_fallback(
    job_data: dict[str, Any],
) -> None:
    llm = JobConfig.model_validate(job_data).llm

    assert llm.models.fast == "gpt-small"
    assert llm.role("fast") == RoleSelection(OPENAI, "gpt-small", None)
    assert llm.role("smart") == RoleSelection(OPENAI, "gpt-large", None)


def test_the_example_sets_a_job_wide_fallback_for_both_roles() -> None:
    llm = load_yaml(EXAMPLE).llm

    assert llm.role("fast") == RoleSelection(
        OPENAI, "gpt-small", LLMTarget(provider=ANTHROPIC, model="claude-haiku-4-5-20251001")
    )
    assert llm.role("smart").fallback == LLMTarget(provider=ANTHROPIC, model="claude-sonnet-4-6")


def test_an_old_config_with_only_fallback_provider_still_validates(
    job_data: dict[str, Any],
) -> None:
    job_data["llm"]["fallback_provider"] = "anthropic"

    llm = JobConfig.model_validate(job_data).llm

    assert llm.fallback_provider is ANTHROPIC
    assert llm.fallback_models is None
    assert llm.role("fast").fallback is None
    assert llm.role("smart").fallback is None


def test_the_long_form_names_a_provider_per_role(job_data: dict[str, Any]) -> None:
    _llm(
        job_data,
        provider="ollama",
        models={"fast": "llama3.2:3b", "smart": {"provider": "anthropic", "model": "sonnet"}},
    )

    llm = JobConfig.model_validate(job_data).llm

    assert isinstance(llm.models.smart, LLMRoleModel)
    assert llm.role("fast") == RoleSelection(OLLAMA, "llama3.2:3b", None)
    assert llm.role("smart") == RoleSelection(ANTHROPIC, "sonnet", None)


def test_the_long_form_without_provider_uses_the_job_provider(job_data: dict[str, Any]) -> None:
    _llm(job_data, models={"fast": {"model": "gpt-small"}, "smart": "gpt-large"})

    assert JobConfig.model_validate(job_data).llm.role("fast") == RoleSelection(
        OPENAI, "gpt-small", None
    )


def test_a_role_fallback_wins_over_the_job_wide_fallback(job_data: dict[str, Any]) -> None:
    own = {"provider": "mistral", "model": "mistral-large"}
    _llm(
        job_data,
        models={"fast": "gpt-small", "smart": {"model": "gpt-large", "fallback": own}},
        fallback_provider="anthropic",
        fallback_models={"fast": "haiku", "smart": "sonnet"},
    )

    llm = JobConfig.model_validate(job_data).llm

    assert llm.role("fast").fallback == LLMTarget(provider=ANTHROPIC, model="haiku")
    assert llm.role("smart").fallback == LLMTarget(provider=MISTRAL, model="mistral-large")


def test_a_role_fallback_works_without_job_wide_fallback(job_data: dict[str, Any]) -> None:
    own = {"provider": "anthropic", "model": "sonnet"}
    _llm(job_data, models={"fast": "gpt-small", "smart": {"model": "gpt-large", "fallback": own}})

    llm = JobConfig.model_validate(job_data).llm

    assert llm.role("fast").fallback is None
    assert llm.role("smart") == RoleSelection(
        OPENAI, "gpt-large", LLMTarget(provider=ANTHROPIC, model="sonnet")
    )


def test_fallback_models_without_fallback_provider_is_rejected(job_data: dict[str, Any]) -> None:
    job_data["llm"]["fallback_models"] = {"fast": "haiku", "smart": "sonnet"}

    errors = validation_errors(job_data)

    assert errors == [(("llm",), "Value error, fallback_models requires fallback_provider")]


def test_a_role_fallback_on_its_own_provider_is_rejected(job_data: dict[str, Any]) -> None:
    own = {"provider": "anthropic", "model": "haiku"}
    _llm(
        job_data,
        models={
            "fast": "gpt-small",
            "smart": {"provider": "anthropic", "model": "s", "fallback": own},
        },
    )

    errors = validation_errors(job_data)

    assert errors == [
        (
            ("llm",),
            "Value error, the fallback provider of role 'smart' must differ from its provider",
        )
    ]


def test_a_job_wide_fallback_on_a_roles_own_provider_is_rejected(
    job_data: dict[str, Any],
) -> None:
    _llm(
        job_data,
        models={"fast": {"provider": "anthropic", "model": "haiku"}, "smart": "gpt-large"},
        fallback_provider="anthropic",
        fallback_models={"fast": "haiku", "smart": "sonnet"},
    )

    errors = validation_errors(job_data)

    assert errors == [
        (
            ("llm",),
            "Value error, the fallback provider of role 'fast' must differ from its provider",
        )
    ]


def test_the_job_wide_rule_still_applies(job_data: dict[str, Any]) -> None:
    job_data["llm"]["fallback_provider"] = "openai"

    errors = validation_errors(job_data)

    assert errors == [(("llm",), "Value error, fallback_provider must differ from provider")]


@pytest.mark.parametrize(
    ("models", "line"),
    [
        ({"fast": "", "smart": "s"}, "llm.models.fast: String should have at least 1 character"),
        ({"fast": "f", "smart": {"model": ""}}, "llm.models.smart.model: String should have"),
        ({"fast": "f", "smart": {"provider": "x", "model": "s"}}, "llm.models.smart.provider:"),
        ({"fast": "f", "smart": {"model": "s", "extra": 1}}, "llm.models.smart.extra: Extra"),
        ({"fast": "f", "smart": {"model": "s", "fallback": {"model": "m"}}}, "fallback.provider:"),
        ({"fast": 3, "smart": "s"}, "llm.models.fast: Input should be a valid string"),
    ],
)
def test_a_bad_role_entry_reports_one_problem_without_form_tags(
    job_data: dict[str, Any], models: dict[str, Any], line: str
) -> None:
    job_data["llm"]["models"] = models

    with pytest.raises(JobConfigError) as info:
        validate_job(job_data)

    (error,) = info.value.errors
    assert line in error
    assert "shorthand" not in error and "long_form" not in error


def test_fallback_models_needs_both_roles(job_data: dict[str, Any]) -> None:
    _llm(
        job_data,
        models={"fast": "f", "smart": "s"},
        fallback_provider="anthropic",
        fallback_models={"fast": "haiku"},
    )

    assert [loc for loc, _ in validation_errors(job_data)] == [("llm", "fallback_models", "smart")]


def test_the_long_form_round_trips_through_yaml() -> None:
    text = EXAMPLE.read_text(encoding="utf-8").replace(
        "    smart: gpt-large\n",
        "    smart:\n      provider: mistral\n      model: mistral-large\n"
        "      fallback: {provider: openai, model: gpt-large}\n",
        1,
    )
    job = loads_yaml(text)

    dumped = yaml.load(dump_yaml(job), Loader=JobYamlLoader)

    assert dumped["llm"]["models"]["smart"] == {
        "provider": "mistral",
        "model": "mistral-large",
        "fallback": {"provider": "openai", "model": "gpt-large"},
    }
    assert loads_yaml(dump_yaml(job)) == job
