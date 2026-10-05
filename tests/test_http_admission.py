"""Tests for ``SafeHttpClient.check_target`` and ``admission``: the checks a fetch made outside
the client (a browser navigation) needs, without the client sending the request."""

import asyncio
import logging

import pytest

from invio.sources.http import (
    BlockedError,
    BlockReason,
    FetchError,
    HttpClientConfig,
    SafeHttpClient,
)
from tests.http_helpers import LOOPBACK, FakeResolver, LoopbackServer, Route, server  # noqa: F401

TOLERANCE = 0.05


def make_client(**overrides: object) -> SafeHttpClient:
    fields: dict[str, object] = {"respect_robots": True, "host_interval": 0.2} | overrides
    return SafeHttpClient(HttpClientConfig.model_validate(fields), allow_networks=LOOPBACK)


# --- properties ------------------------------------------------------------------------------


def test_user_agent_and_size_limit_are_exposed() -> None:
    config = HttpClientConfig(max_response_bytes=12345)
    client = SafeHttpClient(config)

    assert client.user_agent == config.user_agent
    assert client.max_response_bytes == 12345


# --- check_target ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", BlockReason.UNSUPPORTED_SCHEME),
        ("ftp://example.org/", BlockReason.UNSUPPORTED_SCHEME),
        ("http://10.0.0.5/", BlockReason.NON_PUBLIC_ADDRESS),
        ("http://127.0.0.1/", BlockReason.NON_PUBLIC_ADDRESS),
        ("http://[::1]:8080/x", BlockReason.NON_PUBLIC_ADDRESS),
    ],
)
async def test_check_target_blocks_forbidden_targets(url: str, reason: BlockReason) -> None:
    async with SafeHttpClient(HttpClientConfig(respect_robots=False)) as client:
        with pytest.raises(BlockedError) as info:
            await client.check_target(url)

    assert info.value.reason is reason


async def test_check_target_passes_an_allowed_loopback_address(server: LoopbackServer) -> None:
    async with make_client() as client:
        await client.check_target(f"{server.base_url}/page")


async def test_check_target_resolves_names_with_the_clients_resolver() -> None:
    resolver = FakeResolver({"private.example": ["10.1.2.3"], "public.example": ["93.184.216.34"]})
    async with SafeHttpClient(HttpClientConfig(), resolver=resolver) as client:
        await client.check_target("https://public.example/x")
        with pytest.raises(BlockedError):
            await client.check_target("https://private.example/x")

    assert [host for host, _ in resolver.calls] == ["public.example", "private.example"]


async def test_check_target_reports_a_failed_lookup() -> None:
    async with SafeHttpClient(HttpClientConfig(), resolver=FakeResolver({})) as client:
        with pytest.raises(FetchError) as info:
            await client.check_target("https://nowhere.example/")

    assert info.value.reason == "dns_failed"


async def test_check_target_rejects_an_unparsable_url() -> None:
    async with SafeHttpClient(HttpClientConfig()) as client:
        with pytest.raises(FetchError) as info:
            await client.check_target("http://")

    assert info.value.reason == "invalid_url"


async def test_check_target_sends_nothing_and_ignores_robots(server: LoopbackServer) -> None:
    server.routes["/robots.txt"] = Route(body=b"User-agent: *\nDisallow: /\n")

    async with make_client() as client:
        await client.check_target(f"{server.base_url}/secret")

    assert server.requests == []


async def test_check_target_does_not_leak_credentials(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.http")
    async with SafeHttpClient(HttpClientConfig()) as client:
        with pytest.raises(BlockedError) as info:
            await client.check_target("http://user:secret@10.0.0.5/x")

    assert "secret" not in str(info.value)
    assert "secret" not in info.value.url
    assert "secret" not in caplog.text


async def test_check_target_does_not_log_per_request(caplog: pytest.LogCaptureFixture) -> None:
    # A page makes many sub-requests; the caller decides what to report.
    caplog.set_level(logging.DEBUG, logger="invio.sources.http")
    async with SafeHttpClient(HttpClientConfig()) as client:
        with pytest.raises(BlockedError):
            await client.check_target("http://10.0.0.5/")

    assert caplog.records == []


# --- admission -------------------------------------------------------------------------------


async def test_admission_yields_for_an_allowed_page(server: LoopbackServer) -> None:
    server.routes["/robots.txt"] = Route(body=b"User-agent: *\nAllow: /\n")
    entered = False

    async with make_client() as client, client.admission(f"{server.base_url}/page"):
        entered = True

    assert entered
    assert server.paths() == ["/robots.txt"]  # robots.txt only: the page itself is not fetched


async def test_admission_refuses_a_page_disallowed_by_robots_before_yielding(
    server: LoopbackServer,
) -> None:
    server.routes["/robots.txt"] = Route(body=b"User-agent: *\nDisallow: /secret\n")
    entered = False

    async with make_client() as client:
        with pytest.raises(BlockedError) as info:
            async with client.admission(f"{server.base_url}/secret/page"):
                entered = True  # pragma: no cover

    assert info.value.reason is BlockReason.BLOCKED_BY_ROBOTS
    assert not entered


async def test_admission_without_robots_check_allows_everything(server: LoopbackServer) -> None:
    server.routes["/robots.txt"] = Route(body=b"User-agent: *\nDisallow: /\n")

    async with (
        make_client(respect_robots=False) as client,
        client.admission(f"{server.base_url}/page"),
    ):
        pass

    assert server.requests == []


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://10.0.0.5/x"])
async def test_admission_refuses_forbidden_targets(url: str) -> None:
    async with SafeHttpClient(HttpClientConfig(respect_robots=False)) as client:
        with pytest.raises(BlockedError):
            async with client.admission(url):
                pass  # pragma: no cover


async def test_admission_strips_userinfo(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.http")
    server.routes["/robots.txt"] = Route(body=b"User-agent: *\nDisallow: /\n")
    url = f"http://user:secret@127.0.0.1:{server.origin_port}/page"

    async with make_client() as client:
        with pytest.raises(BlockedError) as info:
            async with client.admission(url):
                pass  # pragma: no cover

    assert "secret" not in str(info.value)
    assert "secret" not in caplog.text
    assert server.requests[0].headers.get("authorization") is None


async def test_admission_failures_are_logged_once(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.http")
    server.routes["/robots.txt"] = Route(body=b"User-agent: *\nDisallow: /\n")

    async with make_client() as client:
        with pytest.raises(BlockedError):
            async with client.admission(f"{server.base_url}/page?token=abc"):
                pass  # pragma: no cover

    records = [r for r in caplog.records if r.name == "invio.sources.http"]
    assert [r.getMessage() for r in records] == ["http_blocked"]
    assert records[0].__dict__["reason"] == "blocked_by_robots"
    assert "token" not in caplog.text


async def test_an_error_inside_the_block_is_not_logged_by_admission(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="invio.sources.http")

    async with make_client(respect_robots=False) as client:
        with pytest.raises(FetchError):
            async with client.admission(f"{server.base_url}/page"):
                raise FetchError("timeout", url=server.base_url)

    assert caplog.records == []  # the caller of the block logs its own failures


async def test_two_admissions_on_one_origin_are_spaced(server: LoopbackServer) -> None:
    entered: list[float] = []

    async def visit(client: SafeHttpClient) -> None:
        async with client.admission(f"{server.base_url}/page"):
            entered.append(asyncio.get_running_loop().time())

    async with make_client(respect_robots=False) as client:
        await asyncio.gather(visit(client), visit(client))

    first, second = sorted(entered)
    assert second - first >= 0.2 - TOLERANCE


async def test_the_slot_is_held_for_the_whole_block(server: LoopbackServer) -> None:
    server.routes["/other"] = Route(body=b"x")

    async with make_client(respect_robots=False, host_interval=0.01) as client:
        async with client.admission(f"{server.base_url}/page"):
            blocked = asyncio.create_task(client.get(f"{server.base_url}/other"))
            done, _ = await asyncio.wait({blocked}, timeout=0.3)
            assert not done  # same origin: it waits for the block to end
        await asyncio.wait_for(blocked, timeout=5)

    assert server.paths() == ["/other"]
