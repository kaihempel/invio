"""Tests keeping the job provider list aligned with Settings."""

from typing import Any

from invio.config.job import JobConfig, LLMProvider
from invio.config.settings import Settings


def test_providers_match_settings() -> None:
    fields = Settings.model_fields
    from_settings = {n.removesuffix("_api_key") for n in fields if n.endswith("_api_key")} | {
        n.removesuffix("_base_url") for n in fields if n.endswith("_base_url")
    }

    assert {p.value for p in LLMProvider} == from_settings


def test_provider_without_api_key_is_accepted(job_data: dict[str, Any]) -> None:
    job_data["llm"]["provider"] = "anthropic"
    job_data["llm"]["fallback_provider"] = "google"

    job = JobConfig.model_validate(job_data)

    assert job.llm.provider is LLMProvider.ANTHROPIC
