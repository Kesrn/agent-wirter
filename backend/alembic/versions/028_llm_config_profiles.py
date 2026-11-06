"""LLM 多供应商配置档案。

Revision ID: 028_llm_config_profiles
Revises: 027_chapter_final_version
Create Date: 2026-07-28
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


revision = "028_llm_config_profiles"
down_revision = "027_chapter_final_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("llm_configs"):
        return

    columns = {column["name"] for column in inspector.get_columns("llm_configs")}
    if "name" not in columns:
        op.add_column("llm_configs", sa.Column("name", sa.String(100), nullable=True))
    if "is_active" not in columns:
        op.add_column(
            "llm_configs",
            sa.Column("is_active", sa.Boolean(), nullable=True, server_default=sa.false()),
        )

    op.execute(text("""
        UPDATE llm_configs
        SET name = COALESCE(NULLIF(name, ''),
            CASE
                WHEN model_id IS NOT NULL AND model_id <> '' THEN provider || ' · ' || model_id
                ELSE provider
            END)
    """))
    op.execute(text("UPDATE llm_configs SET is_active = TRUE WHERE is_active IS NULL OR is_active = FALSE"))

    indexes = {index["name"]: index for index in inspect(bind).get_indexes("llm_configs")}
    if "ix_llm_configs_user_id" in indexes:
        op.drop_index("ix_llm_configs_user_id", table_name="llm_configs")
    op.create_index("ix_llm_configs_user_id", "llm_configs", ["user_id"], unique=False)
    op.create_index("ix_llm_configs_is_active", "llm_configs", ["is_active"], unique=False)

    if bind.dialect.name == "postgresql":
        op.alter_column("llm_configs", "name", nullable=False)
        op.alter_column("llm_configs", "is_active", nullable=False, server_default=None)
        op.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_configs_active_user "
            "ON llm_configs (user_id) WHERE is_active"
        ))
    elif bind.dialect.name == "sqlite":
        op.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_llm_configs_active_user "
            "ON llm_configs (user_id) WHERE is_active = 1"
        ))


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("llm_configs"):
        return
    op.execute(text("DROP INDEX IF EXISTS uq_llm_configs_active_user"))
    indexes = {index["name"] for index in inspect(bind).get_indexes("llm_configs")}
    if "ix_llm_configs_is_active" in indexes:
        op.drop_index("ix_llm_configs_is_active", table_name="llm_configs")
    if "ix_llm_configs_user_id" in indexes:
        op.drop_index("ix_llm_configs_user_id", table_name="llm_configs")
    op.create_index("ix_llm_configs_user_id", "llm_configs", ["user_id"], unique=True)
    columns = {column["name"] for column in inspect(bind).get_columns("llm_configs")}
    if "is_active" in columns:
        op.drop_column("llm_configs", "is_active")
    if "name" in columns:
        op.drop_column("llm_configs", "name")
