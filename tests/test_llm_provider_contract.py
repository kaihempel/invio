"""Shared provider contract: every :class:`~invio.llm.base.LLMProvider` must pass these tests.

The suite runs once per provider harness: :class:`FakeProvider` (scripted), ``MistralProvider``
(recorded HTTP via ``tests/mistral_helpers.py``) and ``OpenAIProvider`` (recorded HTTP via
``tests/openai_helpers.py``) and ``AnthropicProvider`` (recorded HTTP via
``tests/anthropic_helpers.py``). A harness maps the abstract :class:`Outcome` of each request to
whatever its provider needs (a scripted step or a recorded fixture). Retries are disabled so
every error outcome costs exactly one request; retry behaviour itself is covered by the
provider-specific test modules.

Adding a provider means adding one harness to :data:`HARNESSES`.
"""

import enum
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
from typing import ClassVar

import pytest

from invio.llm.base import (
    LLMAuthError,
    LLMError,
    LLMInvalidOutputError,
    LLMInvalidRequestError,
    LLMProvider,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
)
from invio.llm.fake import FakeProvider, FakeReply, FakeStep
from invio.llm.http_retry import RetryPolicy
from tests import anthropic_helpers, mistral_helpers, openai_helpers
from tests.llm_helpers import Score

SYSTEM = "You rate things."
USER = "Rate this."
NO_RETRIES = RetryPolicy(max_retries=0)
# Every recorded success fixture reports 12 input and 3 output tokens.
RECORDED_USAGE = Usage(12, 3)


class Outcome(enum.Enum):
    """What one provider request should produce, independent of the provider."""

    TEXT = "text"
    STRUCTURED = "structured"
    STRUCTURED_INVALID = "structured_invalid"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    UNAVAILABLE = "unavailable"
    INVALID_REQUEST = "invalid_request"


class ProviderHarness(ABC):
    """Builds one provider whose requests produce a given sequence of outcomes."""

    name: ClassVar[str]
    model: ClassVar[str]

    def __init__(self) -> None:
        self.requests_made: Callable[[], int] = lambda: 0

    @abstractmethod
    def build(self, *outcomes: Outcome) -> LLMProvider:
        """Return a provider answering its requests with ``outcomes``, in order."""


class FakeHarness(ProviderHarness):
    name = "fake"
    model = "fake-model"

    _ERRORS: ClassVar[dict[Outcome, type[LLMError]]] = {
        Outcome.AUTH: LLMAuthError,
        Outcome.RATE_LIMIT: LLMRateLimitError,
        Outcome.UNAVAILABLE: LLMUnavailableError,
        Outcome.INVALID_REQUEST: LLMInvalidRequestError,
    }

    def _step(self, outcome: Outcome) -> FakeStep:
        if outcome is Outcome.TEXT:
            return FakeReply("Hello", RECORDED_USAGE)
        if outcome is Outcome.STRUCTURED:
            return FakeReply('{"score": 0.8, "reason": "fits"}', RECORDED_USAGE)
        if outcome is Outcome.STRUCTURED_INVALID:
            return FakeReply('{"score": 7, "reason": "fits"}', RECORDED_USAGE)
        return self._ERRORS[outcome](
            f"scripted {outcome.value}", provider=self.name, model=self.model
        )

    def build(self, *outcomes: Outcome) -> LLMProvider:
        fake = FakeProvider([self._step(outcome) for outcome in outcomes], name=self.name)
        self.requests_made = lambda: len(fake.requests)
        return fake


class MistralHarness(ProviderHarness):
    name = "mistral"
    model = "mistral-small-2603"

    _FIXTURES: ClassVar[dict[Outcome, str]] = {
        Outcome.TEXT: "chat_ok",
        Outcome.STRUCTURED: "structured_ok",
        Outcome.STRUCTURED_INVALID: "structured_invalid",
        Outcome.AUTH: "error_401",
        Outcome.RATE_LIMIT: "error_429",
        Outcome.UNAVAILABLE: "error_503",
        Outcome.INVALID_REQUEST: "error_400",
    }

    def build(self, *outcomes: Outcome) -> LLMProvider:
        provider, recorder, _ = mistral_helpers.make_provider(
            *(self._FIXTURES[outcome] for outcome in outcomes), retry=NO_RETRIES
        )
        self.requests_made = lambda: len(recorder.requests)
        return provider


class OpenAIHarness(ProviderHarness):
    name = "openai"
    model = "gpt-4.1-mini-2025-04-14"

    _FIXTURES: ClassVar[dict[Outcome, str]] = {
        Outcome.TEXT: "response_ok",
        Outcome.STRUCTURED: "structured_ok",
        Outcome.STRUCTURED_INVALID: "structured_invalid",
        Outcome.AUTH: "error_401",
        Outcome.RATE_LIMIT: "error_429",
        Outcome.UNAVAILABLE: "error_503",
        Outcome.INVALID_REQUEST: "error_400",
    }

    def build(self, *outcomes: Outcome) -> LLMProvider:
        provider, recorder, _ = openai_helpers.make_provider(
            *(self._FIXTURES[outcome] for outcome in outcomes), retry=NO_RETRIES
        )
        self.requests_made = lambda: len(recorder.requests)
        return provider


class AnthropicHarness(ProviderHarness):
    name = "anthropic"
    model = "claude-haiku-4-5-20251001"

    _FIXTURES: ClassVar[dict[Outcome, str]] = {
        Outcome.TEXT: "message_ok",
        Outcome.STRUCTURED: "tool_use_ok",
        Outcome.STRUCTURED_INVALID: "tool_use_invalid",
        Outcome.AUTH: "error_401",
        Outcome.RATE_LIMIT: "error_429",
        Outcome.UNAVAILABLE: "error_529",
        Outcome.INVALID_REQUEST: "error_400",
    }

    def build(self, *outcomes: Outcome) -> LLMProvider:
        provider, recorder, _ = anthropic_helpers.make_provider(
            *(self._FIXTURES[outcome] for outcome in outcomes), retry=NO_RETRIES
        )
        self.requests_made = lambda: len(recorder.requests)
        return provider


HARNESSES: tuple[type[ProviderHarness], ...] = (
    FakeHarness,
    MistralHarness,
    OpenAIHarness,
    AnthropicHarness,
)


class TestProviderContract:
    """Behaviour every provider shares, whatever its transport."""

    @pytest.fixture(params=HARNESSES, ids=lambda harness: harness.name)
    def harness(self, request: pytest.FixtureRequest) -> ProviderHarness:
        harness_class: type[ProviderHarness] = request.param
        return harness_class()

    @pytest.fixture
    async def built(self, harness: ProviderHarness) -> AsyncIterator[list[LLMProvider]]:
        """Providers built by a test; closed at teardown on the test's event loop."""
        providers: list[LLMProvider] = []
        yield providers
        for provider in providers:
            await provider.aclose()

    @pytest.fixture
    def build(
        self, harness: ProviderHarness, built: list[LLMProvider]
    ) -> Callable[..., LLMProvider]:
        def _build(*outcomes: Outcome) -> LLMProvider:
            provider = harness.build(*outcomes)
            built.append(provider)
            return provider

        return _build

    async def test_complete_returns_text_and_usage(
        self, harness: ProviderHarness, build: Callable[..., LLMProvider]
    ) -> None:
        provider = build(Outcome.TEXT)

        text, usage = await provider.complete(
            SYSTEM, USER, model=harness.model, temperature=0, max_tokens=20
        )

        assert text == "Hello"
        assert usage == RECORDED_USAGE
        assert harness.requests_made() == 1

    async def test_complete_structured_returns_validated_value_and_usage(
        self, harness: ProviderHarness, build: Callable[..., LLMProvider]
    ) -> None:
        provider = build(Outcome.STRUCTURED)

        value, usage = await provider.complete_structured(
            SYSTEM, USER, Score, model=harness.model, temperature=0
        )

        assert value == Score(score=0.8, reason="fits")
        assert usage == RECORDED_USAGE
        assert harness.requests_made() == 1

    async def test_invalid_answer_is_repaired_once(
        self, harness: ProviderHarness, build: Callable[..., LLMProvider]
    ) -> None:
        provider = build(Outcome.STRUCTURED_INVALID, Outcome.STRUCTURED)

        value, usage = await provider.complete_structured(
            SYSTEM, USER, Score, model=harness.model, temperature=0
        )

        assert value == Score(score=0.8, reason="fits")
        assert usage == RECORDED_USAGE + RECORDED_USAGE
        assert usage.requests == 2
        assert harness.requests_made() == 2

    async def test_invalid_answer_after_repair_raises_with_summed_usage(
        self, harness: ProviderHarness, build: Callable[..., LLMProvider]
    ) -> None:
        provider = build(Outcome.STRUCTURED_INVALID, Outcome.STRUCTURED_INVALID)

        with pytest.raises(LLMInvalidOutputError) as info:
            await provider.complete_structured(
                SYSTEM, USER, Score, model=harness.model, temperature=0
            )

        assert info.value.usage == Usage(24, 6, requests=2)
        assert (info.value.provider, info.value.model) == (harness.name, harness.model)
        assert harness.requests_made() == 2

    @pytest.mark.parametrize(
        ("outcome", "error_type"),
        [
            (Outcome.AUTH, LLMAuthError),
            (Outcome.RATE_LIMIT, LLMRateLimitError),
            (Outcome.UNAVAILABLE, LLMUnavailableError),
            (Outcome.INVALID_REQUEST, LLMInvalidRequestError),
        ],
        ids=lambda value: value.value if isinstance(value, Outcome) else value.__name__,
    )
    @pytest.mark.parametrize("call", ["complete", "complete_structured"])
    async def test_errors_are_typed_and_name_provider_and_model(
        self,
        harness: ProviderHarness,
        build: Callable[..., LLMProvider],
        outcome: Outcome,
        error_type: type[LLMError],
        call: str,
    ) -> None:
        provider = build(outcome)

        with pytest.raises(error_type) as info:
            if call == "complete":
                await provider.complete(
                    SYSTEM, USER, model=harness.model, temperature=0, max_tokens=20
                )
            else:
                await provider.complete_structured(
                    SYSTEM, USER, Score, model=harness.model, temperature=0
                )

        assert isinstance(info.value, LLMError)
        assert (info.value.provider, info.value.model) == (harness.name, harness.model)
        assert harness.requests_made() == 1
