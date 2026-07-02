# 阶段 B：Run Manager 接入 generate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让一次现有 `full_pipeline` 生成流程在数据库留下可查询的 `ai_runs` + `ai_run_steps` 轨迹，`generation_records.run_id` 落关联，失败/断开/WAITING_HUMAN 更新 run 状态，旧前端体验不坏（只扩 `SSEEventType`，不做大 UI）。

**Architecture:** 纯服务层 + SSE 钩子，不重构 `routes.py` 结构、不改工作流节点函数、不碰 `LoggedLLMProvider`/token 统计/Prompt snapshot/Guardrail/MemorySaver。新增 `backend/harness/run_manager.py`（run 生命周期：create/mark_running/mark_waiting_human/mark_completed/mark_failed/mark_cancelled）和 `backend/harness/step_logger.py`（step：start/finish/fail/wait_human）。step 写入点选在 `routes.py` 的 LangGraph `astream_events` 循环里（`on_chain_start`→`start_step`，`on_chain_end`→`finish_step`），而非工作流节点内部——这样 `workflow.py` 零改动，且天然覆盖动态 expert 节点（节点名仍是 `writer`/`critic`）。`run_id` 通过给 `create_generation_record` 加参数注入，再经 `_save_generation_history` 透传。前端只扩 `SSEEventType` union，不加 switch case（经核实未知事件运行时静默丢弃，无 `default` 也安全）。

**Tech Stack:** Python 3.10（项目 venv，不用 StrEnum），SQLAlchemy 2.0 Async，FastAPI SSE，pytest（SQLite 内存 + `Base.metadata.create_all`），Vue 3 / TypeScript（仅 types.ts 一行改动）。

---

## 文件结构

**新增：**
- `backend/harness/__init__.py` — 包初始化。
- `backend/harness/run_manager.py` — Run 生命周期服务（纯 async 函数，接 db session）。
- `backend/harness/step_logger.py` — Step 记录服务（纯 async 函数）。
- `backend/tests/test_harness_run_manager.py` — run_manager + step_logger 单元测试。
- `backend/tests/test_harness_generate_integration.py` — generate 流程集成测试（run+step 轨迹 + run_id 关联 + 失败/断开/HITL 状态）。

**修改：**
- `backend/services/generation_record_service.py:43-57` — `create_generation_record` 加 `run_id` 参数，构造时写入。
- `backend/api/routes.py:2484-2516` — `_save_generation_history` 加 `run_id` 参数透传。
- `backend/api/routes.py` — `event_stream` 内：生成开始创建 run + 发 `run_created`；LangGraph 循环 `on_chain_start`/`on_chain_end` 写 step + 发 `run_step`；HITL 暂停 mark WAITING_HUMAN；异常 mark FAILED；正常结束 mark COMPLETED；`_save_generation_history` 调用传 `run_id`。
- `backend/api/routes.py:3324-3347` — `_save_resume_generation_history` 加 `run_id` 透传（resume 路径也关联）。
- `frontend/src/api/types.ts:727` — `SSEEventType` union 加 `run_created` / `run_status` / `run_step`。

**约束（文档 §11 + 阶段 B 范围）：**
- 不删旧 SSE 事件，新增事件不能破坏旧前端。
- `workflow.py` 零改动（step 钩子在 routes 的 astream_events 循环）。
- 不做 `LoggedLLMProvider`/token/cost/snapshot/Guardrail/MemorySaver/Run 面板（留给 C-H）。
- `run_id` 字段已存在（阶段 A 迁移已建），无需新迁移。
- step 写入失败不能中断生成主流程（尽力而为，记录日志后继续）。

---

## Task 1: harness 包 + RunManager

**Files:**
- Create: `backend/harness/__init__.py`
- Create: `backend/harness/run_manager.py`
- Test: `backend/tests/test_harness_run_manager.py`

- [ ] **Step 1: 写失败测试（run 生命周期）**

创建 `backend/tests/test_harness_run_manager.py`：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_run_manager.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness'`

- [ ] **Step 3: 实现 harness 包与 RunManager**

创建 `backend/harness/__init__.py`：

```python
"""Harness 内核：AI Run 生命周期与过程追踪。"""
```

创建 `backend/harness/run_manager.py`：

```python
"""RunManager — AI Run 业务生命周期管理。

纯 async 函数，接 AsyncSession。不管理事务（commit 由调用方负责），
只做对象状态变更与 flush，保证主流程可读 run.id。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from models.ai_run import AiRun
from models.harness_enums import RunStatus


async def create_run(
    db: AsyncSession,
    *,
    project_id: str | uuid.UUID,
    run_type: str,
    mode: str,
    chapter_id: str | uuid.UUID | None = None,
    document_id: str | uuid.UUID | None = None,
    user_goal: str | None = None,
    model_config_snapshot: dict[str, Any] | None = None,
    thread_id: str | None = None,
) -> AiRun:
    run = AiRun(
        project_id=project_id,
        chapter_id=chapter_id,
        document_id=document_id,
        run_type=run_type,
        mode=mode,
        status=RunStatus.CREATED,
        user_goal=user_goal,
        model_config_snapshot=model_config_snapshot,
        thread_id=thread_id,
    )
    db.add(run)
    await db.flush()
    return run


async def mark_running(db: AsyncSession, run: AiRun) -> None:
    run.status = RunStatus.RUNNING
    if run.started_at is None:
        run.started_at = datetime.now(timezone.utc)
    await db.flush()


async def mark_waiting_human(
    db: AsyncSession, run: AiRun, *, step_name: str, thread_id: str | None = None
) -> None:
    run.status = RunStatus.WAITING_HUMAN
    run.current_step = step_name
    if thread_id:
        run.thread_id = thread_id
    await db.flush()


async def mark_completed(db: AsyncSession, run: AiRun) -> None:
    run.status = RunStatus.COMPLETED
    run.finished_at = datetime.now(timezone.utc)
    await db.flush()


async def mark_failed(db: AsyncSession, run: AiRun, error_message: str) -> None:
    run.status = RunStatus.FAILED
    run.error_message = error_message
    run.finished_at = datetime.now(timezone.utc)
    await db.flush()


async def mark_cancelled(db: AsyncSession, run: AiRun) -> None:
    run.status = RunStatus.CANCELLED
    run.finished_at = datetime.now(timezone.utc)
    await db.flush()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_run_manager.py -v`
Expected: PASS（3 个测试）

- [ ] **Step 5: 提交**

```bash
git add backend/harness/__init__.py backend/harness/run_manager.py backend/tests/test_harness_run_manager.py
git commit -m "feat(harness): add RunManager with run lifecycle (create/running/waiting/completed/failed/cancelled)"
```

---

## Task 2: StepLogger

**Files:**
- Create: `backend/harness/step_logger.py`
- Test: `backend/tests/test_harness_run_manager.py`

- [ ] **Step 1: 写失败测试（step start/finish/fail + 幂等键 + 并发）**

追加到 `backend/tests/test_harness_run_manager.py` 末尾：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_run_manager.py -v -k step`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.step_logger'`

- [ ] **Step 3: 实现 StepLogger**

创建 `backend/harness/step_logger.py`：

```python
"""StepLogger — AiRunStep 步骤记录。

纯 async 函数，接 AsyncSession。幂等：start_step 对同一 run+step_name+revision_round
返回既有 step（基于唯一索引），不重复创建。并发安全：critic 与 consistency_checker
step_name 不同 → 幂等键不同 → 可并存。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models.ai_run_step import AiRunStep
from models.harness_enums import RunStepStatus


def _idempotency_key(run_id: str | uuid.UUID, step_name: str, revision_round: int) -> str:
    return f"{run_id}:{step_name}:{revision_round}"


async def start_step(
    db: AsyncSession,
    *,
    run_id: str | uuid.UUID,
    step_order: int,
    step_name: str,
    agent_name: str | None = None,
    revision_round: int = 0,
    input_data: dict[str, Any] | None = None,
    max_retry: int = 0,
) -> AiRunStep:
    key = _idempotency_key(run_id, step_name, revision_round)
    # 幂等：若已存在同 key 的 step，直接返回
    existing = await db.execute(
        select(AiRunStep).where(AiRunStep.idempotency_key == key)
    )
    found = existing.scalar_one_or_none()
    if found is not None:
        return found

    step = AiRunStep(
        run_id=run_id,
        step_order=step_order,
        step_name=step_name,
        agent_name=agent_name,
        status=RunStepStatus.RUNNING,
        input=input_data,
        max_retry=max_retry,
        idempotency_key=key,
        started_at=datetime.now(timezone.utc),
    )
    db.add(step)
    await db.flush()
    return step


async def finish_step(
    db: AsyncSession, step: AiRunStep, *, output: dict[str, Any] | None = None
) -> None:
    step.status = RunStepStatus.SUCCESS
    step.output = output
    step.ended_at = datetime.now(timezone.utc)
    await db.flush()


async def fail_step(
    db: AsyncSession, step: AiRunStep, *, error_message: str
) -> None:
    step.status = RunStepStatus.FAILED
    step.error_message = error_message
    step.ended_at = datetime.now(timezone.utc)
    await db.flush()


async def wait_human_step(
    db: AsyncSession, step: AiRunStep, *, payload: dict[str, Any] | None = None
) -> None:
    step.status = RunStepStatus.WAITING_HUMAN
    step.output = payload
    await db.flush()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_run_manager.py -v`
Expected: PASS（6 个测试全过）

- [ ] **Step 5: 提交**

```bash
git add backend/harness/step_logger.py backend/tests/test_harness_run_manager.py
git commit -m "feat(harness): add StepLogger with idempotent start_step and parallel-safe keys"
```

---

## Task 3: create_generation_record 加 run_id 参数

**Files:**
- Modify: `backend/services/generation_record_service.py:43-79`
- Test: `backend/tests/test_harness_run_manager.py`

- [ ] **Step 1: 写失败测试（record.run_id 落库）**

追加到 `backend/tests/test_harness_run_manager.py` 末尾：

```python
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
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_run_manager.py::test_create_generation_record_with_run_id -v`
Expected: FAIL — `TypeError: create_generation_record() got an unexpected keyword argument 'run_id'`

- [ ] **Step 3: 修改 service 加 run_id 参数**

修改 `backend/services/generation_record_service.py`，在 `create_generation_record` 签名（43-57 行）的 `langfuse_trace_id` 参数后加 `run_id`，并在构造（64-79 行）写入。

签名改为（在 `langfuse_trace_id: str | None = None,` 之后加一行）：

```python
    langfuse_trace_id: str | None = None,
    run_id: str | uuid.UUID | None = None,
    status: str = "candidate",
```

构造改为（在 `langfuse_trace_id=langfuse_trace_id,` 之后加一行）：

```python
        langfuse_trace_id=langfuse_trace_id,
        run_id=run_id,
    )
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_run_manager.py::test_create_generation_record_with_run_id -v`
Expected: PASS

- [ ] **Step 5: 跑 smoke 回归确保老调用未坏**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: 103 passed（老调用不传 run_id，默认 None，行为不变）

- [ ] **Step 6: 提交**

```bash
git add backend/services/generation_record_service.py backend/tests/test_harness_run_manager.py
git commit -m "feat(harness): add run_id param to create_generation_record"
```

---

## Task 4: 前端扩展 SSEEventType union

**Files:**
- Modify: `frontend/src/api/types.ts:727`
- Test: `cd frontend && npx vue-tsc --noEmit`（类型检查不报错）

- [ ] **Step 1: 扩展 union**

修改 `frontend/src/api/types.ts` 第 727 行，在 `'error'` 末尾追加三个新事件：

原行：
```ts
export type SSEEventType = 'progress' | 'agent_start' | 'agent_output' | 'agent_done' | 'writer_output' | 'content_output' | 'editor_output' | 'critic_output' | 'consistency_check' | 'enhance_directions' | 'turn_suggestions' | 'content_suggestions' | 'article_review' | 'revision_suggestions' | 'skill_pack' | 'generation_record' | 'done' | 'error'
```

改为：
```ts
export type SSEEventType = 'progress' | 'agent_start' | 'agent_output' | 'agent_done' | 'writer_output' | 'content_output' | 'editor_output' | 'critic_output' | 'consistency_check' | 'enhance_directions' | 'turn_suggestions' | 'content_suggestions' | 'article_review' | 'revision_suggestions' | 'skill_pack' | 'generation_record' | 'done' | 'error' | 'run_created' | 'run_status' | 'run_step'
```

- [ ] **Step 2: 类型检查确认无错**

Run: `cd frontend && npx vue-tsc --noEmit 2>&1 | tail -5`
Expected: 无新增 error（`run_created`/`run_status`/`run_step` 加入 union 后，`AgentPanel.vue` 的 switch 无匹配 case 也不报错——经核实无 exhaustiveness 断言）。

- [ ] **Step 3: 提交**

```bash
git add frontend/src/api/types.ts
git commit -m "feat(harness): extend SSEEventType union with run_created/run_status/run_step"
```

---

## Task 5: generate_chapter 接入 Run + Step（full_pipeline 路径）

**Files:**
- Modify: `backend/api/routes.py`（event_stream 内，约 3104-3260 行的 full_pipeline 分支）
- Test: `backend/tests/test_harness_generate_integration.py`

这是本阶段核心任务。改动集中在 `event_stream` 的 LangGraph `full_pipeline` 分支，分 5 处接入。

- [ ] **Step 1: 写集成测试（run+step 轨迹 + run_id 关联 + HITL 状态）**

创建 `backend/tests/test_harness_generate_integration.py`：

```python
"""generate_chapter full_pipeline 的 Harness 集成测试。

自包含：自带 TestClient + SQLite 内存库（复用 test_smoke 的 override 套路，
但独立建表，避免依赖 test_smoke 的 session 级 fixture 执行顺序）。
"""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, ARRAY
from sqlalchemy.ext.compiler import compiles

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from models.base import Base
import models  # noqa: F401 — 注册全部模型
from db.session import get_db, set_engine
from main import app

_test_engine = create_async_engine("sqlite+aiosqlite://", echo=False)
_test_session_factory = async_sessionmaker(_test_engine, class_=AsyncSession, expire_on_commit=False)
set_engine(_test_engine)


async def _override_get_db():
    async with _test_session_factory() as session:
        yield session


app.dependency_overrides[get_db] = _override_get_db

client = TestClient(app)


@pytest.fixture(scope="module", autouse=True)
def _setup_db():
    async def _create():
        async with _test_engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    asyncio.run(_create())
    yield


def _parse_sse(raw: str) -> list[tuple[str, object]]:
    events = []
    for block in raw.split("\n\n"):
        ev = ""
        data = ""
        for line in block.split("\n"):
            if line.startswith("event: "):
                ev = line[7:]
            elif line.startswith("data: "):
                data = line[6:]
        if ev or data:
            try:
                events.append((ev, json.loads(data)))
            except json.JSONDecodeError:
                events.append((ev, data))
    return events


def _auth_headers():
    client.post("/api/auth/register", json={"username": "harnessuser", "password": "harnesspass"})
    resp = client.post("/api/auth/login", json={"username": "harnessuser", "password": "harnesspass"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_full_pipeline_leaves_run_and_steps():
    from models.ai_run import AiRun
    from models.ai_run_step import AiRunStep
    from models.generation_record import GenerationRecord
    from models.harness_enums import RunStatus

    headers = _auth_headers()
    proj = client.post("/api/projects", json={"title": "harness 测试", "mode": "novel"}, headers=headers).json()
    pid = proj["id"]
    client.post(f"/api/projects/{pid}/chapters", json={"title": "第一章", "sequence_number": 1}, headers=headers)

    resp = client.post(
        f"/api/projects/{pid}/chapters/generate",
        json={"mode": "full_pipeline", "chapter_num": 1, "target_words": 200},
        headers=headers,
    )
    assert resp.status_code == 200
    events = _parse_sse(resp.text)
    event_names = [e for e, _ in events]

    # 旧事件仍在
    assert "agent_start" in event_names
    assert "writer_output" in event_names
    # 新事件出现
    assert "run_created" in event_names

    async def _query():
        async with _test_session_factory() as s:
            runs = (await s.execute(select(AiRun).where(AiRun.project_id == pid))).scalars().all()
            assert len(runs) >= 1
            run = runs[0]
            assert run.run_type == "CHAPTER_DRAFT"
            assert run.status in (RunStatus.COMPLETED, RunStatus.WAITING_HUMAN, RunStatus.FAILED)
            steps = (await s.execute(
                select(AiRunStep).where(AiRunStep.run_id == run.id).order_by(AiRunStep.step_order)
            )).scalars().all()
            step_names = [s.step_name for s in steps]
            assert "build_context" in step_names
            assert "generate_draft" in step_names
            recs = (await s.execute(
                select(GenerationRecord).where(GenerationRecord.run_id == run.id)
            )).scalars().all()
            assert len(recs) >= 1

    asyncio.run(_query())
```

注意：用 `asyncio.run(_query())` 而非 `get_event_loop().run_until_complete`（与 test_smoke 的 `asyncio.run` 套路一致）。`_setup_db` 是 module 级 autouse fixture，独立建表，不依赖 test_smoke 的 fixture。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_generate_integration.py -v`
Expected: FAIL — `run_created` 不在事件里（尚未接入）

- [ ] **Step 3: 接入 — event_stream full_pipeline 分支创建 run + 发 run_created**

在 `backend/api/routes.py` 的 `event_stream` 内，`full_pipeline` 分支。先在文件顶部 import 区确认/补：

```python
from harness.run_manager import create_run, mark_running, mark_waiting_human, mark_completed, mark_failed, mark_cancelled
from harness.step_logger import start_step, finish_step, fail_step
```

在 `thread_id` 创建之后（约 3113 行后）、`initial_state` 构建之前，插入 run 创建：

```python
        # ── Harness: 创建 AI Run ──
        run_type = {
            "full_pipeline": "CHAPTER_DRAFT",
            "continue": "CHAPTER_CONTINUE",
            "enhance": "CHAPTER_REWRITE",
            "summarize": "SUMMARY_GENERATION",
        }.get(req.mode, "CHAPTER_DRAFT")
        ai_run = await create_run(
            db,
            project_id=uid,
            chapter_id=target_chapter_id,
            document_id=target_document_id,
            run_type=run_type,
            mode=req.mode,
            user_goal=req.user_note,
            model_config_snapshot=llm_config_dict,
        )
        await db.flush()
        run_id_str = str(ai_run.id)
        yield f"event: run_created\ndata: {json.dumps({'run_id': run_id_str, 'status': 'CREATED'}, ensure_ascii=False)}\n\n"
        await mark_running(db, ai_run)
        yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'RUNNING'}, ensure_ascii=False)}\n\n"
```

注意：`llm_config_dict` 在 2552 行已取；`uid` 是 project_id 的字符串形式（event_stream 顶部已有）。若 `llm_config_dict` 作用域在此处不可达，改用 `req.model_dump()` 简化或省略 `model_config_snapshot`（传 None）。

- [ ] **Step 4: 接入 — astream_events 循环写 step**

在 SSE 循环（3172 行 `async for event in app.astream_events(...)`）的 `on_chain_start` 与 `on_chain_end` 分支里，对已注册的节点名写 step。在循环前定义 step 追踪变量：

在 `NODE_EVENT_MAP = {...}`（3157 行）之后追加：

```python
        # ── Harness: step 追踪 ──
        STEP_NODE_MAP = {
            "context_loader": ("build_context", 1),
            "writer": ("generate_draft", 2),
            "critic": ("critique", 3),
            "consistency_checker": ("consistency_check", 3),
            "human_review": ("human_review", 4),
        }
        active_steps: dict[str, object] = {}  # node_name -> AiRunStep
        revision_round = 0
```

在 `on_chain_start` 分支（3178 行 `if kind == "on_chain_start":`）内，`yield agent_start` 之后追加：

```python
            if node_name in STEP_NODE_MAP:
                step_name, step_order = STEP_NODE_MAP[node_name]
                try:
                    step = await start_step(
                        db,
                        run_id=ai_run.id,
                        step_order=step_order,
                        step_name=step_name,
                        agent_name=node_name,
                        revision_round=revision_round,
                    )
                    active_steps[node_name] = step
                    ai_run.current_step = step_name
                    await db.flush()
                    yield f"event: run_step\ndata: {json.dumps({'run_id': run_id_str, 'step_name': step_name, 'status': 'RUNNING'}, ensure_ascii=False)}\n\n"
                except Exception:
                    logger.warning("harness start_step 失败 node=%s", node_name, exc_info=True)
```

在 `on_chain_end` 分支（3187 行 `elif kind == "on_chain_end":`）内，`yield agent_done` 之后、各 node 分支之前追加 step 收尾：

```python
            if node_name in STEP_NODE_MAP and node_name in active_steps:
                step = active_steps.pop(node_name)
                try:
                    output_snapshot: dict = {}
                    if isinstance(output, dict):
                        if node_name == "writer":
                            output_snapshot = {"content_hash": str(hash(output.get("draft", "")))[:128]}
                        elif node_name == "context_loader":
                            output_snapshot = {"context_len": len(output.get("context", ""))}
                        elif node_name == "critic":
                            output_snapshot = {"critique_count": len(output.get("critiques", []))}
                        elif node_name == "consistency_checker":
                            output_snapshot = {"report_len": len(output.get("consistency_report", ""))}
                    await finish_step(db, step, output=output_snapshot)
                    yield f"event: run_step\ndata: {json.dumps({'run_id': run_id_str, 'step_name': STEP_NODE_MAP[node_name][0], 'status': 'SUCCESS'}, ensure_ascii=False)}\n\n"
                except Exception:
                    logger.warning("harness finish_step 失败 node=%s", node_name, exc_info=True)
```

- [ ] **Step 5: 接入 — HITL 暂停 mark WAITING_HUMAN**

在两处 HITL `progress` yield 之前（3218-3221 和 3247-3250），`_save_generation_history` 调用传 `run_id` 并 mark WAITING_HUMAN。

3218 行处（循环内 human_review 分支）：

```python
          elif node_name == "human_review":
              record_id = await _save_generation_history(writer_content, skill_packs=workflow_skill_packs, run_id=run_id_str)
              yield _generation_record_event(record_id)
              await mark_waiting_human(db, ai_run, step_name="human_review", thread_id=thread_id)
              await db.commit()
              yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'WAITING_HUMAN'}, ensure_ascii=False)}\n\n"
              yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
              return
```

3247 行处（循环后 next_nodes 含 human_review）：

```python
      if "human_review" in next_nodes:
          record_id = await _save_generation_history(writer_content, skill_packs=workflow_skill_packs, run_id=run_id_str)
          yield _generation_record_event(record_id)
          await mark_waiting_human(db, ai_run, step_name="human_review", thread_id=thread_id)
          await db.commit()
          yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'WAITING_HUMAN'}, ensure_ascii=False)}\n\n"
          yield f"event: progress\ndata: {json.dumps({'message': '等待人工审核', 'thread_id': thread_id}, ensure_ascii=False)}\n\n"
          return
```

- [ ] **Step 6: 接入 — 正常结束 mark COMPLETED + run_id 关联**

3252 行处（正常 done）：

```python
      record_id = await _save_generation_history(writer_content, skill_packs=workflow_skill_packs, run_id=run_id_str)
      yield _generation_record_event(record_id)
      await mark_completed(db, ai_run)
      await db.commit()
      yield f"event: run_status\ndata: {json.dumps({'run_id': run_id_str, 'status': 'COMPLETED'}, ensure_ascii=False)}\n\n"
      yield f"event: done\ndata: {json.dumps({'message': '生成完成'}, ensure_ascii=False)}\n\n"
```

- [ ] **Step 7: 接入 — 异常 mark FAILED + 客户端断开 mark CANCELLED**

在 `event_stream` 的外层 `except Exception as e:`（3256 行）内，`yield error` 之前加：

```python
      except Exception as e:
          logger.exception("生成失败")
          try:
              await mark_failed(db, ai_run, error_message=str(e))
              await db.commit()
          except Exception:
              logger.warning("harness mark_failed 失败", exc_info=True)
          yield f"event: error\ndata: {json.dumps({'message': f'生成失败: {str(e)}'}, ensure_ascii=False)}\n\n"
```

客户端断开：`_check_cancelled` 返回 True 时（3174 行 `if await _check_cancelled(): return`），在 return 前加：

```python
          if await _check_cancelled():
              try:
                  await mark_cancelled(db, ai_run)
                  await db.commit()
              except Exception:
                  logger.warning("harness mark_cancelled 失败", exc_info=True)
              return
```

- [ ] **Step 8: 接入 — _save_generation_history 加 run_id 参数**

修改 `backend/api/routes.py` 中 `_save_generation_history`（2484-2516 行）签名加 `run_id`，并透传给 `create_generation_record`。

签名（2484 行）加参数：

```python
  async def _save_generation_history(
      content: str,
      *,
      expert_id: str | uuid.UUID | None = None,
      review_results: dict | None = None,
      skill_packs: list[dict] | None = None,
      run_id: str | None = None,
  ) -> str | None:
```

调用 `create_generation_record`（2495 行）加 `run_id=run_id,`：

```python
          record = await create_generation_record(
              db,
              project_id=uid,
              chapter_id=target_chapter_id,
              document_id=target_document_id,
              mode=req.mode,
              expert_id=expert_id,
              content=content,
              req=req,
              review_results=review_results,
              skill_packs=skill_packs,
              langfuse_trace_id=current_langfuse_trace_id(),
              run_id=run_id,
          )
```

- [ ] **Step 9: 运行集成测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_generate_integration.py -v`
Expected: PASS（run_created 出现、DB 有 run+steps+generation_record.run_id 关联）

- [ ] **Step 10: 跑 smoke 回归确保老流程不坏**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: 103 passed

- [ ] **Step 11: 提交**

```bash
git add backend/api/routes.py backend/tests/test_harness_generate_integration.py
git commit -m "feat(harness): integrate Run+Step tracking into full_pipeline generate (run_created/step/WAITING_HUMAN/FAILED/CANCELLED)"
```

---

## Task 6: resume 路径关联 run_id（最小改动）

**Files:**
- Modify: `backend/api/routes.py:3324-3347`（`_save_resume_generation_history`）

- [ ] **Step 1: 给 resume 的保存函数加 run_id 透传**

修改 `backend/api/routes.py` 中 `_save_resume_generation_history`（3324-3347 行），签名加 `run_id` 并透传（与 Task 5 Step 8 同模式）。

签名加 `run_id: str | None = None,`，调用 `create_generation_record` 加 `run_id=run_id,`。

调用方（resume 路径中调用 `_save_resume_generation_history` 处）传入 run_id。resume 路径需要从 `thread_id` 反查 run：在 resume 函数顶部查询 `AiRun`（`select(AiRun).where(AiRun.thread_id == thread_id)`），取 `run.id`。若查不到则传 None（兼容旧 run）。

在 resume endpoint（3267 行 `async def resume_chapter_generation`）内，解析 `thread_id` 后加：

```python
      # ── Harness: 反查 run_id ──
      from sqlalchemy import select as _sa_select
      from models.ai_run import AiRun as _AiRun
      _run_row = (await db.execute(_sa_select(_AiRun).where(_AiRun.thread_id == thread_id))).scalar_one_or_none()
      _resume_run_id = str(_run_row.id) if _run_row else None
```

并把 `_save_resume_generation_history(...)` 调用补 `run_id=_resume_run_id`。

- [ ] **Step 2: 跑 smoke 回归**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: 103 passed

- [ ] **Step 3: 提交**

```bash
git add backend/api/routes.py
git commit -m "feat(harness): thread run_id association in resume path"
```

---

## Task 7: 阶段 B 验收

- [ ] **Step 1: 跑全部 harness 测试**

Run: `cd backend && python -m pytest tests/test_harness_run_manager.py tests/test_harness_generate_integration.py -v`
Expected: 全部 PASS

- [ ] **Step 2: 跑 smoke 回归**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: 103 passed

- [ ] **Step 3: 跑全部后端测试**

Run: `cd backend && python -m pytest tests/ -q`
Expected: 全绿

- [ ] **Step 4: 前端类型检查**

Run: `cd frontend && npx vue-tsc --noEmit 2>&1 | tail -5`
Expected: 无新增 error

- [ ] **Step 5: 确认旧前端体验不坏（手动核对事件流）**

发起一次 full_pipeline 生成，确认：
- `agent_start` / `writer_output` / `done` 仍出现
- `run_created` / `run_step` / `run_status` 新出现但不影响前端渲染（AgentWorkflow 步骤状态仍正常）
- 生成完成后 DB 中 `ai_runs` 状态为 COMPLETED 或 WAITING_HUMAN
- `generation_records.run_id` 非空

---

## 验收清单（对应文档阶段 B 验收 + 用户口径）

1. 发起一次生成后，数据库有一条 `ai_runs` ✅ (Task 7 Step 5)
2. 至少有 `build_context / generate_draft` 两条 `ai_run_steps` ✅ (Task 5 Step 4)
3. 生成失败时 run 可查询到失败原因 ✅ (Task 5 Step 7)
4. 旧前端仍能正常流式显示 ✅ (Task 7 Step 5 + Task 4)
5. `generation_records.run_id` 落关联 ✅ (Task 5 Step 8 + Task 3)
6. WAITING_HUMAN 状态更新 ✅ (Task 5 Step 5)
7. 客户端断开 mark CANCELLED ✅ (Task 5 Step 7)
8. 不重构 routes.py 结构、不改 workflow.py、不碰 LoggedLLMProvider/Guardrail/MemorySaver ✅ (全程约束)
