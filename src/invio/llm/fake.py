"""Scripted, recording fake provider for tests.

The script is a sequence of steps consumed one per provider request: a :class:`FakeReply`
answers, a :class:`FakeDelay` answers after a pause (timeout tests) and an :class:`LLMError`
instance is raised. Every request is recorded in :attr:`FakeProvider.requests`.
"""

import asyncio
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Self

from pydantic import BaseModel

from invio.config.settings import Settings
from invio.llm.base import LLMError, LLMProvider, Usage, structured_with_repair, with_timeout

DEFAULT_USAGE = Usage(10, 5)


@dataclass(frozen=True)
class FakeReply:
    """A scripted answer."""

    text: str
    usage: Usage = DEFAULT_USAGE


@dataclass(frozen=True)
class FakeDelay:
    """A scripted answer that is only given after ``seconds``."""

    seconds: float
    then: FakeReply


FakeStep = FakeReply | FakeDelay | LLMError


@dataclass(frozen=True)
class FakeRequest:
    """One recorded provider request (``max_tokens`` is ``None`` for structured requests)."""

    system: str
    user: str
    model: str
    temperature: float
    max_tokens: int | None


class FakeScriptExhaustedError(AssertionError):
    """The fake was asked for more requests than its script contains."""


class FakeProvider:
    """An :class:`~invio.llm.base.LLMProvider` that plays back a script."""

    def __init__(
        self, script: Iterable[FakeStep], *, timeout_seconds: float = 60.0, name: str = "fake"
    ) -> None:
        self._script: deque[FakeStep] = deque(script)
        self.timeout_seconds = timeout_seconds
        self.name = name
        self.requests: list[FakeRequest] = []

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Build a fake with an empty script (for registry/factory tests)."""
        return cls([], timeout_seconds=settings.llm_timeout_seconds)

    async def _play(self) -> tuple[str, Usage]:
        if not self._script:
            raise FakeScriptExhaustedError(
                f"FakeProvider script exhausted after {len(self.requests)} requests"
            )
        step = self._script.popleft()
        if isinstance(step, LLMError):
            raise step
        if isinstance(step, FakeDelay):
            await asyncio.sleep(step.seconds)
            step = step.then
        return step.text, step.usage

    async def _request(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int | None
    ) -> tuple[str, Usage]:
        self.requests.append(FakeRequest(system, user, model, temperature, max_tokens))
        return await with_timeout(
            self._play(), seconds=self.timeout_seconds, provider=self.name, model=model
        )

    async def complete(
        self, system: str, user: str, *, model: str, temperature: float, max_tokens: int
    ) -> tuple[str, Usage]:
        return await self._request(
            system, user, model=model, temperature=temperature, max_tokens=max_tokens
        )

    async def complete_structured[T: BaseModel](
        self, system: str, user: str, schema: type[T], *, model: str, temperature: float
    ) -> tuple[T, Usage]:
        async def request(system_text: str, user_text: str) -> tuple[str, Usage]:
            return await self._request(
                system_text, user_text, model=model, temperature=temperature, max_tokens=None
            )

        return await structured_with_repair(
            request, system, user, schema, provider=self.name, model=model
        )

    async def aclose(self) -> None:
        """Nothing to release: the fake opens no connections."""


if TYPE_CHECKING:
    _check: type[LLMProvider] = FakeProvider
