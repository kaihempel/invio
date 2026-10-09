"""Offline HTTP harness for the Mistral provider tests (``httpx2``; see ``tests.sdk_harness``)."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx2

from invio.llm.mistral import MistralProvider, RetryPolicy
from tests import sdk_harness
from tests.sdk_harness import HANG as HANG
from tests.sdk_harness import RecordedRequest as RecordedRequest

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mistral"
API_KEY = "sk-test-SECRET123"

Reply = str | httpx2.Response | Exception | object
"""A fixture name, a response, an exception to raise, or :data:`HANG`."""


def load_fixture(name: str) -> httpx2.Response:
    """Build the recorded response ``tests/fixtures/mistral/<name>.json``."""
    response: httpx2.Response = sdk_harness.load_fixture(httpx2, FIXTURE_DIR, name)
    return response


class Recorder(sdk_harness.Recorder):
    def __init__(self, replies: tuple[Reply, ...]) -> None:
        super().__init__(replies, http=httpx2, fixture_dir=FIXTURE_DIR)


def recording_options(recorder: Recorder) -> tuple[dict[str, Any], list[float]]:
    """``MistralProvider`` keyword arguments for offline tests and the list of requested waits."""
    return sdk_harness.recording_options(recorder)


def make_provider(
    *replies: Reply,
    retry: RetryPolicy | None = None,
    timeout_seconds: float = 60.0,
    uniform: Callable[[float, float], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[MistralProvider, Recorder, list[float]]:
    """Build a provider on a :class:`Recorder`; the list receives the requested waits."""
    recorder = Recorder(replies)
    provider, waits = sdk_harness.build_provider(
        MistralProvider,
        API_KEY,
        recorder,
        retry=retry,
        timeout_seconds=timeout_seconds,
        uniform=uniform,
        sleep=sleep,
        now=now,
    )
    return provider, recorder, waits
