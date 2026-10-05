"""Unit tests for the interactive job wizard (scripted prompter, no terminal, no network)."""

from typing import Any

import pytest

from invio.cli import wizard
from invio.cli.prompts import Prompter, WizardAborted
from invio.cli.source_check import CheckResult, SourceChecker
from invio.cli.wizard import (
    OTHER,
    Q_DAY,
    Q_DESC,
    Q_EMAIL,
    Q_FAST,
    Q_FAST_ID,
    Q_FEED_URL,
    Q_FREQUENCY,
    Q_KEEP_LIMITS,
    Q_KEEP_SOURCE,
    Q_KW_ALL,
    Q_KW_ANY,
    Q_KW_EXCLUDE,
    Q_MORE_EMAIL,
    Q_MORE_SOURCE,
    Q_NAME,
    Q_PLAYLIST,
    Q_PROVIDER,
    Q_SAVE,
    Q_SMART,
    Q_SMART_ID,
    Q_SOURCE_TYPE,
    Q_SUBJECT,
    Q_TIME,
    Q_TIMEZONE,
    Q_USE_ANYWAY,
    Q_WEEKDAY,
    run_wizard,
)
from invio.config.job import JobConfig, JobConfigError, LimitsConfig, dump_yaml, validate_job
from invio.llm.base import ModelRegistryError
from invio.llm.registry import ModelInfo, ModelRegistry
from tests.cli_helpers import FakeChecker, FakePrompter, make_registry


class Echo:
    """Collects ``echo`` calls as ``(message, err)``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, message: str = "", *, err: bool = False, nl: bool = True) -> None:
        self.calls.append((message, err))

    @property
    def out(self) -> str:
        return "\n".join(m for m, err in self.calls if not err)

    @property
    def err(self) -> str:
        return "\n".join(m for m, err in self.calls if err)


WEEKLY = ["weekly", "monday", "07:30", "Europe/Berlin"]
SOURCE = ["rss", "https://example.com/feed.xml", False]
KEYWORDS = ["agents, LangGraph", "", "crypto"]
LLM = ["openai", "gpt-small", "gpt-large"]


def script(
    *,
    name: list[str | bool] | None = None,
    schedule: list[str | bool] = WEEKLY,
    recipients: list[str | bool] | None = None,
    sources: list[str | bool] = SOURCE,
    keywords: list[str | bool] = KEYWORDS,
    description: str = "New frameworks.",
    llm: list[str | bool] = LLM,
    limits: list[str | bool] | None = None,
    save: bool = True,
) -> list[str | bool]:
    return [
        *(["ai-news"] if name is None else name),
        *schedule,
        *(["me@example.com", False] if recipients is None else recipients),
        "",
        *sources,
        *keywords,
        description,
        *llm,
        *([True] if limits is None else limits),
        save,
    ]


def run(
    answers: list[str | bool],
    *,
    checker: SourceChecker | None = None,
    registry: ModelRegistry | None = None,
    existing: set[str] | None = None,
    name: str | None = None,
    echo: Echo | None = None,
    use_registry: bool = True,
) -> tuple[tuple[str, JobConfig] | None, FakePrompter, Echo]:
    prompter = FakePrompter(answers)
    typed: Prompter = prompter  # the fake must satisfy the Prompter protocol
    out = echo or Echo()
    result = run_wizard(
        typed,
        checker or FakeChecker(),
        registry=(registry or make_registry()) if use_registry else None,
        existing_names=existing or {"taken"},
        name=name,
        default_timezone="UTC",
        echo=out,
    )
    return result, prompter, out


def test_happy_path_weekly() -> None:
    result, prompter, out = run(script())

    assert result is not None
    name, config = result
    assert name == "ai-news"
    assert (config.schedule.frequency.value, config.schedule.weekday) == ("weekly", "monday")
    assert (config.schedule.time, config.schedule.timezone) == ("07:30", "Europe/Berlin")
    assert config.notification.to == ["me@example.com"]
    assert config.notification.subject == "invio: ai-news"
    assert [str(getattr(s, "url", "")) for s in config.sources] == ["https://example.com/feed.xml"]
    assert config.search.keywords.any == ["agents", "LangGraph"]
    assert config.search.keywords.all == []
    assert config.search.keywords.exclude == ["crypto"]
    assert config.search.semantic_description == "New frameworks."
    assert (config.llm.provider.value, config.llm.models.fast) == ("openai", "gpt-small")
    assert config.limits == LimitsConfig()
    assert prompter.errors == []
    assert dump_yaml(config) in out.out + "\n" or dump_yaml(config).rstrip("\n") in out.out
    assert Q_SAVE in prompter.asked


def test_daily_asks_neither_weekday_nor_day() -> None:
    _, prompter, _ = run(script(schedule=["daily", "06:00", "UTC"]))

    assert Q_WEEKDAY not in prompter.asked and Q_DAY not in prompter.asked


def test_monthly_asks_day_not_weekday() -> None:
    result, prompter, _ = run(script(schedule=["monthly", "15", "06:00", "UTC"]))

    assert Q_DAY in prompter.asked and Q_WEEKDAY not in prompter.asked
    assert result is not None and result[1].schedule.day_of_month == 15


@pytest.mark.parametrize("day", ["29", "30", "31"])
def test_monthly_end_of_month_note(day: str) -> None:
    _, _, out = run(script(schedule=["monthly", day, "06:00", "UTC"]))

    assert f"months shorter than {day} days run on their last day" in out.out


def test_monthly_day_28_has_no_note() -> None:
    _, _, out = run(script(schedule=["monthly", "28", "06:00", "UTC"]))

    assert "months shorter than" not in out.out + out.err


def test_default_timezone_is_offered() -> None:
    _, prompter, _ = run(script(schedule=["daily", "", ""]))

    assert prompter.defaults[Q_TIMEZONE] == "UTC"
    assert prompter.defaults[Q_TIME] == "07:00"
    assert "Europe/Berlin" in prompter.choices[Q_TIMEZONE]


@pytest.mark.parametrize(
    ("question", "bad", "fragment", "answers"),
    [
        (Q_NAME, "taken", "already exists", script(name=["taken", "ok"])),
        (Q_NAME, " x", "whitespace", script(name=[" x", "ok"])),
        (Q_NAME, "x" * 201, "longer than", script(name=["x" * 201, "ok"])),
        (Q_NAME, "", "empty", script(name=["", "ok"])),
        (Q_TIME, "25:00", "HH:MM", script(schedule=["daily", "25:00", "06:00", "UTC"])),
        (Q_TIME, "7:30", "HH:MM", script(schedule=["daily", "7:30", "06:00", "UTC"])),
        (
            Q_TIMEZONE,
            "Mars/Olympus",
            "unknown timezone",
            script(schedule=["daily", "06:00", "Mars/Olympus", "UTC"]),
        ),
        (
            Q_EMAIL,
            "not-an-email",
            "not a valid e-mail",
            script(recipients=["not-an-email", "a@b.de", False]),
        ),
        (
            Q_FEED_URL,
            "htp:/example",
            "not a valid http(s) URL",
            script(sources=["rss", "htp:/example", "https://e.com/f", False]),
        ),
        (Q_DAY, "32", "1 to 31", script(schedule=["monthly", "32", "5", "06:00", "UTC"])),
        (Q_DAY, "x", "1 to 31", script(schedule=["monthly", "x", "5", "06:00", "UTC"])),
    ],
)
def test_inline_rejections(
    question: str, bad: str, fragment: str, answers: list[str | bool]
) -> None:
    result, prompter, _ = run(answers)

    assert result is not None
    assert any(q == question and fragment in err for q, err in prompter.errors), prompter.errors


@pytest.mark.parametrize("bad", ["0", "abc", "-3", "1.5"])
def test_limit_rejections(bad: str) -> None:
    result, prompter, _ = run(script(limits=[False, bad, "5", "", "", "", ""]))

    assert result is not None
    assert prompter.errors and "whole number" in prompter.errors[0][1]
    assert result[1].limits.max_items_per_source == 5


def test_limits_default_yes_asks_nothing_more() -> None:
    _, prompter, _ = run(script())

    assert Q_KEEP_LIMITS in prompter.asked
    assert not [q for q in prompter.asked if q.startswith("Max ")]


def test_limits_no_asks_five_values_prefilled() -> None:
    result, prompter, _ = run(script(limits=[False, "", "", "", "", "7"]))

    assert result is not None
    assert result[1].limits == LimitsConfig(max_llm_tokens_per_run=7)
    limit_questions = [q for q in prompter.asked if q.startswith("Max ")]
    assert len(limit_questions) == 5
    assert [prompter.defaults[q] for q in limit_questions] == ["20", "100", "10", "20", "200000"]


def test_recipients_first_required_and_duplicates_skipped() -> None:
    result, prompter, out = run(
        script(recipients=["a@example.com", True, "a@EXAMPLE.com", True, "b@example.com", False])
    )

    assert result is not None
    assert result[1].notification.to == ["a@example.com", "b@example.com"]
    assert "already added" in out.err
    assert prompter.asked.index(Q_EMAIL) < prompter.asked.index(Q_MORE_EMAIL)


def test_keywords_blank_items_dropped_and_empty_allowed() -> None:
    result, _, _ = run(script(keywords=["a, b,, ", "", ""]))

    assert result is not None
    kw = result[1].search.keywords
    assert (kw.any, kw.all, kw.exclude) == (["a", "b"], [], [])


def test_empty_description_is_rejected() -> None:
    answers = script(description="  ")
    answers.insert(answers.index("  ") + 1, "now valid")

    result, prompter, _ = run(answers)

    assert result is not None and result[1].search.semantic_description == "now valid"
    assert any(q == Q_DESC and "must not be empty" in e for q, e in prompter.errors)


def test_multiline_description_keeps_newlines() -> None:
    result, _, _ = run(script(description="line one\nline two"))

    assert result is not None
    assert result[1].search.semantic_description == "line one\nline two"


def test_questions_in_documented_order() -> None:
    _, prompter, _ = run(script())

    order = [
        Q_NAME, Q_FREQUENCY, Q_WEEKDAY, Q_TIME, Q_TIMEZONE, Q_EMAIL, Q_MORE_EMAIL, Q_SUBJECT,
        Q_SOURCE_TYPE, Q_FEED_URL, Q_MORE_SOURCE, Q_KW_ANY, Q_KW_ALL, Q_KW_EXCLUDE, Q_DESC,
        Q_PROVIDER, Q_FAST, Q_SMART, Q_KEEP_LIMITS, Q_SAVE,
    ]  # fmt: skip
    assert prompter.asked == order


def test_prefilled_name_skips_the_question() -> None:
    result, prompter, _ = run(script(name=[]), name="given")

    assert result is not None and result[0] == "given"
    assert Q_NAME not in prompter.asked
    assert result[1].notification.subject == "invio: given"


# --- models ----------------------------------------------------------------------------------


def test_model_choices_come_from_registry_plus_other() -> None:
    _, prompter, _ = run(script())

    assert prompter.choices[Q_FAST] == ["gpt-large", "gpt-small", OTHER]
    assert prompter.choices[Q_PROVIDER] == ["mistral", "openai", "anthropic", "google", "ollama"]


def test_other_unknown_model_warns_and_confirms() -> None:
    result, _, out = run(script(llm=["openai", OTHER, "gpt-huge", True, "gpt-large"]))

    assert result is not None and result[1].llm.models.fast == "gpt-huge"
    assert "not registered for provider" in out.err and "runs will fail" in out.err


def test_other_unknown_model_declined_asks_again() -> None:
    result, prompter, _ = run(
        script(llm=["openai", OTHER, "gpt-huge", False, "gpt-small", "gpt-large"])
    )

    assert result is not None and result[1].llm.models.fast == "gpt-small"
    assert prompter.asked.count(Q_FAST) == 2
    assert Q_USE_ANYWAY in prompter.asked


def test_other_with_registered_id_has_no_warning() -> None:
    _, _, out = run(script(llm=["openai", OTHER, "gpt-small", "gpt-large"]))

    assert "not registered" not in out.err


def test_provider_without_models_gives_free_text_with_warning() -> None:
    _, prompter, out = run(script(llm=["ollama", "llama-x", True, "llama-y", True]))

    assert Q_FAST not in prompter.choices
    assert Q_FAST_ID in prompter.asked and Q_SMART_ID in prompter.asked
    assert "not registered for provider" in out.err


def test_registry_none_gives_free_text_without_warning() -> None:
    result, _, out = run(script(llm=["openai", "m-fast", "m-smart"]), use_registry=False)

    assert result is not None and result[1].llm.models.smart == "m-smart"
    assert "not registered" not in out.err


def test_broken_registry_warns_once_and_uses_free_text() -> None:
    class Broken(ModelRegistry):
        def models_for(self, provider: str) -> list[ModelInfo]:
            raise ModelRegistryError("models.d/openai.yaml: invalid")

    result, _, out = run(
        script(llm=["openai", "m-fast", "m-smart"]), registry=Broken({}), use_registry=True
    )

    assert result is not None
    assert out.err.count("model registry unavailable") == 1


# --- sources ---------------------------------------------------------------------------------


def test_unreachable_source_keep_declined_asks_url_again() -> None:
    bad, good = "https://down.example.com/f", "https://ok.example.com/f"
    checker = FakeChecker({bad: CheckResult(False, None, "timeout after 10s", None)})

    result, prompter, out = run(script(sources=["rss", bad, False, good, False]), checker=checker)

    assert result is not None
    assert [str(getattr(s, "url", "")) for s in result[1].sources] == [good]
    assert "timeout after 10s" in out.err
    assert Q_KEEP_SOURCE in prompter.asked


def test_unreachable_source_kept_when_confirmed() -> None:
    bad = "https://down.example.com/f"
    checker = FakeChecker({bad: CheckResult(False, 404, "HTTP 404", None)})

    result, _, out = run(script(sources=["rss", bad, True, False]), checker=checker)

    assert result is not None and len(result[1].sources) == 1
    assert "HTTP 404" in out.err


def test_rss_without_feed_warns() -> None:
    page = "https://example.com/blog"
    checker = FakeChecker({page: CheckResult(True, 200, None, False)})

    result, _, out = run(script(sources=["rss", page, True, False]), checker=checker)

    assert result is not None
    assert "no RSS/Atom feed detected" in out.err
    assert checker.calls == [(page, True)]


def test_web_source_checks_reachability_only() -> None:
    checker = FakeChecker()

    run(script(sources=["web", "https://example.com/page", False]), checker=checker)

    assert checker.calls == [("https://example.com/page", False)]


def test_youtube_sources_ask_ids_and_skip_checks() -> None:
    checker = FakeChecker()
    sources: list[str | bool] = ["youtube_channel", "UC123", True, "youtube_playlist", "PL9", False]

    result, prompter, _ = run(script(sources=sources), checker=checker)

    assert result is not None
    assert checker.calls == []
    assert Q_PLAYLIST in prompter.asked
    assert [s.type for s in result[1].sources] == ["youtube_channel", "youtube_playlist"]


def test_duplicate_source_is_skipped_with_warning() -> None:
    url = "https://example.com/feed.xml"
    result, _, out = run(script(sources=["rss", url, True, "rss", url, False]))

    assert result is not None and len(result[1].sources) == 1
    assert "already added" in out.err


def test_feed_detected_message_goes_to_stdout() -> None:
    _, _, out = run(script())

    assert "feed detected" in out.out


# --- preview, errors, back navigation ---------------------------------------------------------


def test_declined_preview_returns_none() -> None:
    result, _, out = run(script(save=False))

    assert result is None
    assert "schedule:" in out.out


def test_running_out_of_answers_aborts() -> None:
    with pytest.raises(WizardAborted):
        run(script()[:5])


def test_going_back_to_the_step_that_owns_the_error(monkeypatch: pytest.MonkeyPatch) -> None:
    real = wizard.validate_job
    calls = 0

    def flaky(data: Any) -> JobConfig:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise JobConfigError(None, ["schedule.timezone: unknown timezone 'X'"])
        return real(data)

    monkeypatch.setattr(wizard, "validate_job", flaky)
    # Second pass through the schedule step: previous answers are the defaults.
    answers = script()
    answers[-1:-1] = ["weekly", "monday", "", "Europe/Berlin"]

    result, prompter, out = run(answers)

    assert result is not None
    assert prompter.asked.count(Q_TIMEZONE) == 2
    assert prompter.asked.count(Q_EMAIL) == 1
    assert prompter.asked.count(Q_DESC) == 1
    assert "schedule.timezone: unknown timezone 'X'" in out.err
    assert prompter.defaults[Q_TIME] == "07:30"


def test_unattributable_error_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(data: Any) -> JobConfig:
        raise JobConfigError(None, ["mystery: what"])

    monkeypatch.setattr(wizard, "validate_job", broken)

    with pytest.raises(JobConfigError):
        run(script())


def test_validate_job_result_is_what_the_preview_shows() -> None:
    result, _, out = run(script())

    assert result is not None
    assert validate_job(result[1].model_dump(mode="json")) == result[1]
    assert dump_yaml(result[1]).rstrip("\n") in out.out


def test_no_owning_step_for_empty_error_list() -> None:
    assert wizard._owning_step([]) is None
    assert wizard._owning_step(["mystery: x"]) is None


@pytest.mark.parametrize(
    ("first", "again", "question", "default", "expected"),
    [
        (WEEKLY, ["weekly", "", "", ""], Q_WEEKDAY, "monday", "monday"),
        (["monthly", "15", "06:00", "UTC"], ["monthly", "", "", ""], Q_DAY, "15", 15),
    ],
    ids=["weekday", "day-of-month"],
)
def test_reask_keeps_weekday_and_day_as_defaults(
    monkeypatch: pytest.MonkeyPatch,
    first: list[str | bool],
    again: list[str | bool],
    question: str,
    default: str,
    expected: object,
) -> None:
    real = wizard.validate_job
    calls = 0

    def flaky(data: Any) -> JobConfig:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise JobConfigError(None, ["schedule.timezone: unknown timezone 'X'"])
        return real(data)

    monkeypatch.setattr(wizard, "validate_job", flaky)
    answers = script(schedule=first)
    answers[-1:-1] = again

    result, prompter, _ = run(answers)

    assert result is not None
    assert prompter.asked.count(question) == 2
    assert prompter.defaults[question] == default
    schedule = result[1].schedule
    assert expected in (schedule.weekday, schedule.day_of_month)


def test_url_credentials_are_not_echoed() -> None:
    bad, good = "https://bob:p@ss@down.example.com/f", "https://ok.example.com/f"
    checker = FakeChecker({bad: CheckResult(False, None, "timeout after 10s", None)})

    _, _, out = run(script(sources=["rss", bad, False, good, False]), checker=checker)

    assert "down.example.com" in out.out
    assert "p@ss" not in out.out + out.err and "bob" not in out.out + out.err
