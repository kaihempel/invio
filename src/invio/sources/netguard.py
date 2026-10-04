"""Network guard for the safe HTTP client (scheme check, address validation, pinning)."""

from typing import Protocol

__all__ = ["Resolver"]


class Resolver(Protocol):
    """Resolves a host name to IP address strings; injectable so tests need no DNS."""

    async def resolve(self, host: str, port: int) -> list[str]: ...
