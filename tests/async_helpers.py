"""Async test doubles shared by the retry, provider and pipeline tests."""


class RecordingSleep:
    """Async ``sleep`` stand-in that records the requested waits."""

    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
