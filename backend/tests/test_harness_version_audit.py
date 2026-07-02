"""Phase F: 版本审计增强测试 — SQLite 内存数据库"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"

@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from models.base import Base

# 在 fixture 的 create_all 之前注册全部模型到 Base.metadata。
import models.project  # noqa: F401
import models.chapter  # noqa: F401
import models.chapter_version  # noqa: F401
import models.generation_record  # noqa: F401

from models.chapter_version import ChapterVersion
from models.chapter import Chapter


@pytest_asyncio.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.fixture
def chapter_id_fixture():
    import uuid
    return uuid.uuid4()


@pytest.mark.asyncio
async def test_create_version_with_run_id(async_db, chapter_id_fixture):
    """create_version 应将 run_id 写入 ChapterVersion.run_id。"""
    from services.version_service import create_version

    version = await create_version(
        async_db, chapter_id_fixture, "内容A", source="ai_approve",
        run_id="11111111-1111-1111-1111-111111111111",
    )
    assert version is not None
    assert str(version.run_id) == "11111111-1111-1111-1111-111111111111"


@pytest.mark.asyncio
async def test_create_version_with_parent_version_id(async_db, chapter_id_fixture):
    """create_version 应将 parent_version_id 写入 ChapterVersion。"""
    from services.version_service import create_version

    v1 = await create_version(async_db, chapter_id_fixture, "第一版", source="manual")
    v2 = await create_version(
        async_db, chapter_id_fixture, "第二版", source="ai_approve",
        parent_version_id=str(v1.id),
    )
    assert str(v2.parent_version_id) == str(v1.id)


@pytest.mark.asyncio
async def test_create_version_with_project_id(async_db, chapter_id_fixture):
    """create_version 应将 project_id 写入 ChapterVersion。"""
    from services.version_service import create_version

    version = await create_version(
        async_db, chapter_id_fixture, "内容", source="manual",
        project_id="22222222-2222-2222-2222-222222222222",
    )
    assert str(version.project_id) == "22222222-2222-2222-2222-222222222222"


@pytest.mark.asyncio
async def test_create_version_run_id_none_default(async_db, chapter_id_fixture):
    """不传 run_id 时，ChapterVersion.run_id 为 None（向后兼容）。"""
    from services.version_service import create_version

    version = await create_version(async_db, chapter_id_fixture, "内容", source="manual")
    assert version.run_id is None
    assert version.parent_version_id is None
    assert version.project_id is None


@pytest.mark.asyncio
async def test_prune_spare_referenced_version(async_db, chapter_id_fixture):
    """prune 不应删除被 GenerationRecord.accepted_version_id 引用的版本。"""
    from services.version_service import create_version, MAX_VERSIONS_PER_CHAPTER
    from models.generation_record import GenerationRecord

    # 创建 10 个版本（达到上限）
    versions = []
    for i in range(MAX_VERSIONS_PER_CHAPTER):
        v = await create_version(async_db, chapter_id_fixture, f"内容{i}", source="manual")
        versions.append(v)

    # 让第 3 个版本被 generation_record 引用
    record = GenerationRecord(
        project_id="33333333-3333-3333-3333-333333333333",
        content="候选",
        word_count=2,
        mode="full_pipeline",
        status="applied",
        accepted_version_id=versions[2].id,
    )
    async_db.add(record)
    await async_db.flush()

    # 再创建一个版本，触发 prune
    await create_version(async_db, chapter_id_fixture, "内容超限", source="manual")
    await async_db.flush()

    # 被引用版本不应被删除
    from sqlalchemy import select
    result = await async_db.execute(
        select(ChapterVersion).where(ChapterVersion.id == versions[2].id)
    )
    assert result.scalar_one_or_none() is not None, "被 generation_record 引用的版本不应被 prune 删除"
