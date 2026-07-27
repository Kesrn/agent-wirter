"""桌面端 SQLite 旧库的安全增量迁移测试。"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from db.session import _ensure_incremental_columns, _missing_sqlite_model_columns
from models.base import Base


def test_legacy_sqlite_gets_outline_and_hidden_thread_columns():
    """旧版素材表启动后必须能被当前 ORM 正常读取。"""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as conn:
                # 模拟 024~027 迁移前的桌面数据库结构。
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("DROP TABLE outlines"))
                await conn.execute(text("DROP TABLE hidden_threads"))
                await conn.execute(text(
                    "CREATE TABLE outlines ("
                    "project_id CHAR(36) NOT NULL, sequence_number INTEGER NOT NULL, "
                    "title VARCHAR(200) NOT NULL, summary TEXT, turning_point TEXT, "
                    "hidden_thread_ids JSON, id CHAR(36) PRIMARY KEY, "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
                ))
                await conn.execute(text(
                    "CREATE TABLE hidden_threads ("
                    "project_id CHAR(36) NOT NULL, name VARCHAR(200) NOT NULL, "
                    "description TEXT, chapter_nums JSON, id CHAR(36) PRIMARY KEY, "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
                ))

                missing_before = await _missing_sqlite_model_columns(
                    conn, {"outlines", "hidden_threads"}
                )
                assert "story_arc_id" in missing_before["outlines"]
                assert "status" in missing_before["hidden_threads"]

                await _ensure_incremental_columns(conn)

                outline_columns = {
                    row[1]
                    for row in (await conn.execute(text("PRAGMA table_info(outlines)"))).fetchall()
                }
                hidden_thread_columns = {
                    row[1]
                    for row in (await conn.execute(text("PRAGMA table_info(hidden_threads)"))).fetchall()
                }

                assert {
                    "story_arc_id", "arc_position", "pacing", "tension_level", "target_scene_count",
                }.issubset(outline_columns)
                assert {
                    "status", "thread_type", "planted_chapter", "reveal_chapter",
                    "resolved_chapter", "payoff_summary", "risk_level",
                }.issubset(hidden_thread_columns)
                assert await _missing_sqlite_model_columns(
                    conn, {"outlines", "hidden_threads"}
                ) == {}
        finally:
            await engine.dispose()

    asyncio.run(run())
