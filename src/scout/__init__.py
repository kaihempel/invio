"""Scout: CLI-based AI research system."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("scout")
except PackageNotFoundError:  # pragma: no cover - only when running from an uninstalled tree
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
