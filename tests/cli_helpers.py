"""Test doubles and fixtures for the ``invio job`` CLI tests (not shipped)."""

from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from typer.testing import CliRunner, Result

from invio.cli import main as cli_main
from invio.cli.commands import job as job_module
from invio.cli.prompts import Validator, WizardAborted
from invio.cli.source_check import CheckResult
from invio.db.repositories import JobRepository, RunRepository
from invio.db.session import session_factory, session_scope
from invio.domain import RunStatus
from invio.llm.registry import ModelInfo, ModelRegistry
from invio.scheduling.next_run import compute_next_run
from invio.services.jobs import JobService
from tests.conftest import FakeClock


class FakePrompter:
    """Scripted :class:`~invio.cli.prompts.Prompter`.

    An empty string answers with the default (the validator still runs on it). Select and
    autocomplete answers must be one of the choices. Running out of answers raises
    ``WizardAborted`` (simulates Ctrl+C).
    """

    def __init__(self, answers: Iterable[str | bool]) -> None:
        self._answers = iter(answers)
        self.asked: list[str] = []
        self.errors: list[tuple[str, str]] = []
        self.choices: dict[str, list[str]] = {}
        self.defaults: dict[str, object] = {}

    def _next(self) -> str | bool:
        try:
            return next(self._answers)
        except StopIteration:
            raise WizardAborted from None

    def _ask(self, message: str, default: str, validate: Validator | None) -> str:
        self.asked.append(message)
        while True:
            answer = self._next()
            assert isinstance(answer, str), f"{message!r}: expected str, got {answer!r}"
            value = default if answer == "" else answer
            result = True if validate is None else validate(value)
            if result is True:
                return value
            self.errors.append((message, "invalid value" if result is False else result))

    def text(
        self,
        message: str,
        *,
        default: str = "",
        validate: Validator | None = None,
        multiline: bool = False,
    ) -> str:
        self.defaults[message] = default
        return self._ask(message, default, validate)

    def select(self, message: str, choices: Sequence[str], *, default: str | None = None) -> str:
        self.choices[message] = list(choices)
        self.defaults[message] = default
        answer = self._ask(message, default or "", None)
        assert answer in choices, f"{message!r}: {answer!r} not in {list(choices)}"
        return answer

    def confirm(self, message: str, *, default: bool = True) -> bool:
        self.asked.append(message)
        self.defaults[message] = default
        answer = self._next()
        assert isinstance(answer, bool), f"{message!r}: expected bool, got {answer!r}"
        return answer

    def autocomplete(
        self,
        message: str,
        choices: Sequence[str],
        *,
        default: str = "",
        validate: Validator | None = None,
    ) -> str:
        self.choices[message] = list(choices)
        self.defaults[message] = default
        answer = self._ask(message, default, validate)
        return answer


REACHABLE_FEED = CheckResult(True, 200, None, True)


class FakeChecker:
    """Scripted :class:`~invio.cli.source_check.SourceChecker`; records every call."""

    def __init__(
        self,
        results: Mapping[str, CheckResult] | None = None,
        default: CheckResult = REACHABLE_FEED,
    ) -> None:
        self._results = dict(results or {})
        self._default = default
        self.calls: list[tuple[str, bool]] = []

    def check(self, url: str, *, expect_feed: bool) -> CheckResult:
        self.calls.append((url, expect_feed))
        return self._results.get(url, self._default)


class FakeEditor:
    """Callable replacement for ``_edit_text``; records the text it was given."""

    def __init__(self, texts: tuple[str | Exception | None, ...]) -> None:
        self._texts = iter(texts)
        self.received: list[str] = []

    def __call__(self, text: str) -> str | None:
        self.received.append(text)
        result = next(self._texts)
        if isinstance(result, Exception):
            raise result
        return result


def fake_editor(*texts: str | Exception | None) -> FakeEditor:
    """Return an editor returning ``texts`` in turn (an exception instance is raised)."""
    return FakeEditor(texts)


@dataclass
class JobCli:
    """What the ``job_cli`` fixture hands to a test."""

    service: JobService
    runner: CliRunner
    monkeypatch: pytest.MonkeyPatch
    engine: Engine

    def corrupt_timezone(self, name: str) -> None:
        """Store an unknown time zone in the job's config (makes it invalid)."""
        with session_scope(session_factory(self.engine)) as session:
            job = JobRepository(session).get_by_name(name)
            assert job is not None
            config = dict(job.config)
            config["schedule"] = {**config["schedule"], "timezone": "Europe/Atlantis"}
            job.config = config

    def add_run(self, name: str, status: RunStatus, started_at: datetime) -> None:
        with session_scope(session_factory(self.engine)) as session:
            job = JobRepository(session).get_by_name(name)
            assert job is not None
            runs = RunRepository(session)
            runs.finish(runs.start(job.id, started_at=started_at), status)

    def set_interactive(self, value: bool) -> None:
        self.monkeypatch.setattr(job_module, "_is_interactive", lambda: value)

    def invoke(
        self,
        args: Sequence[str],
        input: str | None = None,
        env: Mapping[str, str | None] | None = None,
    ) -> Result:
        if env is None:
            env = {"COLUMNS": "200"}
        return self.runner.invoke(cli_main.app, ["job", *args], input=input, env=env)

    def forbid_prompts(self) -> None:
        """Make any construction of a prompter or checker fail the test."""

        def fail() -> object:
            pytest.fail("prompter/checker must not be used")

        self.monkeypatch.setattr(job_module, "_make_prompter", fail)
        self.monkeypatch.setattr(job_module, "_make_checker", fail)

    def use(self, prompter: object, checker: object | None = None) -> None:
        """Install fakes for the prompter and checker seams."""
        self.monkeypatch.setattr(job_module, "_make_prompter", lambda: prompter)
        self.monkeypatch.setattr(job_module, "_make_checker", lambda: checker or FakeChecker())

    def use_editor(self, editor: object) -> None:
        self.monkeypatch.setattr(job_module, "_edit_text", editor)


def make_registry() -> ModelRegistry:
    """A small registry for the wizard: two ``openai`` models and one ``mistral`` model."""

    def info(model_id: str, provider: str) -> ModelInfo:
        return ModelInfo(model_id, provider, Decimal(1), Decimal(2), 1000)

    return ModelRegistry(
        {
            "gpt-small": info("gpt-small", "openai"),
            "gpt-large": info("gpt-large", "openai"),
            "mistral-one": info("mistral-one", "mistral"),
        }
    )


@pytest.fixture
def job_cli(
    monkeypatch: pytest.MonkeyPatch,
    db_engine: Engine,
    clean_jobs: None,
    fake_clock: FakeClock,
) -> Iterator[JobCli]:
    """A CLI runner wired to a real-schedule ``JobService`` on the test engine."""
    service = JobService(session_factory(db_engine), next_run=compute_next_run, clock=fake_clock)
    monkeypatch.setattr(job_module, "_make_service", lambda: service)
    monkeypatch.setattr(job_module, "_make_registry", make_registry)
    monkeypatch.setattr(job_module, "_default_timezone", lambda: "UTC")
    monkeypatch.setattr(job_module, "_is_interactive", lambda: True)
    yield JobCli(service, CliRunner(), monkeypatch, db_engine)
