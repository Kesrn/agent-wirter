"""GenerationRecord.run_id 唯一约束。

Revision ID: 030_generation_record_run_id_unique
Revises: 029_world_entry_chapter_scope
Create Date: 2026-07-29

为 generation_records.run_id 增加 partial unique index，保证每 run_id 最多一条
非 NULL 记录（候选/applied 互斥）。迁移前先确定性清理重复行：优先保留已采用
的记录，其次保留最新记录，避免相同 created_at 或旧 adopted 行导致建索引失败。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

revision = "030_generation_record_run_id_unique"
down_revision = "029_world_entry_chapter_scope"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_generation_records_run_id"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)

    # 本项目的可读 revision id 已超过 Alembic 默认 version_num VARCHAR(32)。
    # PostgreSQL 会严格校验长度；若不先扩容，下面的 DDL 虽成功执行，也会在
    # Alembic 最后写入新版本号时整笔事务回滚。
    if (
        bind.dialect.name == "postgresql"
        and inspector.has_table("alembic_version")
    ):
        op.alter_column(
            "alembic_version",
            "version_num",
            existing_type=sa.String(length=32),
            type_=sa.String(length=64),
            existing_nullable=False,
        )

    if not inspector.has_table("generation_records"):
        return

    # 1) 清理重复 run_id 行。不能只按 created_at < MAX(created_at) 删除：
    # 相同时间戳会留下多行；旧的 accepted 行也可能和新 candidate 并存。
    # 排序后每组只保留第一行，优先级为：
    #   applied/已有 accepted_version_id > 最新 created_at > 最大 id。
    rows = bind.execute(text(
        """
        SELECT id, run_id
        FROM generation_records
        WHERE run_id IS NOT NULL
        ORDER BY
            run_id,
            CASE
                WHEN status = 'applied' OR accepted_version_id IS NOT NULL THEN 0
                ELSE 1
            END,
            created_at DESC,
            id DESC
        """
    )).mappings()
    seen_run_ids: set[str] = set()
    duplicate_ids = []
    for row in rows:
        run_key = str(row["run_id"])
        if run_key in seen_run_ids:
            duplicate_ids.append(row["id"])
        else:
            seen_run_ids.add(run_key)

    # 逐行删除可以同时兼容 SQLite 与 PostgreSQL UUID 参数绑定，并避免拼接 SQL。
    for record_id in duplicate_ids:
        bind.execute(
            text("DELETE FROM generation_records WHERE id = :record_id"),
            {"record_id": record_id},
        )

    # 2) 创建 partial unique index
    existing_indexes = {i["name"] for i in inspector.get_indexes("generation_records")}
    if INDEX_NAME not in existing_indexes:
        op.create_index(
            INDEX_NAME,
            "generation_records",
            ["run_id"],
            unique=True,
            sqlite_where=sa.text("run_id IS NOT NULL"),
            postgresql_where=sa.text("run_id IS NOT NULL"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    if not inspector.has_table("generation_records"):
        return
    existing_indexes = {i["name"] for i in inspector.get_indexes("generation_records")}
    if INDEX_NAME in existing_indexes:
        op.drop_index(INDEX_NAME, table_name="generation_records")
