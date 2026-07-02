# AI Harness 改造实施总结

> 面向 Claude / Codex 的上下文文档：当前已完成的阶段、架构决策、代码结构、已知限制、后续计划。
> 分支：`novel-extraction-mvp`（未提交，全部改动在工作区）。
> 基线文档：`docs/AI_Harness_改造适配技术文档.md`

---

## 1. 总体目标

把 `ai-creative-platform` 从"AI 创作功能平台"升级为"小说创作 AI Harness"：AI 每一步执行都有记录、可复盘、可恢复、可审计。

核心链路：

```
Run → Step → LLM Call → Prompt/Context Snapshot
```

用户写作体验不变，但生成任务的完整过程在数据库留下可查询轨迹。

---

## 2. 已完成阶段

### 阶段 A：Harness Core 数据层 ✅

**目标**：新增 4 张核心表 + 2 张老表加列 + 1 个 Alembic 迁移，不接业务流程。

**产出**：

| 文件 | 作用 |
|---|---|
| `backend/models/harness_enums.py` | 4 个 `(str, Enum)` 状态枚举（RunStatus / RunStepStatus / InterruptStatus / InterruptDecision） |
| `backend/models/ai_run.py` | `AiRun` 模型（表 `ai_runs`） |
| `backend/models/ai_run_step.py` | `AiRunStep` 模型（表 `ai_run_steps`，含部分唯一索引 `idempotency_key`） |
| `backend/models/llm_call_log.py` | `LlmCallLog` 模型（表 `llm_call_logs`，不可变，只有 `created_at`） |
| `backend/models/human_interrupt.py` | `HumanInterrupt` 模型（表 `human_interrupts`，持久化 HITL） |
| `backend/alembic/versions/021_ai_harness_core.py` | 迁移：建 4 张新表 + `generation_records.run_id` + `chapter_versions` 5 列 |
| `backend/models/generation_record.py` | 加 `run_id` 列 |
| `backend/models/chapter_version.py` | 加 `project_id / run_id / parent_version_id / rollback_from_version_id / diff_from_parent` |
| `backend/models/__init__.py` | 注册 4 模型 + 4 枚举 |
| `backend/services/version_service.py` | `VALID_SOURCES` 加 `ai_draft / rollback / import` |
| `backend/tests/test_harness_models.py` | 14 测试 |

**验收**：smoke 103 + models 14 = 117 passed；迁移在 Postgres 三轮（upgrade→downgrade→re-upgrade）OK；alembic head = `021_ai_harness_core`。

**关键决策**：
- 枚举用 `(str, Enum)` 而非 `StrEnum`——项目 venv 是 Python 3.10，没有 `StrEnum`（3.11 才有）。这是执行中发现的兼容性 bug。
- 迁移用 `012_novel_extraction.py` 的可移植辅助模式（`_uuid_type() / _json_type() / _has_table()`），兼容 PostgreSQL / SQLite。
- `idempotency_key` 部分唯一索引用 `sqlite_where` / `postgresql_where` 双方言声明，SQLite 现代版支持部分索引。

---

### 阶段 B：Run Manager 接入 generate ✅

**目标**：一次现有 `full_pipeline` 生成流程在数据库留下可查询的 `ai_runs` + `ai_run_steps` 轨迹，`generation_records.run_id` 落关联，失败/断开/WAITING_HUMAN 更新 run 状态，旧前端体验不坏。

**产出**：

| 文件 | 作用 |
|---|---|
| `backend/harness/__init__.py` | 包初始化 |
| `backend/harness/run_manager.py` | Run 生命周期：`create_run / mark_running / mark_waiting_human / mark_completed / mark_failed / mark_cancelled` |
| `backend/harness/step_logger.py` | Step 记录：`start_step`（幂等）/ `finish_step / fail_step / wait_human_step` |
| `backend/api/routes.py` | `generate_chapter` 的 `full_pipeline` 分支接入 5 处钩子 |
| `backend/services/generation_record_service.py` | `create_generation_record` 加 `run_id` 参数 |
| `frontend/src/api/types.ts` | `SSEEventType` union 加 `run_created / run_status / run_step` |
| `backend/tests/test_harness_run_manager.py` | 6 测试 |
| `backend/tests/test_harness_generate_integration.py` | 1 集成测试 |

**接入点**（`routes.py` 的 `astream_events` 循环）：
1. 生成开始：`create_run` + 发 `run_created` SSE + `mark_running`
2. `on_chain_start`：`start_step`（幂等键 `run_id:step_name:revision_round`）+ 发 `run_step` RUNNING
3. `on_chain_end`：`finish_step` + 发 `run_step` SUCCESS
4. HITL 暂停：`mark_waiting_human` + 发 `run_status` WAITING_HUMAN
5. 异常/断开/完成：`mark_failed / mark_cancelled / mark_completed`

**关键决策**：
- **step 钩子在 `astream_events` 循环，不进 `workflow.py` 节点函数**——天然覆盖动态 expert 节点，`workflow.py` 零改动。
- **`STEP_NODE_MAP`** 映射：`context_loader→build_context(1)`、`writer→generate_draft(2)`、`critic→critique(3)`、`consistency_checker→consistency_check(3)`、`human_review→human_review(4)`。critic 与 consistency 同 `step_order=3` 反映并行。
- **`on_chain_start` 去重**：LangGraph 的 `astream_events` 对同一节点触发多次 `on_chain_start`（链 + 节点），用 `node_name not in active_steps` 守卫避免重复 INSERT step。
- **`start_step` 后用 `db.commit()` 而非 `db.flush()`**：节点可能用独立 session（`ctx_async_session`）操作同一 DB 并 commit，未 commit 的 step 行在 `finish_step` 的 UPDATE 时不可见（`StaleDataError`）。

**验收**：harness 7 + smoke 103 passed；旧 SSE 事件不受影响。

---

### 阶段 C：LLM Call Log 与 Prompt/Context Snapshot ✅

**目标**：`full_pipeline` 的 writer / critic / consistency_checker 三类 LLM 调用每次都在本地 `llm_call_logs` 表留下审计记录。

**产出**：

| 文件 | 作用 |
|---|---|
| `backend/harness/llm_call_logger.py` | `LoggedLLMProvider` 装饰器 + `MAX_PROMPT_SNAPSHOT_CHARS=8000` 截断保护 |
| `backend/agents/workflow.py` | `_maybe_wrap_llm` helper + 4 处 `llm =` 包一层 + `CreativeState` 加 `harness_run_id / harness_step_id` |
| `backend/api/routes.py` | `initial_state` 注入 `harness_run_id` |
| `backend/tests/test_harness_llm_call_logger.py` | 6 测试 |

**关键决策**：
- **`llm_provider.py` 零改动**——不改 `LLMProvider` / `OpenAIProvider` / `MockProvider` / `get_llm_provider`。
- **`LoggedLLMProvider` 用独立 session 写日志**：从 `db.session.get_engine()` 取全局 engine，新开 `async_sessionmaker` 写入 + commit。不把 `AsyncSession` 放进 graph state（会导致 `MemorySaver` msgpack 序列化失败）。
- **`step_id` 靠运行中 step 查询解析**：`_resolve_running_step_id()` 按 `run_id` + `agent_name→step_name` 映射查最新 RUNNING step。**不依赖 graph state 注入**（`aupdate_state` 在并行/动态节点下有竞态，已移除）。
- **token/cost 第一版写 NULL**：`OpenAIProvider.generate` 返回纯 str，不暴露 `resp.usage`。
- **prompt snapshot 截断**：`MAX_PROMPT_SNAPSHOT_CHARS = 8000`，超长截断并在 `request` JSON 记 `{"prompt_snapshot_truncated": true}`。

**验收**：harness 13 + smoke 103 passed；`llm_provider.py` git diff 空；Postgres probe 确认 step_id 精确关联。

---

### 阶段 D：Run API 与最小前端露出 ✅

**目标**：后端新增 3 个 Run 查询 API，前端最小知道 `run_id`。

**产出**：

| 文件 | 作用 |
|---|---|
| `backend/schemas/api.py` | `AiRunResponse` / `AiRunListItemResponse` / `AiRunStepResponse`（不含 input/output 大字段） |
| `backend/api/routes.py` | 3 个路由集中在 `# ==================== AI Runs ====================` 段落 |
| `frontend/src/api/types.ts` | `ApiRun` / `ApiRunStep` / `ApiRunListItem` / `RunCreatedPayload` |
| `frontend/src/api/client.ts` | `getRun` / `getRunSteps` / `listProjectRuns` |
| `frontend/src/components/AgentPanel.vue` | `latestRunId` ref + `run_created` case + 3 处重置 |
| `backend/tests/test_harness_run_api.py` | 9 测试 |

**3 个 API**：
- `GET /api/ai-runs/{run_id}` — run 详情，校验 run 的 project 属于当前用户
- `GET /api/ai-runs/{run_id}/steps` — steps 列表，按 `step_order, started_at nulls_last, created_at` 三级排序，每步附 `llm_call_count`
- `GET /api/projects/{project_id}/ai-runs?limit=N` — run 列表，按 `created_at` 降序，默认 limit 20

**关键决策**：
- **权限**：3 个路由都校验 run 的 project 属于当前用户（跨用户返回 404，不泄露存在性）。
- **steps 不暴露大字段**：`AiRunStepResponse` 只有摘要字段，不含 `input/output/input_hash/output_hash`。
- **排序稳定**：`step_order` 相同时用 `started_at`（nulls last），再兜底 `created_at`，避免并行 step 列表随机抖动。

**验收**：harness 36 + smoke 103 passed；前端 vue-tsc exit 0；py3.10 import OK。

---

### 阶段 E：Human Review 持久化 ✅

**目标**：`WAITING_HUMAN` 状态落库到 `human_interrupts`，新增 `POST /api/ai-runs/{run_id}/human-decisions` API，重复 approve 幂等，approve/reject 自动更新 run 状态。

**产出**：

| 文件 | 作用 |
|---|---|
| `backend/harness/human_interrupt_service.py` | Interrupt 服务层：`create_interrupt` / `resolve_interrupt`（幂等）/ `get_interrupt_by_run` / `get_interrupt_by_thread` / `check_interrupt_resolved` |
| `backend/api/routes.py` | 两处 `WAITING_HUMAN` 自动创建 interrupt + 新增 human-decisions API + resume 幂等检查 + approve 自动解析 interrupt |
| `backend/schemas/api.py` | `HumanDecisionRequest` / `HumanDecisionResponse` |
| `backend/tests/test_harness_human_interrupt.py` | 7 测试 |

**接入点**：
1. **generate 的两处 WAITING_HUMAN**（循环内 `human_review` 分支 + 循环后 `next_nodes` 检查）：`mark_waiting_human` 后调 `create_interrupt`，payload 含 `generation_record_id` 和 `content_hash`。
2. **新 API `POST /api/ai-runs/{run_id}/human-decisions`**：接收 `{decision, feedback}`，校验 run 归属 → 查 interrupt → 幂等检查（已 resolved 直接返回 `already_resolved`）→ `resolve_interrupt` → 按决策更新 run 状态（APPROVE→COMPLETED, REJECT→FAILED, EDIT/REGENERATE→保持 WAITING_HUMAN）。
3. **resume 路径幂等**：approve 前检查 `get_interrupt_by_thread`，若已 resolved 则返回错误、不重复创建版本。
4. **resume 的 approve 自动解析**：approve 执行后调 `resolve_interrupt(APPROVE)`，保证 resume 路径也标记 interrupt。

**关键决策**：
- **幂等双重保护**：human-decisions API 检查 `interrupt.resolved` + resume 路径检查 `interrupt.resolved`，两条路径都防重复 approve。
- **决策→状态映射**：APPROVE→`mark_completed`，REJECT→`mark_failed`，EDIT/REGENERATE→不改变 run 状态（等待后续操作）。
- **interrupt 创建用请求 session**（非独立 session）——因为创建发生在 SSE 生成流中，紧跟 `mark_waiting_human`，此时请求 session 处于可控状态（不像 LLM call log 在节点执行期间写）。

**验收**：harness 43 + smoke 103 passed；py3.10 import OK；前端 vue-tsc exit 0。

---

## 3. 当前测试状态

| 测试套件 | 单独跑 | 说明 |
|---|---|---|
| `test_smoke.py` | 103 passed | 老功能回归 |
| `test_harness_models.py` | 14 passed | 阶段 A 模型层 |
| `test_harness_run_manager.py` | 6 passed | 阶段 B run/step 服务 |
| `test_harness_generate_integration.py` | 1 passed | 阶段 B+C 集成（run+step+llm_call_log） |
| `test_harness_llm_call_logger.py` | 6 passed | 阶段 C wrapper |
| `test_harness_run_api.py` | 9 passed | 阶段 D API |
| `test_harness_human_interrupt.py` | 7 passed | 阶段 E human interrupt |
| **合计** | **146 passed** | |

**已知限制**：harness 测试与 `test_smoke.py` 共享 SQLite `:memory:` 引擎，**单独跑各自全绿，合跑有 `:memory:` 连接隔离的表可见性问题**（非代码缺陷）。生产用 Postgres 不受影响。

---

## 4. 架构全景

```
backend/
├── models/
│   ├── harness_enums.py      # RunStatus / RunStepStatus / InterruptStatus / InterruptDecision
│   ├── ai_run.py             # AiRun（任务生命周期）
│   ├── ai_run_step.py        # AiRunStep（步骤记录，幂等键）
│   ├── llm_call_log.py       # LlmCallLog（LLM 调用审计，不可变）
│   ├── human_interrupt.py    # HumanInterrupt（持久化 HITL）
│   ├── generation_record.py  # +run_id
│   └── chapter_version.py    # +project_id/run_id/parent/rollback/diff
├── harness/
│   ├── run_manager.py        # create_run / mark_running / mark_waiting_human / mark_completed / mark_failed / mark_cancelled
│   ├── step_logger.py        # start_step（幂等）/ finish_step / fail_step / wait_human_step
│   ├── llm_call_logger.py    # LoggedLLMProvider（装饰器，独立 session 写日志，prompt 截断）
│   └── human_interrupt_service.py  # create_interrupt / resolve_interrupt（幂等）/ get_by_run / get_by_thread / check_resolved
├── agents/
│   └── workflow.py           # _maybe_wrap_llm（4 处接入）+ CreativeState 加 harness 键
├── api/
│   └── routes.py             # generate_chapter 5 处钩子 + WAITING_HUMAN 创建 interrupt + 3 个 Run 查询 API + human-decisions API + resume 幂等
├── schemas/
│   └── api.py                # AiRunResponse / AiRunListItemResponse / AiRunStepResponse / HumanDecisionRequest / HumanDecisionResponse
├── alembic/versions/
│   └── 021_ai_harness_core.py
└── services/
    ├── generation_record_service.py  # +run_id 参数
    └── version_service.py            # VALID_SOURCES +ai_draft/rollback/import

frontend/
├── src/api/
│   ├── types.ts              # SSEEventType +run_created/run_status/run_step + ApiRun/ApiRunStep
│   └── client.ts             # getRun / getRunSteps / listProjectRuns
└── src/components/
    └── AgentPanel.vue        # latestRunId ref + run_created case
```

---

## 5. 关键技术约束（勿违背）

1. **`llm_provider.py` 零改动**——用 `LoggedLLMProvider` 装饰器，不改抽象层。
2. **`llm_call_logs.step_id` 靠运行中 step 查询解析**——`_resolve_running_step_id()` 按 `run_id` + `agent_name→step_name` 查最新 RUNNING step。**不要**从 graph state 读 `harness_step_id`（并行/动态节点下有竞态，已移除 `aupdate_state` 注入路径）。
3. **`LoggedLLMProvider` 用独立 session 写日志**——从 `get_engine()` 新开 `async_sessionmaker`，不把 `AsyncSession` 放进 graph state（`MemorySaver` msgpack 序列化失败）。
4. **`start_step` 后用 `db.commit()` 而非 `flush()`**——节点用独立 session commit 时，未 commit 的行会丢（`StaleDataError`）。
5. **不删旧 SSE 事件**——新增事件不能破坏旧前端（`SSEEventType` 是闭集 union，加事件要同步扩）。
6. **`workflow.py` 不改节点函数**——step 钩子在 `astream_events` 循环，天然覆盖动态 expert。
7. **枚举用 `(str, Enum)`**——兼容 Python 3.10（项目 venv），不用 `StrEnum`。

---

## 6. 后续计划（阶段 F-H，未开始）

按 `docs/AI_Harness_改造适配技术文档.md` 的阶段拆分：

| 阶段 | 目标 | 关键产出 | 状态 |
|---|---|---|---|
| **E** | Human Review 持久化 | `human_interrupts` 写入 + `WAITING_HUMAN` 落库 + `POST /api/ai-runs/{run_id}/human-decisions` + 重复 approve 幂等 | ✅ 已完成 |
| **F** | 版本审计增强 | `save_chapter_content` 支持 `run_id` + `create_version` 支持 `parent_version_id` + 取消 `MAX_VERSIONS_PER_CHAPTER` 自动删除 + rollback 创建新版本 | ⏳ 待开始 |
| **G** | 结构化 Guardrail | `GuardrailResult` schema + ConsistencyAgent prompt 改 JSON + HIGH severity 阻止自动提交 | ⏳ 待开始 |
| **H** | 写作记忆 staging | `writing_memory_staging` 表 + 章节确认后触发 FactExtractionAgent + confirmed 入正式表 | ⏳ 待开始 |

**暂不做**：MemorySaver 替换、token/cost 聚合、Prompt snapshot 展示、MCP/tool runtime、完整 AiRunPanel.vue。

---

## 7. 改动文件清单

**新增（14 个文件）**：
- `backend/models/harness_enums.py`
- `backend/models/ai_run.py`
- `backend/models/ai_run_step.py`
- `backend/models/llm_call_log.py`
- `backend/models/human_interrupt.py`
- `backend/harness/__init__.py`
- `backend/harness/run_manager.py`
- `backend/harness/step_logger.py`
- `backend/harness/llm_call_logger.py`
- `backend/harness/human_interrupt_service.py`
- `backend/alembic/versions/021_ai_harness_core.py`
- `backend/tests/test_harness_models.py`
- `backend/tests/test_harness_run_manager.py`
- `backend/tests/test_harness_generate_integration.py`
- `backend/tests/test_harness_llm_call_logger.py`
- `backend/tests/test_harness_run_api.py`
- `backend/tests/test_harness_human_interrupt.py`

**修改（11 个文件）**：
- `backend/models/__init__.py` — 注册新模型 + 枚举
- `backend/models/generation_record.py` — +`run_id`
- `backend/models/chapter_version.py` — +5 列
- `backend/services/generation_record_service.py` — +`run_id` 参数
- `backend/services/version_service.py` — `VALID_SOURCES` +3 值
- `backend/agents/workflow.py` — `_maybe_wrap_llm` + `CreativeState` 键
- `backend/api/routes.py` — 5 处钩子 + WAITING_HUMAN interrupt + 4 个 API + resume 幂等 + schema import
- `backend/schemas/api.py` — 3 个 Run response schema + 2 个 HumanDecision schema
- `frontend/src/api/types.ts` — `SSEEventType` + `ApiRun`/`ApiRunStep`/`RunCreatedPayload`
- `frontend/src/api/client.ts` — 3 个 client 方法
- `frontend/src/components/AgentPanel.vue` — `latestRunId` + `run_created` case

**总计**：+505 行改动（tracked），17 个新文件，146 passed（单独跑）。
