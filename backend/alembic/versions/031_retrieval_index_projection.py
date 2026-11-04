"""检索索引投影表（ORM 四表 + outbox 表）+ PostgreSQL FTS DDL。

Revision ID: 031_retrieval_index_projection
Revises: 030_generation_record_run_id_unique
Create Date: 2026-08-02

正式迁移检索投影四表（此前只由 init_db 的 create_all 创建，服务端
alembic upgrade head 会缺表）：

- retrieval_chunks                检索块（内容 + 分词检索文本 + 内容哈希）
- retrieval_index_generation      一次索引构建记录（building → active/superseded/failed）
- retrieval_index_source_state    每源活动构建版本指针
- retrieval_vectors               检索块向量投影（chunk_id → embedding）
- retrieval_index_jobs            PG outbox 索引构建任务（本特征新增，见
  models.retrieval_index_job；服务端部署同样需要这张表，故并入本迁移）

列与 ORM 模型一致：GUID 在 PG 用 postgresql.UUID(as_uuid=True)、SQLite 用
CHAR(36)；JSONValue 在 PG 用 JSONB、SQLite 用 JSON；FloatList（embedding）
在 PG 用 ARRAY(Float)、SQLite 用 JSON。

PostgreSQL 专属（SQLite 下跳过或语义等价，docstring 说明）：
- retrieval_chunks_fts 同构普通表（对照 services.retrieval_indexer 的
  PG_FTS_TABLE_CREATE_SQL / retrieval_eval/setup_pg.py 的 DDL；SQLite 的
  FTS5 虚拟表无法用迁移建，由 ensure_fts_table 运行时创建）；
- retrieval_chunks 与 retrieval_chunks_fts 的 ``search_tsv`` 存储生成列
  （``to_tsvector('simple', search_text)`` STORED）+ 各自 GIN 索引；
- pg_trgm 扩展 + trgm GIN 索引为**可选步骤**：``CREATE EXTENSION`` 需要
  超级用户权限，无权限时本迁移捕获异常并跳过（仅告警，不让迁移整体失败）。
  如需强制启用可先以超级用户手动执行 ``CREATE EXTENSION IF NOT EXISTS
  pg_trgm`` 后再跑 ``alembic upgrade head``；或用 ``alembic -x pg_trgm=0
  upgrade head`` 显式跳过本步骤。

全部 DDL 幂等（has_table / IF NOT EXISTS / ADD COLUMN IF NOT EXISTS 守卫），
对已由 setup_pg.py 或桌面端建过的环境安全。
"""

import logging

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text
from sqlalchemy.dialects import postgresql

logger = logging.getLogger("alembic.runtime.migration")

revision = "031_retrieval_index_projection"
down_revision = "030_generation_record_run_id_unique"
branch_labels = None
depends_on = None


# ── 方言 / 存在性守卫（与既有迁移风格一致） ───────────────

def _bind():
    return op.get_bind()


def _dialect_name() -> str:
    return _bind().dialect.name


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _has_index(table_name: str, index_name: str) -> bool:
    names = {i["name"] for i in inspect(_bind()).get_indexes(table_name)}
    return index_name in names


def _uuid_type():
    if _dialect_name() == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.CHAR(36)


def _json_type():
    if _dialect_name() == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def _float_list_type():
    if _dialect_name() == "postgresql":
        return postgresql.ARRAY(sa.Float)
    return sa.JSON()


def _ts_columns():
    """项目通用 created_at / updated_at 列定义。"""
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    ]


# ── PostgreSQL FTS 专属 DDL ───────────────────────────────

_PG_FTS_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS retrieval_chunks_fts ("
    "chunk_id VARCHAR(100) NOT NULL, "
    "project_id VARCHAR(100) NOT NULL, "
    "source_kind VARCHAR(50) NOT NULL, "
    "source_id VARCHAR(100) NOT NULL, "
    "chapter_no INTEGER, "
    "title TEXT, "
    "content TEXT NOT NULL, "
    "search_text TEXT NOT NULL"
    ")"
)

# search_tsv 存储生成列：把 to_tsvector('simple', search_text) 固化到列上，
# 检索过滤与 ts_rank 排序都直接读列（对照 setup_pg.py；表达式索引在真实
# 数据下 Bitmap Recheck / ts_rank 会逐行重算分词，性能差一个数量级）。
_PG_SEARCH_TSV_COLUMN_SQL = (
    "ALTER TABLE {table} ADD COLUMN IF NOT EXISTS search_tsv tsvector "
    "GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED"
)
_PG_SEARCH_TSV_INDEXES = [
    (
        "ix_retrieval_chunks_search_tsv",
        "CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_search_tsv "
        "ON retrieval_chunks USING GIN (search_tsv)",
    ),
    (
        "ix_retrieval_chunks_fts_search_tsv",
        "CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_fts_search_tsv "
        "ON retrieval_chunks_fts USING GIN (search_tsv)",
    ),
]
_PG_TRGM_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_fts_search_text_trgm "
    "ON retrieval_chunks_fts USING GIN (search_text gin_trgm_ops)"
)


def _ensure_pg_search_tsv() -> None:
    """search_tsv 生成列 + GIN 索引（PG 专属，幂等：IF NOT EXISTS）。"""
    op.execute(text(_PG_SEARCH_TSV_COLUMN_SQL.format(table="retrieval_chunks_fts")))
    op.execute(text(_PG_SEARCH_TSV_COLUMN_SQL.format(table="retrieval_chunks")))
    for _name, sql in _PG_SEARCH_TSV_INDEXES:
        op.execute(sql)


def _ensure_pg_trgm() -> None:
    """pg_trgm 扩展 + trgm GIN 索引（可选步骤，权限不足时跳过不失败）。

    ``alembic -x pg_trgm=0 upgrade head`` 可显式跳过本步骤（不尝试建扩展）。
    CREATE EXTENSION 需要超级用户；失败仅告警，trgm 索引缺位不影响
    search_tsv 路径的检索（lexical_retriever 的 PG 分支主查询走 search_tsv）。
    """
    try:
        from alembic import context  # noqa: PLC0415 — 迁移内读取 -x 参数
        x_args = context.get_x_argument(as_dictionary=True) or {}
        if str(x_args.get("pg_trgm", "1")).lower() in ("0", "false", "no"):
            logger.warning("[031] 已通过 -x pg_trgm=0 显式跳过 pg_trgm 扩展与索引")
            return
    except Exception:  # pragma: no cover — 代理不可用时按默认尝试
        pass
    bind = _bind()
    try:
        exists = bind.execute(
            text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'")
        ).scalar()
    except Exception:  # pragma: no cover — 查询 pg_extension 失败仅当环境异常
        exists = None
    if not exists:
        try:
            bind.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        except Exception as exc:  # noqa: BLE001 — 权限不足不阻断迁移
            logger.warning(
                "[031] CREATE EXTENSION pg_trgm 失败（需要超级用户权限），"
                "跳过 trgm GIN 索引，可手动执行后重跑: %s", exc
            )
            return
    op.execute(_PG_TRGM_INDEX_SQL)


def upgrade() -> None:
    # 1. retrieval_chunks
    if not _has_table("retrieval_chunks"):
        op.create_table(
            "retrieval_chunks",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_kind", sa.String(50), nullable=False),
            sa.Column("source_id", sa.String(100), nullable=False),
            sa.Column("source_version_id", sa.String(100), nullable=True),
            sa.Column("index_generation_id", sa.String(100), nullable=False),
            sa.Column("chunk_index", sa.Integer, nullable=False),
            sa.Column("content", sa.Text, nullable=False),
            sa.Column("search_text", sa.Text, nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.Column("metadata", _json_type(), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_project_id ON retrieval_chunks (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_source_id ON retrieval_chunks (source_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_index_generation_id ON retrieval_chunks (index_generation_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_project_source ON retrieval_chunks (project_id, source_kind, source_id)")

    # 2. retrieval_index_generation
    if not _has_table("retrieval_index_generation"):
        op.create_table(
            "retrieval_index_generation",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("generation_id", sa.String(100), nullable=False, unique=True),
            sa.Column("source_kind", sa.String(50), nullable=False),
            sa.Column("source_id", sa.String(100), nullable=False),
            sa.Column("status", sa.String(20), nullable=False, server_default="building"),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_index_generation_project_id ON retrieval_index_generation (project_id)")

    # 3. retrieval_index_source_state
    if not _has_table("retrieval_index_source_state"):
        op.create_table(
            "retrieval_index_source_state",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_kind", sa.String(50), nullable=False),
            sa.Column("source_id", sa.String(100), nullable=False),
            sa.Column("active_generation_id", sa.String(100), nullable=False),
            sa.UniqueConstraint(
                "project_id", "source_kind", "source_id",
                name="uq_retrieval_source_state_project_kind_source",
            ),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_index_source_state_project_id ON retrieval_index_source_state (project_id)")

    # 4. retrieval_vectors
    if not _has_table("retrieval_vectors"):
        op.create_table(
            "retrieval_vectors",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("chunk_id", sa.String(100), nullable=False),
            sa.Column("embedding", _float_list_type(), nullable=False),
            sa.Column("dim", sa.Integer, nullable=False),
            sa.Column("content_hash", sa.String(64), nullable=False),
            sa.UniqueConstraint(
                "project_id", "chunk_id",
                name="uq_retrieval_vectors_project_chunk",
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_vectors_project_id ON retrieval_vectors (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_vectors_chunk_id ON retrieval_vectors (chunk_id)")

    # 5. retrieval_index_jobs（outbox；SQLite 桌面端同步构建不写本表，但保持同构可建表）
    if not _has_table("retrieval_index_jobs"):
        op.create_table(
            "retrieval_index_jobs",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), nullable=False),
            sa.Column("source_kind", sa.String(50), nullable=False),
            sa.Column("source_id", sa.String(100), nullable=False),
            sa.Column("source_version_id", sa.String(100), nullable=True),
            sa.Column("payload", _json_type(), nullable=True),
            sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
            sa.Column("error", sa.Text, nullable=True),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_index_jobs_project_id ON retrieval_index_jobs (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_retrieval_index_jobs_poll ON retrieval_index_jobs (status, created_at)")
    # 防重复 pending 的部分唯一索引：同一源最多一条 pending job（同源合并策略：
    # processing/done/failed 不锁死，允许入队最新版本）；逻辑去重由
    # enqueue_index_job 先查后写 + 本索引并发兜底（032 把 031 旧版
    # IN ('pending','processing') 谓词收敛为 = 'pending'，全新库直接建新谓词）。
    if not _has_index("retrieval_index_jobs", "uq_retrieval_index_job_active_source"):
        op.create_index(
            "uq_retrieval_index_job_active_source",
            "retrieval_index_jobs",
            ["project_id", "source_kind", "source_id"],
            unique=True,
            sqlite_where=sa.text("status = 'pending'"),
            postgresql_where=sa.text("status = 'pending'"),
        )

    # 6. PostgreSQL 专属：FTS 同构普通表 + search_tsv 生成列 + GIN/trgm 索引
    if _dialect_name() == "postgresql":
        op.execute(_PG_FTS_TABLE_SQL)
        _ensure_pg_search_tsv()
        _ensure_pg_trgm()


def downgrade() -> None:
    def _drop_index_if_exists(index_name: str, table_name: str) -> None:
        if _has_table(table_name) and _has_index(table_name, index_name):
            op.drop_index(index_name, table_name=table_name)

    if _dialect_name() == "postgresql":
        _drop_index_if_exists("ix_retrieval_chunks_fts_search_text_trgm", "retrieval_chunks_fts")
        for name, _sql in reversed(_PG_SEARCH_TSV_INDEXES):
            table_name = "retrieval_chunks" if name.endswith("chunks_search_tsv") else "retrieval_chunks_fts"
            _drop_index_if_exists(name, table_name)
        for table in ("retrieval_chunks_fts", "retrieval_chunks"):
            if _has_table(table):
                bind = _bind()
                columns = {c["name"] for c in inspect(bind).get_columns(table)}
                if "search_tsv" in columns:
                    op.execute(text(f"ALTER TABLE {table} DROP COLUMN IF EXISTS search_tsv"))
        if _has_table("retrieval_chunks_fts"):
            op.drop_table("retrieval_chunks_fts")

    for index_name, table_name in [
        ("uq_retrieval_index_job_active_source", "retrieval_index_jobs"),
        ("ix_retrieval_index_jobs_poll", "retrieval_index_jobs"),
        ("ix_retrieval_index_jobs_project_id", "retrieval_index_jobs"),
        ("ix_retrieval_vectors_chunk_id", "retrieval_vectors"),
        ("ix_retrieval_vectors_project_id", "retrieval_vectors"),
        ("ix_retrieval_index_source_state_project_id", "retrieval_index_source_state"),
        ("ix_retrieval_index_generation_project_id", "retrieval_index_generation"),
        ("ix_retrieval_chunks_project_source", "retrieval_chunks"),
        ("ix_retrieval_chunks_index_generation_id", "retrieval_chunks"),
        ("ix_retrieval_chunks_source_id", "retrieval_chunks"),
        ("ix_retrieval_chunks_project_id", "retrieval_chunks"),
    ]:
        _drop_index_if_exists(index_name, table_name)

    for table_name in (
        "retrieval_index_jobs",
        "retrieval_vectors",
        "retrieval_index_source_state",
        "retrieval_index_generation",
        "retrieval_chunks",
    ):
        if _has_table(table_name):
            op.drop_table(table_name)
