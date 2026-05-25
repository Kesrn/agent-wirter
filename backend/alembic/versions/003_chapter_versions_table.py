"""chapter_versions table

Revision ID: 003_chapter_versions
Revises: 002_llm_config
Create Date: 2026-05-18

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "003_chapter_versions"
down_revision: Union[str, None] = "002_llm_config"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chapter_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("chapter_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("word_count", sa.Integer, server_default="0"),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )
    op.create_index(
        "ix_chapter_versions_chapter_id_version_number",
        "chapter_versions",
        ["chapter_id", "version_number"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_chapter_versions_chapter_id_version_number", table_name="chapter_versions")
    op.drop_table("chapter_versions")
