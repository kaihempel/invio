"""job names compare exactly (binary collation on MariaDB/MySQL)

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-04 17:00:00

``utf8mb4_unicode_ci`` made ``jobs.name`` case- and accent-insensitive on MariaDB while SQLite
compares names exactly. SQLite needs no change. The downgrade fails if names that differ only
by case or accents exist by then.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MYSQL_BACKENDS = ("mysql", "mariadb")


def _set_name_collation(collation: str) -> None:
    if op.get_context().dialect.name not in _MYSQL_BACKENDS:
        return
    op.alter_column(
        "jobs",
        "name",
        existing_type=sa.String(length=200),
        type_=mysql.VARCHAR(200, charset="utf8mb4", collation=collation),
        existing_nullable=False,
    )


def upgrade() -> None:
    _set_name_collation("utf8mb4_bin")


def downgrade() -> None:
    _set_name_collation("utf8mb4_unicode_ci")
