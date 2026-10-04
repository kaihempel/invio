"""Custom column types and time helpers for the persistence layer."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, Dialect
from sqlalchemy.dialects import mysql
from sqlalchemy.types import TypeDecorator, TypeEngine


def utcnow() -> datetime:
    """Return the current time as an aware UTC datetime."""
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Store naive UTC in the database, return aware UTC datetimes in Python.

    Writing a naive datetime raises :class:`ValueError` so local-time bugs surface at the
    boundary instead of silently shifting instants.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:  # Any: SQLAlchemy API
        if dialect.name in ("mysql", "mariadb"):
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime not allowed; use an aware datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(
        self,
        value: Any | None,  # Any: SQLAlchemy passes the raw driver value
        dialect: Dialect,
    ) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC)  # type: ignore[no-any-return]
