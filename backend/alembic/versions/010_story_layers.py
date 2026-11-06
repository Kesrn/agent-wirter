"""add story layer fields

Revision ID: 010_story_layers
Revises: 009_character_events
Create Date: 2026-06-12
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "010_story_layers"
down_revision = "009_character_events"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    project_columns = _columns("projects")
    if "overall_outline" not in project_columns:
        op.add_column("projects", sa.Column("overall_outline", sa.Text, nullable=True))

    character_columns = _columns("characters")
    if "scope_type" not in character_columns:
        op.add_column("characters", sa.Column("scope_type", sa.String(20), server_default="recurring"))

    world_columns = _columns("world_entries")
    if "scope_type" not in world_columns:
        op.add_column("world_entries", sa.Column("scope_type", sa.String(20), server_default="global"))


def downgrade() -> None:
    world_columns = _columns("world_entries")
    if "scope_type" in world_columns:
        op.drop_column("world_entries", "scope_type")

    character_columns = _columns("characters")
    if "scope_type" in character_columns:
        op.drop_column("characters", "scope_type")

    project_columns = _columns("projects")
    if "overall_outline" in project_columns:
        op.drop_column("projects", "overall_outline")
