"""检索索引 outbox 唯一索引收敛：同源活跃 job 仅限一条 pending。

Revision ID: 032_retrieval_index_job_pending_unique
Revises: 031_retrieval_index_projection
Create Date: 2026-08-02

同源合并策略（P0）：同源存在 processing job 时允许新增 pending（最新版本），
因此 ``retrieval_index_jobs`` 的部分唯一索引谓词从 031 旧版的
``status IN ('pending', 'processing')`` 收敛为 ``status = 'pending'`` ——
同源最多一条 pending，processing 不锁死（v1 处理中仍可入队 v2）。

对 031 已在线的库（谓词为旧版）：DROP 旧索引 → CREATE 新谓词索引（幂等守卫，
不移动数据）。对全新库（031 已按新谓词建索引）：DROP 后重建相同定义，结果一致。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision = "032_retrieval_index_job_pending_unique"
down_revision = "031_retrieval_index_projection"
branch_labels = None
depends_on = None


def _has_table(table_name: str) -> bool:
    return inspect(op.get_bind()).has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    names = {i["name"] for i in inspect(op.get_bind()).get_indexes(table_name)}
    return index_name in names


def upgrade() -> None:
    if not _has_table("retrieval_index_jobs"):
        return
    if _has_index("retrieval_index_jobs", "uq_retrieval_index_job_active_source"):
        op.drop_index(
            "uq_retrieval_index_job_active_source",
            table_name="retrieval_index_jobs",
        )
    op.create_index(
        "uq_retrieval_index_job_active_source",
        "retrieval_index_jobs",
        ["project_id", "source_kind", "source_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    if not _has_table("retrieval_index_jobs"):
        return
    if _has_index("retrieval_index_jobs", "uq_retrieval_index_job_active_source"):
        op.drop_index(
            "uq_retrieval_index_job_active_source",
            table_name="retrieval_index_jobs",
        )
    op.create_index(
        "uq_retrieval_index_job_active_source",
        "retrieval_index_jobs",
        ["project_id", "source_kind", "source_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('pending', 'processing')"),
        postgresql_where=sa.text("status IN ('pending', 'processing')"),
    )
