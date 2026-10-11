"""Test-only bootstrap for the separate-process e2e test; it must never be packaged.

Python imports ``sitecustomize`` at start-up when its directory is on ``PYTHONPATH``. The
installed ``invio`` command builds its HTTP client without ``allow_networks`` (the safe client
refuses loopback), so this wrapper allows ``127.0.0.0/8`` by default. Only
``invio.pipeline.deps`` is imported here, to keep every process start fast; the modules that
later do ``from invio.pipeline.deps import default_deps`` get the wrapper.

With ``INVIO_E2E_BLOCK_EXTERNAL_NETWORK=1`` the loopback-only socket guard is installed too;
refusals are echoed to stderr.
"""

import functools
import os
from ipaddress import ip_network

if os.environ.get("INVIO_E2E_BLOCK_EXTERNAL_NETWORK") == "1":
    import network_guard

    network_guard.install([], echo_stderr=True)

from invio.pipeline import deps

_original = deps.default_deps


@functools.wraps(_original)
def _default_deps(*args, **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("allow_networks", (ip_network("127.0.0.0/8"),))
    return _original(*args, **kwargs)


deps.default_deps = _default_deps
