# AI Harness 改造完成总结：A-H 全链路落地

> 面向 Claude / Codex 的上下文文档：全部阶段已完成，架构决策、代码结构、已知限制。
> 分支：`novel-extraction-mvp`，全部已提交。
> 基线文档：`docs/AI_Harness_改造适配技术文档.md`

---

## 1. 总体目标

把 `ai-creative-platform` 从"AI 创作功能平台"升级为"小说创作 AI Harness"：AI 每一步执行都有记录、可复盘、可恢复、可审计。

完整链路：

```
Run → Step → LLM Call → Prompt/Context Snapshot
  → GuardrailResult（结构化一致性检查）
  → HumanInterrupt（持久化审核 + blocking_issues）
  → ChapterVersion（run_id / accepted_version_id / rollback）
  → FactExtractionAgent → writing_memory_staging（GENERATED）
  → 用户确认 → workbench 正式表（Character / WorldEntry / CharacterEvent / HiddenThread）
  → Context Builder ## 已确认记忆
```

用户写作体验不变，但生成任务的完整过程在数据库留下可查询轨迹，记忆不污染正式设定库。

---

## 2. 已完成阶段

### 阶段 A：Harness Core 数据层 ✅

新增 4 张核心表 + 2 张老表加列 + migration 021。

| 文件 | 作用 |
|---|---|
| `backend/models/harness_enums.py` | 6 个 `(str, Enum)` 枚举（RunStatus / RunStepStatus / InterruptStatus / InterruptDecision / MemoryStagingStatus / MemoryType） |
| `backend/models/ai_run.py` | `AiRun`（表 `ai_runs`） |
| `backend/models/ai_run_step.py` | `AiRunStep`（表 `ai_run_steps`，部分唯一索引 `idempotency_key`） |
| `backend/models/llm_call_log.py` | `LlmCallLog`（表 `llm_call_logs`，不可变） |
| `backend/models/human_interrupt.py` | `HumanInterrupt`（表 `human_interrupts`） |
| `backend/alembic/versions/021_ai_harness_core.py` | 建 4 表 + `generation_records.run_id` + `chapter_versions` 5 列 |

**关键决策**：枚举用 `(str, Enum)` 兼容 Python 3.10（无 `StrEnum`）；迁移用可移植辅助模式兼容 PG/SQLite。

---

### 阶段 B：Run Manager 接入 generate ✅

`full_pipeline` 生成在数据库留下 `ai_runs` + `ai_run_steps` 轨迹，`generation_records.run_id` 落关联。

| 文件 | 作用 |
|---|---|
| `backend/harness/run_manager.py` | Run 生命周期：create_run / mark_running / mark_waiting_human / mark_completed / mark_failed / mark_cancelled |
| `backend/harness/step_logger.py` | start_step（幂等）/ finish_step / fail_step / wait_human_step |
| `backend/api/routes.py` | `generate_chapter` 的 `full_pipeline` 分支 5 处钩子 |

**关键决策**：step 钩子在 `astream_events` 循环不进节点函数（`workflow.py` 零改动）；`start_step` 后用 `commit()` 而非 `flush()`（避免 StaleDataError）。

---

### 阶段 C：LLM Call Log 与 Prompt/Context Snapshot ✅

writer / critic / consistency_checker 每次调用都在 `llm_call_logs` 留审计记录。

| 文件 | 作用 |
|---|---|
| `backend/harness/llm_call_logger.py` | `LoggedLLMProvider` 装饰器 + `MAX_PROMPT_SNAPSHOT_CHARS=8000` 截断 |
| `backend/agents/workflow.py` | `_maybe_wrap_llm` helper + 4 处接入 + `CreativeState` 加 harness 键 |

**关键决策**：`llm_provider.py` 零改动；独立 session 写日志（`get_engine()` + `async_sessionmaker`）；`step_id` 靠运行中 step 查询解析（不依赖 graph state 注入）。

---

### 阶段 D：Run API 与最小前端露出 ✅

3 个 Run 查询 API + 前端 `run_created` 事件。

| 文件 | 作用 |
|---|---|
| `backend/schemas/api.py` | AiRunResponse / AiRunListItemResponse / AiRunStepResponse |
| `backend/api/routes.py` | GET /ai-runs/{id} + GET /ai-runs/{id}/steps + GET /projects/{id}/ai-runs |
| `frontend/src/api/types.ts` | ApiRun / ApiRunStep / RunCreatedPayload |
| `frontend/src/api/client.ts` | getRun / getRunSteps / listProjectRuns |
| `frontend/src/components/AgentPanel.vue` | latestRunId ref + run_created case |

---

### 阶段 E：Human Review 持久化 ✅

`WAITING_HUMAN` 落库到 `human_interrupts`，`POST /api/ai-runs/{run_id}/human-decisions` API，重复 approve 幂等。

| 文件 | 作用 |
|---|---|
| `backend/harness/human_interrupt_service.py` | create_interrupt / resolve_interrupt（幂等）/ get_by_run / get_by_thread / check_resolved |
| `backend/api/routes.py` | 两处 WAITING_HUMAN 创建 interrupt + human-decisions API + resume 幂等 |

---

### 阶段 F：版本审计增强 ✅

`create_version` / `save_chapter_content` 支持 audit 参数；approve 路径串联 `run_id` + `accepted_version_id`；新增 chapter rollback endpoint。

| 文件 | 作用 |
|---|---|
| `backend/services/version_service.py` | `create_version` 加 run_id / parent_version_id / rollback_from_version_id / project_id；`_prune_old_versions` 跳过被引用版本 |
| `backend/services/chapter_save.py` | `save_chapter_content` 加 run_id / parent_version_id / rollback_from_version_id |
| `backend/api/routes.py` | approve 路径传 run_id + accepted_version_id 联动 + POST .../versions/{vid}/restore 回滚 endpoint |
| `backend/schemas/api.py` | ChapterVersionListItemResponse / ChapterVersionResponse 加 audit 字段 |

**关键决策**：rollback 创建新版本（source=rollback）不覆盖旧版本；prune 不删被 `GenerationRecord.accepted_version_id` 引用的版本。

---

### 阶段 G：结构化 Guardrail ✅

ConsistencyAgent 从纯文本升级为结构化 `GuardrailResult` JSON，HIGH severity 在 interrupt payload 标记 blocking_issues，前端结构化渲染。

| 文件 | 作用 |
|---|---|
| `backend/agents/guardrail.py` | parse_guardrail_result（容错解析）/ guardrail_to_text / has_blocking_issues / get_blocking_issues |
| `backend/agents/workflow.py` | DEFAULT_CONSISTENCY_PROMPT 加 JSON 指令 + consistency_checker_node 返回 dict + CreativeState.consistency_report 改 dict |
| `backend/api/routes.py` | 2 处 SSE 发 report+guardrail_result + harness snapshot 改 issue_count + review-prompt 用 guardrail_to_text + 2 处 interrupt payload 加 blocking_issues |
| `frontend/src/api/types.ts` | GuardrailIssue / GuardrailResult / ConsistencyCheckPayload 加 guardrail_result |
| `frontend/src/components/AgentPanel.vue` | consistency_check handler 结构化渲染 + severity 映射 |

**关键决策**：SSE 向后兼容（同时发 report + guardrail_result）；HIGH 阻断不改图拓扑（`interrupt_before` 不变，只在 interrupt payload 标记）；parse 失败容错不崩溃。

---

### 阶段 H1：Memory Staging 数据层 + API ✅

新建 `writing_memory_staging` 表 + migration 022 + 3 个 staging API。

| 文件 | 作用 |
|---|---|
| `backend/models/writing_memory_staging.py` | WritingMemoryStaging 模型（含 chapter_sequence_number / confirmed_target_type/id / reviewed_by / idempotency_key） |
| `backend/models/harness_enums.py` | MemoryStagingStatus（GENERATED/CONFIRMED/REJECTED）+ MemoryType（CHARACTER/WORLD_RULE/PLOT_FACT/EVENT/FORESHADOWING） |
| `backend/alembic/versions/022_writing_memory_staging.py` | 建表 + 6 索引 + partial unique index on idempotency_key |
| `backend/api/routes.py` | GET list + POST confirm + POST reject（幂等 + 409 跨状态规则） |
| `backend/schemas/api.py` | WritingMemoryStagingResponse |

**关键决策**：不复用 `extraction_staging`（那是资料源抽取）；`payload` 用 `default=dict`（非可变 `{}`）；confirm/reject 跨状态规则（CONFIRMED→REJECT 409, REJECTED→CONFIRM 409）。

---

### 阶段 H2：章节确认后抽取进入 staging ✅

approve 后 `asyncio.create_task` 后台调 FactExtractionAgent，抽取结果写 staging。失败不影响 approve。

| 文件 | 作用 |
|---|---|
| `backend/agents/fact_extraction.py` | FACT_EXTRACTION_PROMPT + parse_facts（容错解析）+ extract_facts |
| `backend/services/memory_staging_service.py` | create_staging_from_extraction（幂等：同 chapter_version_id 已有记录则 skip）+ run_fact_extraction（后台入口，独立 session，失败写 ai_run_step extract_memory FAILED） |
| `backend/api/routes.py` | approve 路径加 asyncio.create_task（SQLite 测试环境跳过） |

**关键决策**：用 `_gr.accepted_version_id` 精确绑定版本（不查"最新版本"）；幂等查 ANY status（不限 GENERATED）；`extract_memory` step 固定幂等键 `f"{run_id}:extract_memory:{chapter_version_id}"`；失败写 step FAILED 但 run 保持 COMPLETED；顶层 try/except 兜底环境错误。

---

### 阶段 H3：确认后入正式上下文 + 前端 UI ✅

confirm 写入 workbench 正式表 + Context Builder 注入 confirmed 记忆 + 前端 staging 审核 panel。

| 文件 | 作用 |
|---|---|
| `backend/services/memory_staging_service.py` | confirm_staging_item 按 memory_type 写正式表（含 _confirm_character / _confirm_world_entry / _confirm_event / _confirm_hidden_thread） |
| `backend/services/chapter_context.py` | ConfirmedMemoryInfo + ChapterContext.confirmed_memories + _load_confirmed_memories + ## 已确认记忆 prompt section |
| `backend/api/routes.py` | confirm_memory_staging route 调 confirm_staging_item + background_tasks |
| `frontend/src/api/types.ts` | ApiWritingMemoryStaging + MemoryStagingStatus + MemoryType |
| `frontend/src/api/client.ts` | listMemoryStaging / confirmMemoryStaging / rejectMemoryStaging |
| `frontend/src/components/MemoryStagingPanel.vue` | 列表 + 确认/拒绝按钮 + 刷新 |
| `frontend/src/components/AgentPanel.vue` | 渲染 MemoryStagingPanel |

**confirm 映射规则**：
- CHARACTER → Character（find-or-create by project_id+name, schedule embedding）
- WORLD_RULE → WorldEntry（find-or-create by project_id+title, schedule embedding）
- EVENT → CharacterEvent（仅 payload 有 character_name 且 staging 有 chapter_sequence_number 时；先 find-or-create Character）
- FORESHADOWING → HiddenThread（find-or-create by project_id+name）
- PLOT_FACT → 不写正式表，只保持 CONFIRMED staging（由 Context Builder `## 已确认记忆` 注入）

**关键决策**：PLOT_FACT 不硬塞 CharacterEvent；confirm 幂等（已 CONFIRMED 有 target → 直接返回）；写正式表与 staging 状态更新同一事务；Context Builder 查 `CONFIRMED + chapter_sequence_number <= 当前章节 OR NULL`，limit 20。

---

## 3. 当前测试状态

| 测试套件 | 单独跑 | 说明 |
|---|---|---|
| `test_smoke.py` | 103 passed | 老功能回归 |
| `test_harness_models.py` | 14 passed | 阶段 A 模型层 |
| `test_harness_run_manager.py` | 6 passed | 阶段 B run/step 服务 |
| `test_harness_generate_integration.py` | 1 passed | 阶段 B+C 集成 |
| `test_harness_llm_call_logger.py` | 6 passed | 阶段 C wrapper |
| `test_harness_run_api.py` | 9 passed | 阶段 D API |
| `test_harness_human_interrupt.py` | 7 passed | 阶段 E human interrupt |
| `test_harness_version_audit.py` | 11 passed | 阶段 F 版本审计 |
| `test_harness_guardrail.py` | 15 passed | 阶段 G 结构化 Guardrail |
| `test_harness_memory_staging.py` | 29 passed | 阶段 H1-H3 记忆 staging |
| **合计** | **201 passed** | |

**已知限制**：harness 测试与 `test_smoke.py` 共享 SQLite `:memory:` 引擎，**单独跑各自全绿，合跑有 `:memory:` 连接隔离问题**（非代码缺陷）。生产用 Postgres 不受影响。H2 后台任务在 SQLite 测试环境跳过（dialect 检查）。

---

## 4. 架构全景

```
backend/
├── models/
│   ├── harness_enums.py             # 6 枚举（Run/Step/Interrupt/Decision/MemoryStaging/MemoryType）
│   ├── ai_run.py                    # AiRun
│   ├── ai_run_step.py               # AiRunStep（幂等键）
│   ├── llm_call_log.py              # LlmCallLog（不可变）
│   ├── human_interrupt.py           # HumanInterrupt
│   ├── writing_memory_staging.py    # WritingMemoryStaging（H1）
│   ├── generation_record.py         # +run_id +accepted_version_id
│   └── chapter_version.py           # +project_id/run_id/parent/rollback/diff
├── harness/
│   ├── run_manager.py               # Run 生命周期
│   ├── step_logger.py               # Step 幂等记录
│   ├── llm_call_logger.py           # LoggedLLMProvider 装饰器
│   └── human_interrupt_service.py   # Interrupt 幂等服务
├── agents/
│   ├── workflow.py                  # _maybe_wrap_llm + CreativeState + consistency_checker_node 返回 dict
│   ├── guardrail.py                 # GuardrailResult 解析 + blocking_issues（G）
│   └── fact_extraction.py           # FactExtractionAgent + parse_facts（H2）
├── api/
│   └── routes.py                    # generate 钩子 + interrupt + Run API + human-decisions + memory-staging API + version audit + rollback endpoint
├── schemas/
│   └── api.py                       # Run/Step/Interrupt/Version/Guardrail/Staging schema
├── alembic/versions/
│   ├── 021_ai_harness_core.py       # 4 表 + 老表加列
│   └── 022_writing_memory_staging.py # 记忆 staging 表
└── services/
    ├── generation_record_service.py # +run_id
    ├── version_service.py           # create_version audit 参数 + prune 保护被引用版本
    ├── chapter_save.py              # save_chapter_content audit 参数
    ├── chapter_context.py           # +confirmed_memories + ## 已确认记忆 section
    └── memory_staging_service.py    # create_staging + run_fact_extraction + confirm_staging_item

frontend/
├── src/api/
│   ├── types.ts                     # SSE + ApiRun + GuardrailResult + ApiWritingMemoryStaging
│   └── client.ts                    # getRun/getRunSteps/listProjectRuns + listMemoryStaging/confirm/reject
└── src/components/
    ├── AgentPanel.vue               # run_created + guardrail 结构化渲染 + MemoryStagingPanel
    └── MemoryStagingPanel.vue       # staging 审核 panel（H3c）
```

---

## 5. 关键技术约束（勿违背）

1. **`llm_provider.py` 零改动**——用 `LoggedLLMProvider` 装饰器，不改抽象层。
2. **`llm_call_logs.step_id` 靠运行中 step 查询解析**——不依赖 graph state 注入（并行/动态节点竞态）。
3. **`LoggedLLMProvider` 用独立 session 写日志**——不把 `AsyncSession` 放进 graph state。
4. **`start_step` 后用 `db.commit()` 而非 `flush()`**——避免 StaleDataError。
5. **不删旧 SSE 事件**——新增事件不能破坏旧前端。
6. **`workflow.py` 不改节点函数和图拓扑**——step 钩子在 `astream_events` 循环。
7. **枚举用 `(str, Enum)`**——兼容 Python 3.10。
8. **不复用 `extraction_staging`**——写作记忆 staging 独立表。
9. **PLOT_FACT 不硬塞 CharacterEvent**——只保持 CONFIRMED staging，由 Context Builder 注入。
10. **H2 后台任务用独立 session**——`get_engine()` + `async_sessionmaker`，失败不影响 approve，SQLite 测试环境跳过。

---

## 6. 改动文件清单

**新增文件（24 个）**：
- `backend/models/harness_enums.py`
- `backend/models/ai_run.py`
- `backend/models/ai_run_step.py`
- `backend/models/llm_call_log.py`
- `backend/models/human_interrupt.py`
- `backend/models/writing_memory_staging.py`
- `backend/harness/__init__.py`
- `backend/harness/run_manager.py`
- `backend/harness/step_logger.py`
- `backend/harness/llm_call_logger.py`
- `backend/harness/human_interrupt_service.py`
- `backend/agents/guardrail.py`
- `backend/agents/fact_extraction.py`
- `backend/services/memory_staging_service.py`
- `backend/alembic/versions/021_ai_harness_core.py`
- `backend/alembic/versions/022_writing_memory_staging.py`
- `backend/tests/test_harness_models.py`
- `backend/tests/test_harness_run_manager.py`
- `backend/tests/test_harness_generate_integration.py`
- `backend/tests/test_harness_llm_call_logger.py`
- `backend/tests/test_harness_run_api.py`
- `backend/tests/test_harness_human_interrupt.py`
- `backend/tests/test_harness_version_audit.py`
- `backend/tests/test_harness_guardrail.py`
- `backend/tests/test_harness_memory_staging.py`
- `frontend/src/components/MemoryStagingPanel.vue`

**修改文件（12 个）**：
- `backend/models/__init__.py` — 注册全部新模型 + 枚举
- `backend/models/generation_record.py` — +run_id
- `backend/models/chapter_version.py` — +5 列
- `backend/services/generation_record_service.py` — +run_id 参数
- `backend/services/version_service.py` — audit 参数 + prune 保护
- `backend/services/chapter_save.py` — audit 参数
- `backend/services/chapter_context.py` — confirmed_memories + prompt section
- `backend/agents/workflow.py` — _maybe_wrap_llm + CreativeState + guardrail dict
- `backend/api/routes.py` — 全链路接入
- `backend/schemas/api.py` — 全部新 schema
- `frontend/src/api/types.ts` — 全部新类型
- `frontend/src/api/client.ts` — 全部新方法
- `frontend/src/components/AgentPanel.vue` — run_created + guardrail 渲染 + MemoryStagingPanel

---

## 7. 后续可扩展方向（不在本轮范围）

- **MemorySaver 替换**：当前用 in-process MemorySaver，生产可换 PostgreSQL checkpointer 实现跨进程恢复。
- **token/cost 聚合**：`OpenAIProvider.generate` 返回纯 str，不暴露 `resp.usage`；未来扩展可回收 token 数据。
- **Prompt snapshot 展示**：`llm_call_logs` 已存 prompt/context snapshot，可做前端调试面板。
- **MCP / tool runtime**：skill pack 目前是 prompt 注入，可扩展为真正可执行工具。
- **完整 AiRunPanel.vue**：前端目前只有最小 run_created 事件，可做完整 Run 详情面板。
- **RevisionAgent 自动修订**：Guardrail HIGH 时可触发自动修订而非仅人工审核。
- **多轮 Guardrail 策略**：当前只做一次检查，可扩展为多轮渐进式检查。
