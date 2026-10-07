"""Retry delay after a failed run (#23).

The first failure of a job is retried after 1 hour, every further consecutive failure doubles
the delay (2 h, 4 h, 8 h, 16 h) up to 24 hours. A retry never lands later than the job's regular
next slot (the caller takes the earlier of the two). Pure: no database, no clock.
"""

from datetime import timedelta
from typing import Final

__all__ = ["RETRY_BASE", "RETRY_MAX", "RETRY_STREAK_CAP", "retry_delay"]

RETRY_BASE: Final = timedelta(hours=1)
RETRY_MAX: Final = timedelta(hours=24)
# From this streak on the delay is RETRY_MAX; larger streaks are clamped before the shift.
RETRY_STREAK_CAP: Final = 6


def retry_delay(streak: int) -> timedelta:
    """The delay before the next attempt after ``streak`` consecutive failures (>= 1)."""
    if streak < 1:
        raise ValueError(f"streak must be >= 1, got {streak}")
    delay: timedelta = RETRY_BASE * 2 ** (min(streak, RETRY_STREAK_CAP) - 1)
    return min(delay, RETRY_MAX)
