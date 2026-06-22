"""extraction job control fields

Revision ID: 017_extraction_job_control
Revises: 016_project_knowledge_facts
Create Date: 2026-06-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "017_extraction_job_control"
down_revision = "016_project_knowledge_facts"
branch_labels = None
depends_on = None


def _dialect_name() -> str:
    return op.get_bind().dialect.name


def upgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    table_name = "extraction_jobs"
    existing_cols = {c["name"] for c in inspector.get_columns(table_name)}

    new_columns = [
        ("current_chapter_no", sa.Integer()),
        ("last_error", sa.Text()),
        ("paused_at", sa.DateTime(timezone=True)),
        ("cancelled_at", sa.DateTime(timezone=True)),
        ("last_run_started_at", sa.DateTime(timezone=True)),
        ("last_run_finished_at", sa.DateTime(timezone=True)),
    ]

    for col_name, col_type in new_columns:
        if col_name not in existing_cols:
            op.add_column(table_name, sa.Column(col_name, col_type, nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = inspect(bind)
    table_name = "extraction_jobs"
    existing_cols = {c["name"] for c in inspector.get_columns(table_name)}

    drop_cols = [
        "last_run_finished_at",
        "last_run_started_at",
        "cancelled_at",
        "paused_at",
        "last_error",
        "current_chapter_no",
    ]
    for col_name in drop_cols:
        if col_name in existing_cols:
            op.drop_column(table_name, col_name)
