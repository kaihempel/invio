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
        self.clients_created = 0

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

    def client_factory(self) -> httpx2.AsyncClient:
        self.clients_created += 1
        return httpx2.AsyncClient(transport=httpx2.MockTransport(self))


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
    waits: list[float] = []

    async def record_sleep(seconds: float) -> None:
        waits.append(seconds)

    kwargs: dict[str, Any] = {}
    if now is not None:
        kwargs["now"] = now
    provider = MistralProvider(
        API_KEY,
        timeout_seconds=timeout_seconds,
        retry=retry if retry is not None else RetryPolicy(),
        client_factory=recorder.client_factory,
        sleep=sleep if sleep is not None else record_sleep,
        uniform=uniform if uniform is not None else (lambda a, b: 0.0),
        **kwargs,
    )
    return provider, recorder, waits
