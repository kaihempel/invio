"""SQLAlchemy models for the research data store (see specs/002-gh-issue-4/data-model.md).

No ``relationship()`` is declared on purpose: deletes are enforced by the database through
foreign keys (``CASCADE`` for ``job_id``, ``SET NULL`` for ``run_id`` / ``digest_id``).

Timestamp defaults (``utcnow``) are applied by SQLAlchemy, not by the database; writers that
bypass SQLAlchemy must set ``created_at`` / ``updated_at`` / ``started_at`` themselves.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, get_args

from sqlalchemy import (
    CHAR,
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from invio.db.types import UTCDateTime, utcnow
from invio.domain import ItemStatus, ItemType, NotificationStatus, RunStatus

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

TABLE_ARGS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}

# SQLite only autoincrements INTEGER PRIMARY KEY.
PK = BigInteger().with_variant(Integer, "sqlite")
LONG_TEXT = Text().with_variant(mysql.MEDIUMTEXT(), "mysql", "mariadb")


def _enum(cls: type[StrEnum], name: str) -> Enum:
    return Enum(
        cls,
        name=name,
        native_enum=True,
        # Matters for backends without native ENUM (SQLite): emits the named CHECK constraint.
        create_constraint=True,
        validate_strings=True,
        values_callable=lambda e: [m.value for m in e],
    )


class Base(DeclarativeBase):
    """Declarative base with deterministic constraint names."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class Job(Base):
    """A research job; the parent of all its history."""

    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_enabled_next_run_at", "enabled", "next_run_at"),
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    # Binary collation: names compare exactly (case and accents), as on SQLite.
    name: Mapped[str] = mapped_column(
        String(200).with_variant(
            mysql.VARCHAR(200, charset="utf8mb4", collation="utf8mb4_bin"), "mysql", "mariadb"
        ),
        unique=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("1"))
    config: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), default=None)
    next_run_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Run(Base):
    """One execution of a job."""

    __tablename__ = "runs"
    __table_args__ = (Index("ix_runs_job_id_started_at", "job_id", "started_at"), TABLE_ARGS)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(PK, ForeignKey("jobs.id", ondelete="CASCADE"))
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"),
        default=RunStatus.RUNNING,
        server_default=RunStatus.RUNNING.value,
    )
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    stats: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)


class Digest(Base):
    """A rendered digest of items for a job."""

    __tablename__ = "digests"
    __table_args__ = (TABLE_ARGS,)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(PK, ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(
        PK, ForeignKey("runs.id", ondelete="SET NULL"), index=True, default=None
    )
    title: Mapped[str] = mapped_column(String(500))
    body: Mapped[str] = mapped_column(LONG_TEXT)
    item_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Item(Base):
    """A discovered research item, unique per (job, url_hash)."""

    __tablename__ = "items"
    __table_args__ = (
        UniqueConstraint("job_id", "url_hash"),
        CheckConstraint("relevance >= 0 AND relevance <= 1", name="relevance_range"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(PK, ForeignKey("jobs.id", ondelete="CASCADE"))
    run_id: Mapped[int | None] = mapped_column(
        PK, ForeignKey("runs.id", ondelete="SET NULL"), index=True, default=None
    )
    url: Mapped[str] = mapped_column(Text)
    # Binary collation: hashes are compared exactly, never case-insensitively.
    url_hash: Mapped[str] = mapped_column(
        CHAR(64).with_variant(
            mysql.CHAR(64, charset="ascii", collation="ascii_bin"), "mysql", "mariadb"
        )
    )
    type: Mapped[str] = mapped_column(
        Enum(
            *get_args(ItemType),
            name="item_type",
            native_enum=True,
            create_constraint=True,
            validate_strings=True,
        )
    )
    title: Mapped[str] = mapped_column(String(1000))
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    teaser: Mapped[str | None] = mapped_column(Text, default=None)
    content_hash: Mapped[str | None] = mapped_column(CHAR(64), default=None)
    raw_content: Mapped[str | None] = mapped_column(LONG_TEXT, default=None)
    summary: Mapped[str | None] = mapped_column(Text, default=None)
    status: Mapped[ItemStatus] = mapped_column(
        _enum(ItemStatus, "item_status"),
        default=ItemStatus.NEW,
        server_default=ItemStatus.NEW.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    relevance: Mapped[Decimal | None] = mapped_column(Numeric(3, 2, asdecimal=True), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Notification(Base):
    """A notification sent (or to be sent) for a digest."""

    __tablename__ = "notifications"
    __table_args__ = (TABLE_ARGS,)

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(PK, ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(
        PK, ForeignKey("runs.id", ondelete="SET NULL"), index=True, default=None
    )
    digest_id: Mapped[int | None] = mapped_column(
        PK, ForeignKey("digests.id", ondelete="SET NULL"), index=True, default=None
    )
    channel: Mapped[str] = mapped_column(String(32))
    recipient: Mapped[str] = mapped_column(String(320))
    status: Mapped[NotificationStatus] = mapped_column(
        _enum(NotificationStatus, "notification_status"),
        default=NotificationStatus.PENDING,
        server_default=NotificationStatus.PENDING.value,
    )
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    # Send attempts started so far and the start of the latest one (see NotificationRepository).
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    last_attempt_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class LlmUsage(Base):
    """Token usage and estimated cost of one LLM call."""

    __tablename__ = "llm_usage"
    __table_args__ = (
        CheckConstraint("input_tokens >= 0", name="input_tokens_non_negative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_non_negative"),
        TABLE_ARGS,
    )

    id: Mapped[int] = mapped_column(PK, primary_key=True, autoincrement=True)
    job_id: Mapped[int] = mapped_column(PK, ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    run_id: Mapped[int | None] = mapped_column(
        PK, ForeignKey("runs.id", ondelete="SET NULL"), index=True, default=None
    )
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str | None] = mapped_column(String(64), default=None)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6, asdecimal=True), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
