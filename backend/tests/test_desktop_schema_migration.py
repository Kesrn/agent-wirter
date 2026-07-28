"""桌面端 SQLite 旧库的安全增量迁移测试。"""

import asyncio

import pytest
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


def test_legacy_sqlite_llm_config_unique_user_is_upgraded_to_profiles():
    """旧桌面库每用户只能一条配置；升级后应允许多条且仅一条 active。"""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("DROP TABLE llm_configs"))
                await conn.execute(text(
                    "CREATE TABLE llm_configs ("
                    "id CHAR(36) PRIMARY KEY, user_id CHAR(36) NOT NULL UNIQUE, "
                    "provider VARCHAR(20) NOT NULL, encrypted_api_key VARCHAR(500), "
                    "base_url VARCHAR(500), model_id VARCHAR(100), "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
                ))
                user_id = "11111111-1111-1111-1111-111111111111"
                await conn.execute(text(
                    "INSERT INTO llm_configs (id, user_id, provider, model_id) "
                    "VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', :uid, 'openai', 'gpt-test')"
                ), {"uid": user_id})

                await _ensure_incremental_columns(conn)

                columns = {
                    row[1] for row in (await conn.execute(text("PRAGMA table_info(llm_configs)"))).fetchall()
                }
                assert {"name", "is_active"}.issubset(columns)

                # user_id 不再唯一，因此同一用户可插入第二条配置。
                await conn.execute(text(
                    "INSERT INTO llm_configs (id, user_id, name, provider, model_id, is_active) "
                    "VALUES ('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', :uid, 'DeepSeek 备用', "
                    "'deepseek', 'deepseek-test', 0)"
                ), {"uid": user_id})
                rows = (await conn.execute(text(
                    "SELECT name, is_active FROM llm_configs WHERE user_id = :uid ORDER BY id"
                ), {"uid": user_id})).fetchall()
                assert len(rows) == 2
                assert sum(bool(row[1]) for row in rows) == 1
        finally:
            await engine.dispose()

    asyncio.run(run())


def test_partial_llm_profile_upgrade_normalizes_multiple_active_rows():
    """部分升级库即使已有多条 active，也应恢复为单 active 并保持幂等。"""

    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite://")
        try:
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
                await conn.execute(text("DROP TABLE llm_configs"))
                await conn.execute(text(
                    "CREATE TABLE llm_configs ("
                    "id CHAR(36) PRIMARY KEY, user_id CHAR(36) NOT NULL, "
                    "name VARCHAR(100), provider VARCHAR(20) NOT NULL, "
                    "encrypted_api_key VARCHAR(500), base_url VARCHAR(500), "
                    "model_id VARCHAR(100), is_active BOOLEAN NOT NULL DEFAULT 0, "
                    "created_at DATETIME DEFAULT CURRENT_TIMESTAMP, "
                    "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP)"
                ))
                user_id = "22222222-2222-2222-2222-222222222222"
                await conn.execute(text(
                    "INSERT INTO llm_configs (id, user_id, name, provider, is_active) VALUES "
                    "('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', :uid, '主配置', 'openai', 1), "
                    "('bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb', :uid, '备用配置', 'deepseek', 1)"
                ), {"uid": user_id})

                await _ensure_incremental_columns(conn)
                await _ensure_incremental_columns(conn)

                active_count = (await conn.execute(text(
                    "SELECT COUNT(*) FROM llm_configs WHERE user_id = :uid AND is_active = 1"
                ), {"uid": user_id})).scalar_one()
                assert active_count == 1

                with pytest.raises(Exception):
                    await conn.execute(text(
                        "UPDATE llm_configs SET is_active = 1 WHERE user_id = :uid"
                    ), {"uid": user_id})
        finally:
            await engine.dispose()

    asyncio.run(run())
