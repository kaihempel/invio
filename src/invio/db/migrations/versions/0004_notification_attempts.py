"""notification send attempts

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-05 09:00:00

Adds ``notifications.attempts`` (send attempts started, existing rows read 0) and
``notifications.last_attempt_at`` (start of the latest attempt) for the e-mail notifier's retry
logic. The downgrade drops both columns one by one: a SQLite table rebuild would lose the enum
CHECK constraint on ``status``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

UTC_DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql", "mariadb")

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "notifications",
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column("notifications", sa.Column("last_attempt_at", UTC_DATETIME, nullable=True))


def downgrade() -> None:
    op.drop_column("notifications", "last_attempt_at")
    op.drop_column("notifications", "attempts")
