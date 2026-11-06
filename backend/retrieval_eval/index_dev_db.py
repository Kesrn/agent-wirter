#!/usr/bin/env python3
"""开发库检索索引初始化脚本（阶段一：LexicalRetriever 前置步骤）。

对 backend/dev-web.sqlite3 建立词法检索索引：把 project_sources +
project_source_chunks 投影到 retrieval_chunks / retrieval_chunks_fts，
并写入 retrieval_index_generation（active）与 retrieval_index_source_state
（活动指针）。评测（run_baseline.py --retriever lexical）之前必须先运行本脚本。

安全约束（严格遵守）
--------------------
- 只允许写四张新表：retrieval_chunks / retrieval_chunks_fts /
  retrieval_index_generation / retrieval_index_source_state。
  - 建表用 Base.metadata.create_all(tables=[...]) 显式限定三张 ORM 表
    （已存在的表不会被触碰；业务表一律不建不改）；
  - FTS5 虚拟表由 services.retrieval_indexer.ensure_fts_table 显式创建。
- 业务表（project_sources / project_source_chunks / character_profile /
  ability_profile / event_timeline / world_entries 等）全程只读（SELECT），
  绝不 UPDATE / DELETE / INSERT。

用法
----
    cd backend
    venv/bin/python retrieval_eval/index_dev_db.py
    # 可选：venv/bin/python retrieval_eval/index_dev_db.py --db /path/to/dev.sqlite3

重复运行安全：content_hash 幂等，内容未变时直接跳过（不重复写块、不新建
generation）。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

DEFAULT_DB = Path(__file__).resolve().parent.parent / "dev-web.sqlite3"

# 保证能 import db/services/models（脚本位于 retrieval_eval/ 下，与 run_baseline.py 同策略）
BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR))


async def run(db_path: str) -> int:
    from models.base import Base
    from models.retrieval_index import (
        RetrievalChunk,
        RetrievalIndexGeneration,
        RetrievalIndexSourceState,
    )
    from services.retrieval_indexer import ensure_fts_table, index_source_chunks

    db_path = str(Path(db_path).resolve())
    print(f"[index] 数据库: {db_path}")

    engine = create_async_engine(f"sqlite+aiosqlite:///{db_path}")
    try:
        # 1) 只建检索投影相关的表（其余表一律不动）
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
            print("[index] 检索表已就绪（retrieval_chunks / retrieval_chunks_fts / "
                  "retrieval_index_generation / retrieval_index_source_state）")

        factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with factory() as db:
            # 2) 只读业务表 → 投影写入（只写 retrieval 表）
            from models.project_source import ProjectSource

            sources = (
                await db.execute(select(ProjectSource).order_by(ProjectSource.project_id))
            ).scalars().all()
            if not sources:
                print("[index] project_sources 为空，无事可做")
                return 0

            total_chunks = 0
            indexed_sources = 0
            skipped_sources = 0
            for i, source in enumerate(sources, start=1):
                pid, sid = str(source.project_id), str(source.id)
                gen_id = await index_source_chunks(db, pid, sid)
                # indexer 已移除内部 commit（P0：事务由调用方统一控制），脚本每源提交一次
                await db.commit()
                n = 0
                if gen_id is not None:
                    n = (
                        await db.execute(
                            text(
                                "SELECT COUNT(*) FROM retrieval_chunks "
                                "WHERE index_generation_id = :g"
                            ),
                            {"g": gen_id},
                        )
                    ).scalar() or 0
                    indexed_sources += 1
                else:
                    skipped_sources += 1
                total_chunks += n
                print(
                    f"  [{i}/{len(sources)}] project={pid[:8]}.. source={sid[:8]}.. "
                    f"title={source.title!r} -> generation={gen_id[:8] if gen_id else 'skip'} "
                    f"chunks={n}"
                )

            print(
                f"\n[ok] 完成：sources 共 {len(sources)}（新建 {indexed_sources}，"
                f"幂等跳过 {skipped_sources}），检索块共 {total_chunks}"
            )
            return 0
    finally:
        await engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="为 dev-web.sqlite3 建立词法检索索引（只写四张 retrieval 表，业务表只读）"
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB),
        help=f"SQLite 数据库路径（默认 {DEFAULT_DB.name}）",
    )
    args = parser.parse_args()
    if not Path(args.db).exists():
        print(f"[error] 数据库文件不存在: {args.db}", file=sys.stderr)
        return 1
    return asyncio.run(run(args.db))


if __name__ == "__main__":
    sys.exit(main())
