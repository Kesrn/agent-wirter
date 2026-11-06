"""outlines pacing 字段 — K-4 节奏标记

Revision ID: 026_outline_pacing
Revises: 025_hidden_thread_status
Create Date: 2026-07-07
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "026_outline_pacing"
down_revision = "025_hidden_thread_status"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


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


def upgrade() -> None:
    if not _has_table("outlines"):
        return
    _add_column_if_missing("outlines", sa.Column("pacing", sa.String(30), nullable=True))
    _add_column_if_missing("outlines", sa.Column("tension_level", sa.Integer, nullable=True))
    _add_column_if_missing("outlines", sa.Column("target_scene_count", sa.Integer, nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_outlines_pacing ON outlines (pacing)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_outlines_pacing")
    if _has_table("outlines"):
        for col in ["target_scene_count", "tension_level", "pacing"]:
            _drop_column_if_exists("outlines", col)
