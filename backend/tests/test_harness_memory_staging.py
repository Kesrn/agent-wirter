"""Phase H1: 写作记忆 staging 数据层测试"""

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
import models.project  # noqa: F401
import models.chapter  # noqa: F401
import models.writing_memory_staging  # noqa: F401


@pytest_asyncio.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def test_memory_staging_status_enum():
    from models.harness_enums import MemoryStagingStatus
    assert MemoryStagingStatus.GENERATED == "GENERATED"
    assert MemoryStagingStatus.CONFIRMED == "CONFIRMED"
    assert MemoryStagingStatus.REJECTED == "REJECTED"


def test_memory_type_enum():
    from models.harness_enums import MemoryType
    for t in ["CHARACTER", "WORLD_RULE", "PLOT_FACT", "EVENT", "FORESHADOWING"]:
        assert hasattr(MemoryType, t)


@pytest.mark.asyncio
async def test_writing_memory_staging_create(async_db):
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType

    item = WritingMemoryStaging(
        project_id="11111111-1111-1111-1111-111111111111",
        run_id="22222222-2222-2222-2222-222222222222",
        chapter_id="33333333-3333-3333-3333-333333333333",
        chapter_version_id="44444444-4444-4444-4444-444444444444",
        chapter_sequence_number=3,
        memory_type=MemoryType.CHARACTER,
        title="角色A",
        payload={"name": "角色A", "role_type": "protagonist", "profile": "冷酷杀手"},
        evidence="角色A在第三章出手击杀目标",
        status=MemoryStagingStatus.GENERATED,
    )
    async_db.add(item)
    await async_db.flush()

    assert item.id is not None
    assert item.status == MemoryStagingStatus.GENERATED
    assert item.memory_type == MemoryType.CHARACTER
    assert item.payload["name"] == "角色A"
    assert item.evidence == "角色A在第三章出手击杀目标"
    assert item.chapter_sequence_number == 3
    assert item.reviewed_at is None
    assert item.confirmed_target_type is None
    assert item.confirmed_target_id is None
    assert item.reviewed_by is None
    assert item.idempotency_key is None


@pytest.mark.asyncio
async def test_writing_memory_staging_status_transition(async_db):
    from models.writing_memory_staging import WritingMemoryStaging
    from models.harness_enums import MemoryStagingStatus, MemoryType
    from datetime import datetime, timezone

    item = WritingMemoryStaging(
        project_id="55555555-5555-5555-5555-555555555555",
        memory_type=MemoryType.WORLD_RULE,
        title="魔法体系A",
        payload={},
        evidence="证据",
        status=MemoryStagingStatus.GENERATED,
    )
    async_db.add(item)
    await async_db.flush()

    item.status = MemoryStagingStatus.CONFIRMED
    item.reviewed_at = datetime.now(timezone.utc)
    item.reviewed_by = "user123"
    item.confirmed_target_type = "WorldEntry"
    item.confirmed_target_id = "66666666-6666-6666-6666-666666666666"
    await async_db.flush()

    assert item.status == MemoryStagingStatus.CONFIRMED
    assert item.reviewed_at is not None
    assert item.reviewed_by == "user123"
    assert item.confirmed_target_type == "WorldEntry"


@pytest.mark.asyncio
async def test_writing_memory_staging_columns_exist(async_db):
    """验证表结构包含所有修正字段。"""
    cols = {c.name: c for c in Base.metadata.tables["writing_memory_staging"].columns}
    for name in [
        "id", "project_id", "run_id", "chapter_id", "chapter_version_id",
        "chapter_sequence_number", "memory_type", "title", "payload", "evidence",
        "status", "confirmed_target_type", "confirmed_target_id",
        "reviewed_by", "reviewed_at", "idempotency_key",
        "created_at", "updated_at",
    ]:
        assert name in cols, f"writing_memory_staging 缺列 {name}"
    # payload 不应是 nullable（有 default）
    assert cols["payload"].nullable is False or cols["payload"].default is not None
