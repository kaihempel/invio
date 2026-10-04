"""Tests for the scripted FakeProvider."""

import pytest

from invio.llm.base import (
    LLMAuthError,
    LLMError,
    LLMInvalidOutputError,
    LLMProvider,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
)
from invio.llm.fake import (
    FakeDelay,
    FakeProvider,
    FakeReply,
    FakeRequest,
    FakeScriptExhaustedError,
)
from tests.llm_helpers import VALID_SCORE_JSON, Score, make_settings


async def test_replies_are_returned_in_order_with_their_usage() -> None:
    fake = FakeProvider([FakeReply("A", Usage(1, 2)), FakeReply("B", Usage(3, 4))])

    first = await fake.complete("s", "u", model="m", temperature=0.5, max_tokens=10)
    second = await fake.complete("s", "u", model="m", temperature=0.5, max_tokens=10)

    assert first == ("A", Usage(1, 2))
    assert second == ("B", Usage(3, 4))


def test_reply_default_usage() -> None:
    assert FakeReply("x").usage == Usage(10, 5)


async def test_requests_are_recorded() -> None:
    fake = FakeProvider([FakeReply("A"), FakeReply(VALID_SCORE_JSON)])

    await fake.complete("sys", "usr", model="m1", temperature=0.3, max_tokens=99)
    await fake.complete_structured("sys2", "usr2", Score, model="m2", temperature=0.1)

    assert fake.requests[0] == FakeRequest("sys", "usr", "m1", 0.3, 99)
    structured = fake.requests[1]
    assert (structured.user, structured.model, structured.temperature) == ("usr2", "m2", 0.1)
    assert structured.max_tokens is None


async def test_exhausted_script_raises_assertion_error_with_count() -> None:
    fake = FakeProvider([FakeReply("A")])
    await fake.complete("s", "u", model="m", temperature=0, max_tokens=1)

    with pytest.raises(FakeScriptExhaustedError, match="after 2 requests") as info:
        await fake.complete("s", "u", model="m", temperature=0, max_tokens=1)

    assert isinstance(info.value, AssertionError)


async def test_scripted_error_is_raised_as_is() -> None:
    error = LLMUnavailableError("down")
    fake = FakeProvider([error])

    with pytest.raises(LLMUnavailableError) as info:
        await fake.complete("s", "u", model="m", temperature=0, max_tokens=1)

    assert info.value is error


@pytest.mark.parametrize(
    "error",
    [
        LLMRateLimitError("rl"),
        LLMAuthError("auth"),
        LLMUnavailableError("down"),
        LLMInvalidOutputError("bad", errors="x: y", usage=Usage(1, 1)),
    ],
    ids=lambda e: type(e).__name__,
)
async def test_any_typed_error_can_be_scripted(error: LLMError) -> None:
    fake = FakeProvider([error])

    with pytest.raises(LLMError) as info:
        await fake.complete_structured("s", "u", Score, model="m", temperature=0)

    assert info.value is error
    assert len(fake.requests) == 1


async def test_delay_within_timeout_returns_reply() -> None:
    fake = FakeProvider([FakeDelay(0.001, FakeReply("late"))])

    text, _ = await fake.complete("s", "u", model="m", temperature=0, max_tokens=1)

    assert text == "late"


async def test_delay_beyond_timeout_is_unavailable_and_names_provider() -> None:
    fake = FakeProvider([FakeDelay(1, FakeReply("late"))], timeout_seconds=0.01, name="fakey")

    with pytest.raises(LLMUnavailableError, match="fakey"):
        await fake.complete("s", "u", model="m", temperature=0, max_tokens=1)


async def test_from_settings_has_empty_script_and_configured_timeout() -> None:
    fake = FakeProvider.from_settings(make_settings(llm_timeout_seconds=5))

    with pytest.raises(FakeScriptExhaustedError):
        await fake.complete("s", "u", model="m", temperature=0, max_tokens=1)
    assert fake.timeout_seconds == 5


def test_fake_provider_satisfies_the_protocol() -> None:
    provider: LLMProvider = FakeProvider([])

    assert provider is not None
