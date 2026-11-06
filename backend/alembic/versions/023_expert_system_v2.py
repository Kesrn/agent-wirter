"""Expert System v2 — experts/ai_runs/ai_run_steps 快照字段

Revision ID: 023_expert_system_v2
Revises: 022_writing_memory_staging
Create Date: 2026-07-03

阶段 I-1：Expert System v2 数据层。只加字段，不接业务流程，不改 workflow / 前端 / 旧专家。
- experts: expert_key / version / deprecated / input_schema / output_schema
- ai_runs: workflow_key / workflow_version / workflow_snapshot / expert_snapshot
- ai_run_steps: node_key / expert_key / expert_version / skill_dir
- llm_call_logs: 本阶段不加列（专家/workflow 信息写入已有的 request JSON）
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "023_expert_system_v2"
down_revision = "022_writing_memory_staging"
branch_labels = None
depends_on = None


# ── SQLite/PG 兼容辅助（与 021/022 一致） ──────────────

def _bind():
    return op.get_bind()


def _dialect_name() -> str:
    return _bind().dialect.name


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    return {c["name"] for c in inspect(_bind()).get_columns(table_name)}


def _json_type():
    if _dialect_name() == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _drop_column_if_exists(table_name: str, column_name: str) -> None:
    if _has_table(table_name) and column_name in _columns(table_name):
        op.drop_column(table_name, column_name)


def _drop_index_if_exists(index_name: str) -> None:
    op.execute(f"DROP INDEX IF EXISTS {index_name}")


def upgrade() -> None:
    # ── experts ──
    if _has_table("experts"):
        _add_column_if_missing("experts", sa.Column("expert_key", sa.String(80), nullable=True))
        _add_column_if_missing("experts", sa.Column("version", sa.Integer, nullable=False, server_default="1"))
        _add_column_if_missing(
            "experts", sa.Column("deprecated", sa.Boolean, nullable=False, server_default=sa.text("false"))
        )
        _add_column_if_missing("experts", sa.Column("input_schema", _json_type(), nullable=True))
        _add_column_if_missing("experts", sa.Column("output_schema", _json_type(), nullable=True))
        op.execute("CREATE INDEX IF NOT EXISTS ix_experts_expert_key ON experts (expert_key)")

    # ── ai_runs ──
    if _has_table("ai_runs"):
        _add_column_if_missing("ai_runs", sa.Column("workflow_key", sa.String(100), nullable=True))
        _add_column_if_missing("ai_runs", sa.Column("workflow_version", sa.String(30), nullable=True))
        _add_column_if_missing("ai_runs", sa.Column("workflow_snapshot", _json_type(), nullable=True))
        _add_column_if_missing("ai_runs", sa.Column("expert_snapshot", _json_type(), nullable=True))
        op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_workflow_key ON ai_runs (workflow_key)")

    # ── ai_run_steps ──
    if _has_table("ai_run_steps"):
        _add_column_if_missing("ai_run_steps", sa.Column("node_key", sa.String(100), nullable=True))
        _add_column_if_missing("ai_run_steps", sa.Column("expert_key", sa.String(80), nullable=True))
        _add_column_if_missing("ai_run_steps", sa.Column("expert_version", sa.Integer, nullable=True))
        _add_column_if_missing("ai_run_steps", sa.Column("skill_dir", sa.String(100), nullable=True))


def downgrade() -> None:
    # ── ai_run_steps ──
    for col in ["skill_dir", "expert_version", "expert_key", "node_key"]:
        _drop_column_if_exists("ai_run_steps", col)

    # ── ai_runs ──
    _drop_index_if_exists("ix_ai_runs_workflow_key")
    for col in ["expert_snapshot", "workflow_snapshot", "workflow_version", "workflow_key"]:
        _drop_column_if_exists("ai_runs", col)

    # ── experts ──
    _drop_index_if_exists("ix_experts_expert_key")
    for col in ["output_schema", "input_schema", "deprecated", "version", "expert_key"]:
        _drop_column_if_exists("experts", col)
