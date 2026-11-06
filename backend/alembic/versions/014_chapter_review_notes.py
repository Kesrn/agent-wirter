"""chapter review notes

Revision ID: 014_chapter_review_notes
Revises: 013_provider_tracking
Create Date: 2026-06-21
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "014_chapter_review_notes"
down_revision = "013_provider_tracking"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _dialect_name() -> str:
    return _bind().dialect.name


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _uuid_type():
    if _dialect_name() == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.CHAR(36)


def _json_type():
    if _dialect_name() == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def upgrade() -> None:
    if not _has_table("chapter_review_notes"):
        op.create_table(
            "chapter_review_notes",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("chapter_id", _uuid_type(), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=False),
            sa.Column("chapter_sequence_number", sa.Integer, nullable=False),
            sa.Column("source_type", sa.String(30), nullable=False, server_default="manual"),
            sa.Column("severity", sa.String(20), nullable=False, server_default="info"),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("resolved", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("metadata", _json_type(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_chapter_review_notes_project_id ON chapter_review_notes (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chapter_review_notes_chapter_id ON chapter_review_notes (chapter_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chapter_review_notes_chapter_sequence_number ON chapter_review_notes (chapter_sequence_number)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chapter_review_notes_source_type ON chapter_review_notes (source_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chapter_review_notes_severity ON chapter_review_notes (severity)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chapter_review_notes_resolved ON chapter_review_notes (resolved)")


def downgrade() -> None:
    if _has_table("chapter_review_notes"):
        op.drop_table("chapter_review_notes")
