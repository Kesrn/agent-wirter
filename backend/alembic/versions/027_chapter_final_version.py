"""章节定稿快照指针。

Revision ID: 027_chapter_final_version
Revises: 026_outline_pacing
Create Date: 2026-07-13

``chapters.final_version_id`` 指向用户在最后人工审核确认的 ChapterVersion。
下一章上下文读取该不可变版本，避免后续草稿编辑污染已定稿的衔接内容。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "027_chapter_final_version"
down_revision = "026_outline_pacing"
branch_labels = None
depends_on = None


def _has_column(table_name: str, column_name: str) -> bool:
    return column_name in {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table("chapters") or _has_column("chapters", "final_version_id"):
        return
    op.add_column("chapters", sa.Column("final_version_id", sa.Uuid(), nullable=True))
    op.create_index("ix_chapters_final_version_id", "chapters", ["final_version_id"])


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    if not inspector.has_table("chapters") or not _has_column("chapters", "final_version_id"):
        return
    op.drop_index("ix_chapters_final_version_id", table_name="chapters")
    op.drop_column("chapters", "final_version_id")
