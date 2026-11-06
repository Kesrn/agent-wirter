"""writing_memory_staging table — Phase H1 写作记忆 staging 数据层

Revision ID: 022_writing_memory_staging
Revises: 021_ai_harness_core
Create Date: 2026-07-01

阶段 H1：写作记忆 staging 数据层。只建表，不接 approve，不写正式表。
不复用 extraction_staging（那是资料源抽取 staging，keyed on source_id）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "022_writing_memory_staging"
down_revision = "021_ai_harness_core"
branch_labels = None
depends_on = None


# ── SQLite/PG 兼容辅助（与 021 一致） ──────────────────

def _bind():
    return op.get_bind()


def _dialect_name() -> str:
    return _bind().dialect.name


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _uuid_type():
    if _dialect_name() == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.CHAR(36)


def _json_type():
    if _dialect_name() == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def _ts_columns():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def upgrade() -> None:
    if not _has_table("writing_memory_staging"):
        op.create_table(
            "writing_memory_staging",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("run_id", _uuid_type(), nullable=True),
            sa.Column("chapter_id", _uuid_type(), nullable=True),
            sa.Column("chapter_version_id", _uuid_type(), nullable=True),
            sa.Column("chapter_sequence_number", sa.Integer, nullable=True),
            sa.Column("memory_type", sa.String(30), nullable=False),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("payload", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("evidence", sa.Text, nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="GENERATED"),
            sa.Column("confirmed_target_type", sa.String(30), nullable=True),
            sa.Column("confirmed_target_id", _uuid_type(), nullable=True),
            sa.Column("reviewed_by", sa.String(100), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("idempotency_key", sa.String(200), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_wms_project_id ON writing_memory_staging (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wms_run_id ON writing_memory_staging (run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wms_chapter_id ON writing_memory_staging (chapter_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wms_chapter_seq ON writing_memory_staging (chapter_sequence_number)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wms_memory_type ON writing_memory_staging (memory_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_wms_status ON writing_memory_staging (status)")
    # 部分唯一索引：idempotency_key 非空时唯一（PG/SQLite 双方言）
    if _dialect_name() == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_wms_idempotency "
            "ON writing_memory_staging (idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
    else:
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_wms_idempotency "
            "ON writing_memory_staging (idempotency_key) WHERE idempotency_key IS NOT NULL"
        )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ux_wms_idempotency")
    op.execute("DROP INDEX IF EXISTS ix_wms_status")
    op.execute("DROP INDEX IF EXISTS ix_wms_memory_type")
    op.execute("DROP INDEX IF EXISTS ix_wms_chapter_seq")
    op.execute("DROP INDEX IF EXISTS ix_wms_chapter_id")
    op.execute("DROP INDEX IF EXISTS ix_wms_run_id")
    op.execute("DROP INDEX IF EXISTS ix_wms_project_id")
    if _has_table("writing_memory_staging"):
        op.drop_table("writing_memory_staging")
