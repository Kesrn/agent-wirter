"""story_arcs table + outlines.story_arc_id/arc_position — K-2 长线结构

Revision ID: 024_story_arcs
Revises: 023_expert_system_v2
Create Date: 2026-07-06

阶段 K-2a：长线结构数据层。
- 新建 story_arcs 表（Volume / Act / Arc 层级）
- outlines 增加 story_arc_id / arc_position
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "024_story_arcs"
down_revision = "023_expert_system_v2"
branch_labels = None
depends_on = None


# ── SQLite/PG 兼容辅助（与 022/023 一致） ──────────────

def _bind():
    return op.get_bind()


def _dialect_name() -> str:
    return _bind().dialect.name


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    return {c["name"] for c in inspect(_bind()).get_columns(table_name)}


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


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_table(table_name) and column_name in _columns(table_name):
        op.drop_column(table_name, column_name)


def _drop_index_if_exists(index_name: str) -> None:
    op.execute(f"DROP INDEX IF EXISTS {index_name}")


def upgrade() -> None:
    # ── 新建 story_arcs 表 ──
    if not _has_table("story_arcs"):
        op.create_table(
            "story_arcs",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("parent_arc_id", _uuid_type(), nullable=True),
            sa.Column("arc_type", sa.String(20), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("summary", sa.Text, nullable=True),
            sa.Column("goal", sa.Text, nullable=True),
            sa.Column("main_conflict", sa.Text, nullable=True),
            sa.Column("start_chapter", sa.Integer, nullable=True),
            sa.Column("end_chapter", sa.Integer, nullable=True),
            sa.Column("order_index", sa.Integer, nullable=False, server_default="0"),
            sa.Column("status", sa.String(20), nullable=False, server_default="PLANNED"),
            sa.Column("metadata", _json_type(), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_story_arcs_project_id ON story_arcs (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_story_arcs_parent_arc_id ON story_arcs (parent_arc_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_story_arcs_start_chapter ON story_arcs (start_chapter)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_story_arcs_end_chapter ON story_arcs (end_chapter)")

    # ── outlines 增加 story_arc_id / arc_position ──
    if _has_table("outlines"):
        _add_column_if_missing("outlines", sa.Column("story_arc_id", _uuid_type(), nullable=True))
        _add_column_if_missing("outlines", sa.Column("arc_position", sa.String(30), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_outlines_story_arc_id ON outlines (story_arc_id)")


def downgrade() -> None:
    _drop_index_if_exists("ix_outlines_story_arc_id")
    if _has_table("outlines"):
        _drop_column_if_exists("outlines", "arc_position")
        _drop_column_if_exists("outlines", "story_arc_id")

    _drop_index_if_exists("ix_story_arcs_end_chapter")
    _drop_index_if_exists("ix_story_arcs_start_chapter")
    _drop_index_if_exists("ix_story_arcs_parent_arc_id")
    _drop_index_if_exists("ix_story_arcs_project_id")
    if _has_table("story_arcs"):
        op.drop_table("story_arcs")
