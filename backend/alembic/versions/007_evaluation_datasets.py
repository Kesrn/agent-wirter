"""evaluation datasets

Revision ID: 007_evaluation_datasets
Revises: 006_generation_trace
Create Date: 2026-06-03
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "007_evaluation_datasets"
down_revision = "006_generation_trace"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return inspect(op.get_bind()).has_table(table_name)


def upgrade() -> None:
    if not _has_table("evaluation_datasets"):
        op.create_table(
            "evaluation_datasets",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("description", sa.Text, nullable=True),
            sa.Column("mode", sa.String(20), nullable=False, server_default="creative"),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_evaluation_datasets_project_id ON evaluation_datasets (project_id)")

    if not _has_table("evaluation_cases"):
        op.create_table(
            "evaluation_cases",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("dataset_id", UUID(as_uuid=True), sa.ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("task_type", sa.String(50), nullable=False, server_default="creative_generation"),
            sa.Column("input_text", sa.Text, nullable=False, server_default=""),
            sa.Column("actual_output", sa.Text, nullable=True),
            sa.Column("reference_output", sa.Text, nullable=True),
            sa.Column("expected_properties", JSONB, nullable=True),
            sa.Column("rubric", JSONB, nullable=True),
            sa.Column("tags", JSONB, nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="active"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_evaluation_cases_dataset_id ON evaluation_cases (dataset_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_evaluation_cases_project_id ON evaluation_cases (project_id)")

    if not _has_table("evaluation_runs"):
        op.create_table(
            "evaluation_runs",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("dataset_id", UUID(as_uuid=True), sa.ForeignKey("evaluation_datasets.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("generation_mode", sa.String(30), nullable=False, server_default="generate_and_judge"),
            sa.Column("status", sa.String(20), nullable=False, server_default="running"),
            sa.Column("model_provider", sa.String(50), nullable=True),
            sa.Column("model_id", sa.String(120), nullable=True),
            sa.Column("total_cases", sa.Integer, nullable=False, server_default="0"),
            sa.Column("completed_cases", sa.Integer, nullable=False, server_default="0"),
            sa.Column("failed_cases", sa.Integer, nullable=False, server_default="0"),
            sa.Column("average_score", sa.Float, nullable=True),
            sa.Column("summary", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_evaluation_runs_dataset_id ON evaluation_runs (dataset_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_evaluation_runs_project_id ON evaluation_runs (project_id)")

    if not _has_table("evaluation_results"):
        op.create_table(
            "evaluation_results",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column("run_id", UUID(as_uuid=True), sa.ForeignKey("evaluation_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("case_id", UUID(as_uuid=True), sa.ForeignKey("evaluation_cases.id", ondelete="CASCADE"), nullable=False),
            sa.Column("project_id", UUID(as_uuid=True), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("generated_output", sa.Text, nullable=True),
            sa.Column("scores", JSONB, nullable=True),
            sa.Column("score", sa.Float, nullable=True),
            sa.Column("passed", sa.Boolean, nullable=True),
            sa.Column("feedback", sa.Text, nullable=True),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("latency_ms", sa.Integer, nullable=True),
            sa.Column("langfuse_trace_id", sa.String(64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_evaluation_results_run_id ON evaluation_results (run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_evaluation_results_case_id ON evaluation_results (case_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_evaluation_results_project_id ON evaluation_results (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_evaluation_results_langfuse_trace_id ON evaluation_results (langfuse_trace_id)")


def downgrade() -> None:
    op.drop_index("ix_evaluation_results_langfuse_trace_id", table_name="evaluation_results")
    op.drop_index("ix_evaluation_results_project_id", table_name="evaluation_results")
    op.drop_index("ix_evaluation_results_case_id", table_name="evaluation_results")
    op.drop_index("ix_evaluation_results_run_id", table_name="evaluation_results")
    op.drop_table("evaluation_results")
    op.drop_index("ix_evaluation_runs_project_id", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_dataset_id", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index("ix_evaluation_cases_project_id", table_name="evaluation_cases")
    op.drop_index("ix_evaluation_cases_dataset_id", table_name="evaluation_cases")
    op.drop_table("evaluation_cases")
    op.drop_index("ix_evaluation_datasets_project_id", table_name="evaluation_datasets")
    op.drop_table("evaluation_datasets")
