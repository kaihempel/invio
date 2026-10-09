"""Offline HTTP harness for the Ollama provider tests (``httpx2``; see ``tests.sdk_harness``)."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2

from invio.llm.http_retry import RetryPolicy
from invio.llm.ollama import OllamaProvider
from tests import sdk_harness
from tests.sdk_harness import HANG as HANG

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "ollama"
BASE_URL = "http://ollama.test:11434"

Reply = str | httpx2.Response | Exception | object
"""A fixture name, a response, an exception to raise, or :data:`HANG`."""


class Recorder(sdk_harness.Recorder):
    """Also records the ``httpx2`` timeout configuration of every request."""

    def __init__(self, replies: tuple[Reply, ...]) -> None:
        super().__init__(replies, http=httpx2, fixture_dir=FIXTURE_DIR)
        self.timeouts: list[dict[str, float | None]] = []

    async def __call__(self, request: Any) -> Any:
        self.timeouts.append(dict(request.extensions.get("timeout", {})))
        return await super().__call__(request)


def recording_options(recorder: Recorder) -> tuple[dict[str, Any], list[float]]:
    """``OllamaProvider`` keyword arguments for offline tests and the list of requested waits."""
    return sdk_harness.recording_options(recorder)


def make_provider(
    *replies: Reply,
    retry: RetryPolicy | None = None,
    timeout_seconds: float = 60.0,
    base_url: str = BASE_URL,
    uniform: Callable[[float, float], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[OllamaProvider, Recorder, list[float]]:
    """Build a provider on a :class:`Recorder`; the list receives the requested waits."""
    recorder = Recorder(replies)
    provider, waits = sdk_harness.build_provider(
        OllamaProvider,
        base_url,
        recorder,
        retry=retry,
        timeout_seconds=timeout_seconds,
        uniform=uniform,
        sleep=sleep,
        now=now,
    )
    return provider, recorder, waits
