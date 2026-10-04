"""Provider-neutral LLM layer.

Modules:

* ``base``: ``Usage``, the ``LLMProvider`` protocol, typed errors and shared helpers
  (``structured_with_repair``, ``with_timeout``, ``require_api_key``).
* ``registry``: model registry (prices, context windows) merged from ``models.d/*.yaml``.
* ``factory``: ``@register_provider``, provider discovery, ``get_provider`` and ``resolve``.
* ``fake``: scripted ``FakeProvider`` for tests.
* ``models.d/``: one registry file per provider.

This module deliberately imports nothing: provider discovery imports every module of the
package, so eager imports here would create import cycles.
"""
