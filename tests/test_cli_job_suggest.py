"""CliRunner tests for ``invio job suggest`` (fake provider, scripted prompter, no network)."""

import re
from decimal import Decimal
from typing import Any

import pytest
import yaml

from invio.cli.commands import job as job_module
from invio.config.job import JobConfig, validate_job
from invio.config.settings import Settings
from invio.llm.base import LLMAuthError, LLMUnavailableError, Usage
from invio.llm.fake import FakeProvider, FakeReply, FakeStep
from invio.llm.registry import ModelInfo, ModelRegistry
from invio.services.suggest import refine, search_yaml, suggest
from tests.cli_helpers import CLEAR, FakePrompter, JobCli, job_cli  # noqa: F401 (fixture)
from tests.llm_helpers import make_settings
from tests.suggest_helpers import answer_json, make_suggestion
from tests.test_wizard import script

pytestmark = pytest.mark.db

KEY = "sk-secret-value-123"
BIG = "claude-big"


def _suggest_registry() -> ModelRegistry:
    def info(model_id: str, provider: str, price: int) -> ModelInfo:
        return ModelInfo(model_id, provider, Decimal(price), Decimal(price * 2), 1000)

    return ModelRegistry(
        {
            "claude-small": info("claude-small", "anthropic", 1),
            BIG: info(BIG, "anthropic", 10),
            "gpt-small": info("gpt-small", "openai", 1),
            "gpt-large": info("gpt-large", "openai", 5),
        }
    )


class SuggestCli:
    """The ``job_cli`` fixture plus a fake provider seam, settings and registry."""

    def __init__(self, cli: JobCli) -> None:
        self.cli = cli
        self.provider = FakeProvider([])
        self.provider_calls: list[tuple[str, ModelRegistry]] = []
        self.settings_value: Settings = make_settings(anthropic_api_key=KEY)
        cli.monkeypatch.setattr(job_module, "_make_registry", _suggest_registry)
        cli.monkeypatch.setattr(job_module, "get_settings", lambda: self.settings_value)
        cli.monkeypatch.setattr(job_module, "_make_suggest_provider", self._make_provider)

    def _make_provider(self, name: str, registry: ModelRegistry) -> FakeProvider:
        self.provider_calls.append((name, registry))
        return self.provider

    def script(self, *steps: FakeStep) -> FakeProvider:
        self.provider = FakeProvider(list(steps), name="anthropic")
        return self.provider

    def set_settings(self, **keys: str) -> None:
        self.settings_value = make_settings(**keys)

    def invoke(self, *args: str) -> Any:
        return self.cli.invoke(["suggest", *args])

    def job_names(self) -> list[str]:
        return list(self.cli.service.names())


@pytest.fixture
def suggest_cli(job_cli: JobCli) -> SuggestCli:
    return SuggestCli(job_cli)


def _reply(**overrides: Any) -> FakeReply:
    return FakeReply(answer_json(**overrides))


# --- non-interactive / --yaml ----------------------------------------------------------------


def test_yaml_prints_the_search_block_and_asks_nothing(suggest_cli: SuggestCli) -> None:
    provider = suggest_cli.script(_reply())
    suggest_cli.cli.forbid_prompts()

    result = suggest_cli.invoke("heat pumps", "--yaml")

    assert result.exit_code == 0, result.output
    assert result.stdout == search_yaml(make_suggestion())
    assert len(provider.requests) == 1
    assert provider.requests[0].model == BIG  # the provider's most expensive model
    assert "<topic>heat pumps</topic>" in provider.requests[0].user
    assert suggest_cli.job_names() == []
    assert suggest_cli.provider_calls[0][0] == "anthropic"


def test_non_interactive_without_yaml_prints_the_same_yaml(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply())
    suggest_cli.cli.set_interactive(False)
    suggest_cli.cli.forbid_prompts()

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert result.stdout == search_yaml(make_suggestion())


def test_language_option_names_the_language_in_the_request(suggest_cli: SuggestCli) -> None:
    provider = suggest_cli.script(_reply())

    result = suggest_cli.invoke("Wärmepumpen", "--yaml", "--language", "DE")

    assert result.exit_code == 0, result.output
    assert "German" in provider.requests[0].system


def test_provider_and_model_options_are_used(suggest_cli: SuggestCli) -> None:
    suggest_cli.set_settings(anthropic_api_key=KEY, openai_api_key=KEY)
    provider = suggest_cli.script(_reply())

    result = suggest_cli.invoke("t", "--yaml", "--provider", "openai", "--model", "gpt-small")

    assert result.exit_code == 0, result.output
    assert provider.requests[0].model == "gpt-small"
    assert suggest_cli.provider_calls[0][0] == "openai"


def test_stderr_announces_the_model_and_summarises_usage(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(FakeReply(answer_json(), Usage(1000, 500)))

    result = suggest_cli.invoke("t", "--yaml")

    assert "Asking anthropic/claude-big" in result.stderr
    # 1000 * 10 + 500 * 20 = 20000 / 1e6 = 0.02
    assert "suggestions used 1 request, 1000 in / 500 out tokens, ~$0.020000" in result.stderr


def test_stderr_never_leaks_the_key_or_the_system_prompt(suggest_cli: SuggestCli) -> None:
    provider = suggest_cli.script(_reply())

    result = suggest_cli.invoke("t", "--yaml")

    assert KEY not in result.stderr and KEY not in result.stdout
    first_line = provider.requests[0].system.splitlines()[0]
    assert first_line not in result.stderr and first_line not in result.stdout


# --- usage and configuration errors (exit 2, no model call) ----------------------------------


@pytest.mark.parametrize(
    "args",
    [
        [""],
        ["   "],
        ["x" * 501],
        ["topic", "--language", "xx"],
        ["topic", "--language", "EN1"],
    ],
    ids=["empty", "blank", "too-long", "unknown-language", "bad-language"],
)
def test_usage_errors_exit_2_without_a_request(suggest_cli: SuggestCli, args: list[str]) -> None:
    provider = suggest_cli.script(_reply())

    result = suggest_cli.invoke(*args, "--yaml")

    assert result.exit_code == 2
    assert provider.requests == []
    assert suggest_cli.provider_calls == []


def test_a_topic_of_exactly_500_characters_is_accepted(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply())

    assert suggest_cli.invoke("x" * 500, "--yaml").exit_code == 0


@pytest.mark.parametrize(
    ("keys", "args", "message"),
    [
        ({"anthropic_api_key": KEY}, ["--provider", "nope"], "nope"),
        ({"anthropic_api_key": KEY}, ["--provider", "openai"], "INVIO_OPENAI_API_KEY"),
        ({}, [], "INVIO_ANTHROPIC_API_KEY"),
        ({"anthropic_api_key": KEY}, ["--model", "unknown"], "unknown"),
        ({"anthropic_api_key": KEY}, ["--provider", "anthropic", "--model", "gpt-small"], "gpt"),
        ({"mistral_api_key": KEY}, ["--provider", "mistral"], "mistral"),
    ],
    ids=[
        "unknown-provider",
        "no-key",
        "no-key-at-all",
        "unknown-model",
        "wrong-model",
        "no-models",
    ],
)
def test_configuration_errors_exit_2_without_a_request(
    suggest_cli: SuggestCli, keys: dict[str, str], args: list[str], message: str
) -> None:
    provider = suggest_cli.script(_reply())
    suggest_cli.set_settings(**keys)

    result = suggest_cli.invoke("topic", "--yaml", *args)

    assert result.exit_code == 2, result.output
    assert any(
        line.startswith("Configuration error:") and message in line
        for line in result.stderr.splitlines()
    ), result.stderr
    assert provider.requests == []
    assert suggest_cli.provider_calls == []


def test_an_unreadable_registry_exits_2(suggest_cli: SuggestCli) -> None:
    provider = suggest_cli.script(_reply())
    suggest_cli.cli.monkeypatch.setattr(job_module, "_make_registry", lambda: None)

    result = suggest_cli.invoke("topic", "--yaml")

    assert result.exit_code == 2
    assert "Configuration error: model registry unavailable" in result.stderr
    assert provider.requests == []


def test_a_provider_that_cannot_be_built_exits_2(suggest_cli: SuggestCli) -> None:
    def broken(name: str, registry: ModelRegistry) -> FakeProvider:
        raise LLMAuthError("needs an API key")

    suggest_cli.cli.monkeypatch.setattr(job_module, "_make_suggest_provider", broken)

    result = suggest_cli.invoke("topic", "--yaml")

    assert result.exit_code == 2
    assert "Configuration error: needs an API key" in result.stderr


# --- model failures (exit 1) -----------------------------------------------------------------


def _error_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("Error:")]


def test_a_provider_failure_exits_1_with_one_error_line(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(LLMUnavailableError("provider down"))

    result = suggest_cli.invoke("t", "--yaml")

    assert result.exit_code == 1
    assert _error_lines(result.stderr) == [
        "Error: anthropic/claude-big: LLMUnavailableError: provider down"
    ]
    assert result.stdout == ""
    assert suggest_cli.job_names() == []


def test_an_invalid_answer_after_repair_exits_1_and_counts_both_requests(
    suggest_cli: SuggestCli,
) -> None:
    suggest_cli.script(FakeReply("nope"), FakeReply("still nope"))

    result = suggest_cli.invoke("t", "--yaml")

    assert result.exit_code == 1
    assert any(
        line.startswith("Error: anthropic/claude-big: LLMInvalidOutputError:")
        for line in result.stderr.splitlines()
    )
    assert "suggestions used 2 requests, 20 in / 10 out tokens" in result.stderr


def test_a_repaired_answer_is_a_success_with_two_requests(suggest_cli: SuggestCli) -> None:
    provider = suggest_cli.script(FakeReply("nope"), _reply())

    result = suggest_cli.invoke("t", "--yaml")

    assert result.exit_code == 0, result.output
    assert len(provider.requests) == 2
    assert "suggestions used 2 requests" in result.stderr


def test_a_post_cleaning_failure_is_repaired_once(suggest_cli: SuggestCli) -> None:
    provider = suggest_cli.script(_reply(semantic_description="\x1b[31m"), _reply())

    result = suggest_cli.invoke("t", "--yaml")

    assert result.exit_code == 0, result.output
    assert len(provider.requests) == 2
    assert result.stdout == search_yaml(make_suggestion())


def test_two_post_cleaning_failures_exit_1_and_write_nothing(suggest_cli: SuggestCli) -> None:
    provider = suggest_cli.script(
        _reply(semantic_description="\x1b[31m"), _reply(semantic_description="\x1b[0m")
    )

    result = suggest_cli.invoke("t", "--yaml")

    assert result.exit_code == 1
    assert len(provider.requests) == 2
    assert any(
        line.startswith("Error: anthropic/claude-big: LLMInvalidOutputError:")
        for line in result.stderr.splitlines()
    )
    assert result.stdout == ""
    assert suggest_cli.job_names() == []


# --- interactive: discard / print ------------------------------------------------------------


def test_discard_saves_nothing_and_exits_0(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply())
    prompter = FakePrompter(["Discard"])
    suggest_cli.cli.use(prompter)

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert "Nothing saved." in result.stderr
    assert "Suggestion (anthropic / claude-big, language: en)" in result.stdout
    assert suggest_cli.job_names() == []


def test_print_yaml_prints_the_block_after_the_preview(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply())
    suggest_cli.cli.use(FakePrompter(["Print YAML"]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert result.stdout.endswith(search_yaml(make_suggestion()))
    assert suggest_cli.job_names() == []


def test_running_out_of_answers_in_the_preview_aborts(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply())
    suggest_cli.cli.use(FakePrompter([]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 1
    assert "aborted; nothing saved" in result.stderr
    assert suggest_cli.job_names() == []


# --- production seam -------------------------------------------------------------------------


def test_the_production_seam_builds_the_logging_provider(
    job_cli: JobCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = make_settings(anthropic_api_key=KEY)
    registry = _suggest_registry()
    sentinel = FakeProvider([])
    calls: list[tuple[str, Settings, ModelRegistry | None]] = []

    def recorder(
        name: str, settings: Settings | None = None, *, registry: ModelRegistry | None = None
    ) -> FakeProvider:
        assert settings is not None
        calls.append((name, settings, registry))
        return sentinel

    monkeypatch.setattr(job_module, "get_settings", lambda: settings)
    monkeypatch.setattr(job_module, "get_provider", recorder)

    built = job_module._make_suggest_provider("anthropic", registry)

    assert built is sentinel
    assert calls == [("anthropic", settings, registry)]


class _ClosingProvider(FakeProvider):
    """A fake that counts ``aclose`` calls."""

    closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def test_two_calls_on_one_provider_work_and_each_closes_it() -> None:
    provider = _ClosingProvider([_reply(), _reply(keywords_any=["z"])])

    first, _ = job_module._call(provider, lambda: suggest(provider, "m", topic="t", language="en"))
    second, _ = job_module._call(
        provider,
        lambda: refine(provider, "m", topic="t", language="en", previous=first, remark="z"),
    )

    assert second.keywords_any == ("z",)
    assert provider.closed == 2


def test_a_failing_call_still_closes_the_provider() -> None:
    provider = _ClosingProvider([LLMUnavailableError("down")])

    with pytest.raises(LLMUnavailableError):
        job_module._call(provider, lambda: suggest(provider, "m", topic="t", language="en"))

    assert provider.closed == 1


# --- refine (AC2) ----------------------------------------------------------------------------


def test_refine_sends_the_first_suggestion_and_the_remark_and_prints_the_second(
    suggest_cli: SuggestCli,
) -> None:
    provider = suggest_cli.script(_reply(), _reply(keywords_any=["narrow"]))
    suggest_cli.cli.use(FakePrompter(["Refine", "make it narrower", "Print YAML"]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert len(provider.requests) == 2
    second = provider.requests[1].user
    assert "<topic>heat pumps</topic>" in second
    for keyword in ("Wärmepumpe", "heat pump", "Stellenangebot"):
        assert keyword in second
    assert "Relevant: heat pump news." in second
    assert "<remark>make it narrower</remark>" in second
    assert result.stdout.endswith(search_yaml(make_suggestion(keywords_any=["narrow"])))
    assert "suggestions used 2 requests, 20 in / 10 out tokens" in result.stderr


def test_a_failed_refinement_keeps_the_session_alive(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply(), LLMUnavailableError("down"))
    suggest_cli.cli.use(FakePrompter(["Refine", "narrower", "Print YAML"]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert "Error: anthropic/claude-big: LLMUnavailableError: down" in result.stderr
    assert result.stdout.endswith(search_yaml(make_suggestion()))


# --- create job (AC3, AC4) -------------------------------------------------------------------

WIZARD_KEEP = script(
    keywords=["", "", ""], description="", language="", llm=["", "claude-small", ""]
)


def _stored(cli: SuggestCli, name: str = "ai-news") -> JobConfig:
    record = cli.cli.service.get_by_name(name)
    return record.config


def test_create_job_saves_the_suggestion_through_the_wizard(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply())
    prompter = FakePrompter(["Create job", *WIZARD_KEEP])
    suggest_cli.cli.use(prompter)

    result = suggest_cli.invoke("heat pumps", "--language", "de")

    assert result.exit_code == 0, result.output
    assert re.search(r"^created job 'ai-news' \(next run: ", result.stdout, re.M)
    config = _stored(suggest_cli)
    assert config.search.keywords.any == ["Wärmepumpe", "heat pump"]
    assert config.search.keywords.exclude == ["Stellenangebot"]
    assert config.search.semantic_description == make_suggestion().semantic_description
    assert config.language == "de"
    assert (config.llm.provider.value, config.llm.models.smart) == ("anthropic", BIG)
    assert prompter.defaults["Smart model"] == BIG
    assert "Hint from the suggestion: Trade portals Industry associations" in result.stdout


def test_edits_reach_the_stored_job(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply())
    answers = ["Edit a field", "Keywords — exclude", "spam", "Create job", *WIZARD_KEEP]
    suggest_cli.cli.use(FakePrompter(answers))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert _stored(suggest_cli).search.keywords.exclude == ["spam"]


def test_the_operator_can_change_every_prefilled_value_in_the_wizard(
    suggest_cli: SuggestCli,
) -> None:
    suggest_cli.script(_reply())
    wizard_answers = script(
        keywords=["x, y", "z", CLEAR],
        description="Own text",
        language="fr",
        llm=["openai", "gpt-small", "gpt-large"],
    )
    suggest_cli.cli.use(FakePrompter(["Create job", *wizard_answers]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    config = _stored(suggest_cli)
    assert config.search.keywords.any == ["x", "y"]
    assert config.search.keywords.all == ["z"]
    assert config.search.keywords.exclude == []
    assert config.search.semantic_description == "Own text"
    assert (config.language, config.llm.provider.value) == ("fr", "openai")


def test_declining_the_wizard_saves_nothing(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply())
    answers = [
        "Create job",
        *script(
            keywords=["", "", ""],
            description="",
            language="",
            llm=["", "claude-small", ""],
            save=False,
        ),
    ]
    suggest_cli.cli.use(FakePrompter(answers))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 1
    assert "job not created" in result.stderr
    assert suggest_cli.job_names() == []


def test_running_out_of_answers_in_the_wizard_aborts_and_saves_nothing(
    suggest_cli: SuggestCli,
) -> None:
    suggest_cli.script(_reply())
    suggest_cli.cli.use(FakePrompter(["Create job", *WIZARD_KEEP[:6]]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 1
    assert "aborted; nothing saved" in result.stderr
    assert suggest_cli.job_names() == []


def test_discard_after_edits_saves_nothing(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply())
    suggest_cli.cli.use(FakePrompter(["Edit a field", "Description", "Relevant: x", "Discard"]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0
    assert "Nothing saved." in result.stderr
    assert suggest_cli.job_names() == []


def test_the_database_is_only_touched_when_creating(suggest_cli: SuggestCli) -> None:
    suggest_cli.script(_reply())
    suggest_cli.cli.use(FakePrompter(["Print YAML"]))

    def forbidden() -> object:
        pytest.fail("the job service must not be created")

    suggest_cli.cli.monkeypatch.setattr(job_module, "_make_service", forbidden)

    assert suggest_cli.invoke("heat pumps").exit_code == 0
    suggest_cli.script(_reply())
    assert suggest_cli.invoke("heat pumps", "--yaml").exit_code == 0


# --- YAML round trip (US4) -------------------------------------------------------------------


def test_yaml_of_both_paths_is_identical_and_valid_in_a_job(
    suggest_cli: SuggestCli, job_data: dict[str, Any]
) -> None:
    suggest_cli.script(_reply(suggested_sources_hint="line one\nline two"))
    direct = suggest_cli.invoke("t", "--yaml")
    suggest_cli.script(_reply(suggested_sources_hint="line one\nline two"))
    suggest_cli.cli.use(FakePrompter(["Print YAML"]))
    interactive = suggest_cli.invoke("t")

    assert direct.exit_code == interactive.exit_code == 0
    assert interactive.stdout.endswith(direct.stdout)
    lines = direct.stdout.splitlines()
    assert lines[:2] == ["# Suggested sources: line one", "# line two"]
    assert [line for line in lines if "line one" in line or "line two" in line] == lines[:2]
    job_data["search"] = yaml.safe_load(direct.stdout)["search"]
    assert validate_job(job_data).search.keywords.any == ["Wärmepumpe", "heat pump"]


# --- traceability gaps (spec scenarios not yet pinned end to end) ----------------------------


def test_an_invalid_answer_names_the_invalid_field_and_writes_nothing(
    suggest_cli: SuggestCli,
) -> None:
    """US1-3: the error names what was invalid; no stdout, no job."""
    bad = answer_json(semantic_description="")
    suggest_cli.script(FakeReply(bad), FakeReply(bad))

    result = suggest_cli.invoke("t", "--yaml")

    assert result.exit_code == 1
    errors = _error_lines(result.stderr)
    assert len(errors) == 1 and "semantic_description" in errors[0]
    assert result.stdout == ""
    assert suggest_cli.job_names() == []


def test_an_interactive_first_call_failure_exits_1_before_any_prompt(
    suggest_cli: SuggestCli,
) -> None:
    """Model failure in an interactive session: exit 1, no prompt, nothing saved, no key shown."""
    suggest_cli.script(LLMUnavailableError("provider down"))
    suggest_cli.cli.forbid_prompts()

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 1
    assert _error_lines(result.stderr) == [
        "Error: anthropic/claude-big: LLMUnavailableError: provider down"
    ]
    assert KEY not in result.stderr + result.stdout
    assert suggest_cli.job_names() == []


def test_model_without_provider_uses_the_models_provider(suggest_cli: SuggestCli) -> None:
    """US1-5: ``--model`` alone derives the provider from the registry."""
    suggest_cli.set_settings(anthropic_api_key=KEY, openai_api_key=KEY)
    provider = suggest_cli.script(_reply())

    result = suggest_cli.invoke("t", "--yaml", "--model", "gpt-large")

    assert result.exit_code == 0, result.output
    assert provider.requests[0].model == "gpt-large"
    assert suggest_cli.provider_calls[0][0] == "openai"
    assert "Asking openai/gpt-large" in result.stderr


def test_the_alphabetically_first_provider_with_a_key_is_named_in_the_preview(
    suggest_cli: SuggestCli,
) -> None:
    """US1-6: several keys, no ``--provider``: anthropic before openai, named in the preview."""
    suggest_cli.set_settings(openai_api_key=KEY, anthropic_api_key=KEY)
    suggest_cli.script(_reply())
    suggest_cli.cli.use(FakePrompter(["Discard"]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert suggest_cli.provider_calls[0][0] == "anthropic"
    assert "Suggestion (anthropic / claude-big, language: en)" in result.stdout


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["x" * 501], "1 to 500 characters"),
        (["topic", "--language", "xx"], "ISO 639-1"),
    ],
    ids=["too-long-topic", "unknown-language"],
)
def test_usage_errors_explain_the_rule(
    suggest_cli: SuggestCli, args: list[str], message: str
) -> None:
    """Edge cases: the usage error says what is required."""
    suggest_cli.script(_reply())

    result = suggest_cli.invoke(*args)

    assert result.exit_code == 2
    assert message in result.stderr


def test_editing_before_refining_sends_the_edited_suggestion_to_the_model(
    suggest_cli: SuggestCli,
) -> None:
    """US2-2 with FakeProvider: the edit, not the original answer, is the previous suggestion."""
    provider = suggest_cli.script(_reply(), _reply())
    answers = ["Edit a field", "Keywords — exclude", "Mietwohnung", "Refine", "narrower", "Discard"]
    suggest_cli.cli.use(FakePrompter(answers))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    previous = provider.requests[1].user.split("<previous_suggestion>")[1]
    assert "Mietwohnung" in previous
    assert "Stellenangebot" not in previous


def test_each_refinement_round_sends_the_most_recent_suggestion(suggest_cli: SuggestCli) -> None:
    """US2-3 / SC-003 with FakeProvider over three rounds."""
    provider = suggest_cli.script(
        _reply(),
        _reply(keywords_any=["second"]),
        _reply(keywords_any=["third"]),
    )
    answers = ["Refine", "r1", "Refine", "r2", "Print YAML"]
    suggest_cli.cli.use(FakePrompter(answers))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert len(provider.requests) == 3
    third = provider.requests[2].user
    assert "<topic>heat pumps</topic>" in third
    assert '"second"' in third and "Wärmepumpe" not in third
    assert "<remark>r2</remark>" in third and "r1" not in third
    assert result.stdout.endswith(search_yaml(make_suggestion(keywords_any=["third"])))


def test_an_invalid_refinement_answer_keeps_the_previous_suggestion(
    suggest_cli: SuggestCli,
) -> None:
    """US2-4: invalid answer after the repair round; the session goes on with the old one."""
    suggest_cli.script(_reply(), FakeReply("nope"), FakeReply("still nope"))
    suggest_cli.cli.use(FakePrompter(["Refine", "narrower", "Print YAML"]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert any(
        line.startswith("Error: anthropic/claude-big: LLMInvalidOutputError:")
        for line in result.stderr.splitlines()
    )
    assert result.stdout.endswith(search_yaml(make_suggestion()))
    assert "suggestions used 3 requests" in result.stderr


def test_an_empty_remark_makes_no_model_call(suggest_cli: SuggestCli) -> None:
    """US2-5 end to end: blank remarks are asked again, only the real one reaches the model."""
    provider = suggest_cli.script(_reply(), _reply())
    suggest_cli.cli.use(FakePrompter(["Refine", "", "   ", "narrower", "Discard"]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert len(provider.requests) == 2
    assert "<remark>narrower</remark>" in provider.requests[1].user


@pytest.mark.parametrize(
    "answers",
    [["Refine"], ["Edit a field"], ["Edit a field", "Description"]],
    ids=["refine-remark", "edit-field-choice", "edit-value"],
)
def test_aborting_in_a_refinement_or_edit_prompt_saves_nothing(
    suggest_cli: SuggestCli, answers: list[str]
) -> None:
    """Edge case: Ctrl+C / EOF in a follow-up prompt exits 1, no further call, nothing saved."""
    provider = suggest_cli.script(_reply(), _reply())
    suggest_cli.cli.use(FakePrompter(answers))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 1
    assert "aborted; nothing saved" in result.stderr
    assert len(provider.requests) == 1
    assert suggest_cli.job_names() == []


class _InterruptingPrompter(FakePrompter):
    """Raises ``error`` at the first menu, like a real terminal on Ctrl+C or Ctrl+D."""

    def __init__(self, error: BaseException) -> None:
        super().__init__([])
        self._error = error

    def select(self, message: str, choices: Any, *, default: str | None = None) -> str:
        raise self._error


@pytest.mark.parametrize("error", [KeyboardInterrupt(), EOFError()], ids=["ctrl-c", "eof"])
def test_ctrl_c_or_eof_in_the_preview_saves_nothing(
    suggest_cli: SuggestCli, error: BaseException
) -> None:
    suggest_cli.script(_reply())
    suggest_cli.cli.use(_InterruptingPrompter(error))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 1
    assert "aborted; nothing saved" in result.stderr
    assert suggest_cli.job_names() == []


def test_the_wizard_offers_the_suggestion_language_and_provider_as_defaults(
    suggest_cli: SuggestCli,
) -> None:
    """US3-2 / US3-6: the language and provider questions default to the session's values."""
    suggest_cli.script(_reply())
    prompter = FakePrompter(["Create job", *WIZARD_KEEP])
    suggest_cli.cli.use(prompter)

    result = suggest_cli.invoke("heat pumps", "--language", "DE")

    assert result.exit_code == 0, result.output
    assert prompter.defaults["Summary language (ISO 639-1 code)"] == "de"
    assert prompter.defaults["LLM provider"] == "anthropic"
    assert prompter.defaults["Keywords — any of (comma-separated)"] == "Wärmepumpe, heat pump"


def test_an_existing_job_name_is_rejected_by_the_wizard_and_asked_again(
    suggest_cli: SuggestCli, job_data: dict[str, Any]
) -> None:
    """Edge case: the wizard's name validation applies; the existing job stays untouched."""
    existing = suggest_cli.cli.service.create("ai-news", validate_job(job_data))
    suggest_cli.script(_reply())
    answers = script(
        name=["ai-news", "heat-pumps"],
        keywords=["", "", ""],
        description="",
        language="",
        llm=["", "claude-small", ""],
    )
    prompter = FakePrompter(["Create job", *answers])
    suggest_cli.cli.use(prompter)

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert [e for e in prompter.errors if e[0] == "Job name" and "already exists" in e[1]]
    assert sorted(suggest_cli.job_names()) == ["ai-news", "heat-pumps"]
    assert suggest_cli.cli.service.get_by_name("ai-news").config == existing.config


def test_discard_prints_no_yaml(suggest_cli: SuggestCli) -> None:
    """US3-7: discard prints only the preview and a short notice, never the YAML block."""
    suggest_cli.script(_reply())
    suggest_cli.cli.use(FakePrompter(["Discard"]))

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0
    assert "search:" not in result.stdout
    assert "Nothing saved." in result.stderr


def test_a_non_interactive_run_saves_nothing(suggest_cli: SuggestCli) -> None:
    """US4-2: no terminal: one request, YAML, exit 0, job store unchanged."""
    provider = suggest_cli.script(_reply())
    suggest_cli.cli.set_interactive(False)
    suggest_cli.cli.forbid_prompts()

    result = suggest_cli.invoke("heat pumps")

    assert result.exit_code == 0, result.output
    assert len(provider.requests) == 1
    assert suggest_cli.job_names() == []
