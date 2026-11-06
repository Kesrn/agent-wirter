"""character appearance table and profile stats

Revision ID: 018_character_appearance
Revises: 017_extraction_job_control
Create Date: 2026-06-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "018_character_appearance"
down_revision = "017_extraction_job_control"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)

    # 1. character_profile 补字段
    cp_cols = {c["name"] for c in inspector.get_columns("character_profile")}
    for col_name, col_type in [
        ("first_seen_chapter", sa.Integer()),
        ("last_seen_chapter", sa.Integer()),
        ("appearance_count", sa.Integer()),
    ]:
        if col_name not in cp_cols:
            default_val = 0 if col_name == "appearance_count" else None
            op.add_column("character_profile", sa.Column(col_name, col_type, nullable=True,
                                                           server_default=str(default_val) if default_val is not None else None))

    # 2. 新建 character_appearance 表
    table_name = "character_appearance"
    existing_tables = set(inspector.get_table_names())
    if table_name not in existing_tables:
        op.create_table(
            table_name,
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("project_id", sa.String(36), nullable=False, index=True),
            sa.Column("source_id", sa.String(36), nullable=True, index=True),
            sa.Column("chapter_id", sa.String(36), nullable=True, index=True),
            sa.Column("character_id", sa.String(36), nullable=True, index=True),
            sa.Column("character_name", sa.String(255), nullable=False, index=True),
            sa.Column("canonical_name", sa.String(255), nullable=False, index=True),
            sa.Column("chapter_no", sa.Integer(), nullable=False, index=True),
            sa.Column("chapter_title", sa.String(500), nullable=True),
            sa.Column("role_in_chapter", sa.String(100), nullable=True),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("evidence_text", sa.Text(), nullable=False),
            sa.Column("importance", sa.Integer(), nullable=False, server_default="2"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="0.8"),
            sa.Column("origin", sa.String(50), nullable=False, server_default="llm_extracted"),
            sa.Column("canon_level", sa.String(50), nullable=False, server_default="original"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
            sa.UniqueConstraint("project_id", "source_id", "chapter_no", "canonical_name",
                                name="uq_character_appearance_per_chapter"),
        )


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    if "character_appearance" in existing_tables:
        op.drop_table("character_appearance")

    cp_cols = {c["name"] for c in inspector.get_columns("character_profile")}
    for col_name in ["appearance_count", "last_seen_chapter", "first_seen_chapter"]:
        if col_name in cp_cols:
            op.drop_column("character_profile", col_name)
