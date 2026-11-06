"""抽取任务锁定切分代：extraction_jobs.split_generation_id

Revision ID: 034_extraction_job_split_generation
Revises: 033_split_generation
Create Date: 2026-08-02

方案 v3 §3.5 规则 4：抽取任务启动时锁定 split_generation_id，运行中不随活动
指针漂移（抽取基于的章节定义中途不得变化）。NULL = 迁移前创建的旧任务
（读取当前活动代，保持旧行为）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "034_extraction_job_split_generation"
down_revision = "033_split_generation"
branch_labels = None
depends_on = None


def _bind():
    return op.get_bind()


def _uuid_type():
    if _bind().dialect.name == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.CHAR(36)


def upgrade() -> None:
    if not inspect(_bind()).has_table("extraction_jobs"):
        return
    existing = {c["name"] for c in inspect(_bind()).get_columns("extraction_jobs")}
    if "split_generation_id" in existing:
        return
    with op.batch_alter_table("extraction_jobs") as batch_op:
        batch_op.add_column(sa.Column(
            "split_generation_id", _uuid_type(), nullable=True,
        ))
        batch_op.create_foreign_key(
            "fk_extraction_jobs_split_generation",
            "split_generations",
            ["split_generation_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_extraction_jobs_split_generation_id "
               "ON extraction_jobs (split_generation_id)")


def downgrade() -> None:
    if not inspect(_bind()).has_table("extraction_jobs"):
        return
    fk_names = {fk["name"] for fk in inspect(_bind()).get_foreign_keys("extraction_jobs")}
    with op.batch_alter_table("extraction_jobs") as batch_op:
        if "fk_extraction_jobs_split_generation" in fk_names:
            batch_op.drop_constraint(
                "fk_extraction_jobs_split_generation", type_="foreignkey")
        batch_op.drop_column("split_generation_id")
