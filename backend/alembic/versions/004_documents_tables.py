"""documents and document_versions tables

Revision ID: 004_documents_tables
Revises: 003_chapter_versions
Create Date: 2024-01-01

Creates Document and DocumentVersion tables for article/copywriting projects.
Migrates existing article project data from chapters → documents,
chapter_versions → document_versions (idempotent).
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy import inspect, text

revision = "004_documents_tables"
down_revision = "003_chapter_versions"
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
    # --- Create documents table ---
    if not _has_table("documents"):
        op.create_table(
            "documents",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("project_id", UUID(as_uuid=True), nullable=False, index=True),
            sa.Column("title", sa.String(200), nullable=False),
            sa.Column("content", sa.Text, nullable=True),
            sa.Column("position", sa.Integer, server_default="0"),
            sa.Column("word_count", sa.Integer, server_default="0"),
            sa.Column("status", sa.String(20), server_default="draft"),
            sa.Column("metadata", JSONB, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    else:
        _add_column_if_missing("documents", sa.Column("metadata", JSONB, nullable=True))
        _add_column_if_missing("documents", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
        _add_column_if_missing("documents", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()))

    # --- Create document_versions table ---
    if not _has_table("document_versions"):
        op.create_table(
            "document_versions",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True),
            sa.Column("version_number", sa.Integer, nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("word_count", sa.Integer, server_default="0"),
            sa.Column("source", sa.String(50), server_default="manual"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    else:
        _add_column_if_missing("document_versions", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))

    # --- Migrate existing article project data (idempotent) ---
    # Only migrate if there are article projects with chapters not yet migrated.
    # Uses a left-join to skip documents that already exist for a given chapter.
    op.execute(text("""
        INSERT INTO documents (id, project_id, title, content, position, word_count, status, created_at, updated_at)
        SELECT
            c.id,
            c.project_id,
            c.title,
            c.content,
            c.sequence_number AS position,
            c.word_count,
            COALESCE(c.status, 'draft'),
            c.created_at,
            COALESCE(c.updated_at, c.created_at)
        FROM chapters c
        JOIN projects p ON p.id = c.project_id
        WHERE p.mode = 'article'
          AND NOT EXISTS (
              SELECT 1 FROM documents d WHERE d.id = c.id
          )
    """))

    op.execute(text("""
        INSERT INTO document_versions (id, document_id, version_number, content, word_count, source, created_at)
        SELECT
            cv.id,
            cv.chapter_id AS document_id,
            cv.version_number,
            COALESCE(cv.content, ''),
            cv.word_count,
            cv.source,
            cv.created_at
        FROM chapter_versions cv
        JOIN chapters c ON c.id = cv.chapter_id
        JOIN projects p ON p.id = c.project_id
        WHERE p.mode = 'article'
          AND NOT EXISTS (
              SELECT 1 FROM document_versions dv WHERE dv.id = cv.id
          )
    """))


def downgrade() -> None:
    op.drop_table("document_versions")
    op.drop_table("documents")
