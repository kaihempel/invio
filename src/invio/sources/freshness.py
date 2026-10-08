"""Age filter shared by the source adapters that carry a publication date."""

from datetime import datetime, timedelta

__all__ = ["age_cutoff", "clamp_or_expire"]


def age_cutoff(now: datetime, max_age_days: int | None) -> datetime | None:
    """The oldest ``published`` date to keep, or ``None`` when there is no age limit."""
    return now - timedelta(days=max_age_days) if max_age_days is not None else None


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
