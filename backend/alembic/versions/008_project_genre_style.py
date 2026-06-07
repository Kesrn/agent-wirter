"""add project genre and style

Revision ID: 008_project_genre_style
Revises: 007_evaluation_datasets
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "008_project_genre_style"
down_revision = "007_evaluation_datasets"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    columns = _columns("projects")
    if "genre" not in columns:
        op.add_column("projects", sa.Column("genre", sa.String(200), nullable=True))
    if "style" not in columns:
        op.add_column("projects", sa.Column("style", sa.String(200), nullable=True))


def downgrade() -> None:
    columns = _columns("projects")
    if "style" in columns:
        op.drop_column("projects", "style")
    if "genre" in columns:
        op.drop_column("projects", "genre")
