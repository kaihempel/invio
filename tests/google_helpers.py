"""Offline HTTP harness for the Google provider tests (``httpx``; see ``tests.sdk_harness``)."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from invio.llm.google import GoogleProvider
from invio.llm.http_retry import RetryPolicy
from invio.llm.registry import ModelRegistry, default_registry
from tests import sdk_harness
from tests.sdk_harness import HANG as HANG

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "google"
API_KEY = "AIza-test-SECRET123"
BASE_URL = "https://generativelanguage.googleapis.test/"

Reply = str | httpx.Response | Exception | object
"""A fixture name, a response, an exception to raise, or :data:`HANG`."""


class Tag(BaseModel):
    name: str
    weight: int = Field(ge=0)


class Nested(BaseModel):
    """A schema with a sub-model used three times and a list of them."""

    main: Tag
    others: list[Tag]
    backup: Tag


class Recorder(sdk_harness.Recorder):
    def __init__(self, replies: tuple[Reply, ...]) -> None:
        super().__init__(replies, http=httpx, fixture_dir=FIXTURE_DIR)


def recording_options(recorder: Recorder) -> tuple[dict[str, Any], list[float]]:
    """``GoogleProvider`` keyword arguments for offline tests and the list of waits."""
    return sdk_harness.recording_options(recorder, base_url=BASE_URL)


def make_provider(
    *replies: Reply,
    retry: RetryPolicy | None = None,
    timeout_seconds: float = 60.0,
    registry: ModelRegistry | None = None,
    uniform: Callable[[float, float], float] | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    now: Callable[[], datetime] | None = None,
) -> tuple[GoogleProvider, Recorder, list[float]]:
    """Build a provider on a :class:`Recorder`; the list receives the requested waits."""
    recorder = Recorder(replies)
    provider, waits = sdk_harness.build_provider(
        GoogleProvider,
        API_KEY,
        recorder,
        retry=retry,
        timeout_seconds=timeout_seconds,
        uniform=uniform,
        sleep=sleep,
        now=now,
        base_url=BASE_URL,
        registry=registry if registry is not None else default_registry(),
    )
    return provider, recorder, waits
