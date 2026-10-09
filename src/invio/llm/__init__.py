"""Provider-neutral LLM layer.

Modules:

* ``base``: ``Usage``, the ``LLMProvider`` protocol, typed errors and shared helpers
  (``structured_with_repair``, ``with_timeout``, ``require_api_key``).
* ``registry``: model registry (prices, context windows) merged from ``models.d/*.yaml``.
* ``factory``: ``@register_provider``, provider discovery, ``get_provider`` and ``resolve``.
* ``fake``: scripted ``FakeProvider`` for tests.
* ``http_retry``: retry policy, retry loop and the error classification shared by the
  HTTP-based providers.
* ``loop_clients``: the per-event-loop SDK client table shared by the HTTP-based providers.
* ``mistral``: the Mistral provider (official SDK, retries with backoff).
* ``openai``: the OpenAI provider (official SDK, Responses API, shared retry loop).
* ``anthropic``: the Anthropic provider (official SDK, Messages API, forced tool for structured
  output, shared retry loop).
* ``google``: the Google provider (official SDK, ``generateContent``, converted JSON schema for
  structured output, shared retry loop).
* ``models.d/``: one registry file per provider.

This module deliberately imports nothing: provider discovery imports every module of the
package, so eager imports here would create import cycles.
"""
