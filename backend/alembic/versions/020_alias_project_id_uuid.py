"""fix character_alias_clusters.project_id type to uuid

Revision ID: 020_alias_project_id_uuid
Revises: 019_character_alias_clusters
Create Date: 2026-06-25

迁移 019 把 character_alias_clusters.project_id 建成了 String(36)，
但其它表（character_profile 等）在 Postgres 是 uuid 类型，导致查询时
`varchar = uuid` 类型不匹配失败。此迁移把列改成 uuid，与其它表一致。
SQLite 不受影响（CHAR(36) 兼容）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "020_alias_project_id_uuid"
down_revision = "019_character_alias_clusters"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    inspector = inspect(bind)
    cols = {c["name"]: c["type"] for c in inspector.get_columns("character_alias_clusters")}
    # 仅当 project_id 当前不是 uuid 才改
    if "project_id" in cols and str(cols["project_id"]).lower() != "uuid":
        op.alter_column(
            "character_alias_clusters", "project_id",
            existing_type=sa.String(length=36),
            type_=postgresql.UUID(as_uuid=True),
            existing_nullable=False,
            postgresql_using="project_id::uuid",
        )


def downgrade():
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.alter_column(
        "character_alias_clusters", "project_id",
        existing_type=postgresql.UUID(as_uuid=True),
        type_=sa.String(length=36),
        existing_nullable=False,
        postgresql_using="project_id::text",
    )
