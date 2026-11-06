"""世界设定章节归属。

Revision ID: 029_world_entry_chapter_scope
Revises: 028_llm_config_profiles
Create Date: 2026-07-29
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "029_world_entry_chapter_scope"
down_revision = "028_llm_config_profiles"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("world_entries"):
        return
    columns = {column["name"] for column in inspector.get_columns("world_entries")}
    if "chapter_sequence_number" not in columns:
        op.add_column("world_entries", sa.Column("chapter_sequence_number", sa.Integer(), nullable=True))
    indexes = {index["name"] for index in inspect(bind).get_indexes("world_entries")}
    if "ix_world_entries_chapter_sequence_number" not in indexes:
        op.create_index(
            "ix_world_entries_chapter_sequence_number",
            "world_entries",
            ["chapter_sequence_number"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("world_entries"):
        return
    indexes = {index["name"] for index in inspector.get_indexes("world_entries")}
    if "ix_world_entries_chapter_sequence_number" in indexes:
        op.drop_index("ix_world_entries_chapter_sequence_number", table_name="world_entries")
    columns = {column["name"] for column in inspect(bind).get_columns("world_entries")}
    if "chapter_sequence_number" in columns:
        op.drop_column("world_entries", "chapter_sequence_number")
