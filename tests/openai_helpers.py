"""Offline HTTP harness for the OpenAI provider tests (``httpx``; see ``tests.sdk_harness``)."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from invio.llm.http_retry import RetryPolicy
from invio.llm.openai import OpenAIProvider
from tests import sdk_harness
from tests.sdk_harness import HANG as HANG
from tests.sdk_harness import RecordedRequest as RecordedRequest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "openai"
API_KEY = "sk-test-SECRET123"
BASE_URL = "https://api.openai.test/v1"

Reply = str | httpx.Response | Exception | object
"""A fixture name, a response, an exception to raise, or :data:`HANG`."""


def load_fixture(name: str) -> httpx.Response:
    """Build the recorded response ``tests/fixtures/openai/<name>.json``."""
    response: httpx.Response = sdk_harness.load_fixture(httpx, FIXTURE_DIR, name)
    return response


class Recorder(sdk_harness.Recorder):
    def __init__(self, replies: tuple[Reply, ...]) -> None:
        super().__init__(replies, http=httpx, fixture_dir=FIXTURE_DIR)


def recording_options(recorder: Recorder) -> tuple[dict[str, Any], list[float]]:
    """``OpenAIProvider`` keyword arguments for offline tests and the list of requested waits."""
    return sdk_harness.recording_options(recorder, base_url=BASE_URL)


def make_provider(
    *replies: Reply,
    retry: RetryPolicy | None = None,
    timeout_seconds: float = 60.0,
    uniform: Callable[[float, float], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[OpenAIProvider, Recorder, list[float]]:
    """Build a provider on a :class:`Recorder`; the list receives the requested waits."""
    recorder = Recorder(replies)
    provider, waits = sdk_harness.build_provider(
        OpenAIProvider,
        API_KEY,
        recorder,
        retry=retry,
        timeout_seconds=timeout_seconds,
        uniform=uniform,
        sleep=sleep,
        now=now,
        base_url=BASE_URL,
    )
    return provider, recorder, waits
