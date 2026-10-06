"""Typed, versioned payload stored on ``notifications.payload``.

The payload snapshots everything a retry needs besides the digest body, so a retried mail has
the same subject and footer as the first attempt even if the job was renamed or edited since.
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["DigestStats", "NotificationPayload"]


class DigestStats(BaseModel):
    """Numbers shown in the mail footer, as measured at the first delivery."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    items_found: int | None = Field(default=None, ge=0)
    items_included: int = Field(ge=0)
    duration_seconds: float | None = Field(default=None, ge=0)


class NotificationPayload(BaseModel):
    """Version 1 of the notification payload; unknown keys and versions are rejected."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    job_name: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    digest_date: date
    is_empty: bool
    stats: DigestStats
