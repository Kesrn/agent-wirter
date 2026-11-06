"""Add the nullable human edit metrics cache to generation records."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "037_generation_record_edit_metrics"
down_revision = "036_generation_record_superseded"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("generation_records"):
        return
    columns = {column["name"] for column in inspect(bind).get_columns("generation_records")}
    if "human_edit_metrics" not in columns:
        with op.batch_alter_table("generation_records") as batch_op:
            batch_op.add_column(sa.Column("human_edit_metrics", sa.JSON(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()
    if not inspect(bind).has_table("generation_records"):
        return
    columns = {column["name"] for column in inspect(bind).get_columns("generation_records")}
    if "human_edit_metrics" in columns:
        with op.batch_alter_table("generation_records") as batch_op:
            batch_op.drop_column("human_edit_metrics")
