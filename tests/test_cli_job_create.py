"""CliRunner tests for the interactive ``invio job create`` (scripted prompter and checker)."""

import re

import pytest

from invio.cli import wizard as w
from invio.cli.prompts import Validator, WizardAborted
from invio.cli.source_check import CheckResult
from invio.services.jobs import JobExistsError
from tests.cli_helpers import FakeChecker, FakePrompter, JobCli, job_cli  # noqa: F401 (fixture)
from tests.test_wizard import script

pytestmark = pytest.mark.db

NEXT_RUN_RE = r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} (CET|CEST)"


def _names(cli: JobCli) -> list[str]:
    return [s.name for s in cli.service.overview()]


def test_full_run_creates_job_and_list_shows_it(job_cli: JobCli) -> None:
    prompter = FakePrompter(script())
    job_cli.use(prompter)

    result = job_cli.invoke(["create"])

    assert result.exit_code == 0, result.output
    assert "schedule:" in result.stdout  # the YAML preview
    assert re.search(rf"^created job 'ai-news' \(next run: {NEXT_RUN_RE}\)$", result.stdout, re.M)
    listing = job_cli.invoke(["list"])
    assert re.search(
        rf"ai-news\s+yes\s+weekly mon 07:30 Europe/Berlin\s+{NEXT_RUN_RE}\s+never run",
        listing.stdout,
    )


def test_bad_email_then_good_email(job_cli: JobCli) -> None:
    prompter = FakePrompter(script(recipients=["me@example", "me@example.com", False]))
    job_cli.use(prompter)

    result = job_cli.invoke(["create"])

    assert result.exit_code == 0, result.output
    assert prompter.errors
    assert _names(job_cli) == ["ai-news"]


def test_declined_preview(job_cli: JobCli) -> None:
    job_cli.use(FakePrompter(script(save=False)))

    result = job_cli.invoke(["create"])

    assert result.exit_code == 1
    assert "job not created" in result.stderr
    assert "created job" not in result.stdout
    assert _names(job_cli) == []


def test_abort_leaves_nothing_and_no_traceback(job_cli: JobCli) -> None:
    job_cli.use(FakePrompter(script()[:6]))

    result = job_cli.invoke(["create"])

    assert result.exit_code == 1
    assert "aborted; nothing saved" in result.stderr
    assert "Traceback" not in result.output
    assert _names(job_cli) == []


def test_no_terminal_exits_2_without_building_a_prompter(job_cli: JobCli) -> None:
    job_cli.set_interactive(False)
    job_cli.forbid_prompts()

    result = job_cli.invoke(["create"])

    assert result.exit_code == 2
    assert "interactive creation needs a terminal" in result.stderr
    assert "--from-file" in result.stderr


def test_ctrl_c_during_source_check(job_cli: JobCli) -> None:
    class Interrupting(FakeChecker):
        def check(self, url: str, *, expect_feed: bool) -> CheckResult:
            raise KeyboardInterrupt

    job_cli.use(FakePrompter(script()), Interrupting())

    result = job_cli.invoke(["create"])

    assert result.exit_code == 1
    assert "aborted; nothing saved" in result.stderr
    assert "Traceback" not in result.output
    assert _names(job_cli) == []


def test_name_option_prefills_and_skips_question(job_cli: JobCli) -> None:
    prompter = FakePrompter(script(name=[]))
    job_cli.use(prompter)

    result = job_cli.invoke(["create", "--name", "given"])

    assert result.exit_code == 0, result.output
    assert "Job name" not in prompter.asked
    assert _names(job_cli) == ["given"]


def test_invalid_name_option_is_rejected_before_the_wizard(job_cli: JobCli) -> None:
    prompter = FakePrompter([])
    job_cli.use(prompter)

    result = job_cli.invoke(["create", "--name", " x"])

    assert result.exit_code == 1
    assert "Error: invalid job name ' x':" in result.stderr
    assert prompter.asked == []


def test_existing_name_option_is_rejected_before_the_wizard(
    job_cli: JobCli, job_data: dict[str, object]
) -> None:
    job_cli.service.create("x", job_data)
    prompter = FakePrompter([])
    job_cli.use(prompter)

    result = job_cli.invoke(["create", "--name", "x"])

    assert result.exit_code == 1
    assert "Error: job 'x' already exists" in result.stderr
    assert prompter.asked == []


def test_name_taken_after_the_wizard_keeps_the_preview(
    job_cli: JobCli, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_cli.use(FakePrompter(script()))

    def taken(name: str, config: object) -> None:
        raise JobExistsError(name)

    monkeypatch.setattr(job_cli.service, "create", taken)

    result = job_cli.invoke(["create"])

    assert result.exit_code == 1
    assert "Error: job 'ai-news' already exists" in result.stderr
    assert "semantic_description:" in result.stdout  # the answers are not lost


def test_default_seams_build_real_objects() -> None:
    from invio.cli.commands import job as job_module
    from invio.cli.prompts import QuestionaryPrompter
    from invio.cli.source_check import HttpSourceChecker

    assert isinstance(job_module._make_prompter(), QuestionaryPrompter)
    assert isinstance(job_module._make_checker(), HttpSourceChecker)


# --- QA additions (traceability gaps) --------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "fragment", "answers"),
    [
        (w.Q_NAME, "already exists", script(name=["taken", "ai-news"])),
        (w.Q_TIME, "HH:MM", script(schedule=["weekly", "monday", "25:00", "07:30", "UTC"])),
        (
            w.Q_TIMEZONE,
            "unknown timezone 'Mars/Olympus'",
            script(schedule=["weekly", "monday", "07:30", "Mars/Olympus", "Europe/Berlin"]),
        ),
        (
            w.Q_EMAIL,
            "not a valid e-mail",
            script(recipients=["not-an-email", "me@example.com", False]),
        ),
        (
            w.Q_FEED_URL,
            "not a valid http(s) URL",
            script(sources=["rss", "htp:/example", "https://example.com/feed.xml", False]),
        ),
    ],
    ids=["name", "time", "timezone", "email", "url"],
)
def test_inline_rejection_reasks_and_keeps_earlier_answers(
    job_cli: JobCli,
    job_data: dict[str, object],
    question: str,
    fragment: str,
    answers: list[str | bool],
) -> None:
    job_cli.service.create("taken", job_data)
    prompter = FakePrompter(answers)
    job_cli.use(prompter)

    result = job_cli.invoke(["create"])

    assert result.exit_code == 0, result.output
    assert [q for q, _ in prompter.errors] == [question]
    assert fragment in prompter.errors[0][1]
    assert prompter.asked.count(question) == 1  # re-asked in place, not restarted
    assert prompter.asked.count(w.Q_FREQUENCY) == 1
    record = job_cli.service.get_by_name("ai-news")
    assert record.config.schedule.weekday == "monday"  # earlier answers kept
    assert record.config.notification.to == ["me@example.com"]


@pytest.mark.parametrize("keep", [True, False], ids=["override", "re-enter"])
def test_unreachable_url_override(job_cli: JobCli, keep: bool) -> None:
    bad, good = "https://down.example.com/feed", "https://example.com/feed.xml"
    checker = FakeChecker({bad: CheckResult(False, None, "timed out after 10s", None)})
    sources: list[str | bool] = (
        ["rss", bad, True, False] if keep else ["rss", bad, False, good, False]
    )
    prompter = FakePrompter(script(sources=sources))
    job_cli.use(prompter, checker)

    result = job_cli.invoke(["create"])

    assert result.exit_code == 0, result.output
    assert f"! could not reach {bad}: timed out after 10s" in result.stderr
    assert "could not reach" not in result.stdout
    assert w.Q_KEEP_SOURCE in prompter.asked
    urls = [str(s.url) for s in job_cli.service.get_by_name("ai-news").config.sources]  # type: ignore[union-attr]
    assert urls == ([bad] if keep else [good])
    assert [url for url, _ in checker.calls] == ([bad] if keep else [bad, good])


@pytest.mark.parametrize("keep", [True, False], ids=["override", "re-enter"])
def test_rss_feed_detection_warning(job_cli: JobCli, keep: bool) -> None:
    page, feed = "https://example.com/blog", "https://example.com/feed.xml"
    checker = FakeChecker({page: CheckResult(True, 200, None, False)})
    sources: list[str | bool] = (
        ["rss", page, True, False] if keep else ["rss", page, False, feed, False]
    )
    job_cli.use(FakePrompter(script(sources=sources)), checker)

    result = job_cli.invoke(["create"])

    assert result.exit_code == 0, result.output
    assert "! reachable, but no RSS/Atom feed detected" in result.stderr
    assert checker.calls[0] == (page, True)  # feed detection requested for rss
    urls = [str(s.url) for s in job_cli.service.get_by_name("ai-news").config.sources]  # type: ignore[union-attr]
    assert urls == ([page] if keep else [feed])
    if not keep:
        assert "✓ feed detected" in result.stdout


class _InterruptAt(FakePrompter):
    """Answers like :class:`FakePrompter` but raises ``exc`` when ``question`` is asked."""

    def __init__(self, answers: list[str | bool], question: str, exc: BaseException) -> None:
        super().__init__(answers)
        self._question, self._exc = question, exc

    def _ask(self, message: str, default: str, validate: Validator | None) -> str:
        if message == self._question:
            raise self._exc
        return super()._ask(message, default, validate)

    def confirm(self, message: str, *, default: bool = True) -> bool:
        if message == self._question:
            raise self._exc
        return super().confirm(message, default=default)


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), EOFError(), WizardAborted()])
@pytest.mark.parametrize(
    "question",
    [
        w.Q_NAME,
        w.Q_WEEKDAY,
        w.Q_TIMEZONE,
        w.Q_EMAIL,
        w.Q_SOURCE_TYPE,
        w.Q_FEED_URL,
        w.Q_KW_ALL,
        w.Q_DESC,
        w.Q_LANGUAGE,
        w.Q_SMART,
        w.Q_KEEP_LIMITS,
        w.Q_SAVE,
    ],
)
def test_ctrl_c_at_any_step_saves_nothing(
    job_cli: JobCli, question: str, exc: BaseException
) -> None:
    job_cli.use(_InterruptAt(script(), question, exc))

    result = job_cli.invoke(["create"])

    assert result.exit_code == 1
    assert result.stderr.strip().endswith("aborted; nothing saved")
    assert "Traceback" not in result.output
    assert "created job" not in result.stdout
    assert _names(job_cli) == []


def test_unknown_model_warning_goes_to_stderr(job_cli: JobCli) -> None:
    job_cli.use(FakePrompter(script(llm=["openai", "gpt-small", w.OTHER, "gpt-huge", True])))

    result = job_cli.invoke(["create"])

    assert result.exit_code == 0, result.output
    assert "model 'gpt-huge' is not registered for provider 'openai'" in result.stderr
    assert job_cli.service.get_by_name("ai-news").config.llm.models.smart == "gpt-huge"


def test_preview_is_exactly_the_export(job_cli: JobCli) -> None:
    job_cli.use(FakePrompter(script()))

    result = job_cli.invoke(["create"])

    assert result.exit_code == 0, result.output
    exported = job_cli.invoke(["export", "ai-news"]).stdout
    assert exported in result.stdout
    assert result.stdout.index(exported) < result.stdout.index("created job")
