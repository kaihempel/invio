"""Provider-neutral retry loop and helpers shared by the HTTP-based LLM providers.

A provider adapts its own SDK exceptions to a :class:`Failure` (a classified failed attempt) in
a ``classify`` function and hands one request attempt to :func:`run_with_retries`. The loop
retries rate limits (429), server errors (5xx) and connection failures with exponential backoff
and jitter (:class:`RetryPolicy`); a ``Retry-After`` hint is honoured up to
``max_retry_after`` seconds. Failures that the classifier marks as not retryable (timeouts,
authentication, rejected requests, ...) are raised at once.

Nothing here depends on an SDK: the helpers take plain values (status, response headers, body
text, transport exception classes). Only :mod:`invio.llm.base` and the standard library are
imported. :func:`classify_status`, :func:`classify_transport`, :func:`timeout_failure` and
:func:`bad_response_failure` build the failures every provider shares, so a provider's
``classify`` only has to unwrap its SDK exceptions and check its own special cases.

Error hygiene: the loop raises ``failure.error`` outside the ``except`` block, so the SDK
exception is neither its ``__cause__`` nor its ``__context__``, and its warning log record
(``llm.retry``) carries the provider, model, attempt, status, failure kind and wait only.

This is the *inner*, per-request layer. It is separate from the graph-level
:class:`~invio.llm.retry.RetryingProvider`, which retries whole calls.
"""

import logging
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

from invio.llm.base import (
    LLMAuthError,
    LLMError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMUnavailableError,
)

logger = logging.getLogger("invio.llm")

MAX_DETAIL_CHARS = 300
_DELTA_SECONDS = re.compile(r"[0-9]+(?:\.[0-9]+)?")
AUTH_STATUSES = frozenset({401, 403})
_RATE_LIMIT_STATUS = 429
_CLIENT_ERRORS = range(400, 500)
_SERVER_ERRORS = range(500, 600)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retry behaviour of an HTTP-based provider (Mistral, OpenAI).

    ``max_retries`` >= 0 (attempts = ``max_retries + 1``); ``base_delay`` > 0 seconds (the wait
    before retry *n* is ``base_delay * 2**(n-1)`` scaled by the jitter); ``0 <= jitter < 1``;
    ``max_retry_after`` > 0 seconds (a larger ``Retry-After`` fails immediately).
    """

    max_retries: int = 3
    base_delay: float = 1.0
    jitter: float = 0.25
    max_retry_after: float = 60.0

    def __post_init__(self) -> None:
        if self.max_retries < 0:
            raise ValueError(f"max_retries must be >= 0, got {self.max_retries}")
        if not self.base_delay > 0:
            raise ValueError(f"base_delay must be > 0, got {self.base_delay}")
        if not 0 <= self.jitter < 1:
            raise ValueError(f"jitter must be >= 0 and < 1, got {self.jitter}")
        if not self.max_retry_after > 0:
            raise ValueError(f"max_retry_after must be > 0, got {self.max_retry_after}")


@dataclass(frozen=True, slots=True)
class Failure:
    """One classified failed attempt.

    ``kind`` is a short label for logs, ``retryable`` says whether another attempt may help,
    ``error`` is the typed error to raise when giving up, ``retry_after`` the seconds requested
    by the provider (``math.inf`` when too large for a float).
    """

    kind: str
    retryable: bool
    error: LLMError
    status: int | None = None
    retry_after: float | None = None


Classifier = Callable[[Exception, str, Callable[[], datetime]], Failure | None]
"""``classify(exc, model, now)``: a :class:`Failure`, or ``None`` if ``exc`` is not recognized."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def retry_after(headers: Mapping[str, str], now: Callable[[], datetime]) -> float | None:
    """Return the seconds requested by a ``Retry-After`` header, or ``None``.

    Header names are matched case-insensitively. Accepts ASCII integer or decimal seconds or an
    HTTP date (relative to ``now()``, floored at 0). Seconds too large for a float give
    ``math.inf`` (above any cap). Anything else is ignored.
    """
    raw = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if raw is None:
        return None
    if _DELTA_SECONDS.fullmatch(raw.strip()):
        return float(raw)
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - now()).total_seconds())


def sanitize_detail(text: str) -> str:
    """Return ``text`` with non-printable characters and whitespace runs collapsed, truncated."""
    printable = "".join(ch if ch.isprintable() else " " for ch in text)
    return " ".join(printable.split())[:MAX_DETAIL_CHARS]


def describe(what: str, model: str, status: int | None, detail: str = "") -> str:
    """Build an error message from a summary, the status, the model and a sanitized detail."""
    where = f"HTTP {status}, model {model}" if status is not None else f"model {model}"
    return f"{what} ({where}){': ' + detail if detail else ''}"


def timeout_failure(label: str, *, provider: str, model: str) -> Failure:
    """Return the (not retried) failure of a request that timed out."""
    message = describe(f"{label} request timed out", model, None)
    return Failure("timeout", False, LLMUnavailableError(message, provider=provider, model=model))


def bad_response_failure(
    label: str, *, provider: str, model: str, cause: str, status: int | None = None
) -> Failure:
    """Return the (not retried) failure of a response the client cannot use."""
    message = describe(f"{label} returned an unexpected response{cause}", model, status)
    error = LLMUnavailableError(message, provider=provider, model=model)
    return Failure("bad_response", False, error, status)


def classify_transport(
    cause: BaseException,
    *,
    label: str,
    provider: str,
    model: str,
    unsendable: tuple[type[BaseException], ...],
    bad_response: tuple[type[BaseException], ...],
) -> Failure:
    """Classify a connection-level failure by the transport exception that caused it.

    ``unsendable`` failures happened before anything was sent and ``bad_response`` ones mean the
    server answered with something the client cannot use: retrying helps neither. Every other
    cause is a retryable connection failure.
    """
    name = type(cause).__name__
    if isinstance(cause, unsendable):
        message = describe(f"{label} request could not be sent ({name})", model, None)
        error = LLMUnavailableError(message, provider=provider, model=model)
        return Failure("unsendable", False, error)
    if isinstance(cause, bad_response):
        return bad_response_failure(label, provider=provider, model=model, cause=f" ({name})")
    message = describe(f"{label} connection failed ({name})", model, None)
    return Failure("connection", True, LLMUnavailableError(message, provider=provider, model=model))


def classify_status(
    status: int,
    *,
    label: str,
    env_var: str,
    provider: str,
    model: str,
    detail: str,
    headers: Mapping[str, str],
    now: Callable[[], datetime],
    wait_hint: float | None = None,
) -> Failure:
    """Classify an HTTP error status the same way for every provider.

    401/403 are authentication failures naming ``env_var``; 429 is a retryable rate limit that
    honours ``Retry-After``, or ``wait_hint`` (a provider-specific wait in seconds that takes
    precedence) when given; 5xx are retryable server errors; other 4xx are rejected requests;
    anything else is an unexpected response. Provider-specific cases (an exhausted quota) are
    checked by the caller first. ``detail`` must already be sanitized (:func:`sanitize_detail`).
    """
    if status in AUTH_STATUSES:
        message = describe(f"{label} rejected the API key; check {env_var}", model, status)
        return Failure("auth", False, LLMAuthError(message, provider=provider, model=model), status)
    if status == _RATE_LIMIT_STATUS:
        wait = wait_hint if wait_hint is not None else retry_after(headers, now)
        message = describe(f"{label} rate limit exceeded", model, status, detail)
        finite_wait = wait if wait is not None and math.isfinite(wait) else None
        limited = LLMRateLimitError(
            message, provider=provider, model=model, retry_after=finite_wait
        )
        return Failure("rate_limit", True, limited, status, wait)
    if status in _SERVER_ERRORS:
        message = describe(f"{label} server error", model, status, detail)
        error = LLMUnavailableError(message, provider=provider, model=model)
        return Failure("server", True, error, status)
    if status not in _CLIENT_ERRORS:
        return bad_response_failure(label, provider=provider, model=model, cause="", status=status)
    message = describe(f"{label} rejected the request", model, status, detail)
    invalid = LLMInvalidRequestError(message, provider=provider, model=model, status=status)
    return Failure("invalid_request", False, invalid, status)


def strict_schema(node: object) -> object:
    """Return a copy of a JSON Schema with ``additionalProperties: false`` on every object.

    The strict structured-output modes of the providers expect closed objects. Free-form maps
    (an object with a schema-valued ``additionalProperties``, e.g. ``dict[str, int]``) are left
    as they are; closing them would silently turn them into always-empty objects.
    """
    if isinstance(node, dict):
        closed = {key: strict_schema(value) for key, value in node.items()}
        if closed.get("type") == "object" and not isinstance(
            closed.get("additionalProperties"), dict
        ):
            closed["additionalProperties"] = False
        return closed
    if isinstance(node, list):
        return [strict_schema(value) for value in node]
    return node


def wait_before_retry(
    failure: Failure,
    attempt: int,
    policy: RetryPolicy,
    uniform: Callable[[float, float], float],
) -> float | None:
    """Return the seconds to wait before the next attempt, or ``None`` to give up."""
    if not failure.retryable or attempt > policy.max_retries:
        return None
    if failure.retry_after is None:
        jitter = uniform(-policy.jitter, policy.jitter)
        return float(policy.base_delay * 2 ** (attempt - 1) * (1 + jitter))
    if failure.retry_after > policy.max_retry_after:
        return None
    return failure.retry_after


async def run_with_retries[R](
    attempt: Callable[[], Awaitable[R]],
    *,
    classify: Classifier,
    policy: RetryPolicy,
    provider: str,
    model: str,
    sleep: Callable[[float], Awaitable[None]],
    uniform: Callable[[float, float], float],
    now: Callable[[], datetime],
) -> R:
    """Await ``attempt()`` until it succeeds, retrying the failures ``classify`` allows.

    An :class:`~invio.llm.base.LLMError` raised by ``attempt`` (a deadline, an answer without
    text) is already typed and never retried; exceptions ``classify`` does not recognize
    propagate unchanged.
    """
    number = 0
    while True:
        number += 1
        try:
            return await attempt()
        except LLMError:
            raise  # already typed (deadline, no answer text): never retried
        except Exception as exc:
            failure = classify(exc, model, now)
            if failure is None:
                raise
        # Raised outside the handler, so the SDK exception is not kept as __context__.
        wait = wait_before_retry(failure, number, policy, uniform)
        if wait is None:
            raise failure.error
        logger.warning(
            "llm.retry",
            extra={
                "provider": provider,
                "model": model,
                "attempt": number,
                "status": failure.status,
                "failure": failure.kind,
                "wait_s": round(wait, 3),
            },
        )
        await sleep(wait)
