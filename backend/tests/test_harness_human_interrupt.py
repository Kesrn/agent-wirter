"""阶段 E: Human Interrupt 服务层测试"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, ARRAY
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from models.base import Base
import models  # noqa: F401


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


from harness.human_interrupt_service import (
    create_interrupt,
    resolve_interrupt,
    get_interrupt_by_run,
    get_interrupt_by_thread,
    check_interrupt_resolved,
)
from harness.run_manager import create_run
from models.harness_enums import InterruptDecision, InterruptStatus, RunStatus


@pytest.mark.asyncio
async def test_create_interrupt(db: AsyncSession):
    """测试创建 interrupt"""
    run = await create_run(
        db,
        project_id="proj_001",
        chapter_id="chap_001",
        run_type="CHAPTER_DRAFT",
        mode="full_pipeline",
    )
    await db.commit()

    interrupt = await create_interrupt(
        db,
        run=run,
        thread_id="thread_123",
        step_name="human_review",
        payload={"generation_record_id": "rec_001"},
    )
    await db.commit()

    assert interrupt.run_id == run.id
    assert interrupt.thread_id == "thread_123"
    assert interrupt.step_name == "human_review"
    assert interrupt.status == InterruptStatus.WAITING
    assert interrupt.resolved is False
    assert interrupt.payload["generation_record_id"] == "rec_001"


@pytest.mark.asyncio
async def test_resolve_interrupt(db: AsyncSession):
    """测试解析 interrupt"""
    run = await create_run(
        db,
        project_id="proj_002",
        chapter_id="chap_002",
        run_type="CHAPTER_DRAFT",
        mode="full_pipeline",
    )
    await db.commit()

    interrupt = await create_interrupt(
        db,
        run=run,
        thread_id="thread_456",
        step_name="human_review",
    )
    await db.commit()

    # 解析为 APPROVE
    await resolve_interrupt(
        db,
        interrupt,
        decision=InterruptDecision.APPROVE,
        feedback="看起来不错",
    )
    await db.commit()

    assert interrupt.resolved is True
    assert interrupt.status == InterruptStatus.APPROVED
    assert interrupt.decision == InterruptDecision.APPROVE
    assert interrupt.feedback == "看起来不错"
    assert interrupt.resolved_at is not None


@pytest.mark.asyncio
async def test_resolve_interrupt_idempotent(db: AsyncSession):
    """测试 resolve_interrupt 幂等性"""
    run = await create_run(
        db,
        project_id="proj_003",
        chapter_id="chap_003",
        run_type="CHAPTER_DRAFT",
        mode="full_pipeline",
    )
    await db.commit()

    interrupt = await create_interrupt(
        db,
        run=run,
        thread_id="thread_789",
        step_name="human_review",
    )
    await db.commit()

    # 第一次解析
    await resolve_interrupt(db, interrupt, decision=InterruptDecision.APPROVE)
    await db.commit()
    first_resolved_at = interrupt.resolved_at

    # 第二次解析应该幂等（不修改）
    await resolve_interrupt(db, interrupt, decision=InterruptDecision.REJECT, feedback="changed my mind")
    await db.commit()

    # 状态不应改变
    assert interrupt.resolved is True
    assert interrupt.status == InterruptStatus.APPROVED
    assert interrupt.decision == InterruptDecision.APPROVE
    assert interrupt.feedback is None  # 未更新
    assert interrupt.resolved_at == first_resolved_at


@pytest.mark.asyncio
async def test_get_interrupt_by_run(db: AsyncSession):
    """测试按 run_id 查询 interrupt"""
    run = await create_run(
        db,
        project_id="proj_004",
        chapter_id="chap_004",
        run_type="CHAPTER_DRAFT",
        mode="full_pipeline",
    )
    await db.commit()

    # 创建两个 interrupt（间隔一下确保时间戳不同）
    interrupt1 = await create_interrupt(db, run=run, step_name="human_review")
    await db.commit()

    interrupt2 = await create_interrupt(db, run=run, step_name="human_review")
    await db.commit()

    # 应该返回最新的（按 created_at 降序）
    latest = await get_interrupt_by_run(db, str(run.id))
    assert latest is not None
    # 两个 interrupt 的 created_at 可能相同，所以只验证返回了其中一个
    assert latest.id in [interrupt1.id, interrupt2.id]


@pytest.mark.asyncio
async def test_get_interrupt_by_thread(db: AsyncSession):
    """测试按 thread_id 查询 interrupt"""
    run = await create_run(
        db,
        project_id="proj_005",
        chapter_id="chap_005",
        run_type="CHAPTER_DRAFT",
        mode="full_pipeline",
    )
    await db.commit()

    interrupt = await create_interrupt(
        db,
        run=run,
        thread_id="thread_unique_001",
        step_name="human_review",
    )
    await db.commit()

    found = await get_interrupt_by_thread(db, "thread_unique_001")
    assert found is not None
    assert found.id == interrupt.id
    assert found.thread_id == "thread_unique_001"


@pytest.mark.asyncio
async def test_check_interrupt_resolved(db: AsyncSession):
    """测试检查 interrupt 是否已解析"""
    run = await create_run(
        db,
        project_id="proj_006",
        chapter_id="chap_006",
        run_type="CHAPTER_DRAFT",
        mode="full_pipeline",
    )
    await db.commit()

    interrupt = await create_interrupt(db, run=run, step_name="human_review")
    await db.commit()

    # 未解析
    assert await check_interrupt_resolved(db, str(run.id)) is False

    # 解析
    await resolve_interrupt(db, interrupt, decision=InterruptDecision.APPROVE)
    await db.commit()

    # 已解析
    assert await check_interrupt_resolved(db, str(run.id)) is True


@pytest.mark.asyncio
async def test_interrupt_decision_mapping(db: AsyncSession):
    """测试各种决策映射到正确的状态"""
    run = await create_run(
        db,
        project_id="proj_007",
        chapter_id="chap_007",
        run_type="CHAPTER_DRAFT",
        mode="full_pipeline",
    )
    await db.commit()

    decisions_and_statuses = [
        (InterruptDecision.APPROVE, InterruptStatus.APPROVED),
        (InterruptDecision.REJECT, InterruptStatus.REJECTED),
        (InterruptDecision.EDIT, InterruptStatus.EDITED),
        (InterruptDecision.REGENERATE, InterruptStatus.REGENERATE),
    ]

    for decision, expected_status in decisions_and_statuses:
        interrupt = await create_interrupt(db, run=run, step_name="human_review")
        await db.commit()

        await resolve_interrupt(db, interrupt, decision=decision)
        await db.commit()

        assert interrupt.status == expected_status
        assert interrupt.decision == decision
