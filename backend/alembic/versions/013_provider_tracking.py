"""provider tracking on extraction_jobs / extraction_staging

Revision ID: 013_provider_tracking
Revises: 012_novel_extraction
Create Date: 2026-06-18

为 extraction_jobs / extraction_staging 增加 provider 列，记录本次抽取
使用的 LLM provider（mock / openai / ...），用于区分模拟数据与真实抽取数据。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "013_provider_tracking"
down_revision = "012_novel_extraction"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _has_column(table_name: str, column_name: str) -> bool:
    cols = {c["name"] for c in inspect(_bind()).get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    if not _has_column("extraction_jobs", "provider"):
        op.add_column(
            "extraction_jobs",
            sa.Column("provider", sa.String(50), nullable=False, server_default="mock"),
        )
    if not _has_column("extraction_staging", "provider"):
        op.add_column(
            "extraction_staging",
            sa.Column("provider", sa.String(50), nullable=False, server_default="mock"),
        )


def downgrade() -> None:
    if _has_column("extraction_jobs", "provider"):
        op.drop_column("extraction_jobs", "provider")
    if _has_column("extraction_staging", "provider"):
        op.drop_column("extraction_staging", "provider")
