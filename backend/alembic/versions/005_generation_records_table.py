"""generation records table

Revision ID: 005_generation_records_table
Revises: 004_documents_tables
Create Date: 2026-05-25
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "005_generation_records_table"
down_revision = "004_documents_tables"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return inspect(op.get_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def upgrade() -> None:
    if not _has_table("generation_records"):
        op.create_table(
            "generation_records",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("chapter_id", UUID(as_uuid=True), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True),
            sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=True),
            sa.Column("mode", sa.String(30), nullable=False),
            sa.Column("expert_id", UUID(as_uuid=True), sa.ForeignKey("experts.id", ondelete="SET NULL"), nullable=True),
            sa.Column("direction", sa.Text, nullable=True),
            sa.Column("user_note", sa.Text, nullable=True),
            sa.Column("target_words", sa.Integer, nullable=True),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("word_count", sa.Integer, server_default="0"),
            sa.Column("status", sa.String(20), nullable=False, server_default="candidate"),
            sa.Column("accepted_version_id", UUID(as_uuid=True), nullable=True),
            sa.Column("review_results", JSONB, nullable=True),
            sa.Column("request_params", JSONB, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    else:
        _add_column_if_missing("generation_records", sa.Column("review_results", JSONB, nullable=True))
        _add_column_if_missing("generation_records", sa.Column("request_params", JSONB, nullable=True))
        _add_column_if_missing("generation_records", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
        _add_column_if_missing("generation_records", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()))

    op.execute("CREATE INDEX IF NOT EXISTS ix_generation_records_project_created ON generation_records (project_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_generation_records_chapter_created ON generation_records (chapter_id, created_at)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_generation_records_document_created ON generation_records (document_id, created_at)")


def downgrade() -> None:
    op.drop_index("ix_generation_records_document_created", table_name="generation_records")
    op.drop_index("ix_generation_records_chapter_created", table_name="generation_records")
    op.drop_index("ix_generation_records_project_created", table_name="generation_records")
    op.drop_table("generation_records")
