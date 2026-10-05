"""E-mail notifications: digest delivery over SMTP and retry of failed notifications.

``invio.notify`` may import only ``invio.config``, ``invio.db``, ``invio.domain`` and
``invio.log``; it never imports the CLI, graph, scheduling or services layers.
"""

from invio.notify.email import (
    CHANNEL_EMAIL,
    MAX_ATTEMPTS,
    STALE_PENDING_AFTER,
    DatabaseConfigError,
    DeliveryOutcome,
    RetryOutcome,
    RetryResult,
    SmtpMailer,
    deliver_digest,
    missing_smtp_settings,
    open_session_factory,
    retry_failed,
    run_status_after_delivery,
    scrub_error,
    scrub_secret,
)
from invio.notify.payload import DigestStats, NotificationPayload

__all__ = [
    "CHANNEL_EMAIL",
    "MAX_ATTEMPTS",
    "STALE_PENDING_AFTER",
    "DatabaseConfigError",
    "DeliveryOutcome",
    "DigestStats",
    "NotificationPayload",
    "RetryOutcome",
    "RetryResult",
    "SmtpMailer",
    "deliver_digest",
    "missing_smtp_settings",
    "open_session_factory",
    "retry_failed",
    "run_status_after_delivery",
    "scrub_error",
    "scrub_secret",
]
