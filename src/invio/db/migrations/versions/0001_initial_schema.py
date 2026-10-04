"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-10-04 08:43:46
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

UTC_DATETIME = sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql", "mariadb")

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("config", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("next_run_at", UTC_DATETIME, nullable=True),
        sa.Column("locked_until", UTC_DATETIME, nullable=True),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
        sa.UniqueConstraint("name", name=op.f("uq_jobs_name")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    with op.batch_alter_table("jobs", schema=None) as batch_op:
        batch_op.create_index(
            "ix_jobs_enabled_next_run_at", ["enabled", "next_run_at"], unique=False
        )

    op.create_table(
        "runs",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "running",
                "succeeded",
                "partial",
                "failed",
                name="run_status",
                create_constraint=True,
            ),
            server_default="running",
            nullable=False,
        ),
        sa.Column("started_at", UTC_DATETIME, nullable=False),
        sa.Column("finished_at", UTC_DATETIME, nullable=True),
        sa.Column("stats", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_runs_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    with op.batch_alter_table("runs", schema=None) as batch_op:
        batch_op.create_index("ix_runs_job_id_started_at", ["job_id", "started_at"], unique=False)

    op.create_table(
        "digests",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column(
            "body",
            sa.Text()
            .with_variant(mysql.MEDIUMTEXT(), "mariadb")
            .with_variant(mysql.MEDIUMTEXT(), "mysql"),
            nullable=False,
        ),
        sa.Column("item_ids", sa.JSON(), nullable=False),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_digests_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_digests_run_id_runs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_digests")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    with op.batch_alter_table("digests", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_digests_job_id"), ["job_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_digests_run_id"), ["run_id"], unique=False)

    op.create_table(
        "items",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "url_hash",
            sa.CHAR(length=64).with_variant(
                mysql.CHAR(64, charset="ascii", collation="ascii_bin"), "mysql", "mariadb"
            ),
            nullable=False,
        ),
        sa.Column(
            "type",
            sa.Enum("article", "video", name="item_type", create_constraint=True),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("published_at", UTC_DATETIME, nullable=True),
        sa.Column("teaser", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.CHAR(length=64), nullable=True),
        sa.Column(
            "raw_content",
            sa.Text()
            .with_variant(mysql.MEDIUMTEXT(), "mariadb")
            .with_variant(mysql.MEDIUMTEXT(), "mysql"),
            nullable=True,
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "new",
                "extracted",
                "skipped_keyword",
                "skipped_irrelevant",
                "relevant",
                "summarized",
                "failed",
                name="item_status",
                create_constraint=True,
            ),
            server_default="new",
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("relevance", sa.Numeric(precision=3, scale=2), nullable=True),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.Column("updated_at", UTC_DATETIME, nullable=False),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_items_attempts_non_negative")),
        sa.CheckConstraint(
            "relevance >= 0 AND relevance <= 1", name=op.f("ck_items_relevance_range")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_items_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_items_run_id_runs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_items")),
        sa.UniqueConstraint("job_id", "url_hash", name=op.f("uq_items_job_id_url_hash")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    with op.batch_alter_table("items", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_items_run_id"), ["run_id"], unique=False)

    op.create_table(
        "llm_usage",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("purpose", sa.String(length=64), nullable=True),
        sa.Column("input_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("cost_usd", sa.Numeric(precision=12, scale=6), nullable=True),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.CheckConstraint(
            "input_tokens >= 0", name=op.f("ck_llm_usage_input_tokens_non_negative")
        ),
        sa.CheckConstraint(
            "output_tokens >= 0", name=op.f("ck_llm_usage_output_tokens_non_negative")
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_llm_usage_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_llm_usage_run_id_runs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_llm_usage")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    with op.batch_alter_table("llm_usage", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_llm_usage_job_id"), ["job_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_llm_usage_run_id"), ["run_id"], unique=False)

    op.create_table(
        "notifications",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            autoincrement=True,
            nullable=False,
        ),
        sa.Column("job_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("digest_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=True),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("recipient", sa.String(length=320), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "sent",
                "failed",
                "skipped",
                name="notification_status",
                create_constraint=True,
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column("sent_at", UTC_DATETIME, nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("created_at", UTC_DATETIME, nullable=False),
        sa.ForeignKeyConstraint(
            ["digest_id"],
            ["digests.id"],
            name=op.f("fk_notifications_digest_id_digests"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"], ["jobs.id"], name=op.f("fk_notifications_job_id_jobs"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_notifications_run_id_runs"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notifications")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_unicode_ci",
        mysql_engine="InnoDB",
    )
    with op.batch_alter_table("notifications", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_notifications_digest_id"), ["digest_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_job_id"), ["job_id"], unique=False)
        batch_op.create_index(batch_op.f("ix_notifications_run_id"), ["run_id"], unique=False)


def downgrade() -> None:
    # Indexes are dropped with their tables; dropping FK-backing indexes first fails on MariaDB
    # (errno 1553).
    op.drop_table("notifications")
    op.drop_table("llm_usage")
    op.drop_table("items")
    op.drop_table("digests")
    op.drop_table("runs")
    op.drop_table("jobs")
