# 阶段 D：Run API 与最小前端露出 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 后端新增 3 个 Run 查询 API（按 run_id 查详情、查 steps、按 project 列 run），权限校验 run 属于当前用户的 project；前端加 3 个 API client 方法 + AiRun/AiRunStep 类型 + AgentPanel 保存 latestRunId，不做完整 Run 面板。

**Architecture:** 后端沿用现有 `_verify_project_owner` + `_to_uuid` + `response_model` 模式，新增 Pydantic response schema（`AiRunResponse` / `AiRunStepResponse`）和 3 个路由。`GET /api/ai-runs/{run_id}` 不能裸查 run_id——先查 run 拿 project_id，再 `_verify_project_owner` 校验归属。steps 查询按 `step_order, started_at` 排序，可选附 `llm_call_count`（子查询 COUNT）。前端只加类型 + client 方法 + `run_created` case 存 `latestRunId`，无新组件。

**Tech Stack:** Python 3.10，FastAPI，Pydantic v2，SQLAlchemy 2.0 Async，pytest（SQLite 内存），Vue 3 / TypeScript（仅 types + client + 1 个 switch case）。

---

## 依赖假设（Phase C 经验，勿违背）

- **`llm_call_logs.step_id` 靠运行中 step 查询解析**，不依赖 graph state 注入。`LoggedLLMProvider._resolve_running_step_id()` 在写日志时按 `run_id` + `agent_name→step_name` 映射查最新 RUNNING step。后续做 step/call 摘要时，直接 JOIN `llm_call_logs.run_id + step_id` 即可，**不要**尝试从 graph state 读 `harness_step_id`（并行/动态节点下有竞态，Phase C 已验证并移除该路径）。
- `llm_call_logs` 用独立 session 写入（`get_engine()` 新开），不依赖请求 session。查询时用请求 session 即可（只读）。
- token/cost 字段当前全 NULL（第一阶段），API 响应里 `token_usage`/`cost_usage` 可为 null。

---

## 文件结构

**新增：**
- `backend/tests/test_harness_run_api.py` — 3 个 API 的路由测试（权限 + 排序 + 分页 + step 摘要）。

**修改：**
- `backend/schemas/api.py` — 新增 `AiRunResponse` / `AiRunStepResponse` / `AiRunListItemResponse`（末尾追加）。
- `backend/api/routes.py` — 新增 3 个路由（`GET /ai-runs/{run_id}`、`GET /ai-runs/{run_id}/steps`、`GET /projects/{project_id}/ai-runs`）。
- `frontend/src/api/types.ts` — 新增 `ApiRun` / `ApiRunStep` / `RunCreatedPayload` 接口。
- `frontend/src/api/client.ts` — 新增 `getRun` / `getRunSteps` / `listProjectRuns` 3 个方法。
- `frontend/src/components/AgentPanel.vue` — 新增 `latestRunId` ref + `run_created` case。

**约束：**
- `GET /api/ai-runs/{run_id}` 必须校验 run 的 project 属于当前用户（不能裸查 run_id）。
- 不做 Human decision API、Guardrail UI、trace 可视化、token/cost 聚合、prompt snapshot 展示。
- 前端不做完整 `AiRunPanel.vue`，只存 `latestRunId` + 可选轻量显示。

---

## Task 1: Pydantic response schema

**Files:**
- Modify: `backend/schemas/api.py`（末尾追加）
- Test: `backend/tests/test_harness_run_api.py`

- [ ] **Step 1: 写失败测试（schema 可从 ORM 构造）**

创建 `backend/tests/test_harness_run_api.py`：

```python
"""Run 查询 API 路由测试 — SQLite 内存 + TestClient"""

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine, AsyncSession
from sqlalchemy.dialects.sqlite.base import SQLiteTypeCompiler
from sqlalchemy.dialects.postgresql import JSONB, UUID as PG_UUID, ARRAY
from sqlalchemy.ext.compiler import compiles

SQLiteTypeCompiler.visit_JSONB = lambda self, type_, **kw: "JSON"
SQLiteTypeCompiler.visit_ARRAY = lambda self, type_, **kw: "JSON"


@compiles(PG_UUID, "sqlite")
def _compile_uuid_sqlite(type_, compiler, **kw):
    return "CHAR(36)"


from models.base import Base
import models  # noqa: F401
import tests.test_smoke as smoke
from db.session import get_db, set_engine
from main import app

_test_engine = smoke.test_engine
_test_session_factory = smoke.test_session_factory
client = smoke.client


@pytest.fixture(autouse=True)
def _ensure_db():
    from db.session import set_engine
    set_engine(_test_engine)
    asyncio.run(_ensure_tables_fn())
    yield


async def _ensure_tables_fn():
    async with _test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def _auth_headers():
    client.post("/api/auth/register", json={"username": "runapiuser", "password": "runapipass"})
    resp = client.post("/api/auth/login", json={"username": "runapiuser", "password": "runapipass"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_project_and_run(headers, mode="full_pipeline"):
    """建项目 + 章节 + 发起生成，返回 (project_id, run_id)。"""
    proj = client.post("/api/projects", json={"title": "run api 测试", "mode": "novel"}, headers=headers).json()
    pid = proj["id"]
    client.post(f"/api/projects/{pid}/chapters", json={"title": "ch1", "sequence_number": 1}, headers=headers)
    resp = client.post(f"/api/projects/{pid}/chapters/generate",
                       json={"mode": mode, "chapter_num": 1, "target_words": 200}, headers=headers)
    # 从 SSE 提取 run_id
    run_id = None
    for block in resp.text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: ") and "run_id" in line:
                import json
                try:
                    d = json.loads(line[6:])
                    run_id = d.get("run_id")
                except Exception:
                    pass
    return pid, run_id


def test_ai_run_response_schema():
    """AiRunResponse 可从 ORM 对象构造。"""
    from schemas.api import AiRunResponse
    from models.ai_run import AiRun
    from models.harness_enums import RunStatus

    run = AiRun(project_id=uuid.uuid4(), run_type="CHAPTER_DRAFT", mode="full_pipeline")
    resp = AiRunResponse.model_validate(run)
    assert resp.run_type == "CHAPTER_DRAFT"
    assert resp.status == RunStatus.CREATED
    assert resp.token_usage is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_run_api.py::test_ai_run_response_schema -v`
Expected: FAIL — `ImportError: cannot import name 'AiRunResponse'`

- [ ] **Step 3: 实现 schema**

在 `backend/schemas/api.py` 末尾追加：

```python
class AiRunResponse(BaseModel):
    id: uuid.UUID
    project_id: uuid.UUID
    chapter_id: uuid.UUID | None = None
    document_id: uuid.UUID | None = None
    generation_record_id: uuid.UUID | None = None
    run_type: str
    mode: str
    status: str
    current_step: str | None = None
    user_goal: str | None = None
    thread_id: str | None = None
    token_usage: dict | None = None
    cost_usage: dict | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class AiRunListItemResponse(BaseModel):
    id: uuid.UUID
    run_type: str
    mode: str
    status: str
    current_step: str | None = None
    generation_record_id: uuid.UUID | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class AiRunStepResponse(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    step_order: int
    step_name: str
    agent_name: str | None = None
    status: str
    error_message: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    llm_call_count: int = 0

    model_config = {"from_attributes": True}
```

注意：`AiRunStepResponse` 有 `llm_call_count` 但 ORM `AiRunStep` 无此字段——路由层手动构造时填入。

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_run_api.py::test_ai_run_response_schema -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/schemas/api.py backend/tests/test_harness_run_api.py
git commit -m "feat(harness): add AiRunResponse/AiRunStepResponse/AiRunListItemResponse schemas"
```

---

## Task 2: GET /api/ai-runs/{run_id} 路由

**Files:**
- Modify: `backend/api/routes.py`
- Test: `backend/tests/test_harness_run_api.py`

- [ ] **Step 1: 写失败测试（项目内 run 可查，跨用户不可查）**

追加到 `backend/tests/test_harness_run_api.py`：

```python
def test_get_run_by_id_ok():
    """项目内 run 可查，返回完整字段。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    assert run_id is not None

    resp = client.get(f"/api/ai-runs/{run_id}", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == run_id
    assert data["project_id"] == pid
    assert data["run_type"] == "CHAPTER_DRAFT"
    assert data["status"] in ("COMPLETED", "WAITING_HUMAN", "FAILED", "CANCELLED")


def test_get_run_by_id_cross_user_forbidden():
    """跨用户查 run 返回 404（不泄露存在性）。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    assert run_id is not None

    # 另一用户
    client.post("/api/auth/register", json={"username": "otheruser", "password": "otherpass"})
    other_resp = client.post("/api/auth/login", json={"username": "otheruser", "password": "otherpass"})
    other_headers = {"Authorization": f"Bearer {other_resp.json()['access_token']}"}

    resp = client.get(f"/api/ai-runs/{run_id}", headers=other_headers)
    assert resp.status_code == 404


def test_get_run_by_id_not_found():
    """不存在的 run_id 返回 404。"""
    headers = _auth_headers()
    resp = client.get(f"/api/ai-runs/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_run_api.py -v -k get_run_by_id`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 实现路由**

在 `backend/api/routes.py` 中（resume 路由之前，约 3360 行附近）新增：

```python
@router.get("/ai-runs/{run_id}", response_model=AiRunResponse)
async def get_ai_run(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """查询单个 AI Run 详情。校验 run 的 project 属于当前用户。"""
    rid = _to_uuid(run_id)
    result = await db.execute(select(AiRun).where(AiRun.id == rid))
    run = result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run 不存在")
    # 校验归属：run 的 project 必须属于当前用户
    await _verify_project_owner(run.project_id, user.id, db)
    return AiRunResponse.model_validate(run)
```

同时在 routes.py 顶部 import 区补（若未有）：

```python
from schemas.api import AiRunResponse, AiRunStepResponse, AiRunListItemResponse
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_run_api.py -v -k get_run_by_id`
Expected: 3 passed

- [ ] **Step 5: 提交**

```bash
git add backend/api/routes.py backend/tests/test_harness_run_api.py
git commit -m "feat(harness): GET /api/ai-runs/{run_id} with project ownership check"
```

---

## Task 3: GET /api/ai-runs/{run_id}/steps 路由

**Files:**
- Modify: `backend/api/routes.py`
- Test: `backend/tests/test_harness_run_api.py`

- [ ] **Step 1: 写失败测试（steps 排序 + llm_call_count）**

追加到 `backend/tests/test_harness_run_api.py`：

```python
def test_get_run_steps_ordered():
    """steps 按 step_order 排序，含 llm_call_count。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    assert run_id is not None

    resp = client.get(f"/api/ai-runs/{run_id}/steps", headers=headers)
    assert resp.status_code == 200
    steps = resp.json()
    assert len(steps) >= 1
    # 按 step_order 升序
    orders = [s["step_order"] for s in steps]
    assert orders == sorted(orders)
    # 每个 step 有 llm_call_count 字段
    for s in steps:
        assert "llm_call_count" in s
        assert isinstance(s["llm_call_count"], int)
    # 至少有 build_context
    step_names = [s["step_name"] for s in steps]
    assert "build_context" in step_names


def test_get_run_steps_cross_user_forbidden():
    """跨用户查 steps 返回 404。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    client.post("/api/auth/register", json={"username": "otheruser2", "password": "otherpass"})
    other_resp = client.post("/api/auth/login", json={"username": "otheruser2", "password": "otherpass"})
    other_headers = {"Authorization": f"Bearer {other_resp.json()['access_token']}"}
    resp = client.get(f"/api/ai-runs/{run_id}/steps", headers=other_headers)
    assert resp.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_run_api.py -v -k get_run_steps`
Expected: FAIL

- [ ] **Step 3: 实现路由**

在 `backend/api/routes.py` 的 `get_ai_run` 之后新增：

```python
@router.get("/ai-runs/{run_id}/steps", response_model=list[AiRunStepResponse])
async def get_ai_run_steps(
    run_id: str,
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """查询 Run 的步骤列表，按 step_order 升序。含每步 llm_call_count。"""
    from models.ai_run_step import AiRunStep
    from models.llm_call_log import LlmCallLog
    from sqlalchemy import func

    rid = _to_uuid(run_id)
    # 先校验 run 归属（复用 get_ai_run 的逻辑）
    run_result = await db.execute(select(AiRun).where(AiRun.id == rid))
    run = run_result.scalar_one_or_none()
    if not run:
        raise HTTPException(status_code=404, detail="Run 不存在")
    await _verify_project_owner(run.project_id, user.id, db)

    # 查 steps + 每步 llm_call_count（LEFT JOIN COUNT）
    result = await db.execute(
        select(
            AiRunStep,
            func.count(LlmCallLog.id).label("llm_call_count"),
        )
        .outerjoin(LlmCallLog, LlmCallLog.step_id == AiRunStep.id)
        .where(AiRunStep.run_id == rid)
        .group_by(AiRunStep.id)
        .order_by(AiRunStep.step_order, AiRunStep.started_at.nulls_last())
    )
    return [
        AiRunStepResponse(
            id=row.AiRunStep.id,
            run_id=row.AiRunStep.run_id,
            step_order=row.AiRunStep.step_order,
            step_name=row.AiRunStep.step_name,
            agent_name=row.AiRunStep.agent_name,
            status=row.AiRunStep.status,
            error_message=row.AiRunStep.error_message,
            started_at=row.AiRunStep.started_at,
            ended_at=row.AiRunStep.ended_at,
            llm_call_count=row.llm_call_count,
        )
        for row in result.all()
    ]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_run_api.py -v -k get_run_steps`
Expected: 2 passed

- [ ] **Step 5: 提交**

```bash
git add backend/api/routes.py backend/tests/test_harness_run_api.py
git commit -m "feat(harness): GET /api/ai-runs/{run_id}/steps with llm_call_count"
```

---

## Task 4: GET /api/projects/{project_id}/ai-runs 路由

**Files:**
- Modify: `backend/api/routes.py`
- Test: `backend/tests/test_harness_run_api.py`

- [ ] **Step 1: 写失败测试（列表 + limit + 跨项目不可查）**

追加到 `backend/tests/test_harness_run_api.py`：

```python
def test_list_project_runs():
    """项目 run 列表，按 created_at 降序，默认 limit 20。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    assert run_id is not None

    resp = client.get(f"/api/projects/{pid}/ai-runs", headers=headers)
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) >= 1
    assert any(r["id"] == run_id for r in runs)
    # 降序
    created = [r["created_at"] for r in runs]
    assert created == sorted(created, reverse=True)


def test_list_project_runs_limit():
    """limit 参数生效。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)

    resp = client.get(f"/api/projects/{pid}/ai-runs?limit=1", headers=headers)
    assert resp.status_code == 200
    runs = resp.json()
    assert len(runs) <= 1


def test_list_project_runs_cross_user_forbidden():
    """跨用户查项目 run 列表返回 404。"""
    headers = _auth_headers()
    pid, run_id = _create_project_and_run(headers)
    client.post("/api/auth/register", json={"username": "otheruser3", "password": "otherpass"})
    other_resp = client.post("/api/auth/login", json={"username": "otheruser3", "password": "otherpass"})
    other_headers = {"Authorization": f"Bearer {other_resp.json()['access_token']}"}
    resp = client.get(f"/api/projects/{pid}/ai-runs", headers=other_headers)
    assert resp.status_code == 404
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_run_api.py -v -k list_project_runs`
Expected: FAIL

- [ ] **Step 3: 实现路由**

在 `backend/api/routes.py` 的 `get_ai_run_steps` 之后新增：

```python
@router.get("/projects/{project_id}/ai-runs", response_model=list[AiRunListItemResponse])
async def list_project_ai_runs(
    project_id: str,
    limit: int = Query(default=20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    user: AuthUser = Depends(get_current_user),
):
    """查询项目的 AI Run 列表，按 created_at 降序。"""
    uid = _to_uuid(project_id)
    await _verify_project_owner(uid, user.id, db)
    result = await db.execute(
        select(AiRun)
        .where(AiRun.project_id == uid)
        .order_by(AiRun.created_at.desc())
        .limit(limit)
    )
    return [AiRunListItemResponse.model_validate(run) for run in result.scalars().all()]
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_run_api.py -v -k list_project_runs`
Expected: 3 passed

- [ ] **Step 5: 跑全部 run api 测试 + smoke 回归**

Run: `cd backend && python -m pytest tests/test_harness_run_api.py tests/test_smoke.py -q`
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add backend/api/routes.py backend/tests/test_harness_run_api.py
git commit -m "feat(harness): GET /api/projects/{project_id}/ai-runs with limit"
```

---

## Task 5: 前端类型 + API client 方法

**Files:**
- Modify: `frontend/src/api/types.ts`
- Modify: `frontend/src/api/client.ts`

- [ ] **Step 1: 加类型定义**

在 `frontend/src/api/types.ts` 中 `ApiGenerationRecord` 接口之后（约 919 行后）追加：

```typescript
export interface ApiRunListItem {
  id: string
  run_type: string
  mode: string
  status: string
  current_step: string | null
  generation_record_id: string | null
  started_at: string | null
  finished_at: string | null
  created_at: string
}

export interface ApiRun extends ApiRunListItem {
  project_id: string
  chapter_id: string | null
  document_id: string | null
  user_goal: string | null
  thread_id: string | null
  token_usage: Record<string, unknown> | null
  cost_usage: Record<string, unknown> | null
  error_message: string | null
  updated_at: string
}

export interface ApiRunStep {
  id: string
  run_id: string
  step_order: number
  step_name: string
  agent_name: string | null
  status: string
  error_message: string | null
  started_at: string | null
  ended_at: string | null
  llm_call_count: number
}

export interface RunCreatedPayload {
  run_id: string
  status: string
}
```

- [ ] **Step 2: 加 API client 方法**

在 `frontend/src/api/client.ts` 中 `listChapterGenerations` 附近（约 725 行后）追加：

```typescript
  getRun: (runId: string) =>
    request<ApiRun>(`/ai-runs/${runId}`),

  getRunSteps: (runId: string) =>
    request<ApiRunStep[]>(`/ai-runs/${runId}/steps`),

  listProjectRuns: (projectId: string, limit?: number) =>
    request<ApiRunListItem[]>(`/projects/${projectId}/ai-runs${limit ? `?limit=${limit}` : ''}`),
```

同时在 client.ts 顶部 import 区补 `ApiRun, ApiRunStep, ApiRunListItem`（若 `request` 的泛型需要）。

- [ ] **Step 3: 类型检查**

Run: `cd frontend && npx vue-tsc --noEmit 2>&1 | tail -5`
Expected: 无新增 error

- [ ] **Step 4: 提交**

```bash
git add frontend/src/api/types.ts frontend/src/api/client.ts
git commit -m "feat(harness): add ApiRun/ApiRunStep types + getRun/getRunSteps/listProjectRuns client methods"
```

---

## Task 6: AgentPanel 保存 latestRunId

**Files:**
- Modify: `frontend/src/components/AgentPanel.vue`

- [ ] **Step 1: 加 latestRunId ref**

在 `frontend/src/components/AgentPanel.vue` 中 `latestGenerationRecordId` 声明附近（约 67 行）追加：

```typescript
const latestRunId = ref<string | null>(null)
```

- [ ] **Step 2: 在生成开始时重置**

在 `latestGenerationRecordId.value = null` 的重置点（约 340、384、559 行）旁追加：

```typescript
    latestRunId.value = null
```

（与 `latestGenerationRecordId.value = null` 同位置。）

- [ ] **Step 3: 加 run_created case**

在 `handleSSEEvent` 的 switch 中（`generation_record` case 之后，`done` case 之前）追加：

```typescript
    case 'run_created': {
      const payload = data as unknown as RunCreatedPayload
      latestRunId.value = payload.run_id
      break
    }
```

同时在 AgentPanel.vue 的 import 区（或 types import）补 `RunCreatedPayload`（从 `../api/types` 导入）。

- [ ] **Step 4: 类型检查**

Run: `cd frontend && npx vue-tsc --noEmit 2>&1 | tail -5`
Expected: 无新增 error

- [ ] **Step 5: 提交**

```bash
git add frontend/src/components/AgentPanel.vue
git commit -m "feat(harness): save latestRunId from run_created SSE event in AgentPanel"
```

---

## Task 7: 阶段 D 验收

- [ ] **Step 1: 跑全部后端测试**

Run: `cd backend && python -m pytest tests/test_harness_run_api.py tests/test_harness_generate_integration.py tests/test_harness_run_manager.py tests/test_harness_models.py tests/test_harness_llm_call_logger.py tests/test_smoke.py -q`
Expected: 全绿

- [ ] **Step 2: 前端类型检查**

Run: `cd frontend && npx vue-tsc --noEmit 2>&1 | tail -5`
Expected: 无 error

- [ ] **Step 3: 项目 venv 3.10 import 检查**

Run: `cd backend && /Users/zcx/ai-creative-platform/backend/venv/bin/python -c "import api.routes; print('OK')"`
Expected: OK

- [ ] **Step 4: 手动确认 API 响应**

发起一次生成后：
- `GET /api/ai-runs/{run_id}` 返回完整 run 详情，status/current_step/error_message 齐全
- `GET /api/ai-runs/{run_id}/steps` 返回 steps 按 step_order 排序，每步有 llm_call_count
- `GET /api/projects/{project_id}/ai-runs` 返回 run 列表降序

---

## 验收清单

1. `GET /api/ai-runs/{run_id}` ✅ (Task 2)
2. `GET /api/ai-runs/{run_id}/steps` ✅ (Task 3)
3. `GET /api/projects/{project_id}/ai-runs` ✅ (Task 4)
4. run response 含 status/current_step/error/generation_record_id/thread_id/token_usage/cost_usage ✅ (Task 1)
5. step response 含 step_order/step_name/agent_name/status/llm_call_count ✅ (Task 1+3)
6. 权限：跨用户/跨项目 404 ✅ (Task 2+3+4)
7. steps 按 step_order, started_at 排序 ✅ (Task 3)
8. project run list 可 limit ✅ (Task 4)
9. 前端 types + client + latestRunId ✅ (Task 5+6)
10. 不做 Human decision/Guardrail UI/trace/token 聚合/prompt 展示 ✅ (全程约束)
