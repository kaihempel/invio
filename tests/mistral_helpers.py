"""Offline HTTP harness for the Mistral provider tests.

The SDK talks through ``httpx2``; tests inject an ``httpx2.AsyncClient`` backed by a
``MockTransport`` whose handler (:class:`Recorder`) replays recorded response fixtures.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import httpx2

from invio.llm.mistral import MistralProvider, RetryPolicy

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mistral"
API_KEY = "sk-test-SECRET123"

HANG = object()
"""Queue item: the handler awaits for a long time (used to trigger timeouts)."""


def load_fixture(name: str) -> httpx2.Response:
    """Build the recorded response ``tests/fixtures/mistral/<name>.json``."""
    document = json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))
    body = document["body"]
    headers = dict(document.get("headers", {}))
    if isinstance(body, str):
        return httpx2.Response(document["status"], headers=headers, content=body.encode())
    return httpx2.Response(document["status"], headers=headers, content=json.dumps(body).encode())


class RecordedRequest(NamedTuple):
    method: str
    path: str
    body: dict[str, Any]
    has_authorization: bool


Reply = str | httpx2.Response | Exception | object
"""A fixture name, a response, an exception to raise, or :data:`HANG`."""


class Recorder:
    """``MockTransport`` handler replaying a queue of replies and recording each request."""

    def __init__(self, replies: tuple[Reply, ...]) -> None:
        self._queue = list(replies)
        self.requests: list[RecordedRequest] = []
        self.clients: list[httpx2.AsyncClient] = []

    async def __call__(self, request: httpx2.Request) -> httpx2.Response:
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
        assert isinstance(reply, httpx2.Response)
        return reply

    @property
    def clients_created(self) -> int:
        return len(self.clients)

    def client_factory(self) -> httpx2.AsyncClient:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(self))
        self.clients.append(client)
        return client


def recording_options(recorder: Recorder) -> tuple[dict[str, Any], list[float]]:
    """``MistralProvider`` keyword arguments for offline tests and the list of requested waits.

    The HTTP transport is the recorder, the retry sleep only records its argument and the
    jitter is zero.
    """
    waits: list[float] = []

    async def record_sleep(seconds: float) -> None:
        waits.append(seconds)

    options: dict[str, Any] = {
        "client_factory": recorder.client_factory,
        "sleep": record_sleep,
        "uniform": lambda a, b: 0.0,
    }
    return options, waits


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
    options, waits = recording_options(recorder)
    overrides = {"uniform": uniform, "sleep": sleep, "now": now}
    options.update({name: value for name, value in overrides.items() if value is not None})
    provider = MistralProvider(
        API_KEY, timeout_seconds=timeout_seconds, retry=retry or RetryPolicy(), **options
    )
    return provider, recorder, waits
