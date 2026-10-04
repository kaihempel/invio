"""Per-origin rate limiting: ``HostRateLimiter`` unit tests and client integration tests."""

import asyncio
from itertools import pairwise

import pytest

from invio.sources.http import FetchResult, HttpClientConfig, SafeHttpClient
from invio.sources.netguard import Origin
from invio.sources.ratelimit import CRAWL_DELAY_CAP, HostRateLimiter, effective_interval
from tests.http_helpers import LOOPBACK, LoopbackServer, Route, second_server, server  # noqa: F401

A = Origin("http", "a.example", 80)
B = Origin("http", "b.example", 80)
TOLERANCE = 0.05


class FakeTime:
    """A clock that only moves when ``sleep`` is called."""

    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        await asyncio.sleep(0)


@pytest.mark.parametrize(
    ("configured", "crawl_delay", "expected"),
    [
        (0.2, 1.0, 1.0),
        (1.0, None, 1.0),
        (1.0, 0.5, 1.0),
        (0.2, 120, CRAWL_DELAY_CAP),
        (45.0, None, 45.0),
        (45.0, 120, 45.0),
    ],
)
def test_effective_interval(configured: float, crawl_delay: float | None, expected: float) -> None:
    assert effective_interval(configured, crawl_delay) == expected


async def test_first_slot_starts_immediately() -> None:
    fake = FakeTime()
    limiter = HostRateLimiter(clock=fake.clock, sleep=fake.sleep)

    async with limiter.slot(A, 1.0):
        pass

    assert fake.sleeps == []


async def test_second_slot_waits_for_the_remaining_interval() -> None:
    fake = FakeTime()
    limiter = HostRateLimiter(clock=fake.clock, sleep=fake.sleep)

    async with limiter.slot(A, 1.0):
        pass
    fake.now += 0.4
    async with limiter.slot(A, 1.0):
        pass

    assert fake.sleeps == [pytest.approx(0.6)]


async def test_no_wait_when_the_interval_has_already_passed() -> None:
    fake = FakeTime()
    limiter = HostRateLimiter(clock=fake.clock, sleep=fake.sleep)

    async with limiter.slot(A, 1.0):
        pass
    fake.now += 5
    async with limiter.slot(A, 1.0):
        pass

    assert fake.sleeps == []


async def test_origins_are_independent() -> None:
    fake = FakeTime()
    limiter = HostRateLimiter(clock=fake.clock, sleep=fake.sleep)

    async with limiter.slot(A, 1.0):
        pass
    async with limiter.slot(B, 1.0):
        pass

    assert fake.sleeps == []


async def test_slot_is_exclusive_and_waiters_are_served_in_order() -> None:
    limiter = HostRateLimiter(sleep=lambda _s: asyncio.sleep(0))
    events: list[str] = []
    release = asyncio.Event()

    async def worker(name: str, hold: bool) -> None:
        async with limiter.slot(A, 0.0):
            events.append(f"start {name}")
            if hold:
                await release.wait()
            events.append(f"end {name}")

    first = asyncio.create_task(worker("1", hold=True))
    await asyncio.sleep(0)
    others = [asyncio.create_task(worker(str(n), hold=False)) for n in (2, 3)]
    await asyncio.sleep(0.01)
    assert events == ["start 1"]  # nobody else starts while the slot is held
    release.set()
    await asyncio.gather(first, *others)

    assert events == ["start 1", "end 1", "start 2", "end 2", "start 3", "end 3"]


async def test_cancelled_waiter_does_not_block_later_waiters() -> None:
    limiter = HostRateLimiter(sleep=lambda _s: asyncio.sleep(0))
    release = asyncio.Event()
    done: list[str] = []

    async def holder() -> None:
        async with limiter.slot(A, 0.0):
            await release.wait()

    async def waiter(name: str) -> None:
        async with limiter.slot(A, 0.0):
            done.append(name)

    hold = asyncio.create_task(holder())
    await asyncio.sleep(0)
    cancelled = asyncio.create_task(waiter("cancelled"))
    later = asyncio.create_task(waiter("later"))
    await asyncio.sleep(0)
    cancelled.cancel()
    release.set()
    await asyncio.gather(hold, later)

    assert done == ["later"]
    assert cancelled.cancelled()


async def test_slot_is_released_when_the_body_raises() -> None:
    limiter = HostRateLimiter(sleep=lambda _s: asyncio.sleep(0))

    with pytest.raises(RuntimeError):
        async with limiter.slot(A, 0.0):
            raise RuntimeError("boom")

    async with asyncio.timeout(1), limiter.slot(A, 0.0):
        pass


# --- client integration ----------------------------------------------------------------------


def make_client(**overrides: object) -> SafeHttpClient:
    fields: dict[str, object] = {"respect_robots": False, "host_interval": 0.2} | overrides
    return SafeHttpClient(HttpClientConfig.model_validate(fields), allow_networks=LOOPBACK)


def starts(server: LoopbackServer) -> list[float]:
    return sorted(request.started for request in server.requests)


async def test_concurrent_requests_to_one_origin_are_spaced(
    server: LoopbackServer,
) -> None:
    server.routes["/a"] = Route(body=b"a")
    server.routes["/b"] = Route(body=b"b")

    async with make_client() as client:
        await asyncio.gather(client.get(f"{server.base_url}/a"), client.get(f"{server.base_url}/b"))

    first, second = starts(server)
    assert second - first >= 0.2 - TOLERANCE


async def test_default_interval_is_one_second(server: LoopbackServer) -> None:
    server.routes["/a"] = Route(body=b"a")
    server.routes["/b"] = Route(body=b"b")
    config = HttpClientConfig(respect_robots=False)

    async with SafeHttpClient(config, allow_networks=LOOPBACK) as client:
        await asyncio.gather(client.get(f"{server.base_url}/a"), client.get(f"{server.base_url}/b"))

    first, second = starts(server)
    assert second - first >= 1.0 - TOLERANCE


async def test_other_origin_is_not_delayed(
    server: LoopbackServer,
    second_server: LoopbackServer,
) -> None:
    for srv in (server, second_server):
        srv.routes["/a"] = Route(body=b"a")
        srv.routes["/b"] = Route(body=b"b")

    async with make_client(host_interval=0.5) as client:
        await asyncio.gather(
            client.get(f"{server.base_url}/a"),
            client.get(f"{server.base_url}/b"),
            client.get(f"{second_server.base_url}/a"),
        )

    _first, delayed = starts(server)
    (other,) = starts(second_server)
    assert other < delayed


async def test_robots_request_and_first_page_are_spaced(
    server: LoopbackServer,
) -> None:
    server.routes["/robots.txt"] = Route(body=b"User-agent: *\nAllow: /\n")
    server.routes["/a"] = Route(body=b"a")

    async with make_client(respect_robots=True) as client:
        await client.get(f"{server.base_url}/a")

    robots_start, page_start = starts(server)
    assert server.paths() == ["/robots.txt", "/a"]
    assert page_start - robots_start >= 0.2 - TOLERANCE


async def test_each_redirect_hop_is_spaced(server: LoopbackServer) -> None:
    server.routes["/a"] = Route(status=302, headers={"Location": "/b"})
    server.routes["/b"] = Route(status=302, headers={"Location": "/c"})
    server.routes["/c"] = Route(body=b"c")

    async with make_client() as client:
        result = await client.get(f"{server.base_url}/a")

    assert isinstance(result, FetchResult)
    times = starts(server)
    assert all(later - earlier >= 0.2 - TOLERANCE for earlier, later in pairwise(times))


async def test_crawl_delay_raises_the_interval(server: LoopbackServer) -> None:
    server.routes["/robots.txt"] = Route(body=b"User-agent: invio\nCrawl-delay: 1\n")
    server.routes["/a"] = Route(body=b"a")
    server.routes["/b"] = Route(body=b"b")

    async with make_client(respect_robots=True) as client:
        await client.get(f"{server.base_url}/a")
        await client.get(f"{server.base_url}/b")

    _robots, first, second = starts(server)
    assert second - first >= 1.0 - TOLERANCE
