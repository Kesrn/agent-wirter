"""Harness Core 数据层模型测试 — SQLite 内存数据库，无需 PostgreSQL"""

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, ARRAY
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler

# 与 test_smoke.py 一致：让 SQLite 识别 PG 专有类型（必须在 import models 之前）
SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from models.base import Base

# 在 fixture 的 create_all 之前注册全部 harness 模型到 Base.metadata。
# 延迟 import 会导致 create_all 时表未注册，FK 目标表缺失。
import models.harness_enums  # noqa: F401
import models.ai_run  # noqa: F401
import models.ai_run_step  # noqa: F401
import models.llm_call_log  # noqa: F401
import models.human_interrupt  # noqa: F401


def _cols(table_name: str) -> dict:
    """从模型元数据读取列定义（同步、不依赖 DB 反射）。

    用 Base.metadata 而非 inspect(async_db.bind)：AsyncEngine 不支持直接 inspect，
    且反射回的 default 在 SQLite 下为 None（只有 server_default 才进 DDL）。
    元数据反映的是模型声明状态——正是 TDD 要校验的对象。
    """
    return {c.name: c for c in Base.metadata.tables[table_name].columns}


@pytest_asyncio.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def test_harness_enum_values():
    """枚举值与文档定义一致，且 StrEnum 与裸字符串等价。"""
    from models.harness_enums import RunStatus, RunStepStatus, InterruptStatus, InterruptDecision

    assert RunStatus.CREATED == "CREATED"
    assert RunStatus.RUNNING == "RUNNING"
    assert RunStatus.WAITING_HUMAN == "WAITING_HUMAN"
    assert RunStatus.PAUSED == "PAUSED"
    assert RunStatus.FAILED == "FAILED"
    assert RunStatus.COMPLETED == "COMPLETED"
    assert RunStatus.CANCELLED == "CANCELLED"

    assert RunStepStatus.PENDING == "PENDING"
    assert RunStepStatus.RUNNING == "RUNNING"
    assert RunStepStatus.SUCCESS == "SUCCESS"
    assert RunStepStatus.FAILED == "FAILED"
    assert RunStepStatus.SKIPPED == "SKIPPED"
    assert RunStepStatus.WAITING_HUMAN == "WAITING_HUMAN"
    assert RunStepStatus.RETRYING == "RETRYING"

    assert InterruptStatus.WAITING == "WAITING"
    assert InterruptStatus.APPROVED == "APPROVED"
    assert InterruptStatus.EDITED == "EDITED"
    assert InterruptStatus.REJECTED == "REJECTED"
    assert InterruptStatus.REGENERATE == "REGENERATE"
    assert InterruptStatus.EXPIRED == "EXPIRED"

    assert InterruptDecision.APPROVE == "APPROVE"
    assert InterruptDecision.REJECT == "REJECT"
    assert InterruptDecision.EDIT == "EDIT"
    assert InterruptDecision.REGENERATE == "REGENERATE"


# ==================== AiRun ====================

@pytest.mark.asyncio
async def test_ai_run_table_columns(async_db):
    """ai_runs 表结构与文档 §4.1 一致。"""
    from models.ai_run import AiRun

    cols = _cols("ai_runs")
    for name in [
        "id", "project_id", "chapter_id", "document_id", "generation_record_id",
        "run_type", "mode", "status", "current_step", "user_goal",
        "thread_id", "model_config_snapshot", "token_usage", "cost_usage",
        "error_message", "started_at", "finished_at", "created_at", "updated_at",
    ]:
        assert name in cols, f"ai_runs 缺列 {name}"

    # 可空性：关联 id 全部可空
    assert cols["chapter_id"].nullable is True
    assert cols["document_id"].nullable is True
    assert cols["generation_record_id"].nullable is True
    # run_type / mode 必填
    assert cols["run_type"].nullable is False
    assert cols["mode"].nullable is False


@pytest.mark.asyncio
async def test_ai_run_create_and_default_status(async_db):
    from models.ai_run import AiRun
    from models.harness_enums import RunStatus
    from uuid import uuid4

    run = AiRun(
        project_id=uuid4(),
        run_type="CHAPTER_DRAFT",
        mode="full_pipeline",
    )
    async_db.add(run)
    await async_db.commit()
    await async_db.refresh(run)

    assert run.id is not None
    assert run.status == RunStatus.CREATED
    assert run.created_at is not None
    assert run.updated_at is not None


# ==================== AiRunStep ====================

@pytest.mark.asyncio
async def test_ai_run_step_columns(async_db):
    from models.ai_run_step import AiRunStep

    cols = _cols("ai_run_steps")
    for name in [
        "id", "run_id", "step_order", "step_name", "agent_name", "status",
        "input", "output", "input_hash", "output_hash",
        "retry_count", "max_retry", "idempotency_key",
        "error_message", "started_at", "ended_at", "created_at", "updated_at",
    ]:
        assert name in cols, f"ai_run_steps 缺列 {name}"

    # retry_count 有 Python 端 default（非 server_default）
    assert cols["retry_count"].default is not None
    assert cols["idempotency_key"].nullable is True
    assert cols["run_id"].nullable is False


@pytest.mark.asyncio
async def test_ai_run_step_idempotency_unique(async_db):
    """idempotency_key 唯一，NULL 不参与唯一约束（部分唯一索引语义在 SQLite 下放宽，只验证非 NULL 唯一）。"""
    from models.ai_run import AiRun
    from models.ai_run_step import AiRunStep
    from uuid import uuid4

    run = AiRun(project_id=uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
    async_db.add(run)
    await async_db.flush()

    s1 = AiRunStep(run_id=run.id, step_order=1, step_name="writer",
                   idempotency_key=f"{run.id}:writer:0")
    s2 = AiRunStep(run_id=run.id, step_order=2, step_name="writer",
                   idempotency_key=f"{run.id}:writer:0")  # 重复 key
    async_db.add_all([s1, s2])
    with pytest.raises(Exception):
        await async_db.commit()


@pytest.mark.asyncio
async def test_ai_run_step_parallel_keys(async_db):
    """并发 step：critic 与 consistency_checker 同一 run 同一 round 可并存（不同 step_name → 不同幂等键）。"""
    from models.ai_run import AiRun
    from models.ai_run_step import AiRunStep
    from uuid import uuid4

    run = AiRun(project_id=uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
    async_db.add(run)
    await async_db.flush()

    critic = AiRunStep(run_id=run.id, step_order=2, step_name="critic",
                       idempotency_key=f"{run.id}:critic:0")
    consist = AiRunStep(run_id=run.id, step_order=2, step_name="consistency_checker",
                        idempotency_key=f"{run.id}:consistency_checker:0")
    async_db.add_all([critic, consist])
    await async_db.commit()  # 不应抛异常：step_name 不同 → key 不同
    assert critic.id != consist.id


# ==================== LlmCallLog ====================

@pytest.mark.asyncio
async def test_llm_call_log_columns(async_db):
    from models.llm_call_log import LlmCallLog

    cols = _cols("llm_call_logs")
    for name in [
        "id", "run_id", "step_id", "project_id", "chapter_id", "document_id",
        "agent_name", "provider", "model",
        "prompt_template_id", "prompt_template_version", "prompt_hash",
        "rendered_prompt_snapshot", "context_package_snapshot", "model_config_snapshot",
        "input_tokens", "output_tokens", "total_tokens", "cost", "latency_ms",
        "request", "response", "error_message", "created_at",
    ]:
        assert name in cols, f"llm_call_logs 缺列 {name}"

    # 所有 token/cost 字段可空（第一阶段拿不到真实值时写 NULL）
    for nullable_col in ["input_tokens", "output_tokens", "total_tokens", "cost", "latency_ms"]:
        assert cols[nullable_col].nullable is True, f"{nullable_col} 应可空"
    # run_id / step_id 可空（脱离 run 的独立调用也要能记）
    assert cols["run_id"].nullable is True
    assert cols["step_id"].nullable is True


@pytest.mark.asyncio
async def test_llm_call_log_minimal(async_db):
    from models.llm_call_log import LlmCallLog

    log = LlmCallLog(provider="mock", model="mock-1", agent_name="writer")
    async_db.add(log)
    await async_db.commit()
    await async_db.refresh(log)

    assert log.id is not None
    assert log.created_at is not None
    assert log.input_tokens is None  # 第一阶段允许 NULL


# ==================== HumanInterrupt ====================

@pytest.mark.asyncio
async def test_human_interrupt_columns(async_db):
    from models.human_interrupt import HumanInterrupt

    cols = _cols("human_interrupts")
    for name in [
        "id", "run_id", "step_id", "thread_id", "step_name", "status",
        "payload", "decision", "feedback", "resolved",
        "created_at", "resolved_at",
    ]:
        assert name in cols, f"human_interrupts 缺列 {name}"

    # resolved / status 有 Python 端 default
    assert cols["resolved"].default is not None
    assert cols["status"].default is not None
    assert cols["run_id"].nullable is False
    assert cols["resolved_at"].nullable is True


@pytest.mark.asyncio
async def test_human_interrupt_create(async_db):
    from models.ai_run import AiRun
    from models.human_interrupt import HumanInterrupt
    from models.harness_enums import InterruptStatus
    from uuid import uuid4

    run = AiRun(project_id=uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
    async_db.add(run)
    await async_db.flush()

    hi = HumanInterrupt(run_id=run.id, thread_id="t-1", step_name="human_review")
    async_db.add(hi)
    await async_db.commit()
    await async_db.refresh(hi)

    assert hi.status == InterruptStatus.WAITING
    assert hi.resolved is False
    assert hi.resolved_at is None


# ==================== generation_records / chapter_versions 加列 ====================

@pytest.mark.asyncio
async def test_generation_record_has_run_id(async_db):
    cols = _cols("generation_records")
    assert "run_id" in cols
    assert cols["run_id"].nullable is True


@pytest.mark.asyncio
async def test_chapter_version_new_columns(async_db):
    cols = _cols("chapter_versions")
    for name in ["project_id", "run_id", "parent_version_id", "rollback_from_version_id", "diff_from_parent"]:
        assert name in cols, f"chapter_versions 缺列 {name}"
        assert cols[name].nullable is True, f"{name} 应可空（兼容老数据）"


# ==================== VALID_SOURCES 白名单 ====================

def test_valid_sources_includes_new_values():
    """文档 §4.5 要求新增 ai_draft / rollback / import，阶段 A 就放开白名单。"""
    from services.version_service import VALID_SOURCES

    for src in ["ai_draft", "rollback", "import"]:
        assert src in VALID_SOURCES, f"VALID_SOURCES 缺 {src}"
    # 老值仍在
    for src in ["manual", "ai_approve", "ai_pipeline"]:
        assert src in VALID_SOURCES


# ==================== models 包导出 ====================

def test_models_package_exports_harness():
    """models.__init__ 应导出 4 个新模型 + 4 个枚举。"""
    import models as m

    for cls_name in ["AiRun", "AiRunStep", "LlmCallLog", "HumanInterrupt"]:
        assert hasattr(m, cls_name), f"models 包未导出 {cls_name}"
    for enum_name in ["RunStatus", "RunStepStatus", "InterruptStatus", "InterruptDecision"]:
        assert hasattr(m, enum_name), f"models 包未导出 {enum_name}"
