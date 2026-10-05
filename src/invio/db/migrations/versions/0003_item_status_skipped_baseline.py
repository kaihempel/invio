"""item status skipped_baseline

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05 10:00:00

Items left out by baseline mode get their own status instead of ``skipped_irrelevant`` with
``last_error = 'baseline'``. The downgrade maps them back to exactly that. SQLite rebuilds the
table to replace its CHECK constraint; ``_items_table`` describes it as of 0002 so the rebuild
needs no reflection and also works offline (``--sql``).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql
from sqlalchemy.sql.elements import conv

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = (
    "new",
    "extracted",
    "skipped_keyword",
    "skipped_irrelevant",
    "relevant",
    "summarized",
    "failed",
)
_NEW = (*_OLD, "skipped_baseline")

_PK = sa.BigInteger().with_variant(sa.Integer(), "sqlite")
_UTC_DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql", "mariadb")

_items = sa.table(
    "items",
    sa.column("status", sa.String()),
    sa.column("last_error", sa.Text()),
)


def _status_type(values: tuple[str, ...]) -> sa.Enum:
    return sa.Enum(*values, name="item_status", create_constraint=True)


def _items_table(status_values: tuple[str, ...]) -> sa.Table:
    """The ``items`` table as created by 0001, with ``status_values`` as status enum."""
    return sa.Table(
        "items",
        sa.MetaData(naming_convention={"ck": "ck_%(table_name)s_%(constraint_name)s"}),
        sa.Column("id", _PK, autoincrement=True, nullable=False),
        sa.Column("job_id", _PK, nullable=False),
        sa.Column("run_id", _PK, nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("url_hash", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "type",
            sa.Enum("article", "video", name="item_type", create_constraint=True),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("published_at", _UTC_DATETIME, nullable=True),
        sa.Column("teaser", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=True),
        sa.Column("raw_content", sa.Text(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("status", _status_type(status_values), server_default="new", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("relevance", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("created_at", _UTC_DATETIME, nullable=False),
        sa.Column("updated_at", _UTC_DATETIME, nullable=False),
        sa.CheckConstraint("attempts >= 0", name=conv("ck_items_attempts_non_negative")),
        sa.CheckConstraint(
            "relevance >= 0 AND relevance <= 1", name=conv("ck_items_relevance_range")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=conv("fk_items_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=conv("fk_items_run_id_runs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=conv("pk_items")),
        sa.UniqueConstraint("job_id", "url_hash", name=conv("uq_items_job_id_url_hash")),
        sa.Index("ix_items_run_id", "run_id"),
    )


def _set_status_values(old: tuple[str, ...], new: tuple[str, ...]) -> None:
    # copy_from only matters where batch mode rebuilds the table (SQLite); MariaDB alters in place.
    with op.batch_alter_table("items", copy_from=_items_table(old)) as batch:
        batch.alter_column(
            "status",
            existing_type=_status_type(old),
            type_=_status_type(new),
            existing_server_default="new",
            existing_nullable=False,
        )


def upgrade() -> None:
    _set_status_values(_OLD, _NEW)
    op.execute(
        _items.update()
        .where(_items.c.status == "skipped_irrelevant", _items.c.last_error == "baseline")
        .values(status="skipped_baseline", last_error=None)
    )


def downgrade() -> None:
    op.execute(
        _items.update()
        .where(_items.c.status == "skipped_baseline")
        .values(status="skipped_irrelevant", last_error="baseline")
    )
    _set_status_values(_NEW, _OLD)
