"""检索投影模型层（阶段一）测试：建表、CRUD、唯一约束。

每个测试独立使用 SQLite 内存库（StaticPool 共享同一连接）。
只导入 retrieval_index 模块，因此 Base.metadata.create_all 只创建本层三张表。
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from models.base import Base
from models.retrieval_index import (
    RetrievalChunk,
    RetrievalIndexGeneration,
    RetrievalIndexSourceState,
)


@pytest_asyncio.fixture
async def db():
    """独立的 SQLite 内存库会话。"""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as session:
            yield session
    finally:
        await engine.dispose()


def _pid() -> str:
    return str(uuid.uuid4())


@pytest.mark.asyncio
async def test_three_tables_created(db):
    """create_all 后三张检索投影表都实际落库。"""
    rows = (
        await db.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        )
    ).scalars().all()
    assert "retrieval_chunks" in rows
    assert "retrieval_index_generation" in rows
    assert "retrieval_index_source_state" in rows


@pytest.mark.asyncio
async def test_retrieval_chunk_crud_and_composite_lookup(db):
    """RetrievalChunk 增删改查 + (project_id, source_kind, source_id) 组合查询。"""
    project_id = _pid()
    chunk = RetrievalChunk(
        project_id=project_id,
        source_kind="chapter_version",
        source_id="chapter-0001",
        source_version_id="version-3",
        index_generation_id="gen-0001",
        chunk_index=0,
        content="第一章原文片段……",
        search_text="第一章 主角 觉醒 灵气",
        content_hash="sha256-abc123",
        metadata_={"chapter_no": 1, "lang": "zh"},
    )
    db.add(chunk)
    await db.commit()

    # 读取
    loaded = await db.get(RetrievalChunk, chunk.id)
    assert loaded is not None
    assert loaded.project_id == project_id
    assert loaded.source_kind == "chapter_version"
    assert loaded.content == "第一章原文片段……"
    assert loaded.search_text == "第一章 主角 觉醒 灵气"
    assert loaded.content_hash == "sha256-abc123"
    assert loaded.metadata_ == {"chapter_no": 1, "lang": "zh"}

    # 更新
    loaded.search_text = "第一章 主角 灵气 剑道"
    await db.commit()

    # 组合索引列查询，且只命中目标源
    db.add(
        RetrievalChunk(
            project_id=project_id,
            source_kind="character_profile",
            source_id="char-0001",
            index_generation_id="gen-0002",
            chunk_index=0,
            content="人物档案……",
            search_text="人物档案",
            content_hash="sha256-def456",
        )
    )
    await db.commit()

    fetched = (
        await db.execute(
            select(RetrievalChunk).where(
                RetrievalChunk.project_id == project_id,
                RetrievalChunk.source_kind == "chapter_version",
                RetrievalChunk.source_id == "chapter-0001",
            )
        )
    ).scalars().all()
    assert len(fetched) == 1
    assert fetched[0].search_text == "第一章 主角 灵气 剑道"
    assert fetched[0].metadata_ == {"chapter_no": 1, "lang": "zh"}

    # 删除
    await db.delete(fetched[0])
    await db.commit()
    assert await db.get(RetrievalChunk, chunk.id) is None


@pytest.mark.asyncio
async def test_source_state_unique_project_kind_source(db):
    """同一 (project_id, source_kind, source_id) 只允许一条活动指针。"""
    project_id = _pid()
    db.add(
        RetrievalIndexSourceState(
            project_id=project_id,
            source_kind="character_profile",
            source_id="char-0001",
            active_generation_id="gen-0001",
        )
    )
    await db.commit()

    db.add(
        RetrievalIndexSourceState(
            project_id=project_id,
            source_kind="character_profile",
            source_id="char-0001",
            active_generation_id="gen-0002",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()

    # 同 project 不同 source_id 不受影响
    db.add(
        RetrievalIndexSourceState(
            project_id=project_id,
            source_kind="character_profile",
            source_id="char-0002",
            active_generation_id="gen-0002",
        )
    )
    await db.commit()

    states = (
        await db.execute(
            select(RetrievalIndexSourceState).where(
                RetrievalIndexSourceState.project_id == project_id
            )
        )
    ).scalars().all()
    assert len(states) == 2


@pytest.mark.asyncio
async def test_index_generation_status_default_and_unique_generation_id(db):
    """构建记录默认 status=building，generation_id 全局唯一。"""
    project_id = _pid()
    generation = RetrievalIndexGeneration(
        project_id=project_id,
        generation_id="gen-0001",
        source_kind="world_entry",
        source_id="world-0001",
    )
    db.add(generation)
    await db.commit()
    assert generation.status == "building"
    assert generation.started_at is None
    assert generation.finished_at is None

    generation.status = "active"
    generation.finished_at = None  # 只验证可更新
    db.add(generation)
    await db.commit()
    assert generation.status == "active"

    # 同一 generation_id 重复插入应被唯一约束拒绝
    db.add(
        RetrievalIndexGeneration(
            project_id=_pid(),
            generation_id="gen-0001",
            source_kind="world_entry",
            source_id="world-0002",
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()
