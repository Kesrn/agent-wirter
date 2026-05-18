"""initial tables

Revision ID: 001_initial
Revises:
Create Date: 2026-05-17

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("target_words", sa.Integer, server_default="200000"),
        sa.Column("status", sa.String(20), server_default="active"),
        sa.Column("mode", sa.String(20), server_default="novel"),
        sa.Column("owner_id", sa.String(100), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("username", sa.String(50), nullable=False, unique=True),
        sa.Column("email", sa.String(200), nullable=True, unique=True),
        sa.Column("hashed_password", sa.String(200), nullable=False),
        sa.Column("is_active", sa.Boolean, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "chapters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text, nullable=True),
        sa.Column("outline", sa.Text, nullable=True),
        sa.Column("sequence_number", sa.Integer, server_default="0"),
        sa.Column("word_count", sa.Integer, server_default="0"),
        sa.Column("status", sa.String(20), server_default="draft"),
        sa.Column("metadata", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "experts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.Text, server_default=""),
        sa.Column("role_type", sa.String(20), nullable=False),
        sa.Column("system_prompt", sa.Text, server_default=""),
        sa.Column("temperature", sa.Float, server_default="0.7"),
        sa.Column("max_tokens", sa.Integer, server_default="4096"),
        sa.Column("workflow_position", sa.String(30), nullable=False),
        sa.Column("context_scope", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("trigger", sa.String(30), server_default="manual"),
        sa.Column("is_builtin", sa.Boolean, server_default=sa.text("false")),
        sa.Column("is_enabled", sa.Boolean, server_default=sa.text("true")),
        sa.Column("color", sa.String(20), server_default="blue"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "world_entries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("category", sa.String(50), nullable=False, server_default="general"),
        sa.Column("content", sa.Text, nullable=False, server_default=""),
        sa.Column("rules", postgresql.JSONB, nullable=True),
        sa.Column("confidence", sa.String(10), server_default="medium"),
        sa.Column("embedding", postgresql.ARRAY(sa.Float, dimensions=1), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "characters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("role_type", sa.String(20), server_default="supporting"),
        sa.Column("profile", sa.Text, nullable=True),
        sa.Column("faction", sa.String(100), nullable=True),
        sa.Column("appearance_count", sa.Integer, server_default="0"),
        sa.Column("metadata", postgresql.JSONB, nullable=True),
        sa.Column("embedding", postgresql.ARRAY(sa.Float, dimensions=1), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "outlines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("sequence_number", sa.Integer, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("turning_point", sa.Text, nullable=True),
        sa.Column("hidden_thread_ids", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "hidden_threads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("chapter_nums", postgresql.JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )

    op.create_table(
        "character_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("source_character_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("target_character_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("character_relations")
    op.drop_table("hidden_threads")
    op.drop_table("outlines")
    op.drop_table("characters")
    op.drop_table("world_entries")
    op.drop_table("experts")
    op.drop_table("chapters")
    op.drop_table("users")
    op.drop_table("projects")