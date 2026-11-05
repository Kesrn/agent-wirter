#!/usr/bin/env python3
"""PostgreSQL 检索投影表初始化脚本（阶段一收尾：LexicalRetriever 双后端 PG 分支）。

职责
----
1. 在指定 PostgreSQL 测试库建检索投影表：retrieval_chunks / retrieval_index_generation /
   retrieval_index_source_state（用 ORM ``Base.metadata.create_all`` 显式限定三张
   ORM 表，已存在的表不会被触碰）+ retrieval_chunks_fts（PG 普通表，复用
   services.retrieval_indexer.ensure_fts_table，与索引写入侧契约保持一致）。
2. 显式执行 FTS 相关 DDL（全部幂等）：
   - ``CREATE EXTENSION IF NOT EXISTS pg_trgm``；
   - GIN 表达式索引 ``to_tsvector('simple', search_text)``：
     * retrieval_chunks（ORM 投影表，按任务约定）；
     * retrieval_chunks_fts 上则用 **search_tsv 存储生成列**（``to_tsvector
       ('simple', search_text) STORED，见 setup_pg_tables）+ 该列 GIN 索引：
       真实 PG 验证表明表达式索引会让 Bitmap Recheck / ts_rank 逐行重算
       to_tsvector（7.3k 块时单查询 1.2~3s），生成列把分词结果固化到列上，
       检索排序都直接读列；
   - pg_trgm GIN 索引（retrieval_chunks_fts.search_text gin_trgm_ops，可选
     --no-trgm 关闭）。
3. ``--import-sqlite``：把 dev-web.sqlite3 的检索投影四表数据导入 PG
   （只动四张 retrieval 表，业务表一律不碰；sqlite 侧只读）。
   幂等：先物理删除已导入 project 的旧投影行，再按相同 id 重新插入。
4. 打印一条代表性查询的 EXPLAIN，供人工确认 GIN 索引被使用。

安全约束
--------
- 建表/写库范围：仅四张检索投影表（retrieval_chunks / retrieval_chunks_fts /
  retrieval_index_generation / retrieval_index_source_state）；业务表绝不
  create/alter/drop，import 模式对业务表零写入。
- sqlite 侧全程 ``mode=ro`` 只读，零写入。
- 必须显式传入隔离 PostgreSQL 测试库的 ``--pg-url``，避免误写本地业务库。

用法
----
    cd backend
    venv/bin/python retrieval_eval/setup_pg.py --pg-url "$TEST_PG_URL"  # 建表 + 索引
    venv/bin/python retrieval_eval/setup_pg.py --pg-url "$TEST_PG_URL" --import-sqlite  # 建表 + 索引 + 导数据
    venv/bin/python retrieval_eval/setup_pg.py --import-sqlite --project <pid>  # 只导某项目
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, async_sessionmaker, create_async_engine

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))

DEFAULT_SQLITE = BACKEND_DIR / "dev-web.sqlite3"

# 检索 SQL（lexical_retriever._PG_FTS_SQL）从 retrieval_chunks_fts 过滤，
# 且依赖 search_tsv 存储生成列（见 setup_pg_tables）：
# - retrieval_chunks 上保留表达式 GIN 索引（任务约定落点）；
# - retrieval_chunks_fts 上建 search_tsv 列的 GIN 索引（查询热路径），
#   替换旧的表达式索引（drop + create 幂等）。
_GIN_INDEX_SQLS: list[tuple[str, str]] = [
    (
        "ix_retrieval_chunks_search_text_tsv",
        "CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_search_text_tsv "
        "ON retrieval_chunks USING GIN (to_tsvector('simple', search_text))",
    ),
    (
        "ix_retrieval_chunks_fts_search_text_tsv",
        "CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_fts_search_text_tsv "
        "ON retrieval_chunks_fts USING GIN (to_tsvector('simple', search_text))",
    ),
]

# 生成列方案的检索索引（replace 表达式索引：search_tsv 列 + GIN）
_SEARCH_TSV_INDEX_SQLS: list[tuple[str, str]] = [
    (
        "ix_retrieval_chunks_fts_search_tsv",
        "CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_fts_search_tsv "
        "ON retrieval_chunks_fts USING GIN (search_tsv)",
    ),
]

_TRGM_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_retrieval_chunks_fts_search_text_trgm "
    "ON retrieval_chunks_fts USING GIN (search_text gin_trgm_ops)"
)

# 检索投影四表（导入/删除/统计均只允许这四张表）
_RETRIEVAL_TABLES = (
    "retrieval_chunks_fts",
    "retrieval_chunks",
    "retrieval_index_source_state",
    "retrieval_index_generation",
)

_IMPORT_BATCH = 500


# ── 建表与索引 ───────────────────────────────────────────

async def setup_pg_tables(engine: AsyncEngine) -> None:
    """建四张检索投影表（ORM 三表 + fts 普通表），幂等。

    retrieval_chunks_fts 额外维护 ``search_tsv`` 存储生成列
    （``to_tsvector('simple', search_text)``）：真实 PG 验证发现，表达式索引
    方案下 Bitmap Recheck 与 ts_rank 会逐行重算 to_tsvector（7.3k 块时单查询
    约 1.2~3s），生成列把分词结果固化到列上，索引与排序都直接读列。
    lexical_retriever 的 PG 分支依赖该列；若列缺失（如仅由
    retrieval_indexer 建表而未跑本脚本），检索会报明确错误而非静默降级。
    """
    from models.base import Base
    from models.retrieval_index import (
        RetrievalChunk,
        RetrievalIndexGeneration,
        RetrievalIndexSourceState,
    )
    from services.retrieval_indexer import ensure_fts_table

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(
                sync_conn,
                tables=[
                    RetrievalChunk.__table__,
                    RetrievalIndexGeneration.__table__,
                    RetrievalIndexSourceState.__table__,
                ],
            )
        )
        await ensure_fts_table(conn)
        # search_tsv 存储生成列（幂等：已存在则跳过；新建/已有表都会补齐）
        await conn.execute(text(
            "ALTER TABLE retrieval_chunks_fts "
            "ADD COLUMN IF NOT EXISTS search_tsv tsvector "
            "GENERATED ALWAYS AS (to_tsvector('simple', search_text)) STORED"
        ))
    print("[pg] 检索投影表已就绪（retrieval_chunks / retrieval_chunks_fts / "
          "retrieval_index_generation / retrieval_index_source_state；"
          "fts 含 search_tsv 存储生成列）")


async def setup_indexes(engine: AsyncEngine, *, with_trgm: bool) -> None:
    """FTS 相关 DDL：pg_trgm 扩展 + GIN 表达式索引 + search_tsv GIN + trgm（幂等）。

    表达式索引保留在 retrieval_chunks（任务约定）；retrieval_chunks_fts 用
    search_tsv 生成列索引替代表达式索引（查询走 search_tsv，见
    lexical_retriever._PG_FTS_SQL 注释）。
    """
    async with engine.begin() as conn:
        await conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        for name, sql in _GIN_INDEX_SQLS:
            await conn.execute(text(sql))
            print(f"[pg] 索引就绪: {name}")
        for name, sql in _SEARCH_TSV_INDEX_SQLS:
            await conn.execute(text(sql))
            print(f"[pg] 索引就绪: {name}")
        if with_trgm:
            await conn.execute(text(_TRGM_INDEX_SQL))
            print("[pg] 索引就绪: ix_retrieval_chunks_fts_search_text_trgm (pg_trgm)")


# ── 数据导入（sqlite 只读 → PG 四张检索表） ──────────────

def _parse_dt(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    s = str(value).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _as_uuid(value) -> uuid.UUID:
    return uuid.UUID(str(value))


def _read_sqlite_rows(sqlite_path: Path, table: str, projects: set[str] | None) -> list[dict]:
    """只读 sqlite 检索投影表，返回 dict 行列表（按项目过滤可选）。"""
    conn = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    except sqlite3.OperationalError as exc:
        print(f"[warn] sqlite 表 {table} 不可读（跳过）: {exc}")
        return []
    finally:
        conn.close()
    result: list[dict] = []
    for r in rows:
        row = dict(r)
        if projects is not None and str(row.get("project_id")) not in projects:
            continue
        result.append(row)
    return result


async def _delete_projection_for_projects(
    conn: AsyncConnection, project_ids: list[str],
) -> None:
    """幂等重建语义：先删已导入 project 的旧投影行（只动四张检索表）。"""
    if not project_ids:
        return
    ph = ", ".join(f":p{i}" for i in range(len(project_ids)))
    params = {f"p{i}": str(pid) for i, pid in enumerate(project_ids)}
    for table in ("retrieval_chunks_fts", "retrieval_chunks",
                  "retrieval_index_source_state", "retrieval_index_generation"):
        result = await conn.execute(
            text(f"DELETE FROM {table} WHERE project_id IN ({ph})"), params
        )
        print(f"[pg] 清理 {table}: 删除 {result.rowcount} 行（project: {len(project_ids)} 个）")


async def import_sqlite_projection(
    engine: AsyncEngine, sqlite_path: Path, projects: list[str] | None,
) -> int:
    """把 sqlite 检索投影四表导入 PG。

    只写四张检索表；sqlite 全程只读；返回导入的 chunk 总数。
    幂等：先按 project 删旧投影行再插入（投影表可重建语义）。
    """
    project_set: set[str] | None = {str(p) for p in projects} if projects else None
    chunks = _read_sqlite_rows(sqlite_path, "retrieval_chunks", project_set)
    fts_rows = _read_sqlite_rows(sqlite_path, "retrieval_chunks_fts", project_set)
    gens = _read_sqlite_rows(sqlite_path, "retrieval_index_generation", project_set)
    states = _read_sqlite_rows(sqlite_path, "retrieval_index_source_state", project_set)

    affected = sorted({
        str(r["project_id"]) for r in chunks + fts_rows + gens + states if r.get("project_id")
    })
    print(f"[import] sqlite 读取完成: chunks={len(chunks)} fts={len(fts_rows)} "
          f"generations={len(gens)} states={len(states)} 涉及项目={len(affected)}")

    async with engine.begin() as conn:
        await _delete_projection_for_projects(conn, affected)

        # chunks（id/created_at/updated_at 需要类型转换：uuid / datetime；metadata JSONB）
        chunk_params = [
            {
                "id": _as_uuid(r["id"]),
                "project_id": str(r["project_id"]),
                "source_kind": str(r["source_kind"]),
                "source_id": str(r["source_id"]),
                "source_version_id": r["source_version_id"],
                "index_generation_id": str(r["index_generation_id"]),
                "chunk_index": int(r["chunk_index"]),
                "content": r["content"] or "",
                "search_text": r["search_text"] or "",
                "content_hash": str(r["content_hash"] or ""),
                "metadata": json.loads(r["metadata"]) if isinstance(r["metadata"], str) and r["metadata"] else r["metadata"],
                "created_at": _parse_dt(r.get("created_at")),
                "updated_at": _parse_dt(r.get("updated_at")),
            }
            for r in chunks
        ]
        for start in range(0, len(chunk_params), _IMPORT_BATCH):
            await conn.execute(
                text(
                    "INSERT INTO retrieval_chunks (id, project_id, source_kind, source_id, "
                    "source_version_id, index_generation_id, chunk_index, content, search_text, "
                    "content_hash, metadata, created_at, updated_at) VALUES "
                    "(:id, :project_id, :source_kind, :source_id, :source_version_id, "
                    ":index_generation_id, :chunk_index, :content, :search_text, :content_hash, "
                    ":metadata, :created_at, :updated_at)"
                ),
                chunk_params[start : start + _IMPORT_BATCH],
            )

        # fts 侧（普通表，类型与 retrieval_indexer.PG_FTS_TABLE_CREATE_SQL 一致）
        fts_params = [
            {
                "chunk_id": str(r["chunk_id"]),
                "project_id": str(r["project_id"]),
                "source_kind": str(r["source_kind"]),
                "source_id": str(r["source_id"]),
                "chapter_no": r["chapter_no"],
                "title": r["title"] or "",
                "content": r["content"] or "",
                "search_text": r["search_text"] or "",
            }
            for r in fts_rows
        ]
        for start in range(0, len(fts_params), _IMPORT_BATCH):
            await conn.execute(
                text(
                    "INSERT INTO retrieval_chunks_fts (chunk_id, project_id, source_kind, "
                    "source_id, chapter_no, title, content, search_text) VALUES "
                    "(:chunk_id, :project_id, :source_kind, :source_id, :chapter_no, :title, "
                    ":content, :search_text)"
                ),
                fts_params[start : start + _IMPORT_BATCH],
            )

        # generation / source_state
        gen_params = [
            {
                "id": _as_uuid(r["id"]),
                "project_id": str(r["project_id"]),
                "generation_id": str(r["generation_id"]),
                "source_kind": str(r["source_kind"]),
                "source_id": str(r["source_id"]),
                "status": str(r["status"]),
                "error": r["error"],
                "started_at": _parse_dt(r.get("started_at")),
                "finished_at": _parse_dt(r.get("finished_at")),
                "created_at": _parse_dt(r.get("created_at")),
                "updated_at": _parse_dt(r.get("updated_at")),
            }
            for r in gens
        ]
        for start in range(0, len(gen_params), _IMPORT_BATCH):
            await conn.execute(
                text(
                    "INSERT INTO retrieval_index_generation (id, project_id, generation_id, "
                    "source_kind, source_id, status, error, started_at, finished_at, "
                    "created_at, updated_at) VALUES (:id, :project_id, :generation_id, "
                    ":source_kind, :source_id, :status, :error, :started_at, :finished_at, "
                    ":created_at, :updated_at)"
                ),
                gen_params[start : start + _IMPORT_BATCH],
            )

        state_params = [
            {
                "id": _as_uuid(r["id"]),
                "project_id": str(r["project_id"]),
                "source_kind": str(r["source_kind"]),
                "source_id": str(r["source_id"]),
                "active_generation_id": str(r["active_generation_id"]),
                "created_at": _parse_dt(r.get("created_at")),
                "updated_at": _parse_dt(r.get("updated_at")),
            }
            for r in states
        ]
        for start in range(0, len(state_params), _IMPORT_BATCH):
            await conn.execute(
                text(
                    "INSERT INTO retrieval_index_source_state (id, project_id, source_kind, "
                    "source_id, active_generation_id, created_at, updated_at) VALUES "
                    "(:id, :project_id, :source_kind, :source_id, :active_generation_id, "
                    ":created_at, :updated_at)"
                ),
                state_params[start : start + _IMPORT_BATCH],
            )

    print(f"[import] PG 写入完成: chunks={len(chunk_params)} fts={len(fts_params)} "
          f"generations={len(gen_params)} states={len(state_params)}")

    # ANALYZE 让规划器拿到真实统计：GIN 表达式索引才能在查询中生效
    # （否则小表 + 新导入数据时规划器倾向顺序扫描，tsvector 全表重算）。
    async with engine.begin() as conn:
        await conn.execute(text("ANALYZE retrieval_chunks"))
        await conn.execute(text("ANALYZE retrieval_chunks_fts"))
    print("[import] ANALYZE 完成（retrieval_chunks / retrieval_chunks_fts）")
    return len(chunk_params)


# ── 状态报告与 EXPLAIN ───────────────────────────────────

async def report_projection_counts(engine: AsyncEngine) -> None:
    """打印 PG 检索四表现有数据量（不动任何数据）。"""
    async with engine.connect() as conn:
        for table in _RETRIEVAL_TABLES:
            try:
                n = (
                    await conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                ).scalar() or 0
                print(f"[pg] 现有 {table}: {n} 行")
            except Exception as exc:  # noqa: BLE001 — 表不存在仅提示
                print(f"[pg] 现有 {table}: 不存在（{exc.__class__.__name__}）")


async def explain_fts_query(engine: AsyncEngine) -> None:
    """打印代表性检索查询的 EXPLAIN，确认 GIN 索引被使用。"""
    async with engine.connect() as conn:
        try:
            plan = (
                await conn.execute(
                    text(
                        "EXPLAIN SELECT f.chunk_id, ts_rank(f.search_tsv, "
                        "to_tsquery('simple', '莫凡 | 雷系')) AS bm "
                        "FROM retrieval_chunks_fts f "
                        "JOIN retrieval_chunks c ON c.id::text = f.chunk_id "
                        "WHERE f.search_tsv @@ to_tsquery('simple', '莫凡 | 雷系') "
                        "AND f.project_id = 'd62d4447-b8d7-4fc3-9193-a6d363d8f541' "
                        "ORDER BY bm DESC LIMIT 5"
                    )
                )
            ).scalars().all()
            print("\n[pg] 检索查询 EXPLAIN（确认索引使用）:")
            for line in plan:
                print(f"      {line}")
        except Exception as exc:  # noqa: BLE001
            print(f"[pg] EXPLAIN 失败（可忽略，不影响建表/导入）: {exc}")


# ── 主流程 ───────────────────────────────────────────────

async def run(args) -> int:
    engine = create_async_engine(args.pg_url)
    try:
        print(f"[pg] 目标数据库: {args.pg_url}")
        await report_projection_counts(engine)
        await setup_pg_tables(engine)
        await setup_indexes(engine, with_trgm=not args.no_trgm)

        total_chunks = 0
        if args.import_sqlite:
            if not Path(args.sqlite).exists():
                print(f"[error] sqlite 数据库不存在: {args.sqlite}", file=sys.stderr)
                return 1
            total_chunks = await import_sqlite_projection(
                engine, Path(args.sqlite), args.project
            )
        print(f"[ok] setup_pg 完成（导入 chunks: {total_chunks}）")
        await report_projection_counts(engine)
        await explain_fts_query(engine)
        return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="PostgreSQL 检索投影表初始化：建表 + FTS GIN/trgm 索引 + （可选）从 sqlite 导入检索四表"
    )
    parser.add_argument("--pg-url", required=True,
                        help="必须显式指定隔离 PostgreSQL 测试库连接串")
    parser.add_argument("--sqlite", default=str(DEFAULT_SQLITE),
                        help="dev sqlite 数据库路径（仅 --import-sqlite 时读取，只读）")
    parser.add_argument("--import-sqlite", action="store_true",
                        help="把 sqlite 检索投影四表数据导入 PG（幂等：先删同项目旧投影行）")
    parser.add_argument("--project", action="append", default=None,
                        help="--import-sqlite 时只导入指定项目（可多次传）；默认全部")
    parser.add_argument("--no-trgm", action="store_true",
                        help="跳过 pg_trgm GIN 索引（仅建 tsvector GIN 索引）")
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
