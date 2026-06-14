# DeepSeek 执行指导：章节助手与资料库改造

## 0. 任务背景

当前项目已经完成了章节配置、角色事件、章节记忆、Agent 面板初步改造。下一步目标不是继续堆 UI，而是让“章节助手”真正基于当前章节资料工作，并为后续“每部小说独立资料库 + RAG 问答 + 同人二创”打地基。

核心产品方向：

```text
每部小说 = 独立知识库 + 章节创作工作台
```

执行时请优先阅读：

- `小说资料库与章节助手改造方案.md`
- `backend/api/routes.py`
- `backend/agents/workflow.py`
- `backend/services/structure_extraction.py`
- `frontend/src/components/AgentPanel.vue`
- `frontend/src/components/ChapterConfig.vue`
- `frontend/src/views/Workspace.vue`
- `frontend/src/stores/index.ts`
- `frontend/src/api/types.ts`
- `frontend/src/api/client.ts`

## 1. 总体原则

### 1.1 不要双写结构化数据

不要把章节、角色、角色事件、设定、暗线、大纲同步到 `project_sources`。

这些数据已经在结构化表中：

- `chapters`
- `outlines`
- `characters`
- `character_events`
- `world_entries`
- `hidden_threads`

生成和问答时应直接查原表。

`project_sources` 只用于：

- 用户上传资料
- 用户笔记
- 同人规则
- 时间线文本
- 外部参考资料

### 1.2 ChapterContextService 是现有上下文系统升级

不要引入第三套孤立上下文系统。

应让：

```text
agents.workflow.context_loader_node
  → 调用 services.chapter_context.ChapterContextService
  → 内部按需调用 rag/context_loader.py 或 ProjectKnowledgeService
```

现有 selected IDs 精确加载能力要保留。

### 1.3 走向选择先做独立 API

不要第一版就做 LangGraph 多 interrupt。

先做：

```text
POST /projects/{project_id}/chapters/{sequence_number}/directions
```

返回走向选项后，前端再调用现有 `/chapters/generate`，传入用户选择。

### 1.4 同人规则 always inject

同人规则是硬约束，不应该靠 RAG 命中。

规则：

- `source_type = fanfic_rule`
- `always_inject = true`
- 每次生成、续写、润色、读者视角、走向建议都应加载

### 1.5 先保守实现，避免大爆炸

不要一次性完成完整资料库、RAG 问答、多 interrupt、复杂 UI 全部功能。

推荐先做 MVP：

```text
1. ChapterContextService
2. 章节助手资料统计
3. 走向选择独立 API
4. 前端 DirectionPicker
```

## 2. 推荐实施顺序

### 阶段一：ChapterContextService

目标：让章节助手的所有 AI 动作都能拿到当前章节资料包。

新增文件：

```text
backend/services/chapter_context.py
```

建议核心方法：

```python
async def build_chapter_context(
    db,
    project_id,
    chapter_sequence_number: int,
    intent: str = "generate",
    user_query: str | None = None,
    selected_outline_ids: list[str] | None = None,
    selected_character_ids: list[str] | None = None,
    selected_world_entry_ids: list[str] | None = None,
    selected_hidden_thread_ids: list[str] | None = None,
) -> dict:
    ...
```

返回结构建议：

```json
{
  "chapter": {
    "id": "...",
    "sequence_number": 6,
    "title": "硬刚穆卓云！",
    "content": "..."
  },
  "outline": {
    "id": "...",
    "title": "...",
    "summary": "...",
    "light_line": "..."
  },
  "characters": [],
  "character_events": [],
  "hidden_threads": [],
  "world_entries": [],
  "fanfic_rules": [],
  "retrieved_sources": [],
  "previous_chapters": [],
  "stats": {
    "characters": 0,
    "events": 0,
    "hidden_threads": 0,
    "world_entries": 0,
    "sources": 0
  }
}
```

加载规则：

- 当前章节：`Chapter.sequence_number == chapter_sequence_number`
- 章节大纲：`Outline.sequence_number == chapter_sequence_number`
- 明线：短期使用 `Outline.turning_point`
- 本章事件：`CharacterEvent.chapter_sequence_number == chapter_sequence_number`
- 本章角色：由本章事件反查 `Character`
- 暗线：`HiddenThread.chapter_nums` 包含当前章节
- 全局设定：`WorldEntry.scope_type == "global"`
- 本章设定：如果已有 `chapter_sequence_number` 字段则按章查；没有则先返回所有 `scope_type == "chapter"`，并在 TODO 中标明后续补字段
- 前文：最近 1-3 章，排除当前章之后的章节
- selected IDs：如果用户显式传了 selected IDs，则优先加载这些条目并追加到上下文

请同时提供一个格式化方法：

```python
def format_chapter_context_for_prompt(context: dict) -> str:
    ...
```

输出 sections：

```text
## 当前章节
## 本章大纲
## 明线推进
## 本章角色
## 本章角色事件
## 暗线
## 相关设定
## 同人规则
## 前文摘要
## 检索资料
```

验收：

- 单元测试能验证 build_chapter_context 返回本章角色事件。
- prompt 文本中包含本章角色事件和暗线。
- 没有资料时不报错，返回空数组和合理 stats。

### 阶段二：接入 generate 上下文

目标：`/chapters/generate` 使用 `ChapterContextService`。

修改：

- `backend/api/routes.py`
- `backend/agents/workflow.py`

要求：

1. `generate_chapter` 中构造 `CreativeState` 时，传入 `chapter_num` 或上下文信息。
2. `context_loader_node` 内部优先调用 `ChapterContextService`。
3. 保留原来的 selected IDs 精确加载行为。
4. 保留旧 RAG fallback，避免破坏现有文章模式和测试。

注意：

- 文章模式不要强行套小说上下文。
- `enhance` 仍然是改写当前章节，不得续写。
- `summarize` 仍然不落库。

验收：

- `backend/venv/bin/python -m pytest -q` 通过。
- 现有 `full_pipeline / continue / enhance / summarize` 流程不破。
- Mock provider 下生成内容能从 prompt 中读到当前章节大纲/角色。

### 阶段三：章节助手资料统计

目标：右侧章节助手显示当前章节资料包状态。

新增接口建议：

```text
GET /projects/{project_id}/chapters/{sequence_number}/context
```

返回 `ChapterContextService` 的精简结构：

```json
{
  "stats": {
    "characters": 4,
    "events": 5,
    "hidden_threads": 1,
    "world_entries": 3,
    "sources": 0
  },
  "chapter_goal": {
    "outline": "...",
    "light_line": "..."
  }
}
```

前端改动：

- 新增 `api.getChapterContext(projectId, chapterNum)`
- 新增 store 或 composable：`useChapterContextStats`
- `AgentPanel.vue` 显示：

```text
已加载：角色 4 · 事件 5 · 暗线 1 · 设定 3
```

验收：

- 切换章节后统计刷新。
- 无资料时显示 `暂无章节资料` 或 `已加载：0`，不空白崩溃。

### 阶段四：走向选择独立 API

目标：点击“章节生成”后，先询问用户小说走向。

新增接口：

```text
POST /projects/{project_id}/chapters/{sequence_number}/directions
```

请求：

```json
{
  "user_note": "可选，用户补充想法"
}
```

响应：

```json
{
  "chapter_goal": {
    "summary": "本章应推进测试冲突，突出穆白与主角的对照。",
    "constraints": [
      "不得改变已记录角色事件",
      "不得引入未设定的新世界规则"
    ]
  },
  "directions": [
    {
      "id": "A",
      "title": "爽点爆发",
      "description": "主角当场打破质疑，制造强烈反差。",
      "risk": "冲突消耗较快，需要结尾留钩子。"
    }
  ]
}
```

后端实现：

- 使用 `ChapterContextService` 构造上下文。
- 调用 LLM 输出 JSON。
- 做容错解析。
- MockProvider 也要支持此类请求，返回稳定 mock directions。

前端实现：

- 新增 `DirectionPicker.vue`
- 点击主按钮“章节生成”时：
  1. 请求 directions
  2. 弹出 DirectionPicker
  3. 用户选择方向 + 补充要求
  4. 调用现有 `generateStream`，传 `selected_direction` / `user_note`

需要改 schema/types：

- backend `GenerateRequest`
- frontend `GenerateRequest`

字段：

```text
selected_direction: str | None
direction_option_id: str | None
```

验收：

- 生成前必须出现走向选择。
- 用户选择的方向进入 generate prompt。
- 取消选择不触发生成。
- 快捷操作“润色/读者视角”不走 direction picker。

### 阶段五：AgentPanel 拆分

如果前面实施过程中发现 `AgentPanel.vue` 过于臃肿，请先拆分。

建议拆分：

```text
frontend/src/components/agent/AgentCommandCard.vue
frontend/src/components/agent/AgentQuickActions.vue
frontend/src/components/agent/AgentOutputPanel.vue
frontend/src/components/agent/AgentReviewNotes.vue
frontend/src/components/agent/DirectionPicker.vue
frontend/src/composables/useGenerationFlow.ts
frontend/src/composables/useChapterContextStats.ts
frontend/src/composables/useDirectionOptions.ts
```

拆分原则：

- `AgentPanel.vue` 只保留布局和组合。
- SSE 事件处理放进 `useGenerationFlow`。
- 章节上下文统计放进 `useChapterContextStats`。
- 走向选择请求和状态放进 `useDirectionOptions`。

验收：

- 拆分后 `npm run build` 通过。
- 现有生成、润色、转折、读者视角行为不变。

## 3. 后续资料库阶段

资料库不是第一阶段必须完成的内容。

后续新增：

```text
project_sources
project_source_chunks
```

只保存：

- upload
- fanfic_rule
- timeline
- note
- reference

不要保存：

- chapter
- outline
- character
- character_event
- world_entry
- hidden_thread

第一版检索建议：

- 先做全文检索。
- mock embedding 不作为语义检索验收标准。
- 真实 embedding 接入后再做向量检索。

资料问答接口后置：

```text
POST /projects/{project_id}/knowledge/search
POST /projects/{project_id}/knowledge/ask
```

问答逻辑：

- 角色/事件/章节类问题优先 SQL 查询结构化表。
- 上传资料/笔记/参考资料走全文/向量检索。
- 同人规则 always inject。
- 回答必须带 citations。

## 4. 需要避免的坑

### 4.1 不要破坏文章模式

文章模式走 `/documents/generate`，不要强行加载小说章节资料。

### 4.2 不要让 enhance 变成续写

润色必须只改写当前章节，不新增剧情、不改结尾、不续写后续。

### 4.3 不要依赖 mock embedding 质量

mock embedding 只能证明流程可跑，不能证明检索效果。

### 4.4 不要让 direction picker 影响快捷操作

只有“章节生成”走方向选择。

以下操作不走：

- 转折建议
- 润色
- 读者视角

### 4.5 不要一次性引入多 interrupt

完整多节点 LangGraph 可以后置。

第一版只做独立 directions API。

## 5. 测试要求

后端：

```bash
cd backend
venv/bin/python -m pytest -q
```

前端：

```bash
cd frontend
npm run build
```

重点测试：

- 章节上下文能加载角色事件
- 无角色事件时不报错
- 章节生成不破
- 润色不续写
- 读者视角不落库
- directions API 返回稳定 JSON
- 前端取消 direction picker 不触发生成

## 6. 建议提交拆分

请不要把所有内容塞进一个巨大提交。

建议拆成：

```text
commit 1: Add chapter context service
commit 2: Use chapter context in generation
commit 3: Show chapter context stats in agent panel
commit 4: Add direction options API
commit 5: Add direction picker UI
```

每个提交都应保证：

- 前端 build 通过，或者该提交不涉及前端。
- 后端测试通过，或者该提交只改文档/UI。

## 7. 最小可交付版本

如果时间有限，优先交付：

```text
1. ChapterContextService
2. generate 使用章节上下文
3. 章节助手显示资料统计
4. directions API
5. DirectionPicker UI
```

不要优先做：

- 完整资料库页面
- 完整 RAG 问答
- 多 interrupt LangGraph
- 复杂上传和 embedding 管线

这些放到第二阶段。

