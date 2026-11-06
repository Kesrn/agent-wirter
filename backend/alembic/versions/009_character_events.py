"""add character events

Revision ID: 009_character_events
Revises: 008_project_genre_style
Create Date: 2026-06-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "009_character_events"
down_revision = "008_project_genre_style"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "character_events" in _tables():
        return

    op.create_table(
        "character_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("project_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("character_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("chapter_sequence_number", sa.Integer, nullable=False, index=True),
        sa.Column("appearance_type", sa.String(20), server_default="appeared"),
        sa.Column("appeared", sa.Boolean, server_default=sa.text("true")),
        sa.Column("event_summary", sa.Text, nullable=True),
        sa.Column("actions", postgresql.JSONB, nullable=True),
        sa.Column("state_change", sa.Text, nullable=True),
        sa.Column("location", sa.String(200), nullable=True),
        sa.Column("emotion", sa.String(100), nullable=True),
        sa.Column("importance", sa.Integer, server_default="3"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        sa.UniqueConstraint("project_id", "character_id", "chapter_sequence_number", name="uq_character_event_chapter"),
    )


def downgrade() -> None:
    if "character_events" in _tables():
        op.drop_table("character_events")
