"""Offline HTTP harness shared by the SDK-based LLM provider tests.

Each SDK talks through an httpx-compatible module (``httpx`` or ``httpx2``); tests inject an
``AsyncClient`` of that module backed by a ``MockTransport`` whose handler (:class:`Recorder`)
replays recorded response fixtures. The per-provider modules (``tests/<provider>_helpers.py``)
bind the transport module, the fixture directory and the provider class.
"""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any, NamedTuple

from invio.llm.http_retry import RetryPolicy
from tests.async_helpers import RecordingSleep

HANG = object()
"""Queue item: the handler awaits for a long time (used to trigger timeouts)."""

Reply = object
"""A fixture name, a response, an exception to raise, or :data:`HANG`."""


def load_fixture(http: ModuleType, fixture_dir: Path, name: str) -> Any:
    """Build the recorded response ``<fixture_dir>/<name>.json`` as an ``http.Response``."""
    document = json.loads((fixture_dir / f"{name}.json").read_text(encoding="utf-8"))
    body = document["body"]
    headers = dict(document.get("headers", {}))
    content = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    return http.Response(document["status"], headers=headers, content=content)


class RecordedRequest(NamedTuple):
    method: str
    path: str
    body: dict[str, Any]
    has_authorization: bool
    host: str
    x_api_key: str | None


class Recorder:
    """``MockTransport`` handler replaying a queue of replies and recording each request."""

    def __init__(self, replies: tuple[Reply, ...], *, http: ModuleType, fixture_dir: Path) -> None:
        self._http = http
        self._fixture_dir = fixture_dir
        self._queue = list(replies)
        self.requests: list[RecordedRequest] = []
        self.clients: list[Any] = []

    async def __call__(self, request: Any) -> Any:
        self.requests.append(
            RecordedRequest(
                request.method,
                request.url.path,
                json.loads(request.content) if request.content else {},
                "authorization" in request.headers,
                request.url.host,
                request.headers.get("x-api-key"),
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
            return load_fixture(self._http, self._fixture_dir, reply)
        assert isinstance(reply, self._http.Response)
        return reply

    @property
    def clients_created(self) -> int:
        return len(self.clients)

    def client_factory(self) -> Any:
        client = self._http.AsyncClient(transport=self._http.MockTransport(self))
        self.clients.append(client)
        return client


def recording_options(recorder: Recorder, **extra: Any) -> tuple[dict[str, Any], list[float]]:
    """Provider keyword arguments for offline tests and the list of requested waits.

    The HTTP transport is the recorder, the retry sleep only records its argument and the
    jitter is zero; ``extra`` (such as ``base_url``) is added unchanged.
    """
    sleep = RecordingSleep()
    options: dict[str, Any] = {
        "client_factory": recorder.client_factory,
        "sleep": sleep,
        "uniform": lambda a, b: 0.0,
        **extra,
    }
    return options, sleep.calls


def build_provider[P](
    provider_class: Callable[..., P],
    api_key: str,
    recorder: Recorder,
    *,
    retry: RetryPolicy | None,
    timeout_seconds: float,
    uniform: Callable[[float, float], float] | None,
    sleep: Callable[[float], Awaitable[None]] | None,
    now: Callable[[], datetime] | None,
    **extra: Any,
) -> tuple[P, list[float]]:
    """Build a provider on ``recorder``; the list receives the requested waits.

    ``uniform``, ``sleep`` and ``now`` replace the recording defaults when given; ``extra`` is
    passed to the provider unchanged.
    """
    options, waits = recording_options(recorder, **extra)
    overrides = {"uniform": uniform, "sleep": sleep, "now": now}
    options.update({name: value for name, value in overrides.items() if value is not None})
    provider = provider_class(
        api_key, timeout_seconds=timeout_seconds, retry=retry or RetryPolicy(), **options
    )
    return provider, waits
