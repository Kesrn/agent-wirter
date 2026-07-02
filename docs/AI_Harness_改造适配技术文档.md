# AI Harness 改造适配技术文档

面向 Codex / GLM 的实现指引

## 1. 文档定位

本文档用于把《小说创作平台 AI Harness 需求文档》适配到当前 `ai-creative-platform` 项目。

当前项目已经具备小说/文章创作平台雏形：FastAPI 后端、Vue 前端、LangGraph 工作流、SSE 流式生成、章节版本、生成历史、资料库、结构化抽取、评测和 Langfuse 可观测性。但这些能力目前更像“产品功能集合”，还没有形成需求文档中强调的持久化 AI Harness 内核。

改造目标不是重写项目，而是在现有能力上补齐：

1. AI Run 生命周期。
2. Run Step 持久化。
3. LLM 调用审计。
4. Prompt / Context Snapshot。
5. 版本与 Run 的关联。
6. 可恢复的人工审核状态。
7. 结构化 Guardrail 结果。
8. 记忆 staging 到 confirmed 的闭环。

核心原则：

1. 增量改造，不推翻现有功能。
2. 保留现有 `/generate` SSE 体验。
3. 保留现有 `generation_records` 作为候选稿历史。
4. 新增 `ai_run / ai_run_step / llm_call_log` 作为 Harness 内核。
5. 先把过程追踪做实，再继续扩多 Agent、MCP、复杂记忆。

## 2. 当前项目现状

### 2.1 技术栈现状

后端：

- FastAPI
- SQLAlchemy Async
- Alembic
- Pydantic
- LangGraph
- PostgreSQL / SQLite 兼容
- pgvector 依赖已存在
- Langfuse 可选观测

前端：

- Vue 3
- TypeScript
- Vite
- Pinia
- Electron 桌面端
- SSE 流式解析

### 2.2 已有核心模块

项目与章节：

- `backend/models/project.py`
- `backend/models/chapter.py`
- `backend/api/routes.py`
- `frontend/src/views/Workspace.vue`
- `frontend/src/components/WritingEditor.vue`

章节版本：

- `backend/models/chapter_version.py`
- `backend/services/version_service.py`
- `frontend/src/components/VersionHistoryPanel.vue`

AI 生成历史：

- `backend/models/generation_record.py`
- `backend/services/generation_record_service.py`
- `frontend/src/components/GenerationHistoryPanel.vue`

LangGraph 工作流：

- `backend/agents/workflow.py`
- 当前默认流程不是线性串行，而是：`context_loader -> writer -> [critic || consistency_checker] -> human_review`。
- `human_review` 后存在修订回环：当用户选择 revise 且 `revision_count <= 3` 时，流程会回到 writer。
- 因为 critic 与 consistency_checker 是并行扇出，后续 `ai_run_steps.step_order` 不能假设严格递增串行执行。

上下文聚合：

- `backend/services/chapter_context.py`
- 当前可聚合章节、大纲、角色事件、世界观、暗线、前文、资料库。

结构化抽取与知识库：

- `backend/models/extraction_pipeline.py`
- `backend/models/structured_knowledge.py`
- `backend/services/extraction_service.py`
- `backend/services/knowledge_source.py`

评测：

- `backend/models/evaluation.py`
- `backend/services/evaluation.py`

可观测：

- `backend/observability/langfuse.py`

### 2.3 当前能力与 Harness 文档的匹配情况

高度匹配：

- 项目管理。
- 章节管理。
- 章节版本。
- AI 生成候选内容。
- SSE 生成过程展示。
- LangGraph 多节点工作流。
- Human Review 雏形。
- Context Builder 雏形。
- 结构化知识抽取雏形。
- Evaluation 雏形。

部分匹配：

- `generation_records` 能记录候选稿，但不是完整 `ai_run`。
- `chapter_versions` 能记录版本，但缺少 `run_id / parent_version_id / rollback_from_version_id`。
- `Context Builder` 能聚合内容，但缺少 `ContextPackage / ContextItem / token 裁剪 / snapshot`。
- `ConsistencyChecker` 能输出检查文本，但不是强结构化 `GuardrailResult`。
- Langfuse 能追踪部分 LLM 调用，但业务数据库没有 `llm_call_log`。
- HITL 能暂停，但当前 checkpoint 使用内存 `MemorySaver`，服务重启后不可恢复。
- `chapter_context.py` 已有定长字符截断：当前章节截 500 字符，前文每章截 300 字符且限最近 3 章，检索资料截 200 字符且限 5 条。升级 ContextPackage 时应把这些魔法数字替换为 token 预算和优先级裁剪。

缺失：

- `ai_run` 表。
- `ai_run_step` 表。
- `llm_call_log` 表。
- 持久化 `human_interrupt`。
- Prompt Template 版本表。
- Prompt Snapshot。
- Context Package Snapshot。
- token / cost / latency 本地统计。
- Run 状态查询 API。
- Run Steps 查询 API。
- Guardrail 阻断提交规则。
- 章节版本和 AI Run 的强关联。
- memory staging 到 confirmed 的人工确认闭环。

## 3. 改造总体策略

### 3.1 不推翻现有概念

保留：

- `GenerationRecord`：继续表示“AI 生成候选稿历史”。
- `ChapterVersion`：继续表示章节版本。
- `LangGraph workflow`：继续作为实际执行编排。
- `/projects/{project_id}/chapters/generate`：继续提供 SSE 流式体验。
- `/projects/{project_id}/chapters/resume`：继续作为 HITL 恢复入口。

新增：

- `AiRun`：一次 AI 任务的业务生命周期。
- `AiRunStep`：一次任务中的步骤执行记录。
- `LlmCallLog`：每次 LLM 调用的审计记录。
- `HumanInterrupt`：持久化人工审核等待状态。
- `PromptTemplate`：后续 Prompt 模板版本管理。
- `ContextPackage` 服务结构：先用 Python dataclass / dict，不一定第一步建表。

### 3.2 概念映射

| Harness 文档概念 | 当前项目概念 | 改造方案 |
| --- | --- | --- |
| AI Run | 暂无，接近 GenerationRecord | 新增 `ai_runs`，`generation_records` 只做候选稿 |
| Run Step | SSE agent_start/agent_done 临时事件 | 新增 `ai_run_steps`，SSE 事件同步写库 |
| LLM Call Log | Langfuse 可选追踪 | 新增 `llm_call_logs` 本地表 |
| Chapter Version | `chapter_versions` | 补 `project_id/run_id/parent_version_id/rollback_from_version_id` |
| Human Review | LangGraph `human_review` + `thread_id` | 新增 `human_interrupts` 持久化 |
| Context Builder | `chapter_context.py` | 升级为可 snapshot 的 ContextPackage |
| Consistency Checker | `consistency_checker_node` | 输出结构化 GuardrailResult |
| Memory Staging | `extraction_staging` | 后续新增写作章节 memory_staging，和资料库抽取 staging 区分 |

### 3.3 推荐开发顺序

第一轮只做 Harness 内核，不改太多 UI：

1. 新增 `ai_runs`。
2. 新增 `ai_run_steps`。
3. 新增 `llm_call_logs`。
4. 在现有 `/generate` 中创建 run 和 step。
5. 在现有 SSE 节点事件中写 step 状态，注意 writer 后的 critic / consistency_checker 是并行 step。
6. 在 `generation_records` 中关联 `run_id`。
7. 提供 run 查询接口和 step 查询接口。

第二轮补版本关联和 snapshot：

1. `chapter_versions` 增加 `project_id / run_id / parent_version_id / rollback_from_version_id`。
2. 保存 AI 采纳版本时写入 `run_id`。
3. LLM 调用保存 `rendered_prompt_snapshot`。
4. Context Builder 保存 `context_package_snapshot`。

第三轮补可恢复 Human Review：

1. 新增 `human_interrupts`。
2. `WAITING_HUMAN` 状态落库。
3. `/resume` 根据 `run_id` 或 `interrupt_id` 恢复。
4. 替换或补强 `MemorySaver`，避免服务重启后丢失状态。

第四轮补 Guardrail 与 Memory：

1. ConsistencyAgent 输出结构化 JSON。
2. 高危冲突阻止自动 approve。
3. 新增写作侧 `memory_staging`。
4. 用户确认后写入 confirmed 设定。

## 4. 数据库改造

### 4.1 新增 ai_runs

模型文件建议：

- `backend/models/ai_run.py`

迁移文件建议：

- `backend/alembic/versions/021_ai_harness_core.py`

表结构建议：

```sql
CREATE TABLE ai_runs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    chapter_id UUID NULL,
    document_id UUID NULL,
    generation_record_id UUID NULL,

    run_type VARCHAR(50) NOT NULL,
    mode VARCHAR(50) NOT NULL,
    status VARCHAR(50) NOT NULL,
    current_step VARCHAR(100),
    user_goal TEXT,

    thread_id VARCHAR(200),
    model_config_snapshot JSONB,
    token_usage JSONB,
    cost_usage JSONB,

    error_message TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

状态枚举：

- CREATED
- RUNNING
- WAITING_HUMAN
- PAUSED
- FAILED
- COMPLETED
- CANCELLED

索引：

```sql
CREATE INDEX ix_ai_runs_project_created ON ai_runs(project_id, created_at);
CREATE INDEX ix_ai_runs_chapter_created ON ai_runs(chapter_id, created_at);
CREATE INDEX ix_ai_runs_document_created ON ai_runs(document_id, created_at);
CREATE INDEX ix_ai_runs_status ON ai_runs(status);
CREATE INDEX ix_ai_runs_thread_id ON ai_runs(thread_id);
```

说明：

- `generation_record_id` 用于关联最终候选稿历史。
- `chapter_id` 和 `document_id` 二选一，兼容小说和文章项目。
- 第一阶段不强制外键，避免 SQLite / 桌面端迁移复杂；后续可补。

### 4.2 新增 ai_run_steps

模型文件建议：

- `backend/models/ai_run_step.py`

表结构建议：

```sql
CREATE TABLE ai_run_steps (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL,
    step_order INT NOT NULL,
    step_name VARCHAR(100) NOT NULL,
    agent_name VARCHAR(100),
    status VARCHAR(50) NOT NULL,

    input JSONB,
    output JSONB,
    input_hash VARCHAR(128),
    output_hash VARCHAR(128),

    retry_count INT NOT NULL DEFAULT 0,
    max_retry INT NOT NULL DEFAULT 0,
    idempotency_key VARCHAR(200),

    error_message TEXT,
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

状态枚举：

- PENDING
- RUNNING
- SUCCESS
- FAILED
- SKIPPED
- WAITING_HUMAN
- RETRYING

索引：

```sql
CREATE INDEX ix_ai_run_steps_run_order ON ai_run_steps(run_id, step_order);
CREATE INDEX ix_ai_run_steps_run_status ON ai_run_steps(run_id, status);
CREATE UNIQUE INDEX ux_ai_run_steps_idempotency ON ai_run_steps(idempotency_key)
WHERE idempotency_key IS NOT NULL;
```

### 4.3 新增 llm_call_logs

模型文件建议：

- `backend/models/llm_call_log.py`

表结构建议：

```sql
CREATE TABLE llm_call_logs (
    id UUID PRIMARY KEY,
    run_id UUID,
    step_id UUID,
    project_id UUID,
    chapter_id UUID,
    document_id UUID,

    agent_name VARCHAR(100),
    provider VARCHAR(50),
    model VARCHAR(120),

    prompt_template_id UUID,
    prompt_template_version INT,
    prompt_hash VARCHAR(128),

    rendered_prompt_snapshot TEXT,
    context_package_snapshot JSONB,
    model_config_snapshot JSONB,

    input_tokens INT,
    output_tokens INT,
    total_tokens INT,
    cost NUMERIC(12,6),
    latency_ms INT,

    request JSONB,
    response JSONB,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

索引：

```sql
CREATE INDEX ix_llm_call_logs_run_created ON llm_call_logs(run_id, created_at);
CREATE INDEX ix_llm_call_logs_step_created ON llm_call_logs(step_id, created_at);
CREATE INDEX ix_llm_call_logs_project_created ON llm_call_logs(project_id, created_at);
```

实现注意：

- 不要只依赖 Langfuse。Langfuse 是外部观测，本地 `llm_call_logs` 是业务审计。
- 第一阶段如果拿不到真实 token，可以先写 `NULL`，但字段必须存在。
- OpenAI 兼容响应如果有 usage，则写入 `input_tokens / output_tokens / total_tokens`。

### 4.4 修改 generation_records

当前文件：

- `backend/models/generation_record.py`

新增字段：

```sql
ALTER TABLE generation_records ADD COLUMN run_id UUID NULL;
CREATE INDEX ix_generation_records_run_id ON generation_records(run_id);
```

用途：

- `generation_records` 继续表示候选稿。
- `ai_runs` 表示过程。
- 一个 run 通常对应一个 generation_record。

### 4.5 修改 chapter_versions

当前文件：

- `backend/models/chapter_version.py`
- `backend/services/version_service.py`

新增字段：

```sql
ALTER TABLE chapter_versions ADD COLUMN project_id UUID NULL;
ALTER TABLE chapter_versions ADD COLUMN run_id UUID NULL;
ALTER TABLE chapter_versions ADD COLUMN parent_version_id UUID NULL;
ALTER TABLE chapter_versions ADD COLUMN rollback_from_version_id UUID NULL;
ALTER TABLE chapter_versions ADD COLUMN diff_from_parent JSONB NULL;
```

索引：

```sql
CREATE INDEX ix_chapter_versions_project_chapter ON chapter_versions(project_id, chapter_id);
CREATE INDEX ix_chapter_versions_run_id ON chapter_versions(run_id);
```

版本来源建议扩展：

- manual
- ai_draft
- ai_approve
- ai_enhance
- ai_continue
- ai_pipeline
- rollback
- import

实现注意：

- `backend/services/version_service.py` 当前使用 `VALID_SOURCES` frozenset 校验 source。
- 扩展 `ai_draft / rollback / import` 时必须同步修改 `VALID_SOURCES`，否则创建版本会失败。
- 文档版本已有 `restore` 来源，但章节版本当前没有 restore/rollback API；新增章节回滚时建议使用 `rollback`，不要复用 document 侧的 `restore` 语义。

重要改动：

- 当前 `version_service.py` 有 `MAX_VERSIONS_PER_CHAPTER = 10` 并会删除旧版本。
- Harness 要求可追踪、可回滚，不建议默认删除历史版本。
- 改造时应取消自动裁剪，或改为配置项且默认关闭。

### 4.6 新增 human_interrupts

模型文件建议：

- `backend/models/human_interrupt.py`

表结构建议：

```sql
CREATE TABLE human_interrupts (
    id UUID PRIMARY KEY,
    run_id UUID NOT NULL,
    step_id UUID,
    thread_id VARCHAR(200),
    step_name VARCHAR(100),
    status VARCHAR(50) NOT NULL,
    payload JSONB,
    decision VARCHAR(50),
    feedback TEXT,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_at TIMESTAMPTZ
);
```

状态建议：

- WAITING
- APPROVED
- EDITED
- REJECTED
- REGENERATE
- EXPIRED

### 4.7 数据库通用字段

当前项目已有 `UUIDMixin` 和 `TimestampMixin`，但没有通用软删除和乐观锁。

短期建议：

- 新增 Harness 表时先使用 `UUIDMixin + TimestampMixin`。
- 不强行改所有老表。
- 后续再统一补 `deleted_at / version / created_by / updated_by`。

## 5. 后端服务改造

### 5.1 新增 harness 包

建议目录：

```text
backend/harness/
├── __init__.py
├── run_manager.py
├── step_logger.py
├── llm_call_logger.py
├── context_snapshot.py
├── guardrail_result.py
└── human_interrupt_service.py
```

### 5.2 RunManager

文件：

- `backend/harness/run_manager.py`

职责：

1. 创建 run。
2. 更新 run 状态。
3. 设置 current_step。
4. 汇总 token / cost。
5. 标记失败。
6. 标记 WAITING_HUMAN。
7. 标记完成。

关键函数建议：

```python
async def create_run(db, *, project_id, chapter_id=None, document_id=None, run_type, mode, user_goal, model_config_snapshot=None) -> AiRun:
    ...

async def mark_run_running(db, run: AiRun) -> None:
    ...

async def mark_run_waiting_human(db, run: AiRun, *, step_name: str, thread_id: str | None = None) -> None:
    ...

async def mark_run_completed(db, run: AiRun) -> None:
    ...

async def mark_run_failed(db, run: AiRun, error_message: str) -> None:
    ...
```

### 5.3 StepLogger

文件：

- `backend/harness/step_logger.py`

职责：

1. step start 时创建或更新 `ai_run_steps`。
2. step done 时保存 output。
3. step error 时保存 error。
4. 对 SSE agent_start / agent_done 进行持久化。
5. 支持并发 step：critic 和 consistency_checker 可能同时执行，不能依赖全局自增计数作为唯一顺序来源。

并发约束：

1. `step_order` 使用预分配顺序或开始时间排序。
2. 幂等键建议使用 `run_id:step_name:attempt`，例如 `run_uuid:critic:0`。
3. 如果同一个 run 中同名 step 可能多轮出现，必须把 revision round 或 retry count 纳入幂等键。
4. `start_step` 应使用 get-or-create 语义，避免并发 SSE 事件重复创建 step。

关键函数建议：

```python
async def start_step(db, *, run_id, step_order, step_name, agent_name=None, input=None, max_retry=0) -> AiRunStep:
    ...

async def finish_step(db, step: AiRunStep, *, output=None) -> None:
    ...

async def fail_step(db, step: AiRunStep, *, error_message: str) -> None:
    ...

async def wait_human_step(db, step: AiRunStep, *, payload=None) -> None:
    ...
```

### 5.4 LlmCallLogger

文件：

- `backend/harness/llm_call_logger.py`

职责：

1. 包装 LLM Provider 调用。
2. 保存 prompt、context、model config、usage、latency。
3. 失败时保存 error。

不要在第一步大改所有 provider。

推荐做法：

- 新增一个 `LoggedLLMProvider` 包装器。
- 内部调用现有 `LLMProvider.generate()` 和 `generate_stream()`。
- 先接入主要 workflow 节点：writer / critic / consistency_checker。

伪代码：

```python
class LoggedLLMProvider:
    def __init__(self, provider, db, run_id, step_id, project_id, chapter_id=None, document_id=None, agent_name=None):
        self.provider = provider
        ...

    async def generate(self, system_prompt, user_prompt, *, context_snapshot=None, model_config_snapshot=None, **kwargs):
        started = time.perf_counter()
        rendered_prompt = build_rendered_prompt(system_prompt, user_prompt)
        try:
            result = await self.provider.generate(system_prompt, user_prompt, **kwargs)
            await log_success(...)
            return result
        except Exception as exc:
            await log_failure(...)
            raise
```

### 5.5 Context Snapshot

当前：

- `backend/services/chapter_context.py` 返回 `ChapterContext` dataclass。
- 现有实现已经有粗粒度截断：当前章节 500 字符、前文每章 300 字符、检索资料 200 字符、自动命中最多 5 条、关键词最多 8 个。

目标：

- 保留 `ChapterContext`。
- 新增 `ContextPackage` 导出函数，不必第一步替换全部调用。
- 用 token 估算、优先级排序、相关性排序替换现有魔法数字截断。

建议新增：

```python
def context_to_package(context: ChapterContext, *, run_id: str | None, user_goal: str | None, max_token_budget: int | None = None) -> dict:
    ...
```

ContextItem 字段：

- id
- type
- content
- source_id
- source_type
- priority
- relevance_score
- hard_constraint

第一阶段可以先把现有章节、大纲、角色、世界观、暗线、前文、资料库转成 items。

后续再补：

- token 估算。
- priority 裁剪。
- hard rules 永不裁剪。
- confirmed 优先级。

### 5.6 GuardrailResult

文件：

- `backend/harness/guardrail_result.py`

结构建议：

```python
class GuardrailIssue(BaseModel):
    type: str
    severity: str
    location: str | None = None
    message: str
    evidence: str | None = None
    suggestion: str | None = None

class GuardrailResult(BaseModel):
    passed: bool
    severity: str
    score: float | None = None
    issues: list[GuardrailIssue] = []
    required_fixes: list[str] = []
```

改造 `consistency_checker_node`：

- Prompt 要求只输出 JSON。
- JSON parse 失败时最多修复一次。
- 最终结果写入 `ai_run_steps.output.guardrail_result`。
- HIGH severity 不允许自动提交，需要进入人工审核。

## 6. API 改造

### 6.1 保留现有 API

必须保留：

- `POST /api/projects/{project_id}/chapters/generate`
- `POST /api/projects/{project_id}/chapters/resume`
- `GET /api/projects/{project_id}/chapters/{sequence_number}/generations`
- `GET /api/projects/{project_id}/chapters/{sequence_number}/versions`

原因：

- 前端已依赖这些接口。
- Electron 桌面端也可能依赖。

### 6.2 新增 Run 查询 API

新增路由建议仍放在：

- `backend/api/routes.py`

后续可拆：

- `backend/api/harness_routes.py`

新增接口：

```text
GET /api/ai-runs/{run_id}
GET /api/ai-runs/{run_id}/steps
GET /api/projects/{project_id}/ai-runs
```

响应示例：

```json
{
  "id": "run_uuid",
  "project_id": "project_uuid",
  "chapter_id": "chapter_uuid",
  "run_type": "CHAPTER_DRAFT",
  "mode": "full_pipeline",
  "status": "WAITING_HUMAN",
  "current_step": "human_review",
  "generation_record_id": "record_uuid",
  "token_usage": {
    "input_tokens": 12000,
    "output_tokens": 5000,
    "total_tokens": 17000
  },
  "created_at": "...",
  "updated_at": "..."
}
```

### 6.3 修改生成接口返回事件

现有 SSE 已有：

- progress
- agent_start
- agent_output
- agent_done
- writer_output
- content_output
- editor_output
- critic_output
- consistency_check
- enhance_directions
- turn_suggestions
- content_suggestions
- article_review
- revision_suggestions
- skill_pack
- generation_record
- done
- error

建议新增事件：

- run_created
- run_status
- run_step

示例：

```text
event: run_created
data: {"run_id":"...","status":"CREATED"}

event: run_step
data: {"run_id":"...","step_name":"writer","status":"RUNNING"}
```

兼容原则：

- 新增事件不能破坏旧前端。
- 运行时旧前端不识别新事件时应直接忽略。
- 类型层也必须同步：`frontend/src/api/types.ts` 中的 `SSEEventType` 是闭集 union，新增 `run_created / run_status / run_step` 时必须扩展 union，或把 envelope event 改成带兜底的 `string` 类型。
- `frontend/src/components/AgentPanel.vue` 的 `handleSSEEvent` 需要补 `run_*` 分支或显式 default 忽略，避免后续维护者误以为事件丢失是后端问题。

### 6.4 Resume 接口改造

当前：

```text
POST /api/projects/{project_id}/chapters/resume?thread_id=...&action=approve
```

现状说明：

- 后端 FastAPI 参数当前是 `thread_id: str = Query(...)`。
- 前端本地变量名常用 `threadId`，但 `frontend/src/api/client.ts` 发出的 query key 是 `thread_id`。
- 现有接口还有 `feedback` 参数，`action` 限定为 `approve | reject | review | revise`。

建议保留，同时新增：

```text
POST /api/ai-runs/{run_id}/human-decisions
```

请求：

```json
{
  "decision": "APPROVE",
  "feedback": "节奏再紧一点"
}
```

实现过渡：

- 第一阶段 `human-decisions` 内部仍调用现有 resume 逻辑。
- `human_interrupts` 中保存 `thread_id`，可从 `run_id` 找到 thread。

## 7. 现有生成流程改造方案

当前生成流程位于：

- `backend/api/routes.py` 的 `generate_chapter`
- `backend/agents/workflow.py`

现有小说 full_pipeline 拓扑：

```text
context_loader
  -> writer
  -> critic ----------------\
  -> consistency_checker ----> human_review
       ^                      |
       |______________________|
          revise 时回到 writer
```

实现影响：

1. critic 与 consistency_checker 是 writer 后的并行 step。
2. human_review 后可能回到 writer，writer / critic / consistency_checker 可能出现多轮。
3. `ai_run_steps` 的 `step_order` 应表达“建议展示顺序”，不能当作唯一并发控制依据。
4. 幂等键必须包含 step_name 和轮次，例如 `run_id:consistency_checker:revision_0`。

### 7.1 生成开始时创建 run

在 `generate_chapter` 解析完 project、chapter/document、llm_config 后：

1. 创建 `AiRun`。
2. status = CREATED。
3. SSE 发出 `run_created`。
4. status 改 RUNNING。

run_type 映射：

| req.mode | run_type |
| --- | --- |
| full_pipeline | CHAPTER_DRAFT |
| continue | CHAPTER_CONTINUE |
| enhance | CHAPTER_REWRITE |
| summarize | SUMMARY_GENERATION |

文章项目可使用：

- DOCUMENT_DRAFT
- DOCUMENT_REWRITE
- DOCUMENT_REVIEW

### 7.2 ContextLoader step

当现有流程调用 `context_loader_node` 或 `build_chapter_context`：

1. start step：`build_context`。
2. 成功后 output 保存：
   - context stats
   - context package snapshot
   - formatted context hash
3. 失败后：
   - step FAILED
   - run FAILED 或降级空上下文继续，取决于 mode。

第一阶段建议：

- full_pipeline 中上下文失败可 FAILED。
- summarize/enhance 可以降级空上下文，但 step 记录 FAILED 或 SKIPPED_WITH_FALLBACK。

### 7.3 Writer step

writer 生成时：

1. start step：`generate_draft`。
2. 使用 `LoggedLLMProvider` 记录 LLM 调用。
3. 输出写入 step.output：
   - content_hash
   - word_count
   - generation_record_id
4. 创建 `GenerationRecord` 时写入 `run_id`。

注意：

- 保持现有流式 token 输出。
- 流式中断时，保存 partial output 到 step.output.partial_content。
- 客户端断开时 run 标记 CANCELLED，不创建正式版本。

### 7.4 Critic step

当前 critic 输出文本。

注意：

- Critic 与 ConsistencyChecker 在 full_pipeline 中并行执行。
- 如果后续把 step output 写入同一个 run 汇总对象，必须避免并发覆盖。

第一阶段：

- 继续保存文本到 `ai_run_steps.output.critiques`。

第二阶段：

- 改为结构化 JSON，或至少包装为：

```json
{
  "raw_text": "...",
  "issues": []
}
```

### 7.5 Consistency step

当前 consistency 输出文本。

注意：

- ConsistencyChecker 与 Critic 并行执行。
- HIGH severity 的阻断逻辑应在两个并行分支都结束、进入 human_review 前后统一判断，避免只等到其中一个 step 完成就提前提交。

改造目标：

- 输出 `GuardrailResult`。
- HIGH severity 进入 `WAITING_HUMAN`，不能自动 approve。

### 7.6 Human Review step

当 LangGraph 暂停在 `human_review`：

1. 创建或更新 step：`human_review`，status = WAITING_HUMAN。
2. run.status = WAITING_HUMAN。
3. 创建 `human_interrupts`。
4. 保存 payload：
   - thread_id
   - draft content hash
   - generation_record_id
   - guardrail_result
   - critique result
5. SSE 返回 `thread_id` 和 `run_id`。

### 7.7 Approve 后保存版本

当前 approve 逻辑调用：

- `save_chapter_content(..., source="ai_approve")`

改造：

- `save_chapter_content` 增加 `run_id` 参数。
- `create_version` 增加 `run_id / parent_version_id`。
- `chapter_versions.source` 写 `ai_approve`。
- `generation_records.status` 更新为 `applied`。
- `generation_records.accepted_version_id` 写入版本 id。
- run.status = COMPLETED。
- human_interrupt.resolved = true。

## 8. 前端改造

### 8.1 第一阶段最小改造

当前前端已有：

- `AgentPanel.vue`
- `AgentWorkflow.vue`
- `GenerationHistoryPanel.vue`
- `ApprovalModal.vue`

第一阶段只做最小变化：

1. 接收 `run_created` 事件，保存 `latestRunId`。
2. 生成历史中可显示 run id 或 trace 入口。
3. AgentWorkflow 每个步骤状态仍从 SSE 更新。
4. 可选新增“Run 详情”按钮。

### 8.2 Run 面板

后续新增组件：

- `frontend/src/components/AiRunPanel.vue`

功能：

1. 显示 run 状态。
2. 显示 steps。
3. 显示每步耗时。
4. 显示 token / cost。
5. 显示错误信息。
6. 显示 guardrail 结果。

### 8.3 生成历史与版本的关系

当前 `GenerationHistoryPanel` 显示候选稿。

改造后：

- 候选稿显示 `run_id`。
- 如果已应用，显示 `accepted_version_id`。
- 支持从候选稿跳转到 run steps。

## 9. 记忆系统适配

### 9.1 当前记忆现状

当前项目已有两套相关能力：

1. 创作工作台结构：`Character / WorldEntry / Outline / HiddenThread / CharacterEvent`。
2. 资料库结构化知识：`ExtractionJob / ExtractionStaging / CharacterProfile / AbilityProfile / EventTimeline / WorldRule`。

问题：

- `extraction_staging` 面向资料源抽取，不完全等于“章节确认后的新设定 staging”。
- 结构化知识已有 `origin / canon_level / source_priority`，但还不是用户确认闭环。
- Context Builder 目前优先读取工作台表，不完整读取 confirmed 结构化知识。

### 9.2 新增 writing_memory_staging

为避免和现有 `extraction_staging` 混淆，建议新增：

- `writing_memory_staging`

表结构建议：

```sql
CREATE TABLE writing_memory_staging (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL,
    run_id UUID,
    chapter_id UUID,
    chapter_version_id UUID,
    memory_type VARCHAR(50) NOT NULL,
    payload JSONB NOT NULL,
    evidence TEXT,
    status VARCHAR(50) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at TIMESTAMPTZ,
    reviewed_by VARCHAR(100)
);
```

status：

- GENERATED
- CONFIRMED
- REJECTED

### 9.3 confirmed 入库策略

用户确认后：

- 人物写入 `Character` 或 `CharacterProfile`。
- 世界规则写入 `WorldEntry` 或 `WorldRule`。
- 剧情事实写入 `EventTimeline` 或新增 `plot_fact`。

短期建议：

- 工作台写作优先使用现有 `Character / WorldEntry / CharacterEvent`。
- 资料库 QA 优先使用 `structured_knowledge`。
- 不要第一步强行合并两套模型。

中期再统一：

- `Character` 作为编辑友好的人物卡。
- `CharacterProfile` 作为抽取出来的结构化事实层。
- Context Builder 同时读取二者，manual / confirmed 优先。

## 10. 异常流要求

必须覆盖：

1. LLM 超时。
2. LLM 返回空内容。
3. LLM 流式输出中断。
4. JSON parse 失败。
5. 结构化输出字段缺失。
6. 上下文构建失败。
7. 客户端 SSE 断开。
8. 用户长时间未审核。
9. 同一 run 重复提交。
10. approve 保存版本失败。

处理规则：

1. step 失败必须写 `error_message`。
2. run 失败必须写 `error_message` 和 `current_step`。
3. 可重试 step 支持 retry_count。
4. JSON parse 失败最多自动修复一次。
5. 流式中断保存 partial output。
6. 用户未审核时保持 WAITING_HUMAN，不自动提交。
7. 重复 approve 必须幂等，不能创建多个正式版本。

幂等建议：

- `human_interrupt.resolved = true` 后再次提交直接返回当前结果。
- `ai_run_steps.idempotency_key` 防止重复创建同一 step。
- approve 创建版本前检查 run 是否已 COMPLETED。

## 11. Codex / GLM 执行约束

实现时必须遵守：

1. 先读当前文件，不要凭文档臆造架构。
2. 保留现有 API，不删除旧字段。
3. 数据库迁移必须兼容已有数据。
4. 不要一次性重写 `backend/api/routes.py`。
5. 优先新增 service，再在 routes 中小范围接入。
6. 前端新增 SSE 事件必须兼容旧事件。
7. 不要把 `generation_records` 改名为 `ai_runs`。
8. 不要删除 Langfuse，Langfuse 与本地日志并存。
9. 不要把 `extraction_staging` 直接当成写作 memory_staging 使用。
10. 每阶段都要补测试。

## 12. 阶段任务拆分

### 阶段 A：Harness Core 数据层

目标：

新增核心表和模型，不接入复杂流程。

任务：

1. 新增 `backend/models/ai_run.py`。
2. 新增 `backend/models/ai_run_step.py`。
3. 新增 `backend/models/llm_call_log.py`。
4. 新增 `backend/models/human_interrupt.py`。
5. 更新 `backend/models/__init__.py`。
6. 新增 Alembic migration。
7. `generation_records` 增加 `run_id`。
8. `chapter_versions` 增加 `project_id / run_id / parent_version_id / rollback_from_version_id`。

验收：

1. `pytest backend/tests/test_smoke.py` 不破坏现有测试。
2. Alembic upgrade 可以创建新表。
3. 老数据没有必填字段迁移失败。

### 阶段 B：Run Manager 接入 generate

目标：

每次生成都有 run。

任务：

1. 新增 `backend/harness/run_manager.py`。
2. 新增 `backend/harness/step_logger.py`。
3. 在 `generate_chapter` 创建 `AiRun`。
4. SSE 输出 `run_created`。
5. 在 context / writer / critic / consistency / human_review 节点写 `AiRunStep`。
6. 为并行 step 设计幂等键：`run_id:step_name:revision_round`。
7. 同步扩展前端 `SSEEventType` union，加入 `run_created / run_status / run_step`。
8. 生成失败时 run 标记 FAILED。
9. 客户端断开时 run 标记 CANCELLED。
10. `GenerationRecord` 创建时写入 run_id。

验收：

1. 发起一次生成后，数据库有一条 `ai_runs`。
2. 至少有 `build_context / generate_draft` 两条 `ai_run_steps`。
3. full_pipeline 中 critic 和 consistency_checker 可以并行写入 step，不重复、不互相覆盖。
4. 生成失败时 run 可查询到失败原因。
5. 旧前端仍能正常流式显示。

### 阶段 C：LLM Call Log 与 Snapshot

目标：

每次 LLM 调用可复盘。

任务：

1. 新增 `backend/harness/llm_call_logger.py`。
2. 包装 writer / critic / consistency 的 LLM 调用。
3. 记录 rendered prompt。
4. 记录 context package snapshot。
5. 记录 model config snapshot。
6. 尽量记录 token / latency。

验收：

1. 一次 full_pipeline 至少写入 writer / critic / consistency 的 LLM 调用日志。
2. 每条日志有 prompt snapshot。
3. 每条日志有关联 run_id 和 step_id。

### 阶段 D：Run API 与前端 Run 面板

目标：

用户可以查看 AI Run 过程。

任务：

1. 新增 `GET /api/ai-runs/{run_id}`。
2. 新增 `GET /api/ai-runs/{run_id}/steps`。
3. 新增 `GET /api/projects/{project_id}/ai-runs`。
4. 前端接收 `run_created`。
5. 新增或扩展 Run 状态展示。

验收：

1. 前端能看到本次 run id。
2. 后端能返回完整 steps。
3. 失败 run 能看到失败 step。

### 阶段 E：Human Review 持久化

目标：

WAITING_HUMAN 状态可恢复、可审计。

任务：

1. 写入 `human_interrupts`。
2. run.status = WAITING_HUMAN。
3. step.status = WAITING_HUMAN。
4. approve / reject / revise 更新 interrupt。
5. 新增 `POST /api/ai-runs/{run_id}/human-decisions`。
6. 处理重复 approve 幂等。

验收：

1. 生成暂停后数据库可查 interrupt。
2. approve 后 interrupt resolved。
3. reject 后 run 不创建正式版本。
4. 重复 approve 不创建多个版本。

### 阶段 F：版本审计增强

目标：

AI 输出和章节版本强关联。

任务：

1. `save_chapter_content` 支持 run_id。
2. `create_version` 支持 run_id / parent_version_id。
3. approve 创建版本后写 `generation_records.accepted_version_id`。
4. 取消或配置化 `MAX_VERSIONS_PER_CHAPTER` 自动删除。
5. 新增章节版本 restore/rollback 规则：创建 `rollback` 版本，而不是覆盖历史。

验收：

1. AI approve 后版本能查到 run_id。
2. generation record 能查到 accepted_version_id。
3. 回滚创建新版本，source = rollback。
4. 历史版本不被自动删除。

### 阶段 G：结构化 Guardrail

目标：

一致性检查从文本建议升级为可阻断规则。

任务：

1. 定义 `GuardrailResult` schema。
2. 修改 ConsistencyAgent prompt。
3. JSON parse 失败自动修复一次。
4. HIGH severity 阻止自动提交。
5. 前端展示结构化问题。

验收：

1. Consistency step output 是结构化 JSON。
2. HIGH 冲突时 run 进入 WAITING_HUMAN。
3. 用户可以看到冲突原因和建议。

### 阶段 H：写作记忆 staging

目标：

章节确认后抽取新设定，但不污染正式设定库。

任务：

1. 新增 `writing_memory_staging`。
2. 章节版本确认后触发 FactExtractionAgent。
3. 抽取人物、规则、事件、伏笔。
4. 写入 staging。
5. 前端提供确认/拒绝。
6. confirmed 后写入正式表。
7. Context Builder 优先读取 confirmed。

验收：

1. AI 抽取不会直接写入正式设定。
2. 用户确认后才进入 confirmed。
3. rejected 不进入上下文。

## 13. 测试建议

后端测试：

1. 创建 run。
2. 创建 step。
3. 生成成功后 run COMPLETED。
4. 生成失败后 run FAILED。
5. human review 后 run WAITING_HUMAN。
6. approve 后创建版本。
7. reject 后不创建版本。
8. LLM call log 写入 prompt snapshot。
9. 重复 approve 幂等。
10. rollback 创建新版本。

前端测试：

1. SSE 中未知事件不会报错。
2. `run_created` 能被保存。
3. AgentWorkflow 旧状态显示不受影响。
4. GenerationHistory 仍能查看候选稿。

## 14. 第一轮最小落地闭环

第一轮不要追求完整多 Agent 重构，只实现：

1. 创建 ai_run。
2. 记录 ai_run_step。
3. 记录 generation_record.run_id。
4. 提供 run 查询 API。
5. approve 后版本关联 run_id。

完成后的演示路径：

1. 创建项目。
2. 创建章节。
3. 发起章节生成。
4. 前端流式显示生成过程。
5. 后端 `ai_runs` 可查到 run。
6. 后端 `ai_run_steps` 可查到每个节点。
7. 生成候选稿在 `generation_records` 中。
8. approve 后创建 `chapter_versions`。
9. 版本能关联 run。
10. 原稿不被无确认覆盖。

## 15. 最终目标

改造完成后，当前项目应从“AI 创作功能平台”升级为“小说创作 AI Harness”：

1. 用户仍然拥有流畅的写作体验。
2. AI 每一步执行都有记录。
3. Prompt、上下文、输出、版本都可复盘。
4. 失败、中断、人工审核都有状态。
5. AI 不覆盖用户原稿。
6. AI 不直接污染正式设定库。
7. 多 Agent 不再只是流程展示，而是可审计、可恢复、可控成本的工程系统。
