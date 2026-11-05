"""Add request correlation IDs to AI runs."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "038_ai_run_request_id"
down_revision = "037_generation_record_edit_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("ai_runs"):
        return
    columns = {column["name"] for column in inspect(bind).get_columns("ai_runs")}
    if "request_id" not in columns:
        with op.batch_alter_table("ai_runs") as batch_op:
            batch_op.add_column(sa.Column("request_id", sa.String(64), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_request_id ON ai_runs (request_id)")


def downgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("ai_runs"):
        return
    op.execute("DROP INDEX IF EXISTS ix_ai_runs_request_id")
    columns = {column["name"] for column in inspect(bind).get_columns("ai_runs")}
    if "request_id" in columns:
        with op.batch_alter_table("ai_runs") as batch_op:
            batch_op.drop_column("request_id")
