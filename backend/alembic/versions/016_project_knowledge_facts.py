"""project knowledge facts

Revision ID: 016_project_knowledge_facts
Revises: 015_expert_skill_dir
Create Date: 2026-06-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "016_project_knowledge_facts"
down_revision = "015_expert_skill_dir"
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
    if not _has_table("project_knowledge_facts"):
        op.create_table(
            "project_knowledge_facts",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_id", _uuid_type(), nullable=False),
            sa.Column("chunk_id", _uuid_type(), nullable=True),
            sa.Column("fact_type", sa.String(50), nullable=False),
            sa.Column("subject", sa.String(120), nullable=False),
            sa.Column("predicate", sa.String(80), nullable=False),
            sa.Column("object", sa.String(120), nullable=False),
            sa.Column("confidence", sa.String(30), nullable=False, server_default="explicit"),
            sa.Column("evidence_text", sa.Text, nullable=False, server_default=""),
            sa.Column("evidence_start", sa.Integer, nullable=True),
            sa.Column("evidence_end", sa.Integer, nullable=True),
            sa.Column("extractor", sa.String(80), nullable=False, server_default="rule.character_system.v1"),
            sa.Column("metadata", _json_type(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_knowledge_facts_project_id ON project_knowledge_facts (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_knowledge_facts_source_id ON project_knowledge_facts (source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_knowledge_facts_chunk_id ON project_knowledge_facts (chunk_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_knowledge_facts_fact_type ON project_knowledge_facts (fact_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_knowledge_facts_subject ON project_knowledge_facts (subject)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_knowledge_facts_predicate ON project_knowledge_facts (predicate)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_knowledge_facts_object ON project_knowledge_facts (object)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_knowledge_facts_confidence ON project_knowledge_facts (confidence)")


def downgrade() -> None:
    if _has_table("project_knowledge_facts"):
        op.execute("DROP INDEX IF EXISTS ix_project_knowledge_facts_confidence")
        op.execute("DROP INDEX IF EXISTS ix_project_knowledge_facts_object")
        op.execute("DROP INDEX IF EXISTS ix_project_knowledge_facts_predicate")
        op.execute("DROP INDEX IF EXISTS ix_project_knowledge_facts_subject")
        op.execute("DROP INDEX IF EXISTS ix_project_knowledge_facts_fact_type")
        op.execute("DROP INDEX IF EXISTS ix_project_knowledge_facts_chunk_id")
        op.execute("DROP INDEX IF EXISTS ix_project_knowledge_facts_source_id")
        op.execute("DROP INDEX IF EXISTS ix_project_knowledge_facts_project_id")
    if _has_table("project_knowledge_facts"):
        op.drop_table("project_knowledge_facts")
