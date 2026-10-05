"""Network guard for the safe HTTP client: scheme check, address validation and pinning.

The guard resolves the host of every hop itself and returns the validated addresses. The client
connects only to those addresses, so the addresses that were checked are the ones that are used
and a DNS answer that changes in between (DNS rebinding) has no effect.
"""

import asyncio
import socket
from collections.abc import Iterable
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Network, IPv6Address, IPv6Network, ip_address
from typing import Final, Literal, NamedTuple, Protocol

import httpx

from invio.sources.errors import BlockedError, BlockReason, FetchError

__all__ = [
    "DEFAULT_PORTS",
    "GuardedTarget",
    "Origin",
    "Resolver",
    "SystemResolver",
    "check_scheme",
    "guard_url",
    "is_public_address",
    "origin_of",
]

# Ranges ``is_global`` may call global, depending on the Python version, that must not be
# contacted: deprecated site-local, SRv6 SIDs and the deprecated 6to4 relay anycast.
_DENIED: Final[tuple[IPv4Network | IPv6Network, ...]] = (
    IPv6Network("fec0::/10"),
    IPv6Network("5f00::/16"),
    IPv4Network("192.88.99.0/24"),
)
_NAT64: Final = IPv6Network("64:ff9b::/96")
DEFAULT_PORTS: Final = {"http": 80, "https": 443}


class Origin(NamedTuple):
    """Scheme, lower-cased host and port: the key for per-origin policies."""

    scheme: Literal["http", "https"]
    host: str
    port: int


class Resolver(Protocol):
    """Resolves a host name to IP address strings; injectable so tests need no DNS."""

    async def resolve(self, host: str, port: int) -> list[str]: ...


class SystemResolver:
    """Resolve through the operating system (``getaddrinfo`` in the event loop's executor)."""

    async def resolve(self, host: str, port: int) -> list[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        # sockaddr[0] is the address string for both AF_INET and AF_INET6.
        return [str(info[4][0]) for info in infos]


@dataclass(frozen=True, slots=True)
class GuardedTarget:
    """A hop's URL (logical, with the original host name) and the addresses to connect to.

    ``addresses`` keeps the resolver's order and is never empty; the client tries them in turn.
    """

    url: httpx.URL
    origin: Origin
    addresses: tuple[IPv4Address | IPv6Address, ...]


def is_public_address(addr: IPv4Address | IPv6Address) -> bool:
    """True only for globally routable unicast addresses.

    IPv6 forms that embed an IPv4 address (IPv4-mapped and 6to4) are judged by the embedded
    address, so ``::ffff:127.0.0.1`` and ``2002:7f00:1::1`` are not a way around the check.
    """
    if any(addr in net for net in _DENIED if net.version == addr.version):
        return False
    if isinstance(addr, IPv6Address):
        if int(addr) >> 32 == 0:
            # ::/96 (IPv4-compatible, plus :: and ::1) is never a real destination, and some
            # stacks still map it to the embedded IPv4 address.
            return False
        if addr.ipv4_mapped is not None:
            # Judge by the embedded address only: ``is_global`` differs between Python versions.
            return is_public_address(addr.ipv4_mapped)
        embedded = addr.sixtofour
        if embedded is None and addr in _NAT64:
            embedded = IPv4Address(int(addr) & 0xFFFF_FFFF)
        if embedded is not None and not is_public_address(embedded):
            return False
    return addr.is_global and not addr.is_multicast


def check_scheme(url: httpx.URL) -> None:
    """Raise :class:`BlockedError` unless ``url`` is http or https."""
    if url.scheme.lower() not in DEFAULT_PORTS:
        raise BlockedError(BlockReason.UNSUPPORTED_SCHEME, url=str(url))


def origin_of(url: httpx.URL) -> Origin:
    """The :class:`Origin` of ``url`` (host lower-cased, default port filled in)."""
    scheme: Literal["http", "https"] = "https" if url.scheme.lower() == "https" else "http"
    # raw_host is the ASCII (IDNA) form, which is what gets resolved and sent as SNI.
    return Origin(scheme, url.raw_host.decode("ascii").lower(), url.port or DEFAULT_PORTS[scheme])


async def guard_url(
    url: httpx.URL,
    resolver: Resolver,
    allow_networks: Iterable[IPv4Network | IPv6Network],
) -> GuardedTarget:
    """Validate the scheme and the resolved addresses of ``url``.

    Standard IP literals are used as they are. Every other host, including legacy numeric forms
    such as ``2130706433`` or ``0x7f.1``, goes to the resolver and its answers are judged like
    any other. All answers must be public (or inside ``allow_networks``): one private address
    is enough to refuse. Raises :class:`BlockedError` or ``FetchError("dns_failed")``.
    """
    check_scheme(url)
    origin = origin_of(url)
    literal = _parse_literal(origin.host)
    if isinstance(literal, IPv6Address) and literal.scope_id is not None:
        raise BlockedError(BlockReason.NON_PUBLIC_ADDRESS, url=str(url))  # link-local zone
    if literal is not None:
        addresses = [literal]
    else:
        try:
            answers = await resolver.resolve(origin.host, origin.port)
            addresses = [ip_address(answer) for answer in answers]
        except (socket.gaierror, ValueError):
            raise FetchError("dns_failed", url=str(url)) from None
        if not addresses:
            raise FetchError("dns_failed", url=str(url))
    allowed = tuple(allow_networks)
    for address in addresses:
        if not is_public_address(address) and not any(address in net for net in allowed):
            raise BlockedError(BlockReason.NON_PUBLIC_ADDRESS, url=str(url))
    return GuardedTarget(url, origin, tuple(addresses))


def _parse_literal(host: str) -> IPv4Address | IPv6Address | None:
    # httpx hosts carry no brackets, so an IPv6 literal parses as it is.
    try:
        return ip_address(host)
    except ValueError:
        return None
