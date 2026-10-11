"""Loopback-only socket guard shared by the pytest fixture and the subprocess bootstrap.

No pytest dependency and only the standard library, so ``sitecustomize`` can import it in every
``invio`` process the e2e smoke test starts. Test-only; it must never be packaged.

Wrapped: ``socket.socket.connect`` / ``connect_ex`` and the name lookups ``getaddrinfo``,
``gethostbyname`` and ``gethostbyname_ex``. Not wrapped: UDP ``sendto`` / ``sendmsg`` on an
unconnected socket and ``gethostbyaddr``; nothing in invio uses them.
"""

import ipaddress
import socket
import sys
from collections.abc import Callable
from typing import Any

__all__ = ["ExternalNetworkError", "install", "is_local"]

MESSAGE = "external network access blocked"
_LOOPBACK_NAMES = frozenset({"localhost", ""})


class ExternalNetworkError(AssertionError):
    """A connection or lookup outside loopback was attempted."""


def is_local(host: object) -> bool:
    """Whether ``host`` is ``None`` (the local host), ``localhost`` or a loopback address."""
    if host is None:
        return True  # getaddrinfo(None, port) resolves the local host
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False


def install(blocked: list[str], *, echo_stderr: bool = False) -> Callable[[], None]:
    """Patch the socket module; return a function that restores it.

    Every refused host is appended to ``blocked`` and raised as :class:`ExternalNetworkError`.
    With ``echo_stderr`` the refusal is also printed, so a subprocess reports it even when the
    product code swallows the exception.
    """
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_gethostbyname = socket.gethostbyname
    real_gethostbyname_ex = socket.gethostbyname_ex

    def check(host: object) -> None:
        if not is_local(host):
            blocked.append(str(host))
            if echo_stderr:
                print(f"{MESSAGE}: {host}", file=sys.stderr, flush=True)
            raise ExternalNetworkError(f"{MESSAGE}: {host}")

    def check_address(sock: socket.socket, address: Any) -> None:
        if sock.family != socket.AF_UNIX:
            check(address[0])

    def connect(self: socket.socket, address: Any) -> None:
        check_address(self, address)
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        check_address(self, address)
        return real_connect_ex(self, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        check(host)
        return real_getaddrinfo(host, *args, **kwargs)

    def gethostbyname(host: str) -> str:
        check(host)
        return real_gethostbyname(host)

    def gethostbyname_ex(host: str) -> Any:
        check(host)
        return real_gethostbyname_ex(host)

    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    socket.getaddrinfo = getaddrinfo
    socket.gethostbyname = gethostbyname
    socket.gethostbyname_ex = gethostbyname_ex

    def uninstall() -> None:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]
        socket.getaddrinfo = real_getaddrinfo
        socket.gethostbyname = real_gethostbyname
        socket.gethostbyname_ex = real_gethostbyname_ex

    return uninstall
