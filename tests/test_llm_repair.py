"""Tests for structured output validation and the single repair request."""

import json
import logging
from collections.abc import Awaitable

import pytest

from invio.llm import fake as fake_module
from invio.llm.base import (
    LLMInvalidOutputError,
    LLMRateLimitError,
    LLMUnavailableError,
    Usage,
    with_timeout,
)
from invio.llm.fake import FakeDelay, FakeProvider, FakeReply
from tests.llm_helpers import VALID_SCORE_JSON, Score

INVALID_JSON = '{"score": 5, "reason": "x"}'


async def _structured(fake: FakeProvider) -> tuple[Score, Usage]:
    return await fake.complete_structured("rate", "the item", Score, model="m", temperature=0)


async def test_valid_first_answer_needs_one_request() -> None:
    fake = FakeProvider([FakeReply(VALID_SCORE_JSON, Usage(10, 5))])

    value, usage = await _structured(fake)

    assert value == Score(score=0.8, reason="relevant")
    assert usage == Usage(10, 5, 1)
    assert len(fake.requests) == 1


async def test_invalid_then_valid_repairs_exactly_once() -> None:
    fake = FakeProvider(
        [FakeReply(INVALID_JSON, Usage(10, 5)), FakeReply(VALID_SCORE_JSON, Usage(20, 7))]
    )

    value, usage = await _structured(fake)

    assert value.score == 0.8
    assert usage == Usage(30, 12, 2)
    assert len(fake.requests) == 2
    repair = fake.requests[1].user
    assert "the item" in repair
    assert INVALID_JSON in repair
    assert "score: " in repair


async def test_requests_carry_the_json_schema_instruction() -> None:
    fake = FakeProvider([FakeReply(INVALID_JSON), FakeReply(VALID_SCORE_JSON)])

    await _structured(fake)

    for request in fake.requests:
        assert request.system.startswith("rate")
        assert json.dumps(Score.model_json_schema()) in request.system


async def test_invalid_twice_raises_after_exactly_two_requests() -> None:
    fake = FakeProvider(
        [FakeReply(INVALID_JSON, Usage(10, 5)), FakeReply('{"score": 0.5}', Usage(11, 6))]
    )

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(fake)

    assert len(fake.requests) == 2
    assert "reason: Field required" in info.value.errors
    assert "score" not in info.value.errors.replace("reason", "")
    assert info.value.usage == Usage(21, 11, 2)
    assert "Score" in str(info.value)


async def test_prose_answer_is_repaired_once() -> None:
    fake = FakeProvider([FakeReply("The score is high"), FakeReply(VALID_SCORE_JSON)])

    value, usage = await _structured(fake)

    assert value.reason == "relevant"
    assert usage.requests == 2
    assert "(root): " in fake.requests[1].user


async def test_prose_twice_raises_invalid_output_after_one_repair() -> None:
    fake = FakeProvider([FakeReply("high", Usage(4, 1)), FakeReply("still prose", Usage(6, 2))])

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(fake)

    assert len(fake.requests) == 2
    assert info.value.errors.startswith("(root): ")
    assert info.value.usage == Usage(10, 3, 2)


async def test_timeout_bounds_each_request_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """FR-022: original and repair request get one full timeout each (no shared budget)."""
    bounds: list[float] = []

    async def recording_timeout[R](
        awaitable: Awaitable[R], *, seconds: float, provider: str, model: str
    ) -> R:
        bounds.append(seconds)
        return await with_timeout(awaitable, seconds=seconds, provider=provider, model=model)

    monkeypatch.setattr(fake_module, "with_timeout", recording_timeout)
    fake = FakeProvider([FakeReply(INVALID_JSON), FakeReply(VALID_SCORE_JSON)], timeout_seconds=7)

    await _structured(fake)

    assert bounds == [7, 7]


@pytest.mark.parametrize("fence", ["```json\n{}\n```", "```\n{}\n```", "  ```json\n{}\n```\n"])
async def test_markdown_fence_is_stripped(fence: str) -> None:
    fake = FakeProvider([FakeReply(fence.replace("{}", VALID_SCORE_JSON))])

    value, usage = await _structured(fake)

    assert value.score == 0.8
    assert usage.requests == 1


async def test_unclosed_fence_is_not_valid() -> None:
    fake = FakeProvider([FakeReply("```json\n" + VALID_SCORE_JSON), FakeReply(VALID_SCORE_JSON)])

    _, usage = await _structured(fake)

    assert usage.requests == 2


async def test_provider_error_in_repair_propagates_unchanged() -> None:
    fake = FakeProvider([FakeReply(INVALID_JSON), LLMRateLimitError("slow down")])

    with pytest.raises(LLMRateLimitError, match="slow down"):
        await _structured(fake)


async def test_timeout_on_repair_request_is_unavailable() -> None:
    fake = FakeProvider(
        [FakeReply(INVALID_JSON), FakeDelay(1, FakeReply(VALID_SCORE_JSON))],
        timeout_seconds=0.01,
    )

    with pytest.raises(LLMUnavailableError):
        await _structured(fake)


async def test_answer_text_never_appears_in_error_or_problems() -> None:
    leaky = '{"score": 0.5, "reason": 12345, "extra": "SECRET_ANSWER_TOKEN"}'
    fake = FakeProvider([FakeReply(leaky), FakeReply(leaky)])

    with pytest.raises(LLMInvalidOutputError) as info:
        await _structured(fake)

    assert "SECRET_ANSWER_TOKEN" not in str(info.value)
    assert "SECRET_ANSWER_TOKEN" not in info.value.errors
    assert "12345" not in info.value.errors


async def test_repair_is_logged_once_without_answer_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    leaky = '{"score": 9, "reason": "SECRET_ANSWER_TOKEN"}'
    fake = FakeProvider([FakeReply(leaky), FakeReply(VALID_SCORE_JSON)])

    with caplog.at_level(logging.INFO, logger="invio.llm"):
        await _structured(fake)

    records = [r for r in caplog.records if r.getMessage() == "llm.repair"]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].__dict__["schema"] == "Score"
    assert "score: " in records[0].__dict__["errors"]
    assert "SECRET_ANSWER_TOKEN" not in str(records[0].__dict__)
