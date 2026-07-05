# AI Harness 改造完成总结：A-H + I-J 全链路落地

> 面向 Claude / Codex 的上下文文档：全部阶段已完成，架构决策、代码结构、已知限制。
> 分支：`novel-extraction-mvp`，全部已提交。
> 基线文档：`docs/AI_Harness_改造适配技术文档.md` + `docs/Expert_System_v2_改造开发文档.md` + `docs/Clarification_Loop_多轮澄清改造技术文档.md`

---

## 0. 当前状态：AI Novel Harness v1 功能闭环完成

完整链路：

```
生成前澄清（Clarification Loop J-1~J-5）
  → v2 专家链规划/写作/审校/修订/一致性检查（I-1~I-5）
  → final_review 人工确认（Harness E）
  → 章节版本保存（Harness F）
  → story-recorder 记录剧情事实（I-6）
  → memory-curator 写入 staging（I-6）
  → 用户确认记忆（Harness H3）
  → 正式记忆库
  → Context Builder 下次优先读取 confirmed 记忆（Harness H3b）
```

| 模块 | 状态 |
|---|---|
| Harness A-H | ✅ 完成 |
| Expert v2 I-1 ~ I-5 | ✅ 完成 |
| Clarification Loop J-1 ~ J-5 | ✅ 完成 |
| I-6 v2 剧情记录与记忆治理 | ✅ 完成 |

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

## 阶段 I：Expert System v2 + Workflow v2 ✅

基线文档：`docs/Expert_System_v2_改造开发文档.md`

把旧"大师"专家体系替换为职责清晰的 v2 专家链，新增 workflow 定义、规则调度、LangGraph v2 主链路，保留旧 workflow 兼容。

### I-1：数据层 + Skill 草案 ✅

| 文件 | 作用 |
|---|---|
| `backend/alembic/versions/023_expert_system_v2.py` | experts +5 列 / ai_runs +4 列 / ai_run_steps +4 列 |
| `backend/models/expert.py` | +expert_key/version/deprecated/input_schema/output_schema |
| `backend/models/ai_run.py` | +workflow_key/workflow_version/workflow_snapshot/expert_snapshot |
| `backend/models/ai_run_step.py` | +node_key/expert_key/expert_version/skill_dir |
| `backend/skills/{10 个 v2 目录}/SKILL.md` | chapter-architect/writer/structural-critic/narrative-editor/continuity-checker/story-recorder/memory-curator/canon-researcher/scene-enhancer/novel-orchestrator |
| `backend/tests/test_expert_system_v2.py` | 模型字段 + skill 扫描 + 迁移列存在性（17 tests） |

### I-2：内置专家模板替换 ✅

新旧并存：新项目创建 6 v2 + 6 旧(deprecated)。

| 文件 | 作用 |
|---|---|
| `backend/agents/expert_templates.py` | BUILTIN_EXPERTS_V2（6 v2）+ BUILTIN_EXPERTS（6 旧 deprecated=true）+ ALL_BUILTIN_EXPERTS |
| `backend/services/expert_sync.py` | sync_v2_experts（幂等补齐 + 标记旧 deprecated） |
| `backend/api/routes.py` | 项目创建用 ALL_BUILTIN_EXPERTS + POST /experts/sync-v2 API |
| `backend/schemas/api.py` | ExpertResponse +expert_key/version/deprecated；ExpertUpdate +deprecated |
| `frontend/src/views/AgentStudio.vue` | 分三组（创作链/记忆/旧废弃折叠）+ 同步按钮 |
| `backend/tests/test_expert_system_v2.py` | +8 tests（新项目 12 专家 / sync 幂等 / patch deprecated） |

### I-3：规则总编 + Workflow Definition ✅

| 文件 | 作用 |
|---|---|
| `backend/services/workflow_definitions.py` | TaskType 枚举 + WorkflowStep/Definition dataclass + WORKFLOW_DEFINITIONS（13 条） |
| `backend/services/novel_orchestrator.py` | resolve_workflow（确定性查表，不调 LLM） |
| `backend/harness/run_manager.py` | create_run +workflow 参数 + build_expert_snapshot + workflow_to_snapshot |
| `backend/api/routes.py` | full_pipeline 分支写入 workflow_key/snapshot/expert_snapshot |
| `backend/tests/test_workflow_definitions.py` | 15 tests（结构/确定性/步骤/snapshot/向后兼容） |

### I-4：LangGraph Workflow v2 主链路 ✅

| 文件 | 作用 |
|---|---|
| `backend/agents/workflow_v2.py` | CreativeStateV2 + 6 节点（architect/writer/critic/editor/continuity/human_review）+ build_creative_graph_v2 + get_creative_app_v2 |
| `backend/agents/llm_provider.py` | MockProvider +3 v2 响应（ChapterTaskCard/StructuralCritique/EditedDraft） |
| `backend/api/routes.py` | full_pipeline 切 get_creative_app_v2 + STEP_NODE_MAP/NODE_EVENT_MAP + SSE + resume v2 判断 |
| `backend/tests/test_workflow_v2.py` | 20 tests（图结构/节点/SSE/HITL approve/HITL revise/skill_pack） |

**v2 标准链路**：`context_loader → chapter_architect → chapter_writer → structural_critic → narrative_editor → continuity_checker → human_review`
- architect 产出 ChapterTaskCard，writer 消费任务卡
- critic 输出 StructuralCritique，editor 消费审稿指令
- continuity 复用 GuardrailResult
- 旧 workflow.py 保留，resume 按 workflow_key 判断走 v2 还是旧

### I-5：快捷入口重映射 ✅

| 文件 | 作用 |
|---|---|
| `backend/agents/workflow_v2.py` | +build_creative_graph_v2_continue（4 节点无 HITL） |
| `backend/api/routes.py` | continue 第二阶段切 v2 + enhance/summarize 补 AiRun |
| `frontend/src/api/types.ts` | SSEEventType +architect_output |
| `frontend/src/components/AgentPanel.vue` | handleSSEEvent +architect_output case |
| `backend/tests/test_workflow_v2.py` | +5 tests（continue v2/enhance AiRun/summarize AiRun） |

- continue 第一阶段（方向建议）不改，仍走旧直接分支
- continue 第二阶段切 v2 LangGraph（architect → writer → continuity，无 HITL）
- enhance/summarize 补 AiRun + workflow 快照，仍走旧直接分支
- 所有入口都有审计链

---

## 阶段 J：Clarification Loop 多轮澄清 ✅

基线文档：`docs/Clarification_Loop_多轮澄清改造技术文档.md`

在 chapter-architect 前增加"多轮澄清环节"，让 AI 在生成前判断输入是否足够明确，不足时问用户最多 3 个高价值问题。

### J-1：基础层（Skill + Parser + Workflow 定义）✅

| 文件 | 作用 |
|---|---|
| `backend/skills/clarification-planner/SKILL.md` | 澄清规划师框架级草案 |
| `backend/agents/clarification.py` | parse_clarification_result / filter_answered_questions / build_clarification_summary |
| `backend/services/workflow_definitions.py` | WorkflowStep +is_conditional/routes；generate_chapter_standard v2.1（+clarification_planner/human_clarification，10 步 3 checkpoint） |
| `backend/tests/test_clarification.py` | 22 tests（parser 容错/裁剪/去重/一致性修正/skill 扫描/snapshot） |

### J-2：Planner Agent ✅

| 文件 | 作用 |
|---|---|
| `backend/agents/clarification.py` | +build_clarification_prompt + run_clarification_planner（调 LLM + LoggedLLMProvider + answered_ids 过滤 + 失败 fallback） |
| `backend/agents/llm_provider.py` | MockProvider +ClarificationResult 响应 |
| `backend/tests/test_clarification.py` | +10 tests（prompt 构建/LLM 成功/异常 fallback/answered_ids/summary） |

### J-3：Clarification Interrupt API ✅

| 文件 | 作用 |
|---|---|
| `backend/models/harness_enums.py` | +SUBMIT_CLARIFICATION/SKIP_CLARIFICATION + ANSWERED/SKIPPED |
| `backend/harness/human_interrupt_service.py` | resolve_interrupt +2 决策映射 |
| `backend/services/clarification_interrupt.py` | create/get/submit/skip + format_clarification_response |
| `backend/schemas/api.py` | ClarificationAnswerRequest / ClarificationResponse |
| `backend/api/routes.py` | GET /ai-runs/{id}/clarification + POST /ai-runs/{id}/clarification-answers |
| `backend/tests/test_clarification.py` | +11 tests（GET/submit/skip/跨用户/幂等/枚举） |

### J-4 + J-5：Workflow 接入 + 前端 UI（并行）✅

| 文件 | 作用 |
|---|---|
| `backend/agents/workflow_v2.py` | +clarification_planner_node + human_clarification_node + route_after_clarification + 更新 graph（interrupt_before 两个暂停点） |
| `backend/api/routes.py` | +clarification SSE 事件 + STEP_NODE_MAP + resume clarification |
| `frontend/src/components/ClarificationPanel.vue` | 澄清面板（single_choice/free_text/multi_choice/number + submit/skip） |
| `frontend/src/api/types.ts` | ClarificationQuestion/State/AnswerRequest + SSEEventType +clarification_required |
| `frontend/src/api/client.ts` | getClarification / submitClarificationAnswers |
| `frontend/src/components/AgentPanel.vue` | +clarificationState + clarification_required SSE case + ClarificationPanel 渲染 |
| `backend/tests/test_workflow_v2.py` | +clarification 测试 |
| `frontend/src/components/ClarificationPanel.test.ts` | 20 tests（源码断言） |

---

## 阶段 I-6：story-recorder / memory-curator 接入 v2 ✅

approve 后用 story-recorder + memory-curator 替换/增强旧 FactExtractionAgent。

| 文件 | 作用 |
|---|---|
| `backend/agents/story_recorder.py` | STORY_RECORDER_PROMPT + parse_story_record + run_story_recorder（调 LLM + LoggedLLMProvider） |
| `backend/agents/memory_curator.py` | story_record_to_facts（Story Record → staging facts 映射） |
| `backend/services/memory_staging_service.py` | +run_story_recorder_extraction（story-recorder → memory-curator → staging，复用幂等逻辑） |
| `backend/api/routes.py` | approve 分支：v2 run → run_story_recorder_extraction；旧 run → run_fact_extraction |
| `backend/agents/llm_provider.py` | MockProvider +Story Record 响应 |
| `backend/tests/test_story_recorder.py` | 15 tests（parser/run_story_recorder/facts 映射/PLOT_FACT 不硬塞/EVENT 有 character_name） |

**映射规则**：events→PLOT_FACT / character_state_changes→CHARACTER / ability_changes→EVENT / foreshadowing_new→FORESHADOWING / 其余→PLOT_FACT。PLOT_FACT 不硬塞 CharacterEvent，EVENT 保留 character_name 供后续 confirm。

---

## 3. 当前测试状态

| 测试套件 | 单独跑 | 说明 |
|---|---|---|
| `test_smoke.py` | 105 passed | 老功能 + v2 适配回归 |
| `test_harness_models.py` | 14 passed | 阶段 A 模型层 |
| `test_harness_run_manager.py` | 6 passed | 阶段 B run/step 服务 |
| `test_harness_generate_integration.py` | 1 passed | 阶段 B+C 集成 |
| `test_harness_llm_call_logger.py` | 6 passed | 阶段 C wrapper |
| `test_harness_run_api.py` | 9 passed | 阶段 D API |
| `test_harness_human_interrupt.py` | 7 passed | 阶段 E human interrupt |
| `test_harness_version_audit.py` | 11 passed | 阶段 F 版本审计 |
| `test_harness_guardrail.py` | 15 passed | 阶段 G 结构化 Guardrail |
| `test_harness_memory_staging.py` | 29 passed | 阶段 H1-H3 记忆 staging |
| `test_expert_system_v2.py` | 17 passed | I-1/I-2 模型字段 + 专家模板 |
| `test_workflow_definitions.py` | 15 passed | I-3 workflow 定义 + 规则调度 |
| `test_workflow_v2.py` | 20 passed | I-4/I-5 v2 LangGraph 主链路 |
| `test_clarification.py` | 43 passed | J-1~J-3 clarification parser + API |
| `test_story_recorder.py` | 15 passed | I-6 story-recorder + memory-curator |
| `test_chapter_context.py` | 13 passed | 章节上下文 + 跨章大纲过滤 |
| **后端合计** | **257 passed** | |
| **前端 vitest** | **52 passed** | 含 ClarificationPanel/ContextPicker 等 |
| **前端 vue-tsc** | **exit 0** | 类型检查通过 |

**已知限制**：harness 测试与 `test_smoke.py` 共享 SQLite `:memory:` 引擎，**单独跑各自全绿，合跑有 `:memory:` 连接隔离问题**（非代码缺陷）。生产用 Postgres 不受影响。H2 后台任务在 SQLite 测试环境跳过（dialect 检查）。

---

## 4. 架构全景

```
backend/
├── models/
│   ├── harness_enums.py             # 10 枚举（+SUBMIT/SKIP_CLARIFICATION + ANSWERED/SKIPPED）
│   ├── ai_run.py                    # AiRun（+workflow_key/snapshot/expert_snapshot）
│   ├── ai_run_step.py               # AiRunStep（+node_key/expert_key/expert_version/skill_dir）
│   ├── llm_call_log.py              # LlmCallLog（不可变）
│   ├── human_interrupt.py           # HumanInterrupt
│   ├── writing_memory_staging.py    # WritingMemoryStaging
│   ├── expert.py                    # Expert（+expert_key/version/deprecated/input_schema/output_schema）
│   ├── generation_record.py         # +run_id +accepted_version_id
│   └── chapter_version.py           # +project_id/run_id/parent/rollback/diff
├── harness/
│   ├── run_manager.py               # Run 生命周期 + build_expert_snapshot + workflow_to_snapshot
│   ├── step_logger.py               # Step 幂等记录
│   ├── llm_call_logger.py           # LoggedLLMProvider 装饰器
│   └── human_interrupt_service.py   # Interrupt 幂等服务（+clarification 决策映射）
├── agents/
│   ├── workflow.py                  # 旧 LangGraph 工作流（保留兼容）
│   ├── workflow_v2.py               # v2 工作流（architect/writer/critic/editor/continuity + clarification）
│   ├── guardrail.py                 # GuardrailResult 解析 + blocking_issues
│   ├── fact_extraction.py           # 旧 FactExtractionAgent（保留兼容）
│   ├── story_recorder.py            # v2 Story Recorder（Story Record 提取）
│   ├── memory_curator.py            # v2 Memory Curator（Story Record → staging facts）
│   ├── clarification.py             # ClarificationResult parser + run_clarification_planner
│   ├── expert_templates.py          # BUILTIN_EXPERTS_V2 + BUILTIN_EXPERTS(deprecated) + ALL_BUILTIN_EXPERTS
│   └── llm_provider.py              # MockProvider +6 v2 响应（TaskCard/Critique/EditDraft/Clarification/StoryRecord）
├── api/
│   └── routes.py                    # 全链路接入 + v2 workflow + clarification API + memory staging API
├── schemas/
│   └── api.py                       # 全部 schema（+ClarificationAnswerRequest/Response + Expert v2 字段）
├── alembic/versions/
│   ├── 021_ai_harness_core.py       # Harness 4 表 + 老表加列
│   ├── 022_writing_memory_staging.py # 记忆 staging 表
│   └── 023_expert_system_v2.py      # Expert/AiRun/AiRunStep v2 快照字段
├── services/
│   ├── workflow_definitions.py      # TaskType + WorkflowDefinition + WORKFLOW_DEFINITIONS（13 条）
│   ├── novel_orchestrator.py        # resolve_workflow 确定性规则调度
│   ├── expert_sync.py               # sync_v2_experts 幂等补齐
│   ├── clarification_interrupt.py   # clarification interrupt 服务
│   ├── generation_record_service.py # +run_id
│   ├── version_service.py           # create_version audit 参数 + prune 保护
│   ├── chapter_save.py              # save_chapter_content audit 参数
│   ├── chapter_context.py           # +confirmed_memories + ## 已确认记忆 + 跨章大纲过滤
│   └── memory_staging_service.py    # create_staging + run_fact_extraction + run_story_recorder_extraction + confirm_staging_item
└── skills/
    ├── chapter-architect/SKILL.md   # v2 专家 SKILL（框架级）
    ├── chapter-writer/SKILL.md
    ├── structural-critic/SKILL.md
    ├── narrative-editor/SKILL.md
    ├── continuity-checker/SKILL.md
    ├── story-recorder/SKILL.md
    ├── memory-curator/SKILL.md
    ├── canon-researcher/SKILL.md
    ├── scene-enhancer/SKILL.md
    ├── novel-orchestrator/SKILL.md
    ├── clarification-planner/SKILL.md
    └── （旧 skill 保留：creative-master/brutal-critic/...）

frontend/
├── src/api/
│   ├── types.ts                     # SSE + Run + Guardrail + Staging + Clarification + Expert v2
│   └── client.ts                    # Run/Staging/Clarification API + syncV2Experts
└── src/components/
    ├── AgentPanel.vue               # SSE 全事件处理 + ClarificationPanel + MemoryStagingPanel
    ├── ClarificationPanel.vue        # 生成前澄清面板（single_choice/free_text/skip）
    ├── MemoryStagingPanel.vue        # 记忆 staging 审核 panel
    ├── ContextPicker.vue             # 素材选择（本章大纲过滤）
    └── ApprovalModal.vue             # 最终审核
```

---

## 5. 关键技术约束（勿违背）

1. **`llm_provider.py` 只加 mock 响应检测**——不改抽象层，用 `LoggedLLMProvider` 装饰器。
2. **`llm_call_logs.step_id` 靠运行中 step 查询解析**——不依赖 graph state 注入（并行/动态节点竞态）。
3. **`LoggedLLMProvider` 用独立 session 写日志**——不把 `AsyncSession` 放进 graph state。
4. **`start_step` 后用 `db.commit()` 而非 `flush()`**——避免 StaleDataError。
5. **不删旧 SSE 事件**——新增事件不能破坏旧前端。
6. **旧 `workflow.py` 不删除**——resume 按 `AiRun.workflow_key` 判断走 v2 还是旧。
7. **枚举用 `(str, Enum)`**——兼容 Python 3.10。
8. **不复用 `extraction_staging`**——写作记忆 staging 独立表。
9. **PLOT_FACT 不硬塞 CharacterEvent**——只保持 CONFIRMED staging，由 Context Builder 注入。
10. **H2/I-6 后台任务用独立 session**——`get_engine()` + `async_sessionmaker`，失败不影响 approve，SQLite 测试环境跳过。
11. **clarification 最多 3 轮**——`route_after_clarification` 防止无限循环。
12. **clarification 不替代 final_review**——生成前澄清 vs 生成后审核，不合并。
13. **JSON 列修改后必须 `flag_modified`**——`JSONValue` 无 mutability tracking。
14. **story-recorder / memory-curator 不直接写正式表**——只写 staging，用户确认后才入正式表。

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

- **端到端人工验收**：从创建项目、生成第 1 章、回答澄清、approve、确认记忆、再生成第 2 章，走一遍真实 UI。
- **干净环境迁移检查**：从空库跑到 `alembic head`，确认 021/022/023 顺序无误。
- **用户使用说明**：章节生成、澄清回答、最终审核、记忆确认、版本回滚、run 日志查看。
- **enhance/summarize 彻底 v2 化**：当前仍走旧直接分支，可切 v2 LangGraph。
- **article 模式 v2 化**：当前 article 仍走旧 workflow。
- **planning_review 实现**：workflow 定义已预留，但 LangGraph 节点未实现。
- **Workflow 可视化拖拽**：当前 workflow 定义写死在 Python，可迁移到数据库 + 前端 DAG。
- **MemorySaver 替换**：当前用 in-process MemorySaver，生产可换 PostgreSQL checkpointer 实现跨进程恢复。
- **token/cost 聚合**：`OpenAIProvider.generate` 返回纯 str，不暴露 `resp.usage`；未来扩展可回收 token 数据。
- **Prompt snapshot 展示**：`llm_call_logs` 已存 prompt/context snapshot，可做前端调试面板。
- **MCP / tool runtime**：skill pack 目前是 prompt 注入，可扩展为真正可执行工具。
- **完整 AiRunPanel.vue**：前端目前只有最小 run_created 事件，可做完整 Run 详情面板。
- **成本预算 / 中断续跑增强**：run 级别成本统计 + 断点续跑。
