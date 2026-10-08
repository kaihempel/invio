"""Age filter shared by the source adapters that carry a publication date."""

from datetime import datetime

__all__ = ["clamp_or_expire"]


def clamp_or_expire(
    published: datetime | None, now: datetime, cutoff: datetime | None
) -> tuple[bool, datetime | None]:
    """``(keep, published)``: a date in the future is clamped to ``now``, one older than
    ``cutoff`` drops the entry, an undated entry is kept."""
    if published is None:
        return True, None
    if published > now:
        return True, now
    if cutoff is not None and published < cutoff:
        return False, published
    return True, published
