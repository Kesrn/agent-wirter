"""character alias clusters table (project-level alias normalization config)

Revision ID: 019_character_alias_clusters
Revises: 018_character_appearance
Create Date: 2026-06-22

新增 character_alias_clusters 表：项目级人物别名归一簇，替代硬编码的
_CHARACTER_ALIAS_CLUSTERS。merge / QA 阶段按本项目簇把异体名归一到
canonical_name。候选探测只给建议，必须人工确认后才入此表。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "019_character_alias_clusters"
down_revision = "018_character_appearance"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    table_name = "character_alias_clusters"
    existing_tables = set(inspector.get_table_names())
    if table_name in existing_tables:
        return

    op.create_table(
        table_name,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("project_id", sa.String(36), nullable=False, index=True),
        sa.Column("canonical_name", sa.String(200), nullable=False),
        sa.Column("aliases", sa.JSON(), nullable=True),
        sa.Column("source", sa.String(50), nullable=False, server_default="manual"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.UniqueConstraint("project_id", "canonical_name",
                            name="uq_character_alias_project_canonical"),
    )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "character_alias_clusters" in existing_tables:
        op.drop_table("character_alias_clusters")
