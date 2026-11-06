"""hidden_threads 生命周期字段 — K-3 伏笔状态追踪

Revision ID: 025_hidden_thread_status
Revises: 024_story_arcs
Create Date: 2026-07-07

阶段 K-3a：HiddenThread 生命周期升级。
- status / thread_type / planted_chapter / reveal_chapter / resolved_chapter / payoff_summary / risk_level
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "025_hidden_thread_status"
down_revision = "024_story_arcs"
branch_labels = None
depends_on = None


# ── SQLite/PG 兼容辅助（与 023 一致） ──────────────

def _bind():
    return op.get_bind()


def _dialect_name() -> str:
    return _bind().dialect.name


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    return {c["name"] for c in inspect(_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_table(table_name) and column_name in _columns(table_name):
        op.drop_column(table_name, column_name)


def _drop_index_if_exists(index_name: str) -> None:
    op.execute(f"DROP INDEX IF EXISTS {index_name}")


def upgrade() -> None:
    if not _has_table("hidden_threads"):
        return

    _add_column_if_missing(
        "hidden_threads",
        sa.Column("status", sa.String(20), nullable=False, server_default="PLANNED"),
    )
    _add_column_if_missing("hidden_threads", sa.Column("thread_type", sa.String(30), nullable=True))
    _add_column_if_missing("hidden_threads", sa.Column("planted_chapter", sa.Integer, nullable=True))
    _add_column_if_missing("hidden_threads", sa.Column("reveal_chapter", sa.Integer, nullable=True))
    _add_column_if_missing("hidden_threads", sa.Column("resolved_chapter", sa.Integer, nullable=True))
    _add_column_if_missing("hidden_threads", sa.Column("payoff_summary", sa.Text, nullable=True))
    _add_column_if_missing("hidden_threads", sa.Column("risk_level", sa.String(20), nullable=True))

    op.execute("CREATE INDEX IF NOT EXISTS ix_hidden_threads_status ON hidden_threads (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_hidden_threads_planted_chapter ON hidden_threads (planted_chapter)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_hidden_threads_reveal_chapter ON hidden_threads (reveal_chapter)")


def downgrade() -> None:
    _drop_index_if_exists("ix_hidden_threads_reveal_chapter")
    _drop_index_if_exists("ix_hidden_threads_planted_chapter")
    _drop_index_if_exists("ix_hidden_threads_status")

    if _has_table("hidden_threads"):
        for col in [
            "risk_level", "payoff_summary", "resolved_chapter",
            "reveal_chapter", "planted_chapter", "thread_type", "status",
        ]:
            _drop_column_if_exists("hidden_threads", col)
