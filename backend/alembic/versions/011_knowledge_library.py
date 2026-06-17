"""knowledge library tables

Revision ID: 011_knowledge_library
Revises: 010_story_layers
Create Date: 2026-06-17
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "011_knowledge_library"
down_revision = "010_story_layers"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _dialect_name() -> str:
    return _bind().dialect.name


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(_bind()).get_columns(table_name)}


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _uuid_type():
    if _dialect_name() == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.CHAR(36)


def _json_type():
    if _dialect_name() == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def _float_list_type():
    if _dialect_name() == "postgresql":
        return postgresql.ARRAY(sa.Float)
    return sa.JSON()


def _drop_index_if_exists(index_name: str, table_name: str) -> None:
    indexes = {index["name"] for index in inspect(_bind()).get_indexes(table_name)}
    if index_name in indexes:
        op.drop_index(index_name, table_name=table_name)


def _drop_table_if_exists(table_name: str) -> None:
    if _has_table(table_name):
        op.drop_table(table_name)


def upgrade() -> None:
    if not _has_table("project_sources"):
        op.create_table(
            "project_sources",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("title", sa.String(300), nullable=False),
            sa.Column("source_type", sa.String(30), nullable=False, server_default="upload"),
            sa.Column("content", sa.Text, nullable=False, server_default=""),
            sa.Column("summary", sa.Text, nullable=True),
            sa.Column("key_facts", _json_type(), nullable=True),
            sa.Column("constraints", _json_type(), nullable=True),
            sa.Column("characters", _json_type(), nullable=True),
            sa.Column("keywords", _json_type(), nullable=True),
            sa.Column("tags", _json_type(), nullable=True),
            sa.Column("always_inject", sa.Boolean, nullable=False, server_default=sa.text("false")),
            sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("token_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("metadata", _json_type(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    else:
        _add_column_if_missing("project_sources", sa.Column("summary", sa.Text, nullable=True))
        _add_column_if_missing("project_sources", sa.Column("key_facts", _json_type(), nullable=True))
        _add_column_if_missing("project_sources", sa.Column("constraints", _json_type(), nullable=True))
        _add_column_if_missing("project_sources", sa.Column("characters", _json_type(), nullable=True))
        _add_column_if_missing("project_sources", sa.Column("keywords", _json_type(), nullable=True))
        _add_column_if_missing("project_sources", sa.Column("tags", _json_type(), nullable=True))
        _add_column_if_missing("project_sources", sa.Column("always_inject", sa.Boolean, nullable=False, server_default=sa.text("false")))
        _add_column_if_missing("project_sources", sa.Column("chunk_count", sa.Integer, nullable=False, server_default="0"))
        _add_column_if_missing("project_sources", sa.Column("token_count", sa.Integer, nullable=False, server_default="0"))
        _add_column_if_missing("project_sources", sa.Column("metadata", _json_type(), nullable=True))
        _add_column_if_missing("project_sources", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
        _add_column_if_missing("project_sources", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()))

    op.execute("CREATE INDEX IF NOT EXISTS ix_project_sources_project_id ON project_sources (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_sources_source_type ON project_sources (source_type)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_sources_always_inject ON project_sources (always_inject)")

    if not _has_table("project_source_chunks"):
        op.create_table(
            "project_source_chunks",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_id", _uuid_type(), nullable=False),
            sa.Column("chunk_index", sa.Integer, nullable=False),
            sa.Column("content", sa.Text, nullable=False, server_default=""),
            sa.Column("summary", sa.Text, nullable=True),
            sa.Column("facts", _json_type(), nullable=True),
            sa.Column("constraints", _json_type(), nullable=True),
            sa.Column("keywords", _json_type(), nullable=True),
            sa.Column("token_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("embedding", _float_list_type(), nullable=True),
            sa.Column("metadata", _json_type(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    else:
        _add_column_if_missing("project_source_chunks", sa.Column("summary", sa.Text, nullable=True))
        _add_column_if_missing("project_source_chunks", sa.Column("facts", _json_type(), nullable=True))
        _add_column_if_missing("project_source_chunks", sa.Column("constraints", _json_type(), nullable=True))
        _add_column_if_missing("project_source_chunks", sa.Column("keywords", _json_type(), nullable=True))
        _add_column_if_missing("project_source_chunks", sa.Column("token_count", sa.Integer, nullable=False, server_default="0"))
        _add_column_if_missing("project_source_chunks", sa.Column("embedding", _float_list_type(), nullable=True))
        _add_column_if_missing("project_source_chunks", sa.Column("metadata", _json_type(), nullable=True))
        _add_column_if_missing("project_source_chunks", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
        _add_column_if_missing("project_source_chunks", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()))

    op.execute("CREATE INDEX IF NOT EXISTS ix_project_source_chunks_project_id ON project_source_chunks (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_project_source_chunks_source_id ON project_source_chunks (source_id)")

    if not _has_table("knowledge_qa_sessions"):
        op.create_table(
            "knowledge_qa_sessions",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("title", sa.String(300), nullable=False, server_default="资料问答"),
            sa.Column("summary", sa.Text, nullable=True),
            sa.Column("user_preferences", sa.Text, nullable=True),
            sa.Column("open_questions", sa.Text, nullable=True),
            sa.Column("important_citations", _json_type(), nullable=True),
            sa.Column("message_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    else:
        _add_column_if_missing("knowledge_qa_sessions", sa.Column("summary", sa.Text, nullable=True))
        _add_column_if_missing("knowledge_qa_sessions", sa.Column("user_preferences", sa.Text, nullable=True))
        _add_column_if_missing("knowledge_qa_sessions", sa.Column("open_questions", sa.Text, nullable=True))
        _add_column_if_missing("knowledge_qa_sessions", sa.Column("important_citations", _json_type(), nullable=True))
        _add_column_if_missing("knowledge_qa_sessions", sa.Column("message_count", sa.Integer, nullable=False, server_default="0"))
        _add_column_if_missing("knowledge_qa_sessions", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
        _add_column_if_missing("knowledge_qa_sessions", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()))

    op.execute("CREATE INDEX IF NOT EXISTS ix_knowledge_qa_sessions_project_id ON knowledge_qa_sessions (project_id)")

    if not _has_table("knowledge_qa_messages"):
        op.create_table(
            "knowledge_qa_messages",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("session_id", _uuid_type(), nullable=False),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("role", sa.String(20), nullable=False),
            sa.Column("content", sa.Text, nullable=False, server_default=""),
            sa.Column("citations", _json_type(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    else:
        _add_column_if_missing("knowledge_qa_messages", sa.Column("citations", _json_type(), nullable=True))
        _add_column_if_missing("knowledge_qa_messages", sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()))
        _add_column_if_missing("knowledge_qa_messages", sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()))

    op.execute("CREATE INDEX IF NOT EXISTS ix_knowledge_qa_messages_session_id ON knowledge_qa_messages (session_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_knowledge_qa_messages_project_id ON knowledge_qa_messages (project_id)")


def downgrade() -> None:
    if _has_table("knowledge_qa_messages"):
        _drop_index_if_exists("ix_knowledge_qa_messages_project_id", "knowledge_qa_messages")
        _drop_index_if_exists("ix_knowledge_qa_messages_session_id", "knowledge_qa_messages")
    _drop_table_if_exists("knowledge_qa_messages")

    if _has_table("knowledge_qa_sessions"):
        _drop_index_if_exists("ix_knowledge_qa_sessions_project_id", "knowledge_qa_sessions")
    _drop_table_if_exists("knowledge_qa_sessions")

    if _has_table("project_source_chunks"):
        _drop_index_if_exists("ix_project_source_chunks_source_id", "project_source_chunks")
        _drop_index_if_exists("ix_project_source_chunks_project_id", "project_source_chunks")
    _drop_table_if_exists("project_source_chunks")

    if _has_table("project_sources"):
        _drop_index_if_exists("ix_project_sources_always_inject", "project_sources")
        _drop_index_if_exists("ix_project_sources_source_type", "project_sources")
        _drop_index_if_exists("ix_project_sources_project_id", "project_sources")
    _drop_table_if_exists("project_sources")
