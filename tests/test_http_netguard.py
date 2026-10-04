"""Unit tests for ``invio.sources.netguard``: scheme check, address classes and ``guard_url``."""

import socket
from ipaddress import IPv4Address, IPv6Address, ip_address, ip_network

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st

from invio.sources.errors import BlockedError, BlockReason, FetchError
from invio.sources.netguard import (
    Origin,
    SystemResolver,
    check_scheme,
    guard_url,
    is_public_address,
    origin_of,
)
from tests.http_helpers import LOOPBACK, FakeResolver


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "0.0.0.0",
        "100.64.0.1",
        "224.0.0.1",
        "255.255.255.255",
        "::1",
        "::",
        "fe80::1",
        "fc00::1",
        "ff02::1",
        "::ffff:127.0.0.1",
        "::ffff:10.0.0.1",
        "2002:7f00:0001::1",  # 6to4 of 127.0.0.1
        "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
        "64:ff9b::a00:1",  # NAT64 of 10.0.0.1
        "::127.0.0.1",  # IPv4-compatible
        "::7f00:1",
        "::a00:5",
        "::1.2.3.4",  # ::/96 is never a public destination
        "fec0::1",  # deprecated site-local
        "5f00::1",  # SRv6 SIDs
        "192.88.99.1",  # deprecated 6to4 relay anycast
        "2002:c058:6301::1",  # 6to4 of 192.88.99.1
    ],
)
def test_non_public_addresses(address: str) -> None:
    assert is_public_address(ip_address(address)) is False


@pytest.mark.parametrize(
    "address", ["93.184.216.34", "2606:4700::1111", "::ffff:93.184.216.34", "64:ff9b::5db8:d822"]
)
def test_public_addresses(address: str) -> None:
    assert is_public_address(ip_address(address)) is True


@given(st.builds(IPv4Address, st.integers(min_value=0, max_value=2**32 - 1)))
def test_ipv4_mapped_has_same_verdict_as_plain_ipv4(address: IPv4Address) -> None:
    assert is_public_address(address) == is_public_address(IPv6Address(f"::ffff:{address}"))


@pytest.mark.parametrize("scheme", ["file", "ftp", "gopher", "data", "javascript", ""])
def test_check_scheme_rejects_other_schemes(scheme: str) -> None:
    url = httpx.URL(f"{scheme}://u:p@example.org/") if scheme else httpx.URL("//example.org/")

    with pytest.raises(BlockedError) as info:
        check_scheme(url)

    assert info.value.reason is BlockReason.UNSUPPORTED_SCHEME
    assert "u:p" not in str(info.value)


@pytest.mark.parametrize("url", ["http://a/", "https://a/", "HTTP://a/", "HtTpS://a/"])
def test_check_scheme_accepts_http_and_https(url: str) -> None:
    check_scheme(httpx.URL(url))


def test_origin_lowercases_host_and_fills_default_ports() -> None:
    assert origin_of(httpx.URL("http://Example.ORG/x")) == Origin("http", "example.org", 80)
    assert origin_of(httpx.URL("https://Example.org/x")) == Origin("https", "example.org", 443)
    assert origin_of(httpx.URL("https://example.org:8443/")) == Origin("https", "example.org", 8443)


async def test_guard_rejects_when_any_address_is_private() -> None:
    resolver = FakeResolver({"mixed.example": ["93.184.216.34", "10.0.0.1"]})

    with pytest.raises(BlockedError) as info:
        await guard_url(httpx.URL("http://mixed.example/"), resolver, ())

    assert info.value.reason is BlockReason.NON_PUBLIC_ADDRESS


async def test_guard_returns_first_validated_address() -> None:
    resolver = FakeResolver({"ok.example": ["93.184.216.34", "2606:4700::1111"]})

    target = await guard_url(httpx.URL("https://ok.example/p"), resolver, ())

    assert target.address == ip_address("93.184.216.34")
    assert target.origin == Origin("https", "ok.example", 443)
    assert str(target.url) == "https://ok.example/p"
    assert resolver.calls == [("ok.example", 443)]


async def test_guard_maps_resolver_failure_to_dns_failed() -> None:
    with pytest.raises(FetchError) as info:
        await guard_url(httpx.URL("http://missing.example/"), FakeResolver({}), ())

    assert info.value.reason == "dns_failed"
    assert not isinstance(info.value, BlockedError)


async def test_guard_treats_empty_answer_as_dns_failed() -> None:
    with pytest.raises(FetchError) as info:
        await guard_url(httpx.URL("http://empty.example/"), FakeResolver({"empty.example": []}), ())

    assert info.value.reason == "dns_failed"


async def test_allow_networks_only_exempts_listed_networks() -> None:
    resolver = FakeResolver({"lo.example": ["127.0.0.1"], "lan.example": ["10.0.0.5"]})

    target = await guard_url(httpx.URL("http://lo.example/"), resolver, LOOPBACK)
    with pytest.raises(BlockedError):
        await guard_url(httpx.URL("http://lan.example/"), resolver, LOOPBACK)

    assert target.address == ip_address("127.0.0.1")


async def test_allow_networks_accepts_other_network_objects() -> None:
    resolver = FakeResolver({"lan.example": ["10.0.0.5"]})

    target = await guard_url(httpx.URL("http://lan.example/"), resolver, [ip_network("10.0.0.0/8")])

    assert target.address == ip_address("10.0.0.5")


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1/", "http://[::1]/", "http://[::ffff:10.0.0.1]/", "http://10.1.2.3:8080/"],
)
async def test_ip_literals_are_checked_without_resolving(url: str) -> None:
    resolver = FakeResolver({})

    with pytest.raises(BlockedError) as info:
        await guard_url(httpx.URL(url), resolver, ())

    assert info.value.reason is BlockReason.NON_PUBLIC_ADDRESS
    assert resolver.calls == []


async def test_public_ip_literal_is_not_resolved() -> None:
    resolver = FakeResolver({})

    target = await guard_url(httpx.URL("http://93.184.216.34/"), resolver, ())

    assert target.address == ip_address("93.184.216.34")
    assert resolver.calls == []


@pytest.mark.parametrize("url", ["http://2130706433/", "http://0x7f.1/"])
async def test_legacy_numeric_hosts_are_resolved_and_blocked(url: str) -> None:
    # getaddrinfo converts numeric forms locally, without a DNS query.
    with pytest.raises(BlockedError):
        await guard_url(httpx.URL(url), SystemResolver(), ())


async def test_system_resolver_resolves_numeric_host() -> None:
    assert await SystemResolver().resolve("127.0.0.1", 80) == ["127.0.0.1"]


async def test_system_resolver_propagates_gaierror() -> None:
    with pytest.raises(socket.gaierror):
        await SystemResolver().resolve("no-such-host.invalid", 80)


@pytest.mark.parametrize(
    "url", ["http://[::7f00:1]/", "http://[::127.0.0.1]/", "http://[fec0::1]/"]
)
async def test_embedded_and_deprecated_ipv6_literals_are_blocked(url: str) -> None:
    with pytest.raises(BlockedError):
        await guard_url(httpx.URL(url), FakeResolver({}), ())


async def test_scoped_ipv6_literal_is_blocked() -> None:
    resolver = FakeResolver({})

    with pytest.raises(BlockedError):
        await guard_url(httpx.URL("http://[2606:4700::1111%25eth0]/"), resolver, ())

    assert resolver.calls == []
