"""AI harness core tables (ai_runs / ai_run_steps / llm_call_logs / human_interrupts)
   + generation_records.run_id + chapter_versions harness columns

Revision ID: 021_ai_harness_core
Revises: 020_alias_project_id_uuid
Create Date: 2026-07-01

阶段 A：Harness Core 数据层。只建表与加列，不接入业务流程。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "021_ai_harness_core"
down_revision = "020_alias_project_id_uuid"
branch_labels = None
depends_on = None


# ── SQLite/PG 兼容辅助（与 012 一致） ──────────────────

def _bind():
    return op.get_bind()


def _dialect_name() -> str:
    return _bind().dialect.name


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    return {c["name"] for c in inspect(_bind()).get_columns(table_name)}


def _uuid_type():
    if _dialect_name() == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.CHAR(36)


def _json_type():
    if _dialect_name() == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _ts_columns():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def upgrade() -> None:
    # 1. ai_runs
    if not _has_table("ai_runs"):
        op.create_table(
            "ai_runs",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("chapter_id", _uuid_type(), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True),
            sa.Column("document_id", _uuid_type(), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=True),
            sa.Column("generation_record_id", _uuid_type(), nullable=True),
            sa.Column("run_type", sa.String(50), nullable=False),
            sa.Column("mode", sa.String(50), nullable=False),
            sa.Column("status", sa.String(50), nullable=False, server_default="CREATED"),
            sa.Column("current_step", sa.String(100), nullable=True),
            sa.Column("user_goal", sa.Text, nullable=True),
            sa.Column("thread_id", sa.String(200), nullable=True),
            sa.Column("model_config_snapshot", _json_type(), nullable=True),
            sa.Column("token_usage", _json_type(), nullable=True),
            sa.Column("cost_usage", _json_type(), nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_project_id ON ai_runs (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_chapter_id ON ai_runs (chapter_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_document_id ON ai_runs (document_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_status ON ai_runs (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_thread_id ON ai_runs (thread_id)")

    # 2. ai_run_steps
    if not _has_table("ai_run_steps"):
        op.create_table(
            "ai_run_steps",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("run_id", _uuid_type(), sa.ForeignKey("ai_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("step_order", sa.Integer, nullable=False),
            sa.Column("step_name", sa.String(100), nullable=False),
            sa.Column("agent_name", sa.String(100), nullable=True),
            sa.Column("status", sa.String(50), nullable=False, server_default="PENDING"),
            sa.Column("input", _json_type(), nullable=True),
            sa.Column("output", _json_type(), nullable=True),
            sa.Column("input_hash", sa.String(128), nullable=True),
            sa.Column("output_hash", sa.String(128), nullable=True),
            sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("max_retry", sa.Integer, nullable=False, server_default="0"),
            sa.Column("idempotency_key", sa.String(200), nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_run_steps_run_id ON ai_run_steps (run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_run_steps_run_order ON ai_run_steps (run_id, step_order)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_run_steps_run_status ON ai_run_steps (run_id, status)")
    # 部分唯一索引：idempotency_key 非空时唯一
    if _dialect_name() == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_run_steps_idempotency "
            "ON ai_run_steps (idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
    else:
        # SQLite：现代版支持部分索引（WHERE），与 PG 语义一致
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_run_steps_idempotency "
            "ON ai_run_steps (idempotency_key) WHERE idempotency_key IS NOT NULL"
        )

    # 3. llm_call_logs（只有 created_at）
    if not _has_table("llm_call_logs"):
        op.create_table(
            "llm_call_logs",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("run_id", _uuid_type(), sa.ForeignKey("ai_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("step_id", _uuid_type(), sa.ForeignKey("ai_run_steps.id", ondelete="SET NULL"), nullable=True),
            sa.Column("project_id", _uuid_type(), nullable=True),
            sa.Column("chapter_id", _uuid_type(), nullable=True),
            sa.Column("document_id", _uuid_type(), nullable=True),
            sa.Column("agent_name", sa.String(100), nullable=True),
            sa.Column("provider", sa.String(50), nullable=True),
            sa.Column("model", sa.String(120), nullable=True),
            sa.Column("prompt_template_id", _uuid_type(), nullable=True),
            sa.Column("prompt_template_version", sa.Integer, nullable=True),
            sa.Column("prompt_hash", sa.String(128), nullable=True),
            sa.Column("rendered_prompt_snapshot", sa.Text, nullable=True),
            sa.Column("context_package_snapshot", _json_type(), nullable=True),
            sa.Column("model_config_snapshot", _json_type(), nullable=True),
            sa.Column("input_tokens", sa.Integer, nullable=True),
            sa.Column("output_tokens", sa.Integer, nullable=True),
            sa.Column("total_tokens", sa.Integer, nullable=True),
            sa.Column("cost", sa.Numeric(12, 6), nullable=True),
            sa.Column("latency_ms", sa.Integer, nullable=True),
            sa.Column("request", _json_type(), nullable=True),
            sa.Column("response", _json_type(), nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_logs_run_id ON llm_call_logs (run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_logs_step_id ON llm_call_logs (step_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_logs_project_id ON llm_call_logs (project_id)")

    # 4. human_interrupts（只有 created_at / resolved_at）
    if not _has_table("human_interrupts"):
        op.create_table(
            "human_interrupts",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("run_id", _uuid_type(), sa.ForeignKey("ai_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("step_id", _uuid_type(), sa.ForeignKey("ai_run_steps.id", ondelete="SET NULL"), nullable=True),
            sa.Column("thread_id", sa.String(200), nullable=True),
            sa.Column("step_name", sa.String(100), nullable=True),
            sa.Column("status", sa.String(50), nullable=False, server_default="WAITING"),
            sa.Column("payload", _json_type(), nullable=True),
            sa.Column("decision", sa.String(50), nullable=True),
            sa.Column("feedback", sa.Text, nullable=True),
            sa.Column("resolved", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_human_interrupts_run_id ON human_interrupts (run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_human_interrupts_thread_id ON human_interrupts (thread_id)")

    # 5. generation_records 加 run_id
    _add_column_if_missing("generation_records", sa.Column("run_id", _uuid_type(), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_generation_records_run_id ON generation_records (run_id)")

    # 6. chapter_versions 加 5 列
    _add_column_if_missing("chapter_versions", sa.Column("project_id", _uuid_type(), nullable=True))
    _add_column_if_missing("chapter_versions", sa.Column("run_id", _uuid_type(), nullable=True))
    _add_column_if_missing("chapter_versions", sa.Column("parent_version_id", _uuid_type(), nullable=True))
    _add_column_if_missing("chapter_versions", sa.Column("rollback_from_version_id", _uuid_type(), nullable=True))
    _add_column_if_missing("chapter_versions", sa.Column("diff_from_parent", _json_type(), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_chapter_versions_project_id ON chapter_versions (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chapter_versions_run_id ON chapter_versions (run_id)")


def downgrade() -> None:
    # 先删老表加的索引与列
    op.execute("DROP INDEX IF EXISTS ix_chapter_versions_run_id")
    op.execute("DROP INDEX IF EXISTS ix_chapter_versions_project_id")
    for col in ["diff_from_parent", "rollback_from_version_id", "parent_version_id", "run_id", "project_id"]:
        if col in _columns("chapter_versions"):
            op.drop_column("chapter_versions", col)

    op.execute("DROP INDEX IF EXISTS ix_generation_records_run_id")
    if "run_id" in _columns("generation_records"):
        op.drop_column("generation_records", "run_id")

    op.execute("DROP INDEX IF EXISTS ix_human_interrupts_thread_id")
    op.execute("DROP INDEX IF EXISTS ix_human_interrupts_run_id")
    if _has_table("human_interrupts"):
        op.drop_table("human_interrupts")

    op.execute("DROP INDEX IF EXISTS ix_llm_call_logs_project_id")
    op.execute("DROP INDEX IF EXISTS ix_llm_call_logs_step_id")
    op.execute("DROP INDEX IF EXISTS ix_llm_call_logs_run_id")
    if _has_table("llm_call_logs"):
        op.drop_table("llm_call_logs")

    op.execute("DROP INDEX IF EXISTS ux_ai_run_steps_idempotency")
    op.execute("DROP INDEX IF EXISTS ix_ai_run_steps_run_status")
    op.execute("DROP INDEX IF EXISTS ix_ai_run_steps_run_order")
    op.execute("DROP INDEX IF EXISTS ix_ai_run_steps_run_id")
    if _has_table("ai_run_steps"):
        op.drop_table("ai_run_steps")

    op.execute("DROP INDEX IF EXISTS ix_ai_runs_thread_id")
    op.execute("DROP INDEX IF EXISTS ix_ai_runs_status")
    op.execute("DROP INDEX IF EXISTS ix_ai_runs_document_id")
    op.execute("DROP INDEX IF EXISTS ix_ai_runs_chapter_id")
    op.execute("DROP INDEX IF EXISTS ix_ai_runs_project_id")
    if _has_table("ai_runs"):
        op.drop_table("ai_runs")
