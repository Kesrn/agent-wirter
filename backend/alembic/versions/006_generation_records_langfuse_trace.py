"""add langfuse trace id to generation records

Revision ID: 006_generation_trace
Revises: 005_generation_records_table
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "006_generation_trace"
down_revision = "005_generation_records_table"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if "langfuse_trace_id" not in _columns("generation_records"):
        op.add_column("generation_records", sa.Column("langfuse_trace_id", sa.String(64), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_generation_records_langfuse_trace_id ON generation_records (langfuse_trace_id)")


def downgrade() -> None:
    op.drop_index("ix_generation_records_langfuse_trace_id", table_name="generation_records")
    if "langfuse_trace_id" in _columns("generation_records"):
        op.drop_column("generation_records", "langfuse_trace_id")
