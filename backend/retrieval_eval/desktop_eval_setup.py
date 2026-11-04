# -*- coding: utf-8 -*-
"""在桌面库副本上建立新检索索引（绘魔者项目影子评测用）。

流程：
1. init_db()：创建缺失的 ORM 表（含检索四表+job 表，FTS5 表由 indexer 显式建）
2. 把旧 RAG 的 characters 表投影为 retrieval_chunks（source_kind=character_profile），
   保证新旧链路数据同源可比（旧链路读 characters 表，新链路读 retrieval_chunks）
3. rebuild_project()：全量重建（source_chunk / chapter_version / structured / world_entry）

只操作副本（retrieval_eval/data/desktop_eval_20260803.sqlite3），生产桌面库零接触。
"""
import asyncio
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

DB_PATH = Path(__file__).resolve().parent / "data" / "desktop_eval_20260803.sqlite3"
os.environ["DATABASE_URL"] = f"sqlite+aiosqlite:///{DB_PATH}"  # 必须在 import settings 前

from db.session import async_session, init_db  # noqa: E402
from models.character import Character  # noqa: E402
from services.retrieval_indexer import build_structured_index, rebuild_project  # noqa: E402

PROJECT_ID = "b68d3daa-9704-4388-ad75-1a00a415c7f3"  # 全职法师之绘魔者


async def main() -> None:
    print(f"[setup] 副本: {DB_PATH}")
    await init_db()
    print("[setup] init_db 完成（缺失表已创建）")

    async with async_session() as db:
        # 1) 投影旧 RAG characters 表 → retrieval_chunks（同源可比）
        chars = (
            await db.execute(
                __import__("sqlalchemy").select(Character).where(Character.project_id == PROJECT_ID)
            )
        ).scalars().all()
        projected = 0
        for c in chars:
            content = f"{c.name}（{c.role_type}）：{c.profile or ''}"
            gid = await build_structured_index(
                db, PROJECT_ID, "character_profile", str(c.id),
                title=c.name, content=content,
            )
            if gid:
                projected += 1
        await db.commit()
        print(f"[setup] characters 投影完成: {projected}/{len(chars)}")

        # 2) 全量重建（sources / chapter_version / structured / world_entry）
        result = await rebuild_project(db, PROJECT_ID)
        await db.commit()
        print(f"[setup] rebuild_project 结果: {result}")


if __name__ == "__main__":
    asyncio.run(main())
