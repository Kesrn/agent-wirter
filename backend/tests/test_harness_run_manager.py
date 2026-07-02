"""RunManager + StepLogger 单元测试 — SQLite 内存数据库"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, ARRAY
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from models.base import Base
import models  # noqa: F401 — 注册全部模型到 metadata


@pytest_asyncio.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_create_run_defaults(async_db):
    from harness.run_manager import create_run
    from models.harness_enums import RunStatus

    run = await create_run(
        async_db,
        project_id=uuid.uuid4(),
        run_type="CHAPTER_DRAFT",
        mode="full_pipeline",
        user_goal="写第一章",
    )
    await async_db.commit()
    await async_db.refresh(run)

    assert run.id is not None
    assert run.status == RunStatus.CREATED
    assert run.run_type == "CHAPTER_DRAFT"
    assert run.mode == "full_pipeline"
    assert run.user_goal == "写第一章"


@pytest.mark.asyncio
async def test_run_lifecycle_transitions(async_db):
    from harness.run_manager import create_run, mark_running, mark_waiting_human, mark_completed, mark_failed, mark_cancelled
    from models.harness_enums import RunStatus

    run = await create_run(async_db, project_id=uuid.uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
    await async_db.flush()

    await mark_running(async_db, run)
    assert run.status == RunStatus.RUNNING
    assert run.started_at is not None
    assert run.current_step is None

    await mark_waiting_human(async_db, run, step_name="human_review", thread_id="t-1")
    assert run.status == RunStatus.WAITING_HUMAN
    assert run.current_step == "human_review"
    assert run.thread_id == "t-1"

    await mark_running(async_db, run)
    await mark_completed(async_db, run)
    assert run.status == RunStatus.COMPLETED
    assert run.finished_at is not None

    # failed / cancelled 在独立 run 上测
    run2 = await create_run(async_db, project_id=uuid.uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
    await async_db.flush()
    await mark_running(async_db, run2)
    await mark_failed(async_db, run2, error_message="LLM 超时")
    assert run2.status == RunStatus.FAILED
    assert run2.error_message == "LLM 超时"
    assert run2.finished_at is not None

    run3 = await create_run(async_db, project_id=uuid.uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
    await async_db.flush()
    await mark_running(async_db, run3)
    await mark_cancelled(async_db, run3)
    assert run3.status == RunStatus.CANCELLED
    assert run3.finished_at is not None


# ==================== StepLogger ====================

@pytest.mark.asyncio
async def test_step_start_finish_fail(async_db):
    from harness.run_manager import create_run
    from harness.step_logger import start_step, finish_step, fail_step
    from models.harness_enums import RunStepStatus

    run = await create_run(async_db, project_id=uuid.uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
    await async_db.flush()

    step = await start_step(
        async_db,
        run_id=run.id,
        step_order=1,
        step_name="context_loader",
        agent_name="context_loader",
        revision_round=0,
    )
    await async_db.flush()
    assert step.status == RunStepStatus.RUNNING
    assert step.idempotency_key == f"{run.id}:context_loader:0"
    assert step.started_at is not None

    await finish_step(async_db, step, output={"stats": {"chars": 1234}})
    assert step.status == RunStepStatus.SUCCESS
    assert step.ended_at is not None
    assert step.output == {"stats": {"chars": 1234}}

    # fail 在独立 step 上测
    step2 = await start_step(async_db, run_id=run.id, step_order=2, step_name="writer", revision_round=0)
    await fail_step(async_db, step2, error_message="LLM 空返回")
    assert step2.status == RunStepStatus.FAILED
    assert step2.error_message == "LLM 空返回"
    assert step2.ended_at is not None


@pytest.mark.asyncio
async def test_step_idempotency_skips_duplicate(async_db):
    """同一 run+step_name+revision_round 重复 start 应返回既有 step 而非新建。"""
    from harness.run_manager import create_run
    from harness.step_logger import start_step
    from sqlalchemy import select
    from models.ai_run_step import AiRunStep

    run = await create_run(async_db, project_id=uuid.uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
    await async_db.flush()

    s1 = await start_step(async_db, run_id=run.id, step_order=1, step_name="writer", revision_round=0)
    s2 = await start_step(async_db, run_id=run.id, step_order=1, step_name="writer", revision_round=0)
    assert s1.id == s2.id  # 幂等：返回同一条

    count = (await async_db.execute(select(AiRunStep).where(AiRunStep.run_id == run.id))).scalars().all()
    assert len(count) == 1


@pytest.mark.asyncio
async def test_step_parallel_critic_consistency(async_db):
    """critic 与 consistency_checker 并发：不同 step_name → 不同幂等键 → 可共存。"""
    from harness.run_manager import create_run
    from harness.step_logger import start_step

    run = await create_run(async_db, project_id=uuid.uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
    await async_db.flush()

    critic = await start_step(async_db, run_id=run.id, step_order=2, step_name="critic", revision_round=0)
    consist = await start_step(async_db, run_id=run.id, step_order=2, step_name="consistency_checker", revision_round=0)
    assert critic.id != consist.id
    assert critic.idempotency_key != consist.idempotency_key


# ==================== create_generation_record run_id ====================

@pytest.mark.asyncio
async def test_create_generation_record_with_run_id(async_db):
    from services.generation_record_service import create_generation_record
    from schemas.api import GenerateRequest

    pid = uuid.uuid4()
    run_id = uuid.uuid4()
    req = GenerateRequest(mode="full_pipeline")
    record = await create_generation_record(
        async_db,
        project_id=pid,
        mode="full_pipeline",
        content="这是一段生成的正文内容。",
        req=req,
        run_id=run_id,
    )
    await async_db.commit()
    assert record is not None
    assert str(record.run_id) == str(run_id)
