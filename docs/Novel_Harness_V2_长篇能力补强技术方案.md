# Novel Harness V2 长篇能力补强技术方案

面向：GLM / Codex 开发执行

状态：设计方案，先审批，不直接实现

---

## 0. 一句话结论

当前项目已经完成 AI Harness、Expert System v2、Clarification Loop、Story Recorder、Memory Staging 与 Context Builder 的主闭环。

下一步不是重做小说系统，而是在现有闭环上补强长篇小说能力：

```text
章节连续性
→ 分卷 / 分幕 / Arc 结构
→ 伏笔状态追踪
→ 节奏控制
→ 角色弧线视图
→ 风格样本与反馈闭环
```

优先级最高的是：

1. `opening_anchor` 自动提取：把上一章末尾直接作为本章开篇承接锚点。
2. 分卷 / 分幕 / Story Arc：解决长篇中层规划缺失。
3. 伏笔状态追踪：让暗线从“涉及章节”升级为“埋设 / 活跃 / 回收”。
4. 节奏标记：让每章知道自己是铺垫、升级、反转、高潮还是余波。

---

## 1. 当前已有能力与真实缺口

### 1.1 已有能力

| 能力 | 当前实现 | 说明 |
|---|---|---|
| 前文摘要 | `backend/services/chapter_context.py::_load_previous_chapters` | 最近 3 章，每章前 300 字，注入 `## 前文摘要` |
| 章节任务卡 | `backend/agents/workflow_v2.py::chapter_architect_node` | `ChapterTaskCard` 已要求包含 `opening_anchor` |
| 角色事件 | `backend/models/character_event.py` | 已有 `event_summary / state_change / appearance_type / importance` |
| 剧情记录 | `backend/agents/story_recorder.py` | 提取事件、角色变化、关系变化、伏笔、时间线 |
| 记忆暂存 | `WritingMemoryStaging` | AI 候选记忆先进入 staging，用户确认后入正式上下文 |
| 已确认记忆注入 | `chapter_context.py::_load_confirmed_memories` | 按 `chapter_sequence_number <= 当前章节` 注入 `## 已确认记忆` |
| 暗线基础 | `HiddenThread.chapter_nums` | 能表达哪些章节涉及某条暗线 |
| 信息揭示规则 | `ChapterTaskCard.information_rules` | 已有 `forbidden / hint_only / may_reveal` |
| 资料库可选注入 | `include_knowledge_sources` | 默认不加入资料库，用户勾选才进入上下文 |

### 1.2 真实缺口

| 缺口 | 现状 | 改造方向 |
|---|---|---|
| 开篇承接不够强 | `opening_anchor` 由 LLM 从上下文推断 | 后端直接提取上一章末尾 500 字并注入 |
| 长篇中层结构缺失 | 只有章节序号，没有卷 / 幕 / Arc | 新增 StoryArc / Volume / Act 模型或字段 |
| 伏笔状态不明确 | `HiddenThread` 只有 `chapter_nums` | 增加 planted / active / revealed / closed 状态 |
| 节奏不可控 | `Outline` 没有 pacing 字段 | 给大纲加 pacing / tension_level |
| 角色弧线缺少视图 | 数据在 CharacterEvent 与 confirmed memory 里 | 做聚合查询与前端 Timeline，不先加复杂表 |
| 风格一致性较弱 | Project 有 `genre/style`，但无样本库 | 后续新增 StyleSample 或复用 project_sources 类型 |
| 读者反馈不闭环 | summarize 反馈不落 staging | 后续让反馈进入 staging，用户确认后成为改进约束 |
| 时间线精度不够 | confirmed memory 只按章节序号过滤 | 后续加 valid_from / valid_to 或 timeline 结构 |

---

## 2. 非目标

本轮补强不要做以下事情：

- 不新建 `.crazy_writer_memory/` 或任何文件型私有记忆系统。
- 不绕开 `WritingMemoryStaging` 直接污染正式设定库。
- 不让 LLM 自由决定 workflow DAG。
- 不推翻现有 `ChapterContext / AiRun / AiRunStep / LlmCallLog / HumanInterrupt`。
- 不删除旧专家、旧 run、旧 version 的可追溯数据。
- 不把所有功能一次性做成复杂平台，继续阶段化推进。

---

## 3. 阶段 K-1：Opening Anchor 自动提取

### 3.1 目标

让每章开头能自然承接上一章结尾，减少“每章都像重新开局”。

当前已有：

- `ChapterTaskCard.opening_anchor`
- `## 前文摘要`

但缺少：

- 明确的“上一章最后一段 / 最后 500 字”锚点。

### 3.2 后端设计

修改 `backend/services/chapter_context.py`。

新增 dataclass：

```python
@dataclass
class PreviousChapterEndingInfo:
    id: str
    sequence_number: int
    title: str
    ending_text: str
```

`ChapterContext` 新增字段：

```python
previous_chapter_ending: PreviousChapterEndingInfo | None = None
```

新增加载函数：

```python
async def _load_previous_chapter_ending(
    db: AsyncSession,
    project_id: str,
    current_seq: int,
    ctx: ChapterContext,
    *,
    max_chars: int = 500,
) -> None:
    ...
```

查询规则：

1. 优先查 `sequence_number == current_seq - 1`。
2. 如果缺章，则查 `sequence_number < current_seq` 的最近一章。
3. 正文为空则不注入。
4. 取 `content.strip()[-500:]`，不是开头 500 字。

`format_chapter_context_for_prompt()` 增加 section：

```text
## 上章结尾锚点
第 N 章《标题》的结尾：
...
```

位置建议：

```text
## 当前章节
## 本章大纲
## 上章结尾锚点
## 明线推进
## 本章角色
...
```

原因：architect 最先需要用它决定开篇方式。

### 3.3 Workflow v2 设计

修改 `backend/agents/workflow_v2.py::chapter_architect_node`。

在 system/base prompt 中强化：

```text
如果上下文中存在“上章结尾锚点”，ChapterTaskCard.opening_anchor 必须明确说明本章开头如何承接该锚点。
不得无视上章末尾另起新场景，除非本章大纲明确要求跳切，并需说明跳切方式。
```

在 user prompt 中无需单独拼，因为 `context` 已包含 `## 上章结尾锚点`。

`chapter_writer_node` 不需要重新提取上一章，它只消费任务卡与上下文。

### 3.4 测试

新增 / 修改：

- `backend/tests/test_chapter_context.py`
  - `test_previous_chapter_ending_uses_tail_text`
  - `test_previous_chapter_ending_falls_back_to_latest_previous_chapter`
  - `test_previous_chapter_ending_omitted_when_no_previous_content`
  - 断言 prompt 包含 `## 上章结尾锚点`
  - 断言取的是正文末尾，不是正文开头
- `backend/tests/test_workflow_v2.py`
  - architect prompt 中包含 opening_anchor 强约束
  - mock LLM 下 `chapter_task_card.opening_anchor` 不为空

### 3.5 验收标准

- 第 2 章生成上下文中包含第 1 章末尾 500 字。
- 第 1 章无上一章时不出现该 section。
- 章节缺号时能回退到最近的前一章。
- `ChapterTaskCard.opening_anchor` 明确承接上章末尾。
- 不新增 migration。

---

## 4. 阶段 K-2：分卷 / 分幕 / Story Arc 结构

### 4.1 目标

解决长篇小说只有 chapter 维度的问题，让系统知道当前章节属于哪个长线阶段。

示例：

```text
第一卷：觉醒与入学
  Arc 1：穿越适应
  Arc 2：觉醒仪式
  Arc 3：第一次危机
```

### 4.2 数据模型

新增模型 `backend/models/story_arc.py`。

建议表：`story_arcs`

```text
id                  UUID PK
project_id          UUID FK projects.id, indexed
parent_arc_id        UUID nullable, indexed
arc_type             String(20) not null   -- VOLUME / ACT / ARC
name                 String(200) not null
summary              Text nullable
goal                 Text nullable         -- 本卷/本幕/本 arc 的目标
main_conflict        Text nullable
start_chapter        Integer nullable, indexed
end_chapter          Integer nullable, indexed
order_index          Integer not null default 0
status               String(20) not null default 'PLANNED'
metadata             JSON nullable
created_at
updated_at
```

状态枚举：

```text
PLANNED
ACTIVE
COMPLETED
PAUSED
ARCHIVED
```

`outlines` 增加：

```text
story_arc_id         UUID nullable, indexed
arc_position         String(30) nullable   -- SETUP / BUILDUP / TURNING_POINT / CLIMAX / AFTERMATH
```

迁移：

```text
024_story_arcs.py
```

### 4.3 Context Builder

`ChapterContext` 新增：

```python
story_arcs: list[StoryArcInfo]
```

加载规则：

1. 优先通过当前章 `Outline.story_arc_id` 查所属 arc。
2. 同时查 `start_chapter <= 当前章 <= end_chapter` 的 arc。
3. 返回顺序：Volume → Act → Arc。
4. prompt 注入：

```text
## 当前长线结构
- [VOLUME] 第一卷：觉醒与入学
  目标：...
  主冲突：...
- [ARC] 觉醒仪式
  当前阶段：BUILDUP
```

### 4.4 API

新增：

```text
GET    /api/projects/{project_id}/story-arcs
POST   /api/projects/{project_id}/story-arcs
PATCH  /api/projects/{project_id}/story-arcs/{arc_id}
DELETE /api/projects/{project_id}/story-arcs/{arc_id}
```

可选：

```text
POST /api/projects/{project_id}/story-arcs/reorder
```

### 4.5 前端

第一版不要做复杂拖拽，先做可用 UI：

- 在章节资料弹窗 `ChapterConfig.vue` 中增加“所属长线”选择。
- 新增一个“长线结构”管理入口，可放在资料侧栏或项目配置里。
- 展示 Volume / Act / Arc 的层级列表。

### 4.6 测试

- `test_story_arcs.py`
  - CRUD
  - 跨用户 404
  - arc 范围查询
  - outline 关联 arc
- `test_chapter_context.py`
  - 当前章注入所属 StoryArc
  - 超出章节范围不注入
- 前端源码契约测试：
  - `ChapterConfig` 有 StoryArc 选择
  - API client 有 storyArc CRUD

---

## 5. 阶段 K-3：伏笔状态追踪

### 5.1 目标

把 `HiddenThread` 从“涉及哪些章节”升级为可追踪生命周期：

```text
计划中 → 已埋设 → 活跃中 → 已回收 → 已关闭
```

### 5.2 数据模型

修改 `backend/models/hidden_thread.py`。

新增字段：

```text
status                String(20) default 'PLANNED', indexed
thread_type           String(30) nullable       -- FORESHADOWING / SECRET / RELATIONSHIP / WORLD_RULE
planted_chapter       Integer nullable, indexed
reveal_chapter        Integer nullable, indexed
resolved_chapter      Integer nullable, indexed
payoff_summary        Text nullable
risk_level            String(20) nullable       -- LOW / MEDIUM / HIGH
```

迁移：

```text
025_hidden_thread_status.py
```

状态枚举：

```text
PLANNED
PLANTED
ACTIVE
REVEALED
RESOLVED
DROPPED
```

### 5.3 Story Recorder / Memory Curator

已有：

- `story_recorder` 提取 `foreshadowing_new`
- `story_recorder` 提取 `foreshadowing_resolved`
- `memory_curator` 可转成 `FORESHADOWING` staging

补强：

- `FORESHADOWING` staging payload 增加建议字段：

```json
{
  "thread_name": "...",
  "status_delta": "PLANTED | REVEALED | RESOLVED",
  "planted_chapter": 3,
  "reveal_chapter": 8,
  "evidence": "..."
}
```

`confirm_staging_item()`：

- `status_delta=PLANTED`：创建或更新 `HiddenThread.status=PLANTED`
- `status_delta=REVEALED`：更新 `reveal_chapter`
- `status_delta=RESOLVED`：更新 `resolved_chapter/status=RESOLVED/payoff_summary`

### 5.4 Context Builder

注入 active hidden threads 时区分：

```text
## 暗线
- [ACTIVE] 某秘密：已在第 3 章埋设，预计第 8 章回收
- [PLANNED] 某伏笔：本章可轻微提示，不可揭示
```

如果当前章接近 `reveal_chapter`，给 architect 增加提醒：

```text
本章接近伏笔回收章节，请根据大纲决定是否进入 may_reveal。
```

### 5.5 测试

- HiddenThread 新字段模型测试。
- migration upgrade/downgrade。
- `confirm_staging_item` 对 FORESHADOWING 的状态更新测试。
- Context Builder 只注入相关状态。
- `PLOT_FACT` 仍不硬塞 CharacterEvent。

---

## 6. 阶段 K-4：节奏标记与章节 pacing

### 6.1 目标

让每一章有明确节奏职责，避免所有章节都写成同一种“持续推进”。

### 6.2 数据模型

`outlines` 新增：

```text
pacing              String(30) nullable, indexed
tension_level       Integer nullable
target_scene_count  Integer nullable
```

枚举建议：

```text
SETUP          铺垫
BUILDUP        升级
REVERSAL       反转
CLIMAX         高潮
AFTERMATH      余波
TRANSITION     过渡
SLICE          日常
```

迁移：

```text
026_outline_pacing.py
```

### 6.3 Architect Prompt

`chapter_architect_node` 增强：

```text
如果上下文中包含 pacing，请让 ChapterTaskCard.scenes 与 tension_design 匹配该节奏。
不得把 SETUP 写成高潮章，也不得把 AFTERMATH 写成新主冲突爆发。
```

`ChapterTaskCard` 建议增加：

```json
{
  "pacing": "BUILDUP",
  "tension_curve": ["low", "medium", "high"],
  "scene_count": 4
}
```

第一版不一定改 DB 存任务卡 schema，只要求 LLM 输出结构并进入 `AiRunStep.output`。

### 6.4 前端

在章节资料 / 大纲编辑处增加：

- 节奏类型选择
- 张力等级 1-5
- 目标场景数

### 6.5 测试

- Outline 模型字段。
- Context Builder prompt 包含 pacing。
- Architect prompt 包含 pacing 约束。
- 前端 `ChapterConfig` 或 outline form 包含 pacing 控件。

---

## 7. 阶段 K-5：角色弧线聚合视图

### 7.1 目标

不急着加新表，先把已有数据变成可读视图：

- `CharacterEvent.state_change`
- `CharacterEvent.event_summary`
- confirmed staging 中的 `CHARACTER / EVENT`
- `story_recorder.character_state_changes`
- `story_recorder.relationship_changes`

### 7.2 API

新增：

```text
GET /api/projects/{project_id}/characters/{character_id}/arc?to_chapter=10
```

返回：

```json
{
  "character_id": "...",
  "name": "程旋",
  "items": [
    {
      "chapter_sequence_number": 1,
      "source_type": "CharacterEvent",
      "title": "首次觉醒",
      "summary": "...",
      "state_change": "...",
      "confidence": "confirmed"
    }
  ]
}
```

### 7.3 前端

在角色详情中增加“角色弧线”面板：

```text
第 1 章：初到异世界，身份不稳
第 2 章：觉醒仪式前焦虑
第 3 章：第一次暴露能力
```

### 7.4 测试

- 聚合 CharacterEvent。
- 聚合 confirmed memory。
- 不返回未来章节。
- 跨用户 404。

---

## 8. 后续阶段：暂不作为第一批

### K-6 风格样本库

真实缺口，但建议排在 K-1 到 K-5 后。

可选设计：

- 新表 `style_samples`
- 或复用 `project_sources.source_type = style_sample`
- Context Builder 在用户勾选“加入风格样本”时注入

注意：不要默认注入，避免污染正文。

### K-7 读者反馈闭环

把 summarize / 读者视角结果写入 `WritingMemoryStaging`。

建议新增 memory type：

```text
READER_FEEDBACK
```

或先用 `PLOT_FACT` payload 标注：

```json
{
  "feedback_type": "reader_confusion",
  "suggestion": "..."
}
```

用户确认后，Context Builder 可在下一章注入：

```text
## 已确认读者反馈
```

### K-8 场景库 / Location

新增 `locations` 表前，先观察是否真的需要。

如果做：

```text
locations:
  project_id
  name
  location_type
  description
  atmosphere
  rules
  first_chapter
  last_seen_chapter
```

---

## 9. 推荐执行顺序

| 阶段 | 内容 | 迁移 | 风险 | 收益 |
|---|---|---:|---|---|
| K-1 | Opening Anchor 自动提取 | 否 | 低 | 高 |
| K-2 | Story Arc / 分卷分幕 | 是，024 | 中 | 高 |
| K-3 | 伏笔状态追踪 | 是，025 | 中 | 高 |
| K-4 | Outline pacing | 是，026 | 低 | 中高 |
| K-5 | 角色弧线聚合视图 | 否或少量 | 低 | 中 |
| K-6 | 风格样本库 | 待定 | 中 | 中 |
| K-7 | 读者反馈闭环 | 待定 | 中 | 中 |
| K-8 | 场景库 | 是 | 中 | 中 |

第一轮建议只执行：

```text
K-1
```

K-1 不需要 migration，改动面小，最容易人工验收。

---

## 10. K-1 详细开发任务

### K-1-T1：ChapterContext 增加 previous_chapter_ending

文件：

- `backend/services/chapter_context.py`
- `backend/tests/test_chapter_context.py`

任务：

1. 新增 `PreviousChapterEndingInfo` dataclass。
2. `ChapterContext` 增加 `previous_chapter_ending`。
3. 新增 `_load_previous_chapter_ending()`。
4. `build_chapter_context()` 在 `_load_previous_chapters()` 后调用。
5. `format_chapter_context_for_prompt()` 增加 `## 上章结尾锚点`。
6. 更新 `to_dict()` 如果当前 `ChapterContext` 有序列化方法。

验收：

- 第 2 章能取到第 1 章正文末尾。
- 取末尾 500 字，不取开头。
- 第 1 章不报错。
- prompt section 顺序正确。

### K-1-T2：Architect 强化 opening_anchor 约束

文件：

- `backend/agents/workflow_v2.py`
- `backend/agents/llm_provider.py`
- `backend/tests/test_workflow_v2.py`

任务：

1. 修改 `chapter_architect_node` base prompt。
2. 要求 `opening_anchor` 必须基于 `## 上章结尾锚点`。
3. MockProvider 的 ChapterTaskCard 响应保持包含 opening_anchor。
4. 测试 architect prompt / output。

验收：

- 有上章锚点时，architect prompt 明确要求承接。
- Mock 下 `chapter_task_card.opening_anchor` 存在。
- 不影响 full_pipeline 既有测试。

### K-1-T3：人工验收

准备测试数据：

```text
第 1 章结尾：手机屏幕亮起，“天澜魔法高中——觉醒仪式 08:30”
第 2 章大纲：主角进入天澜魔法高中，参加觉醒仪式
```

预期：

- 第 2 章上下文中出现 `## 上章结尾锚点`。
- 第 2 章任务卡 `opening_anchor` 提到手机提醒 / 觉醒仪式。
- 正文开头自然承接提醒，而不是重新从宿舍醒来。

---

## 11. 全局测试建议

每个阶段至少跑：

```bash
pytest backend/tests/test_chapter_context.py
pytest backend/tests/test_workflow_v2.py
pytest backend/tests/test_smoke.py
cd frontend && npm test
cd frontend && npm run build
```

涉及 migration 的阶段额外跑：

```bash
cd backend
alembic heads
alembic upgrade head
alembic downgrade -1
alembic upgrade head
```

Postgres 迁移验证优先于 SQLite，因为历史迁移里已有 PG 专用类型。

---

## 12. 风险与约束

### 12.1 不要让上下文过长

K-1 只加入上一章末尾 500 字。

不要把上一章全文塞进 prompt。

### 12.2 不要让资料库再次默认注入

资料库仍然遵守：

```text
include_knowledge_sources = false by default
```

`opening_anchor` 来自章节正文，不属于资料库。

### 12.3 不要让 writer 自己决定承接规则

承接规则应由：

```text
Context Builder
→ chapter-architect
→ ChapterTaskCard.opening_anchor
→ chapter-writer
```

writer 只消费任务卡，不重新规划。

### 12.4 不要把 PLOT_FACT 硬塞 CharacterEvent

继续保留当前规则：

- EVENT 有 `character_name` 才写 CharacterEvent。
- PLOT_FACT 保持 staging / confirmed memory 注入。

### 12.5 保持审计链

所有新增结构化输出仍然通过：

- `AiRun`
- `AiRunStep`
- `LlmCallLog`
- prompt snapshot
- context snapshot

可追踪、可复盘。

---

## 13. 最终验收口径

这轮 V2 补强完成后，用户应该能明显感受到：

1. 新章节开头会自然承接上一章末尾。
2. 长篇有分卷 / 分幕 / Arc 目标，不再只靠单章大纲。
3. 伏笔有生命周期，不只是“哪些章涉及”。
4. 每章节奏有明确职责。
5. 角色变化能跨章查看。

第一批完成 K-1 后就应该先人工验收，不要等 K-2/K-3 全做完才验证方向。

