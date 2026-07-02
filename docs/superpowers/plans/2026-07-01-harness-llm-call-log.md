# 阶段 C：LLM Call Log 与 Prompt/Context Snapshot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 full_pipeline 的 writer / critic / consistency_checker 三类 LLM 调用每次都在本地 `llm_call_logs` 表留下审计记录，含 run_id/step_id/agent_name/provider/model/latency_ms/rendered_prompt_snapshot/context_package_snapshot，token/cost 拿到就写、拿不到允许 NULL，不改 llm_provider.py 抽象、不改前端 UI。

**Architecture:** 新增 `backend/harness/llm_call_logger.py`，提供 `LoggedLLMProvider`——一个实现 `LLMProvider` 同接口（`generate`/`generate_stream`）的装饰器，内部委托给被包装的真实 provider，在每次调用前后测量 latency、拼装 rendered_prompt_snapshot、写 `LlmCallLog` 行。不修改 `llm_provider.py`（零改动抽象层）。接入点在 `workflow.py` 的 3 个 builtin 节点 + 4 个 dynamic expert 调用点：把 `llm = get_llm_provider(state.get("llm_config"))` 改为包一层 `LoggedLLMProvider`，run_id/step_id/db/context 通过 `CreativeState` 新增的可选键注入（routes.py 在创建 run 后把 `harness_run_id`/`harness_db`/`harness_step_id` 塞进 state）。token/cost：`OpenAIProvider.generate` 不暴露 usage（返回纯 str），所以第一阶段 token 列写 NULL；后续若改 provider 返回 usage 再补。

**Tech Stack:** Python 3.10（项目 venv），SQLAlchemy 2.0 Async，pytest（SQLite 内存 + MockProvider）。

---

## 文件结构

**新增：**
- `backend/harness/llm_call_logger.py` — `LoggedLLMProvider` 装饰器 + `log_llm_call` 辅助函数。
- `backend/tests/test_harness_llm_call_logger.py` — wrapper 单测（mock provider + latency + prompt snapshot + 失败记录）。

**修改：**
- `backend/agents/workflow.py` — 3 个 builtin 节点 + `_make_expert_node` 的 4 个调用点：`llm = get_llm_provider(...)` → 包 `LoggedLLMProvider`；`CreativeState` 加可选键 `harness_run_id`/`harness_db`/`harness_agent_name`。
- `backend/api/routes.py` — `initial_state` 构造时注入 `harness_run_id`/`harness_db`（约 2 行）。

**约束（阶段 C 边界）：**
- `llm_provider.py` 零改动（不动 `LLMProvider`/`OpenAIProvider`/`MockProvider`/`get_llm_provider`）。
- 不做 Guardrail 结构化、不替换 MemorySaver、不改前端。
- token/cost 拿不到写 NULL（字段已存在，阶段 A 已建）。
- wrapper 写日志失败不能中断生成（best-effort，记 warning 后继续）。
- 只接入 full_pipeline 的 writer/critic/consistency（含 dynamic expert 替换路径）。

---

## Task 1: LoggedLLMProvider wrapper + 单测

**Files:**
- Create: `backend/harness/llm_call_logger.py`
- Test: `backend/tests/test_harness_llm_call_logger.py`

- [ ] **Step 1: 写失败测试（wrapper 记录 latency/prompt/provider/model，失败也记）**

创建 `backend/tests/test_harness_llm_call_logger.py`：

```python
"""LoggedLLMProvider 单元测试 — SQLite 内存 + MockProvider"""

import asyncio
import time
import uuid

import pytest
import pytest_asyncio
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
from models.llm_call_log import LlmCallLog


@pytest_asyncio.fixture
async def async_db():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sf = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with sf() as session:
        yield session
    await engine.dispose()


class _StubProvider:
    """最小 stub，模拟 LLMProvider.generate（返回固定串，sleep 模拟 latency）。"""
    model = "stub-model"

    async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        await asyncio.sleep(0.05)
        return "stub result"

    async def generate_stream(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        for chunk in ["stub", " ", "result"]:
            yield chunk


class _FailingProvider:
    model = "fail-model"

    async def generate(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        raise RuntimeError("LLM boom")

    async def generate_stream(self, system_prompt, user_prompt, temperature=0.7, max_tokens=4096):
        raise RuntimeError("LLM boom stream")
        yield  # noqa: F821 — 使其为 async generator


@pytest.mark.asyncio
async def test_logged_provider_generate_writes_log(async_db):
    from harness.llm_call_logger import LoggedLLMProvider

    run_id = uuid.uuid4()
    step_id = uuid.uuid4()
    provider = LoggedLLMProvider(
        _StubProvider(),
        db=async_db,
        run_id=run_id,
        step_id=step_id,
        agent_name="writer",
        provider_name="stub",
    )
    result = await provider.generate("sys", "usr", temperature=0.8)
    assert result == "stub result"
    await async_db.commit()

    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert log.run_id == run_id
    assert log.step_id == step_id
    assert log.agent_name == "writer"
    assert log.provider == "stub"
    assert log.model == "stub-model"
    assert log.latency_ms is not None and log.latency_ms >= 40
    assert "sys" in log.rendered_prompt_snapshot
    assert "usr" in log.rendered_prompt_snapshot
    assert log.error_message is None
    assert log.input_tokens is None  # 第一阶段拿不到


@pytest.mark.asyncio
async def test_logged_provider_generate_failure_logs_error(async_db):
    from harness.llm_call_logger import LoggedLLMProvider

    provider = LoggedLLMProvider(
        _FailingProvider(),
        db=async_db,
        run_id=uuid.uuid4(),
        step_id=None,
        agent_name="critic",
        provider_name="stub",
    )
    with pytest.raises(RuntimeError, match="LLM boom"):
        await provider.generate("sys", "usr")
    await async_db.commit()

    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert log.agent_name == "critic"
    assert log.error_message == "LLM boom"
    assert log.latency_ms is not None
    assert log.rendered_prompt_snapshot is not None


@pytest.mark.asyncio
async def test_logged_provider_context_snapshot(async_db):
    from harness.llm_call_logger import LoggedLLMProvider

    provider = LoggedLLMProvider(
        _StubProvider(),
        db=async_db,
        run_id=uuid.uuid4(),
        step_id=None,
        agent_name="consistency_checker",
        provider_name="stub",
        context_snapshot={"context_len": 1234, "stats": {"chars": 100}},
    )
    await provider.generate("sys", "usr")
    await async_db.commit()

    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert log.context_package_snapshot == {"context_len": 1234, "stats": {"chars": 100}}


@pytest.mark.asyncio
async def test_logged_provider_generate_stream_passes_through(async_db):
    from harness.llm_call_logger import LoggedLLMProvider

    provider = LoggedLLMProvider(
        _StubProvider(),
        db=async_db,
        run_id=uuid.uuid4(),
        step_id=None,
        agent_name="writer",
        provider_name="stub",
    )
    chunks = []
    async for c in provider.generate_stream("sys", "usr"):
        chunks.append(c)
    assert chunks == ["stub", " ", "result"]
    await async_db.commit()
    # 流式也记一条日志（latency 测到流结束）
    log = (await async_db.execute(select(LlmCallLog))).scalar_one()
    assert log.agent_name == "writer"
    assert log.latency_ms is not None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd backend && python -m pytest tests/test_harness_llm_call_logger.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'harness.llm_call_logger'`

- [ ] **Step 3: 实现 LoggedLLMProvider**

创建 `backend/harness/llm_call_logger.py`：

```python
"""LoggedLLMProvider — 包装 LLMProvider，每次调用写 llm_call_logs 审计行。

不修改 llm_provider.py 抽象。实现与 LLMProvider 相同的 generate/generate_stream 接口，
内部委托给被包装的真实 provider。best-effort：写日志失败只记 warning，不中断生成。
token/cost 第一阶段拿不到（OpenAIProvider.generate 返回纯 str），写 NULL。
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from models.llm_call_log import LlmCallLog

logger = logging.getLogger(__name__)


def _build_rendered_prompt(system_prompt: str, user_prompt: str) -> str:
    return f"[system]\n{system_prompt}\n\n[user]\n{user_prompt}"


class LoggedLLMProvider:
    """装饰 LLMProvider，记录每次调用的 prompt/latency/provider/model 到 llm_call_logs。

    被包装的 provider 须有 generate(system_prompt, user_prompt, temperature, max_tokens) -> str
    和 generate_stream(...) -> AsyncIterator[str]。provider/model 从被包装对象读取：
    OpenAIProvider 有 self.model；MockProvider 无 model 属性 → 用 provider_name 兜底。
    """

    def __init__(
        self,
        wrapped,
        *,
        db: AsyncSession,
        run_id: str | uuid.UUID | None = None,
        step_id: str | uuid.UUID | None = None,
        agent_name: str | None = None,
        provider_name: str | None = None,
        context_snapshot: dict[str, Any] | None = None,
        model_config_snapshot: dict[str, Any] | None = None,
    ):
        self._wrapped = wrapped
        self._db = db
        self._run_id = run_id
        self._step_id = step_id
        self._agent_name = agent_name
        self._provider_name = provider_name
        self._context_snapshot = context_snapshot
        self._model_config_snapshot = model_config_snapshot

    @property
    def model(self) -> str | None:
        return getattr(self._wrapped, "model", None)

    async def generate(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> str:
        rendered = _build_rendered_prompt(system_prompt, user_prompt)
        started = time.perf_counter()
        try:
            result = await self._wrapped.generate(system_prompt, user_prompt, temperature, max_tokens)
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._write_log(
                rendered_prompt=rendered,
                latency_ms=latency_ms,
                error_message=None,
            )
            return result
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._write_log(
                rendered_prompt=rendered,
                latency_ms=latency_ms,
                error_message=str(exc),
            )
            raise

    async def generate_stream(self, system_prompt: str, user_prompt: str, temperature: float = 0.7, max_tokens: int = 4096) -> AsyncIterator[str]:
        rendered = _build_rendered_prompt(system_prompt, user_prompt)
        started = time.perf_counter()
        try:
            async for chunk in self._wrapped.generate_stream(system_prompt, user_prompt, temperature, max_tokens):
                yield chunk
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._write_log(
                rendered_prompt=rendered,
                latency_ms=latency_ms,
                error_message=None,
            )
        except Exception as exc:
            latency_ms = int((time.perf_counter() - started) * 1000)
            await self._write_log(
                rendered_prompt=rendered,
                latency_ms=latency_ms,
                error_message=str(exc),
            )
            raise

    async def _write_log(self, *, rendered_prompt: str, latency_ms: int, error_message: str | None) -> None:
        try:
            log = LlmCallLog(
                run_id=self._run_id,
                step_id=self._step_id,
                agent_name=self._agent_name,
                provider=self._provider_name,
                model=self.model,
                rendered_prompt_snapshot=rendered_prompt,
                context_package_snapshot=self._context_snapshot,
                model_config_snapshot=self._model_config_snapshot,
                latency_ms=latency_ms,
                input_tokens=None,
                output_tokens=None,
                total_tokens=None,
                cost=None,
                error_message=error_message,
            )
            self._db.add(log)
            # commit 而非仅 flush：节点可能用独立 session（如 context_loader 的
            # ctx_async_session）操作同一 DB 并 commit，未 commit 的 log 行会丢失
            # （Phase B 的 StaleDataError 同源问题）。commit 保证持久化。
            await self._db.commit()
        except Exception:
            logger.warning("harness llm_call_log 写入失败 agent=%s", self._agent_name, exc_info=True)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd backend && python -m pytest tests/test_harness_llm_call_logger.py -v`
Expected: PASS（4 个测试）

- [ ] **Step 5: 提交**

```bash
git add backend/harness/llm_call_logger.py backend/tests/test_harness_llm_call_logger.py
git commit -m "feat(harness): add LoggedLLMProvider wrapper with prompt/latency/context snapshot logging"
```

---

## Task 2: CreativeState 加 harness 注入键

**Files:**
- Modify: `backend/agents/workflow.py:34-58`（CreativeState TypedDict）
- Test: 无（类型声明，由后续集成测试覆盖）

- [ ] **Step 1: 给 CreativeState 加可选 harness 键**

`CreativeState` 是 TypedDict，加三个可选键（用 `total=False` 的子 TypedDict 或直接加键并用 `.get()` 访问）。最简：直接在 TypedDict 末尾加三键，调用方不传时用 `.get()` 取 None。

修改 `backend/agents/workflow.py` 的 `CreativeState`（34-58 行），在 `skill_packs` 之后追加：

```python
    skill_packs: Annotated[list[dict], lambda a, b: a + b]  # 已注入的专家 skill pack 摘要
    # ── Harness 注入（可选，generate 路径写入，节点读取用于 LLM call log）──
    harness_run_id: str  # AiRun.id
    harness_db: object  # AsyncSession（节点内写 llm_call_logs）
    harness_agent_name: str  # 当前节点名，用于 log.agent_name
```

注意：TypedDict 仍可用 `.get()` 访问未提供的键（运行时是 dict）。`harness_db` 类型用 `object` 避免循环 import AsyncSession。

- [ ] **Step 2: 跑 smoke 确认未坏**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: 103 passed（CreativeState 加键不破坏现有调用，因为 `initial_state` 构造时不传这些键也不报错——TypedDict 运行时是普通 dict）

- [ ] **Step 3: 提交**

```bash
git add backend/agents/workflow.py
git commit -m "feat(harness): add optional harness injection keys to CreativeState"
```

---

## Task 3: workflow 节点接入 LoggedLLMProvider

**Files:**
- Modify: `backend/agents/workflow.py`（3 个 builtin 节点 + `_make_expert_node`）
- Test: 由 Task 4 集成测试覆盖

接入策略：每个节点在 `llm = get_llm_provider(state.get("llm_config"))` 后，若 `state.get("harness_db")` 存在，则包一层 `LoggedLLMProvider`。`agent_name` 用节点名/role_type。`context_snapshot` 对 writer/consistency 取 `state.get("context")` 的长度统计。

- [ ] **Step 1: 顶部 import**

在 `backend/agents/workflow.py` 顶部 import 区（`from agents.llm_provider import ...` 附近）加：

```python
from harness.llm_call_logger import LoggedLLMProvider
```

- [ ] **Step 2: writer_node 接入（约 373 行）**

把 `llm = get_llm_provider(state.get("llm_config"))` 改为：

```python
    llm = get_llm_provider(state.get("llm_config"))
    _hdb = state.get("harness_db")
    if _hdb is not None:
        _ctx = state.get("context", "")
        llm = LoggedLLMProvider(
            llm,
            db=_hdb,
            run_id=state.get("harness_run_id"),
            agent_name="writer",
            provider_name=state.get("llm_config", {}).get("provider") if isinstance(state.get("llm_config"), dict) else None,
            context_snapshot={"context_len": len(_ctx)} if _ctx else None,
        )
```

- [ ] **Step 3: critic_node 接入（约 386 行）**

同样模式，`agent_name="critic"`，无 context_snapshot（critic 只读 draft，不读 context）：

```python
    llm = get_llm_provider(state.get("llm_config"))
    _hdb = state.get("harness_db")
    if _hdb is not None:
        llm = LoggedLLMProvider(
            llm,
            db=_hdb,
            run_id=state.get("harness_run_id"),
            agent_name="critic",
            provider_name=state.get("llm_config", {}).get("provider") if isinstance(state.get("llm_config"), dict) else None,
        )
```

- [ ] **Step 4: consistency_checker_node 接入（约 399 行）**

`agent_name="consistency_checker"`，带 context_snapshot（它读 `state["context"]`）：

```python
    llm = get_llm_provider(state.get("llm_config"))
    _hdb = state.get("harness_db")
    if _hdb is not None:
        _ctx = state.get("context", "")
        llm = LoggedLLMProvider(
            llm,
            db=_hdb,
            run_id=state.get("harness_run_id"),
            agent_name="consistency_checker",
            provider_name=state.get("llm_config", {}).get("provider") if isinstance(state.get("llm_config"), dict) else None,
            context_snapshot={"context_len": len(_ctx)} if _ctx else None,
        )
```

- [ ] **Step 5: _make_expert_node 接入（约 170 行）**

dynamic expert 的 provider 获取在闭包内（170 行 `llm = get_llm_provider(state.get("llm_config"))`）。包一层，`agent_name` 用 `expert_id[:8]`（节点已改名 `expert_{expert_id[:8]}`），`role_type` 可从外层闭包变量取。改 170 行：

```python
        llm = get_llm_provider(state.get("llm_config"))
        _hdb = state.get("harness_db")
        if _hdb is not None:
            _ctx = state.get("context", "")
            llm = LoggedLLMProvider(
                llm,
                db=_hdb,
                run_id=state.get("harness_run_id"),
                agent_name=f"expert_{expert_id[:8]}",
                provider_name=state.get("llm_config", {}).get("provider") if isinstance(state.get("llm_config"), dict) else None,
                context_snapshot={"context_len": len(_ctx)} if _ctx and role_type in ("writer", "researcher", "custom") else None,
            )
```

- [ ] **Step 6: 语法检查**

Run: `cd backend && python -c "import ast; ast.parse(open('agents/workflow.py').read()); print('workflow.py syntax OK')"`
Expected: syntax OK

- [ ] **Step 7: 跑 smoke 确认未坏（harness_db 未注入时走原路径）**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: 103 passed（smoke 不传 harness_db，节点走原 `llm`，LoggedLLMProvider 不生效）

- [ ] **Step 8: 提交**

```bash
git add backend/agents/workflow.py
git commit -m "feat(harness): wrap writer/critic/consistency/dynamic-expert LLM calls with LoggedLLMProvider"
```

---

## Task 4: routes.py 注入 harness 键 + step_id 关联

**Files:**
- Modify: `backend/api/routes.py`（initial_state 构造，约 3127-3152 行；step 钩子处补 step_id）

`LoggedLLMProvider` 需要 `run_id`/`db`/`agent_name`，这些通过 `CreativeState` 注入。`step_id` 当前在 routes 的 `active_steps` 字典里，但节点拿不到——需要把当前 step_id 也塞进 state。最简：在 `on_chain_start` 写 step 后，把 `step.id` 存入一个可在节点读到的位置。但节点在 step start 之后才执行，所以可在 `on_chain_start` 的 step 创建后更新 state。

更简的方案：`step_id` 不强求精确关联到节点（第一版），`LoggedLLMProvider` 的 `step_id` 先传 None，run_id 关联即可。后续若要精确 step 关联，再通过 `app.aupdate_state` 注入。**第一版 step_id 传 None，run_id 精确**——满足"run_id/step_id 落关联"的最低要求（run_id 一定有，step_id 可选）。

- [ ] **Step 1: initial_state 注入 harness_run_id / harness_db**

在 `backend/api/routes.py` 的 full_pipeline `initial_state` 构造（约 3127-3152 行的 dict literal）末尾，加两键：

```python
        "harness_run_id": run_id_str,
        "harness_db": db,
```

（`run_id_str` 在 Task 5 已创建；`db` 是请求的 AsyncSession，闭包内可达。）

- [ ] **Step 2: 跑集成测试确认 llm_call_logs 有记录**

Run: `cd backend && python -m pytest tests/test_harness_generate_integration.py -v`
Expected: 集成测试通过（run+step 仍正常）

- [ ] **Step 3: 扩展集成测试，断言 llm_call_logs 有 writer/critic/consistency 记录**

在 `backend/tests/test_harness_generate_integration.py` 的 `_query` 函数内，追加 llm_call_logs 断言：

```python
        from models.llm_call_log import LlmCallLog
        logs = (await s.execute(
            select(LlmCallLog).where(LlmCallLog.run_id == run.id)
        )).scalars().all()
        assert len(logs) >= 1
        log_agents = {l.agent_name for l in logs}
        # full_pipeline 至少有 writer（mock provider 下也会记）
        assert "writer" in log_agents
        for l in logs:
            assert l.latency_ms is not None
            assert l.rendered_prompt_snapshot is not None
            assert l.run_id == run.id
```

- [ ] **Step 4: 跑扩展后的集成测试**

Run: `cd backend && python -m pytest tests/test_harness_generate_integration.py -v`
Expected: PASS（llm_call_logs 有 writer 记录，run_id 关联，latency/prompt snapshot 非空）

- [ ] **Step 5: 跑 smoke 回归**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: 103 passed

- [ ] **Step 6: 提交**

```bash
git add backend/api/routes.py backend/tests/test_harness_generate_integration.py
git commit -m "feat(harness): inject harness_run_id/db into CreativeState; assert llm_call_logs in integration test"
```

---

## Task 5: 阶段 C 验收

- [ ] **Step 1: 跑全部 harness 测试**

Run: `cd backend && python -m pytest tests/test_harness_llm_call_logger.py tests/test_harness_run_manager.py tests/test_harness_models.py tests/test_harness_generate_integration.py -v`
Expected: 全部 PASS

- [ ] **Step 2: 跑 smoke 回归**

Run: `cd backend && python -m pytest tests/test_smoke.py -q`
Expected: 103 passed

- [ ] **Step 3: 确认 llm_provider.py 零改动**

Run: `cd /Users/zcx/ai-creative-platform && git diff backend/agents/llm_provider.py`
Expected: 空输出（未改动）

- [ ] **Step 4: 确认前端未改动**

Run: `cd /Users/zcx/ai-creative-platform && git diff frontend/`
Expected: 仅 Phase B 的 types.ts 一行（无新增前端改动）

- [ ] **Step 5: 在项目 venv (3.10) 下 import 检查**

Run: `cd backend && /Users/zcx/ai-creative-platform/backend/venv/bin/python -c "import agents.workflow, harness.llm_call_logger; print('imports OK under 3.10')"`
Expected: imports OK

---

## 验收清单（对应阶段 C 边界）

1. 新增 `llm_call_logger.py` ✅ (Task 1)
2. 只接入 full_pipeline 的 writer/critic/consistency ✅ (Task 3，含 dynamic expert 替换路径)
3. 写入 run_id/step_id/agent_name/provider/model/latency_ms ✅ (Task 1 + Task 4；step_id 第一版 None，run_id 精确)
4. 保存 rendered_prompt_snapshot ✅ (Task 1)
5. 保存 context_package_snapshot（初版：context_len stats）✅ (Task 3 writer/consistency)
6. token/cost 拿不到写 NULL ✅ (Task 1，input_tokens/output_tokens/total_tokens/cost 全 None)
7. 不做 Guardrail/MemorySaver/前端 UI ✅ (全程约束)
8. llm_provider.py 零改动 ✅ (Task 5 Step 3)
9. 不大改 routes.py（只加 2 行 state 注入）✅ (Task 4)
