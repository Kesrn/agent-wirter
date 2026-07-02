# 阶段 A：Harness Core 数据层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 `ai_runs / ai_run_steps / llm_call_logs / human_interrupts` 四张 Harness 核心表及其 SQLAlchemy 模型，并给 `generation_records` 加 `run_id`、给 `chapter_versions` 加 `project_id/run_id/parent_version_id/rollback_from_version_id/diff_from_parent` 字段，全部通过一个 Alembic 迁移 `021_ai_harness_core.py` 落地，不接入任何业务流程。

**Architecture:** 纯数据层。新增 4 个模型文件（各自只定义一张表，复用 `UUIDMixin + TimestampMixin`），修改 2 个现有模型加可空列，新增 1 个 Alembic 迁移（沿用 `012_novel_extraction.py` 的 `_uuid_type()/_json_type()/_has_table()/_columns()` 可移植辅助模式，兼容 PostgreSQL / SQLite / 桌面端）。状态枚举用 Python 3.11 的 `StrEnum` 定义在 `backend/models/harness_enums.py`，供模型与服务层共用。每张表先用 TDD 写模型层测试（基于现有 `test_smoke.py` 的 SQLite 内存 + `Base.metadata.create_all` 套路），再写模型，再写迁移，最后跑全量冒烟回归。

**Tech Stack:** Python 3.11, SQLAlchemy 2.0 Async, Alembic, Pydantic-free（模型层纯 SQLAlchemy），pytest（SQLite 内存数据库，JSONB 经 monkey-patch 编译为 JSON）。

---

## 文件结构

**新增文件：**
- `backend/models/harness_enums.py` — RunStatus / RunStepStatus / InterruptStatus / InterruptDecision 四个 `StrEnum`，集中定义，供模型与服务层共用。
- `backend/models/ai_run.py` — `AiRun` 模型（表 `ai_runs`）。
- `backend/models/ai_run_step.py` — `AiRunStep` 模型（表 `ai_run_steps`）。
- `backend/models/llm_call_log.py` — `LlmCallLog` 模型（表 `llm_call_logs`）。
- `backend/models/human_interrupt.py` — `HumanInterrupt` 模型（表 `human_interrupts`）。
- `backend/alembic/versions/021_ai_harness_core.py` — 单一迁移：建 4 张新表 + 给 2 张老表加列 + 建索引。
- `backend/tests/test_harness_models.py` — 模型层测试（建表、列存在性、枚举默认值、唯一索引、可空约束）。

**修改文件：**
- `backend/models/generation_record.py` — 加 `run_id` 可空列 + 索引。
- `backend/models/chapter_version.py` — 加 `project_id / run_id / parent_version_id / rollback_from_version_id / diff_from_parent` 五个可空列。
- `backend/models/__init__.py` — 导入 4 个新模型 + `harness_enums` 的枚举，加入 `__all__`。
- `backend/services/version_service.py` — `VALID_SOURCES` frozenset 加 `ai_draft / rollback / import`（文档 §4.5 要求，且阶段 A 就把白名单放开，避免后续踩坑）；本阶段不动 `_prune_old_versions`。

**约束（文档 §11 + 阶段 A 验收）：**
- 不删旧字段、不改老表既有列。
- 迁移必须兼容已有数据（新列全部 nullable，有 server_default 的只给纯新增表）。
- 不一次性重写 `routes.py`（本阶段完全不碰 routes）。
- `pytest backend/tests/test_smoke.py` 仍全绿。
- `alembic upgrade head` 能建出新表且老数据不报错。

---

## Task 1: harness_enums.py — 状态枚举

**Files:**
- Create: `backend/models/harness_enums.py`
- Test: `backend/tests/test_harness_models.py`

- [ ] **Step 1: 写失败测试（枚举值与字符串等价）**

创建 `backend/tests/test_harness_models.py`：

```python
"""Harness Core 数据层模型测试 — SQLite 内存数据库，无需 PostgreSQL"""

import pytest

from models.base import Base


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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_models.py::test_harness_enum_values -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'models.harness_enums'`

- [ ] **Step 3: 实现枚举文件**

创建 `backend/models/harness_enums.py`：

```python
"""Harness 核心状态枚举。StrEnum 使其与裸字符串直接等价，便于 JSON 序列化与 DB 字符串列兼容。"""

from enum import StrEnum


class RunStatus(StrEnum):
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    PAUSED = "PAUSED"
    FAILED = "FAILED"
    COMPLETED = "COMPLETED"
    CANCELLED = "CANCELLED"


class RunStepStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    WAITING_HUMAN = "WAITING_HUMAN"
    RETRYING = "RETRYING"


class InterruptStatus(StrEnum):
    WAITING = "WAITING"
    APPROVED = "APPROVED"
    EDITED = "EDITED"
    REJECTED = "REJECTED"
    REGENERATE = "REGENERATE"
    EXPIRED = "EXPIRED"


class InterruptDecision(StrEnum):
    APPROVE = "APPROVE"
    REJECT = "REJECT"
    EDIT = "EDIT"
    REGENERATE = "REGENERATE"
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_models.py::test_harness_enum_values -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/models/harness_enums.py backend/tests/test_harness_models.py
git commit -m "feat(harness): add harness status enums (RunStatus/RunStepStatus/InterruptStatus/InterruptDecision)"
```

---

## Task 2: AiRun 模型

**Files:**
- Create: `backend/models/ai_run.py`
- Test: `backend/tests/test_harness_models.py`

- [ ] **Step 1: 写失败测试（表存在 + 关键列 + 枚举默认值）**

追加到 `backend/tests/test_harness_models.py` 末尾。先在文件顶部 import 区补一个共享 fixture（用与 `test_smoke.py` 相同的 SQLite 内存套路，但本文件独立建表，避免依赖 smoke 的 session 级 fixture）：

```python
import pytest_asyncio
from sqlalchemy import inspect as sa_inspect
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
```

再追加测试：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v -k ai_run`
Expected: FAIL — `ModuleNotFoundError: No module named 'models.ai_run'`

- [ ] **Step 3: 实现 AiRun 模型**

创建 `backend/models/ai_run.py`：

```python
"""AI Run 模型 — 一次 AI 任务的业务生命周期。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, TimestampMixin, UUIDMixin
from .harness_enums import RunStatus


class AiRun(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "ai_runs"

    project_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True, index=True
    )
    document_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("documents.id", ondelete="CASCADE"), nullable=True, index=True
    )
    generation_record_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), nullable=True, index=True
    )

    run_type: Mapped[str] = mapped_column(String(50), nullable=False)
    mode: Mapped[str] = mapped_column(String(50), nullable=False)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default=RunStatus.CREATED)
    current_step: Mapped[str | None] = mapped_column(String(100), nullable=True)
    user_goal: Mapped[str | None] = mapped_column(Text, nullable=True)

    thread_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    model_config_snapshot: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    token_usage: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    cost_usage: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v -k ai_run`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/models/ai_run.py backend/tests/test_harness_models.py
git commit -m "feat(harness): add AiRun model (ai_runs table)"
```

---

## Task 3: AiRunStep 模型

**Files:**
- Create: `backend/models/ai_run_step.py`
- Test: `backend/tests/test_harness_models.py`

- [ ] **Step 1: 写失败测试（列结构 + 幂等唯一索引 + 并发 step 约束）**

追加到 `backend/tests/test_harness_models.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v -k ai_run_step`
Expected: FAIL — `ModuleNotFoundError: No module named 'models.ai_run_step'`

- [ ] **Step 3: 实现 AiRunStep 模型**

创建 `backend/models/ai_run_step.py`：

```python
"""AI Run Step 模型 — 一次任务中的步骤执行记录。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, TimestampMixin, UUIDMixin
from .harness_enums import RunStepStatus


class AiRunStep(UUIDMixin, TimestampMixin, Base):
    __tablename__ = "ai_run_steps"
    __table_args__ = (
        Index("ix_ai_run_steps_run_order", "run_id", "step_order"),
        Index("ix_ai_run_steps_run_status", "run_id", "status"),
        # 部分唯一索引：idempotency_key 非空时唯一。
        # sqlite_where / postgresql_where 双方言声明，SQLite 现代版支持部分索引。
        Index(
            "ux_ai_run_steps_idempotency",
            "idempotency_key",
            unique=True,
            sqlite_where=text("idempotency_key IS NOT NULL"),
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("ai_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_order: Mapped[int] = mapped_column(Integer, nullable=False)
    step_name: Mapped[str] = mapped_column(String(100), nullable=False)
    agent_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, default=RunStepStatus.PENDING)

    input: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retry: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

（`text` 已在顶部 import 行包含。）

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v -k ai_run_step`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/models/ai_run_step.py backend/tests/test_harness_models.py
git commit -m "feat(harness): add AiRunStep model with parallel-safe idempotency key"
```

---

## Task 4: LlmCallLog 模型

**Files:**
- Create: `backend/models/llm_call_log.py`
- Test: `backend/tests/test_harness_models.py`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_harness_models.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v -k llm_call_log`
Expected: FAIL — `ModuleNotFoundError: No module named 'models.llm_call_log'`

- [ ] **Step 3: 实现 LlmCallLog 模型**

注意：`llm_call_logs` 只有 `created_at`（文档 §4.3 表结构无 `updated_at`，调用日志不可变）。因此不直接用 `TimestampMixin`，只混 `UUIDMixin` 并单独声明 `created_at`。

创建 `backend/models/llm_call_log.py`：

```python
"""LLM Call Log 模型 — 每次 LLM 调用的本地审计记录（不可变，只有 created_at）。"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, Numeric, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin


class LlmCallLog(UUIDMixin, Base):
    __tablename__ = "llm_call_logs"

    run_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("ai_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("ai_run_steps.id", ondelete="SET NULL"), nullable=True, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    chapter_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True, index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)

    agent_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(50), nullable=True)
    model: Mapped[str | None] = mapped_column(String(120), nullable=True)

    prompt_template_id: Mapped[uuid.UUID | None] = mapped_column(GUID(), nullable=True)
    prompt_template_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)

    rendered_prompt_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_package_snapshot: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    model_config_snapshot: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)

    input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost: Mapped[float | None] = mapped_column(Numeric(12, 6), nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)

    request: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    response: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v -k llm_call_log`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/models/llm_call_log.py backend/tests/test_harness_models.py
git commit -m "feat(harness): add LlmCallLog model (immutable local LLM audit log)"
```

---

## Task 5: HumanInterrupt 模型

**Files:**
- Create: `backend/models/human_interrupt.py`
- Test: `backend/tests/test_harness_models.py`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_harness_models.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v -k human_interrupt`
Expected: FAIL — `ModuleNotFoundError: No module named 'models.human_interrupt'`

- [ ] **Step 3: 实现 HumanInterrupt 模型**

注意：文档 §4.6 表结构无 `updated_at`（只有 `created_at` / `resolved_at`），所以不混 `TimestampMixin`。

创建 `backend/models/human_interrupt.py`：

```python
"""Human Interrupt 模型 — 持久化人工审核等待状态（可恢复 HITL）。"""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, GUID, JSONValue, UUIDMixin
from .harness_enums import InterruptStatus


class HumanInterrupt(UUIDMixin, Base):
    __tablename__ = "human_interrupts"

    run_id: Mapped[uuid.UUID] = mapped_column(
        GUID(), ForeignKey("ai_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    step_id: Mapped[uuid.UUID | None] = mapped_column(
        GUID(), ForeignKey("ai_run_steps.id", ondelete="SET NULL"), nullable=True
    )
    thread_id: Mapped[str | None] = mapped_column(String(200), nullable=True, index=True)
    step_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default=InterruptStatus.WAITING
    )
    payload: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
    decision: Mapped[str | None] = mapped_column(String(50), nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v -k human_interrupt`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/models/human_interrupt.py backend/tests/test_harness_models.py
git commit -m "feat(harness): add HumanInterrupt model (persistent HITL state)"
```

---

## Task 6: 修改 generation_record 与 chapter_version 加列

**Files:**
- Modify: `backend/models/generation_record.py`
- Modify: `backend/models/chapter_version.py`
- Test: `backend/tests/test_harness_models.py`

- [ ] **Step 1: 写失败测试（新列存在且可空）**

追加到 `backend/tests/test_harness_models.py`：

```python
@pytest.mark.asyncio
async def test_generation_record_has_run_id(async_db):
    from models.generation_record import GenerationRecord

    cols = _cols("generation_records")
    assert "run_id" in cols
    assert cols["run_id"].nullable is True


@pytest.mark.asyncio
async def test_chapter_version_new_columns(async_db):
    from models.chapter_version import ChapterVersion

    cols = _cols("chapter_versions")
    for name in ["project_id", "run_id", "parent_version_id", "rollback_from_version_id", "diff_from_parent"]:
        assert name in cols, f"chapter_versions 缺列 {name}"
        assert cols[name].nullable is True, f"{name} 应可空（兼容老数据）"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v -k "generation_record_has_run_id or chapter_version_new_columns"`
Expected: FAIL — 列不存在

- [ ] **Step 3: 给 generation_record 加 run_id**

在 `backend/models/generation_record.py` 的 `langfuse_trace_id` 行之后追加：

```python
    run_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
```

（放在 `langfuse_trace_id` 定义之后，保持文件内列顺序自然。）

- [ ] **Step 4: 给 chapter_version 加 5 列**

在 `backend/models/chapter_version.py` 的 `source` 行之后追加：

```python
    project_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    run_id: Mapped[str | None] = mapped_column(GUID(), nullable=True, index=True)
    parent_version_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    rollback_from_version_id: Mapped[str | None] = mapped_column(GUID(), nullable=True)
    diff_from_parent: Mapped[dict | None] = mapped_column(JSONValue(), nullable=True)
```

注意：`chapter_version.py` 当前未 import `JSONValue`，需在 import 行补上：
将 `from .base import Base, GUID, UUIDMixin, TimestampMixin` 改为
`from .base import Base, GUID, JSONValue, UUIDMixin, TimestampMixin`。

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v -k "generation_record_has_run_id or chapter_version_new_columns"`
Expected: PASS

- [ ] **Step 6: 跑全量 smoke 回归确保老测试未坏**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: PASS（103 passed，与基线一致）

- [ ] **Step 7: 提交**

```bash
git add backend/models/generation_record.py backend/models/chapter_version.py backend/tests/test_harness_models.py
git commit -m "feat(harness): add run_id to generation_records; add project_id/run_id/parent/rollback/diff to chapter_versions"
```

---

## Task 7: 扩展 VALID_SOURCES 白名单

**Files:**
- Modify: `backend/services/version_service.py:12-19`
- Test: `backend/tests/test_harness_models.py`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_harness_models.py`：

```python
def test_valid_sources_includes_new_values():
    """文档 §4.5 要求新增 ai_draft / rollback / import，阶段 A 就放开白名单。"""
    from services.version_service import VALID_SOURCES

    for src in ["ai_draft", "rollback", "import"]:
        assert src in VALID_SOURCES, f"VALID_SOURCES 缺 {src}"
    # 老值仍在
    for src in ["manual", "ai_approve", "ai_pipeline"]:
        assert src in VALID_SOURCES
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_models.py::test_valid_sources_includes_new_values -v`
Expected: FAIL — `ai_draft` 不在 frozenset

- [ ] **Step 3: 扩展 VALID_SOURCES**

修改 `backend/services/version_service.py:12-19`：

```python
VALID_SOURCES = frozenset({
    "manual",
    "ai_enhance",
    "ai_continue",
    "ai_generate",
    "ai_pipeline",
    "ai_approve",
    "ai_draft",
    "rollback",
    "import",
})
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_models.py::test_valid_sources_includes_new_values -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/services/version_service.py backend/tests/test_harness_models.py
git commit -m "feat(harness): extend VALID_SOURCES with ai_draft/rollback/import"
```

---

## Task 8: 注册新模型到 __init__.py

**Files:**
- Modify: `backend/models/__init__.py`
- Test: `backend/tests/test_harness_models.py`

- [ ] **Step 1: 写失败测试（从 models 包能 import 全部新类）**

追加到 `backend/tests/test_harness_models.py`：

```python
def test_models_package_exports_harness():
    """models.__init__ 应导出 4 个新模型 + 4 个枚举。"""
    import models as m

    for cls_name in ["AiRun", "AiRunStep", "LlmCallLog", "HumanInterrupt"]:
        assert hasattr(m, cls_name), f"models 包未导出 {cls_name}"
    for enum_name in ["RunStatus", "RunStepStatus", "InterruptStatus", "InterruptDecision"]:
        assert hasattr(m, enum_name), f"models 包未导出 {enum_name}"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_models.py::test_models_package_exports_harness -v`
Expected: FAIL — `AiRun` 不在 models 包

- [ ] **Step 3: 修改 __init__.py**

在 `backend/models/__init__.py` 中，`from .knowledge_qa_message import KnowledgeQaMessage` 之后追加：

```python
from .harness_enums import RunStatus, RunStepStatus, InterruptStatus, InterruptDecision
from .ai_run import AiRun
from .ai_run_step import AiRunStep
from .llm_call_log import LlmCallLog
from .human_interrupt import HumanInterrupt
```

在 `__all__` 列表末尾（`"KnowledgeQaSession", "KnowledgeQaMessage",` 之后）追加：

```python
    "RunStatus", "RunStepStatus", "InterruptStatus", "InterruptDecision",
    "AiRun", "AiRunStep", "LlmCallLog", "HumanInterrupt",
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_models.py::test_models_package_exports_harness -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/models/__init__.py backend/tests/test_harness_models.py
git commit -m "feat(harness): register harness models and enums in models.__init__"
```

---

## Task 9: Alembic 迁移 021_ai_harness_core

**Files:**
- Create: `backend/alembic/versions/021_ai_harness_core.py`
- Test: 手动验证 `alembic upgrade head`

本迁移沿用 `012_novel_extraction.py` 的可移植辅助（`_uuid_type()/_json_type()/_has_table()/_columns()`），确保 PG / SQLite / 桌面端一致。

- [ ] **Step 1: 写迁移文件**

创建 `backend/alembic/versions/021_ai_harness_core.py`：

```python
"""AI harness core tables (ai_runs / ai_run_steps / llm_call_logs / human_interrupts)
   + generation_records.run_id + chapter_versions harness columns

Revision ID: 021_ai_harness_core
Revises: 020_alias_project_id_uuid
Create Date: 2026-07-01

阶段 A：Harness Core 数据层。只建表与加列，不接入业务流程。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision = "021_ai_harness_core"
down_revision = "020_alias_project_id_uuid"
branch_labels = None
depends_on = None


# ── SQLite/PG 兼容辅助（与 012 一致） ──────────────────

def _bind():
    return op.get_bind()


def _dialect_name() -> str:
    return _bind().dialect.name


def _has_table(table_name: str) -> bool:
    return inspect(_bind()).has_table(table_name)


def _columns(table_name: str) -> set[str]:
    return {c["name"] for c in inspect(_bind()).get_columns(table_name)}


def _uuid_type():
    if _dialect_name() == "postgresql":
        return postgresql.UUID(as_uuid=True)
    return sa.CHAR(36)


def _json_type():
    if _dialect_name() == "postgresql":
        return postgresql.JSONB()
    return sa.JSON()


def _add_column_if_missing(table_name: str, column: sa.Column) -> None:
    if column.name not in _columns(table_name):
        op.add_column(table_name, column)


def _ts_columns():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now()),
    )


def upgrade() -> None:
    # 1. ai_runs
    if not _has_table("ai_runs"):
        op.create_table(
            "ai_runs",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("project_id", _uuid_type(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
            sa.Column("chapter_id", _uuid_type(), sa.ForeignKey("chapters.id", ondelete="CASCADE"), nullable=True),
            sa.Column("document_id", _uuid_type(), sa.ForeignKey("documents.id", ondelete="CASCADE"), nullable=True),
            sa.Column("generation_record_id", _uuid_type(), nullable=True),
            sa.Column("run_type", sa.String(50), nullable=False),
            sa.Column("mode", sa.String(50), nullable=False),
            sa.Column("status", sa.String(50), nullable=False, server_default="CREATED"),
            sa.Column("current_step", sa.String(100), nullable=True),
            sa.Column("user_goal", sa.Text, nullable=True),
            sa.Column("thread_id", sa.String(200), nullable=True),
            sa.Column("model_config_snapshot", _json_type(), nullable=True),
            sa.Column("token_usage", _json_type(), nullable=True),
            sa.Column("cost_usage", _json_type(), nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_project_id ON ai_runs (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_chapter_id ON ai_runs (chapter_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_document_id ON ai_runs (document_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_status ON ai_runs (status)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_runs_thread_id ON ai_runs (thread_id)")

    # 2. ai_run_steps
    if not _has_table("ai_run_steps"):
        op.create_table(
            "ai_run_steps",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("run_id", _uuid_type(), sa.ForeignKey("ai_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("step_order", sa.Integer, nullable=False),
            sa.Column("step_name", sa.String(100), nullable=False),
            sa.Column("agent_name", sa.String(100), nullable=True),
            sa.Column("status", sa.String(50), nullable=False, server_default="PENDING"),
            sa.Column("input", _json_type(), nullable=True),
            sa.Column("output", _json_type(), nullable=True),
            sa.Column("input_hash", sa.String(128), nullable=True),
            sa.Column("output_hash", sa.String(128), nullable=True),
            sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("max_retry", sa.Integer, nullable=False, server_default="0"),
            sa.Column("idempotency_key", sa.String(200), nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            *_ts_columns(),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_run_steps_run_id ON ai_run_steps (run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_run_steps_run_order ON ai_run_steps (run_id, step_order)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_ai_run_steps_run_status ON ai_run_steps (run_id, status)")
    # 部分唯一索引：idempotency_key 非空时唯一
    if _dialect_name() == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_run_steps_idempotency "
            "ON ai_run_steps (idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
    else:
        # SQLite：CREATE UNIQUE INDEX IF NOT EXISTS 不支持 WHERE，退化为全唯一
        # 服务层保证只对非 NULL key 写入；NULL 行不受唯一约束（SQLite 把多 NULL 视为不等）
        op.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_ai_run_steps_idempotency "
            "ON ai_run_steps (idempotency_key)"
        )

    # 3. llm_call_logs（只有 created_at）
    if not _has_table("llm_call_logs"):
        op.create_table(
            "llm_call_logs",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("run_id", _uuid_type(), sa.ForeignKey("ai_runs.id", ondelete="SET NULL"), nullable=True),
            sa.Column("step_id", _uuid_type(), sa.ForeignKey("ai_run_steps.id", ondelete="SET NULL"), nullable=True),
            sa.Column("project_id", _uuid_type(), nullable=True),
            sa.Column("chapter_id", _uuid_type(), nullable=True),
            sa.Column("document_id", _uuid_type(), nullable=True),
            sa.Column("agent_name", sa.String(100), nullable=True),
            sa.Column("provider", sa.String(50), nullable=True),
            sa.Column("model", sa.String(120), nullable=True),
            sa.Column("prompt_template_id", _uuid_type(), nullable=True),
            sa.Column("prompt_template_version", sa.Integer, nullable=True),
            sa.Column("prompt_hash", sa.String(128), nullable=True),
            sa.Column("rendered_prompt_snapshot", sa.Text, nullable=True),
            sa.Column("context_package_snapshot", _json_type(), nullable=True),
            sa.Column("model_config_snapshot", _json_type(), nullable=True),
            sa.Column("input_tokens", sa.Integer, nullable=True),
            sa.Column("output_tokens", sa.Integer, nullable=True),
            sa.Column("total_tokens", sa.Integer, nullable=True),
            sa.Column("cost", sa.Numeric(12, 6), nullable=True),
            sa.Column("latency_ms", sa.Integer, nullable=True),
            sa.Column("request", _json_type(), nullable=True),
            sa.Column("response", _json_type(), nullable=True),
            sa.Column("error_message", sa.Text, nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_logs_run_id ON llm_call_logs (run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_logs_step_id ON llm_call_logs (step_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_llm_call_logs_project_id ON llm_call_logs (project_id)")

    # 4. human_interrupts（只有 created_at / resolved_at）
    if not _has_table("human_interrupts"):
        op.create_table(
            "human_interrupts",
            sa.Column("id", _uuid_type(), primary_key=True),
            sa.Column("run_id", _uuid_type(), sa.ForeignKey("ai_runs.id", ondelete="CASCADE"), nullable=False),
            sa.Column("step_id", _uuid_type(), sa.ForeignKey("ai_run_steps.id", ondelete="SET NULL"), nullable=True),
            sa.Column("thread_id", sa.String(200), nullable=True),
            sa.Column("step_name", sa.String(100), nullable=True),
            sa.Column("status", sa.String(50), nullable=False, server_default="WAITING"),
            sa.Column("payload", _json_type(), nullable=True),
            sa.Column("decision", sa.String(50), nullable=True),
            sa.Column("feedback", sa.Text, nullable=True),
            sa.Column("resolved", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_human_interrupts_run_id ON human_interrupts (run_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_human_interrupts_thread_id ON human_interrupts (thread_id)")

    # 5. generation_records 加 run_id
    _add_column_if_missing("generation_records", sa.Column("run_id", _uuid_type(), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_generation_records_run_id ON generation_records (run_id)")

    # 6. chapter_versions 加 5 列
    _add_column_if_missing("chapter_versions", sa.Column("project_id", _uuid_type(), nullable=True))
    _add_column_if_missing("chapter_versions", sa.Column("run_id", _uuid_type(), nullable=True))
    _add_column_if_missing("chapter_versions", sa.Column("parent_version_id", _uuid_type(), nullable=True))
    _add_column_if_missing("chapter_versions", sa.Column("rollback_from_version_id", _uuid_type(), nullable=True))
    _add_column_if_missing("chapter_versions", sa.Column("diff_from_parent", _json_type(), nullable=True))
    op.execute("CREATE INDEX IF NOT EXISTS ix_chapter_versions_project_id ON chapter_versions (project_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_chapter_versions_run_id ON chapter_versions (run_id)")


def downgrade() -> None:
    # 先删老表加的索引与列
    op.execute("DROP INDEX IF EXISTS ix_chapter_versions_run_id")
    op.execute("DROP INDEX IF EXISTS ix_chapter_versions_project_id")
    for col in ["diff_from_parent", "rollback_from_version_id", "parent_version_id", "run_id", "project_id"]:
        if col in _columns("chapter_versions"):
            op.drop_column("chapter_versions", col)

    op.execute("DROP INDEX IF EXISTS ix_generation_records_run_id")
    if "run_id" in _columns("generation_records"):
        op.drop_column("generation_records", "run_id")

    op.execute("DROP INDEX IF EXISTS ix_human_interrupts_thread_id")
    op.execute("DROP INDEX IF EXISTS ix_human_interrupts_run_id")
    if _has_table("human_interrupts"):
        op.drop_table("human_interrupts")

    op.execute("DROP INDEX IF EXISTS ix_llm_call_logs_project_id")
    op.execute("DROP INDEX IF EXISTS ix_llm_call_logs_step_id")
    op.execute("DROP INDEX IF EXISTS ix_llm_call_logs_run_id")
    if _has_table("llm_call_logs"):
        op.drop_table("llm_call_logs")

    op.execute("DROP INDEX IF EXISTS ux_ai_run_steps_idempotency")
    op.execute("DROP INDEX IF EXISTS ix_ai_run_steps_run_status")
    op.execute("DROP INDEX IF EXISTS ix_ai_run_steps_run_order")
    op.execute("DROP INDEX IF EXISTS ix_ai_run_steps_run_id")
    if _has_table("ai_run_steps"):
        op.drop_table("ai_run_steps")

    op.execute("DROP INDEX IF EXISTS ix_ai_runs_thread_id")
    op.execute("DROP INDEX IF EXISTS ix_ai_runs_status")
    op.execute("DROP INDEX IF EXISTS ix_ai_runs_document_id")
    op.execute("DROP INDEX IF EXISTS ix_ai_runs_chapter_id")
    op.execute("DROP INDEX IF EXISTS ix_ai_runs_project_id")
    if _has_table("ai_runs"):
        op.drop_table("ai_runs")
```

- [ ] **Step 2: 在 SQLite 内存上验证迁移可执行（alembic 离线/在线）**

仓库可能未配置可空迁移目标 DB。用项目既有方式验证：在 `backend` 目录跑 `alembic upgrade head`（指向测试用 SQLite 或现有开发库）。若环境无配置，则用如下 Python 校验迁移 upgrade/downgrade 逻辑能在干净 SQLite 上跑通：

Run: `cd backend && python -c "
import asyncio
from sqlalchemy.ext.asyncio import create_async_engine
from alembic.config import Config
from alembic import command
cfg = Config('alembic.ini')
command.upgrade(cfg, 'head')
print('upgrade head OK')
command.downgrade(cfg, '020_alias_project_id_uuid')
print('downgrade to 020 OK')
command.upgrade(cfg, 'head')
print('re-upgrade head OK')
"`

Expected: 三行 OK，无异常。

- [ ] **Step 3: 跑全量回归**

Run: `cd backend && python -m pytest tests/ -q`
Expected: 全绿（smoke 103 + harness 新增用例）

- [ ] **Step 4: 提交**

```bash
git add backend/alembic/versions/021_ai_harness_core.py
git commit -m "feat(harness): alembic 021 — create harness core tables + extend generation_records/chapter_versions"
```

---

## Task 10: 阶段 A 验收确认

- [ ] **Step 1: 跑全部 harness 模型测试**

Run: `cd backend && python -m pytest tests/test_harness_models.py -v`
Expected: 全部 PASS

- [ ] **Step 2: 跑 smoke 回归**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: 103 passed（与改造前基线一致，老功能未坏）

- [ ] **Step 3: 确认迁移 head**

Run: `cd backend && python -m alembic heads`
Expected: `021_ai_harness_core (head)`

- [ ] **Step 4: 确认 models 包可正常 import（无循环依赖）**

Run: `cd backend && python -c "import models; print(sorted(models.__all__))"`
Expected: 列表含 `AiRun, AiRunStep, LlmCallLog, HumanInterrupt, RunStatus, RunStepStatus, InterruptStatus, InterruptDecision`，无 ImportError。

- [ ] **Step 5: 提交（如有遗漏的收尾改动）**

若以上全绿则阶段 A 完成，无需额外提交。最终在分支上推送：

```bash
git push -u origin novel-extraction-mvp
```

---

## 验收清单（对应文档阶段 A 验收）

1. `pytest backend/tests/test_smoke.py` 不破坏现有测试 ✅ (Task 10 Step 2)
2. Alembic upgrade 可以创建新表 ✅ (Task 9 Step 2)
3. 老数据没有必填字段迁移失败 ✅ (Task 6 + Task 9：新列全 nullable，新表与老数据无关)
