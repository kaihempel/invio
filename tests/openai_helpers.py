"""Offline HTTP harness for the OpenAI provider tests.

The SDK talks through ``httpx``; tests inject an ``httpx.AsyncClient`` backed by a
``MockTransport`` whose handler (:class:`Recorder`) replays recorded response fixtures.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import httpx

from invio.llm.http_retry import RetryPolicy
from invio.llm.openai import OpenAIProvider
from tests.async_helpers import RecordingSleep

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "openai"
API_KEY = "sk-test-SECRET123"
BASE_URL = "https://api.openai.test/v1"

HANG = object()
"""Queue item: the handler awaits for a long time (used to trigger timeouts)."""


def load_fixture(name: str) -> httpx.Response:
    """Build the recorded response ``tests/fixtures/openai/<name>.json``."""
    document = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    body = document["body"]
    headers = dict(document.get("headers", {}))
    if isinstance(body, str):
        return httpx.Response(document["status"], headers=headers, content=body.encode())
    return httpx.Response(document["status"], headers=headers, content=json.dumps(body).encode())


class RecordedRequest(NamedTuple):
    method: str
    path: str
    body: dict[str, Any]
    has_authorization: bool


Reply = str | httpx.Response | Exception | object
"""A fixture name, a response, an exception to raise, or :data:`HANG`."""


class Recorder:
    """``MockTransport`` handler replaying a queue of replies and recording each request."""

    def __init__(self, replies: tuple[Reply, ...]) -> None:
        self._queue = list(replies)
        self.requests: list[RecordedRequest] = []
        self.clients: list[httpx.AsyncClient] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(
            RecordedRequest(
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else {},
                "authorization" in request.headers,
            )
        )
        if not self._queue:
            raise AssertionError(f"Recorder queue exhausted after {len(self.requests)} requests")
        reply = self._queue.pop(0)
        if reply is HANG:
            await asyncio.sleep(30)
            raise AssertionError("hanging request was not cancelled")
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, str):
            return load_fixture(reply)
        assert isinstance(reply, httpx.Response)
        return reply

    @property
    def clients_created(self) -> int:
        return len(self.clients)

    def client_factory(self) -> httpx.AsyncClient:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self))
        self.clients.append(client)
        return client


def recording_options(recorder: Recorder) -> tuple[dict[str, Any], list[float]]:
    """``OpenAIProvider`` keyword arguments for offline tests and the list of requested waits.

    The HTTP transport is the recorder, the retry sleep only records its argument and the
    jitter is zero.
    """
    sleep = RecordingSleep()
    options: dict[str, Any] = {
        "client_factory": recorder.client_factory,
        "base_url": BASE_URL,
        "sleep": sleep,
        "uniform": lambda a, b: 0.0,
    }
    return options, sleep.calls


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
    options, waits = recording_options(recorder)
    overrides = {"uniform": uniform, "sleep": sleep, "now": now}
    options.update({name: value for name, value in overrides.items() if value is not None})
    provider = OpenAIProvider(
        API_KEY, timeout_seconds=timeout_seconds, retry=retry or RetryPolicy(), **options
    )
    return provider, recorder, waits
