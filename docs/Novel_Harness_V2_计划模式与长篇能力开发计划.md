# Novel Harness V2 计划模式与长篇能力开发计划

面向：GLM / Codex 开发执行

状态：新权威计划。本文合并并取代以下旧方案：

- `docs/Clarification_Loop_多轮澄清改造技术文档.md`
- `docs/Novel_Harness_V2_长篇能力补强技术方案.md`

---

## 0. 一句话结论

小说 Harness 下一步不应该继续堆“多问几轮”的独立澄清弹窗，而应该把生成前控制权收束到一个核心产品动作：

```text
AI 先给出章节任务卡
→ 用户能看、能改、能确认
→ writer 严格按确认后的任务卡写正文
```

也就是说，真正的小说“计划模式”不是让 AI 像聊天一样一直问，而是让 AI 在动笔前把“准备怎么写”讲清楚。

---

## 1. 当前基线

当前项目已经完成：

- AI Harness A-H：run / step / llm log / human interrupt / version / memory staging。
- Expert System v2 I-1~I-6：architect / writer / critic / editor / continuity / story-recorder / memory-curator。
- Clarification Loop J-1~J-5：clarification-planner、HumanInterrupt、前端 ClarificationPanel。
- 长篇补强 K-1：Opening Anchor 自动提取，生成下一章时注入 `## 上章结尾锚点`。

当前真实缺口：

| 缺口 | 说明 |
|---|---|
| 任务卡不可见 | `ChapterTaskCard` 已生成，但用户不能在 writer 执行前确认或修改 |
| 澄清与计划分离 | 当前 Clarification Loop 是独立问题面板，容易和任务卡预览形成两层打扰 |
| 长篇中层结构不足 | 没有分卷 / 分幕 / Story Arc |
| 伏笔生命周期不足 | `HiddenThread` 只有涉及章节，缺少埋设 / 活跃 / 回收状态 |
| 节奏控制不足 | `Outline` 缺少 pacing / tension / scene_count |
| 角色弧线缺少视图 | 数据已在 CharacterEvent / confirmed memory 中，但缺少聚合查询和 UI |

---

## 2. 产品原则

### 2.1 计划可见优先于多轮追问

用户最需要的是知道 AI 准备怎么写，而不是被 AI 反复问问题。

推荐体验：

```text
用户点击“章节生成”
→ Context Builder 构建上下文
→ chapter-architect 生成 ChapterTaskCard
→ 前端展示“章节生成计划”
→ 用户确认 / 修改 / 取消
→ chapter-writer 写正文
```

### 2.2 澄清问题嵌入任务卡，不再单独形成第二层流程

不要做成：

```text
问问题 → 回答 → 再看任务卡 → 再确认 → 再生成
```

应该做成：

```text
任务卡预览
  + 可选澄清问题
  + 可编辑计划字段
  + 查看上下文摘要
```

### 2.3 澄清基于规则触发，LLM 只做辅助

不要让 LLM 自由决定每次问什么，否则容易过度澄清或完全不澄清。

第一版规则示例：

```python
if current_chapter_has_no_outline:
    ask("本章核心推进目标是什么？")

if previous_ending_exists and task_card.opening_anchor_is_weak:
    ask("本章开头如何承接上章？")

if user_instruction_has_vague_terms(["爽一点", "节奏快点", "更压抑"]):
    ask("这次最想强化哪一类效果？")

if multiple_active_characters and no_pov:
    ask("本章主要视角跟随谁？")
```

每次最多 3 个问题。用户可以跳过。

---

## 3. 目标工作流

标准章节生成目标链路：

```text
Context Builder
→ chapter_architect
→ task_card_review          # 新增：生成前任务卡预览 / 可编辑 / 可选澄清
→ chapter_writer
→ structural_critic
→ narrative_editor
→ continuity_checker
→ final_review              # 现有：生成后人工审核
→ approve
→ story_recorder
→ memory_curator
→ writing_memory_staging
```

边界：

- `task_card_review` 不保存章节版本。
- `final_review` 才决定是否保存正文版本。
- 澄清回答默认只影响本次生成，不直接写入正式记忆库。
- 正文 approve 后仍通过 story-recorder / memory-curator 进入 staging。

---

## 4. Phase L-1：TaskCard Preview 可见可编辑

### 4.1 目标

让用户在 AI 动笔前看到并修改 `ChapterTaskCard`。

这是下一步最高优先级。

### 4.2 后端改造

文件建议：

```text
backend/agents/workflow_v2.py
backend/services/task_card_review.py          # 新增，可选
backend/api/routes.py
backend/schemas/api.py
backend/tests/test_task_card_review.py        # 新增
backend/tests/test_workflow_v2.py
```

`CreativeStateV2` 新增字段：

```python
task_card_reviewed: bool
task_card_review_action: str | None       # confirm / edit / cancel
task_card_review_notes: str | None
```

新增节点：

```python
human_task_card_review_node
```

节点职责：

1. 读取 state 中的 `chapter_task_card`。
2. 创建 `HumanInterrupt`，payload type 为 `task_card_review`。
3. payload 包含：
   - `task_card`
   - `context_summary`
   - `source_summary`
   - `previous_chapter_ending`
   - `optional_questions`，L-1 可为空
4. run 状态进入 `WAITING_HUMAN`。

`build_creative_graph_v2()` 改为：

```text
context_loader
→ clarification_planner   # 现有，可先保留
→ chapter_architect
→ human_task_card_review
→ chapter_writer
→ structural_critic
→ narrative_editor
→ continuity_checker
→ human_review
```

第一版可以保留现有 clarification 节点，但如果它触发过多问题，应在 L-1 后改为 L-2 的嵌入式策略。

### 4.3 Resume / API

推荐新增专用 API，避免和 final_review 的 `/human-decisions` 混淆：

```text
GET  /api/ai-runs/{run_id}/task-card-review
POST /api/ai-runs/{run_id}/task-card-review-decisions
```

POST request：

```json
{
  "action": "confirm | edit | cancel",
  "task_card": {},
  "notes": "用户补充要求"
}
```

规则：

- `confirm`：保持原任务卡，继续 writer。
- `edit`：用用户提交的 task_card 覆盖 state.chapter_task_card，继续 writer。
- `cancel`：run 标记 CANCELLED，不生成正文。
- 重复提交同一 interrupt 应幂等。
- 跨用户 404。

也可以复用现有 resume 端点，但必须通过 payload type 区分：

```text
task_card_review != clarification_questions != final_review
```

### 4.4 SSE 事件

新增事件：

```text
task_card_review_required
```

payload：

```json
{
  "run_id": "...",
  "interrupt_id": "...",
  "task_card": {},
  "context_summary": {},
  "source_summary": {},
  "can_edit": true
}
```

前端旧事件继续兼容。

### 4.5 前端改造

新增组件：

```text
frontend/src/components/TaskCardReviewPanel.vue
```

展示字段不要直接给用户一坨 JSON，第一版用表单：

- 本章核心任务
- 开篇承接
- 场景顺序
- 必须出现的事件
- 禁止事项
- 信息揭示规则
- 情绪 / 节奏
- 用户补充要求
- 上下文摘要 / 查看上下文

操作：

```text
[确认生成]
[保存修改并生成]
[取消生成]
[查看本次上下文]
```

UI 约束：

- 不要点击遮罩就关闭。
- 关闭 / 取消必须明确按钮。
- 编辑 task_card 后才允许 writer 继续。
- 资料库内容如果未勾选，不应出现在上下文摘要里。

### 4.6 测试

后端：

- architect 后触发 `task_card_review_required`。
- confirm 后进入 writer。
- edit 后 writer 消费修改后的任务卡。
- cancel 后 run=CANCELLED，正文不保存。
- task_card_review 不影响 final_review。
- interrupt payload type 不会和 clarification / final_review 混淆。

前端：

- 收到 `task_card_review_required` 显示面板。
- 用户可编辑任务卡字段。
- confirm / edit / cancel 调用正确 API。
- 点击遮罩不关闭。
- vue-tsc 通过。

### 4.7 验收

- 章节生成时，正文写作前必须先显示任务卡。
- 用户能修改任务卡并让 writer 按修改后的计划写。
- 用户能取消生成。
- 所有动作有 AiRun / AiRunStep / HumanInterrupt 记录。

---

## 5. Phase L-2：嵌入式澄清

状态：已实现（2026-07-12），待真实模型端到端验收。

当前实现采用任务卡单暂停点：`clarification-planner` 先产生最多三个问题，
问题随 `task_card_review_required` 展示；用户回答后通过 `refresh_task_card`
回到 `chapter_architect`，新任务卡生成后再次停在同一面板。旧的独立
`human_clarification` 路径仅用于历史 Run 兼容。

### 5.1 目标

把现有 Clarification Loop 从独立问题流程，改造成任务卡预览里的“可选澄清区”。

### 5.2 触发策略

先用规则判断是否需要澄清，LLM 只负责润色问题或生成候选选项。

模板建议：

```python
CLARIFICATION_TEMPLATES = {
    "opening_style": {
        "question": "本章开头如何承接上章？",
        "type": "single_choice",
        "options": ["直接承接", "跳过时间", "切换场景", "自定义"],
    },
    "pov_character": {
        "question": "本章主要视角跟随谁？",
        "type": "single_choice",
        "options_from": "active_characters",
    },
    "chapter_priority": {
        "question": "本章最重要的写作重点是什么？",
        "type": "free_text",
    },
}
```

### 5.3 UI

在 TaskCardReviewPanel 底部展示：

```text
可选澄清
- Q1 ...
- Q2 ...

[回答后重新规划]
[跳过澄清，按当前任务卡生成]
```

用户回答后：

```text
answers
→ clarification_summary
→ 重新调用 chapter_architect
→ 更新 task_card
→ 留在任务卡预览面板
```

不要重新弹第二个 clarification modal。

### 5.4 兼容现有 J-1~J-5

现有 `clarification-planner`、`ClarificationResult`、`HumanInterrupt` 可以保留，但产品入口从“独立 Panel”调整为“TaskCardReviewPanel 内嵌”。

如果短期实现成本高：

- L-1 先完全不动现有 ClarificationPanel。
- L-2 再逐步把它的渲染逻辑迁移进 TaskCardReviewPanel。

### 5.5 验收

- 信息足够时不出现澄清问题。
- 信息不足时最多出现 3 个问题。
- 用户可以跳过。
- 用户回答后任务卡更新，而不是直接进入 writer。
- 澄清回答不直接写正式记忆库。

---

## 5A. Phase L-3：本次上下文预览

状态：预览与逐项排除已实现（2026-07-12），待真实模型和完整工作台验收。

实现链路：

```text
ChapterContextService
→ context_summary
→ CreativeState.context_summary
→ task_card_review_required / HumanInterrupt.payload
→ TaskCardReviewPanel「查看本次上下文」
```

当前展示：

- 前章结尾锚点。
- 本章大纲与用户额外选择的大纲。
- Story Arc / 分卷分幕信息。
- 人物、伏笔与世界设定。
- 已确认写作记忆。
- 知识库规则与检索资料。
- 用户显式选择项和来源类型。

本阶段严格复用已经构建的 `ChapterContext`，不写入或删除正式记忆。每个条目具有稳定 key；作者取消勾选后，`refresh_task_card_context` 会通过专用 `context_refresher` 重建实际 prompt，再运行 Architect 并回到任务卡。被排除条目仍保留在预览中，便于恢复。

最终 `excluded_context_keys` 保存在 LangGraph state，并进入 LLM context snapshot，确保界面、prompt 和审计记录一致。

---

## 6. Phase K-2：Story Arc / 分卷分幕

### 6.1 目标

解决长篇只有 chapter 维度的问题，让系统知道当前章节属于哪个长线阶段。

### 6.2 数据模型

新增表：

```text
story_arcs
  id
  project_id
  parent_arc_id
  arc_type              # VOLUME / ACT / ARC
  name
  summary
  goal
  main_conflict
  start_chapter
  end_chapter
  order_index
  status                # PLANNED / ACTIVE / COMPLETED / PAUSED / ARCHIVED
  metadata
  created_at
  updated_at
```

`outlines` 增加：

```text
story_arc_id
arc_position           # SETUP / BUILDUP / TURNING_POINT / CLIMAX / AFTERMATH
```

迁移建议：

```text
024_story_arcs.py
```

### 6.3 Context Builder

prompt 增加：

```text
## 当前长线结构
- [VOLUME] 第一卷：觉醒与入学
  目标：...
  主冲突：...
- [ARC] 觉醒仪式
  当前阶段：BUILDUP
```

### 6.4 前端

第一版不做拖拽，先做：

- 长线结构列表
- 新增 / 编辑 / 删除 Arc
- 章节资料弹窗里选择所属 Arc

---

## 7. Phase K-3：伏笔状态追踪

### 7.1 目标

把暗线从“涉及哪些章节”升级为生命周期：

```text
PLANNED → PLANTED → ACTIVE → REVEALED → RESOLVED → DROPPED
```

### 7.2 数据模型

`hidden_threads` 增加：

```text
status
thread_type
planted_chapter
reveal_chapter
resolved_chapter
payoff_summary
risk_level
```

迁移建议：

```text
025_hidden_thread_status.py
```

### 7.3 Memory Curator

`FORESHADOWING` staging payload 支持：

```json
{
  "thread_name": "...",
  "status_delta": "PLANTED | REVEALED | RESOLVED",
  "planted_chapter": 3,
  "reveal_chapter": 8,
  "evidence": "..."
}
```

confirm 时更新 HiddenThread 状态。

---

## 8. Phase K-4：节奏标记与章节 pacing

### 8.1 目标

让每章有节奏职责，避免所有章节都写成同一种持续推进。

### 8.2 数据模型

`outlines` 增加：

```text
pacing               # SETUP / BUILDUP / REVERSAL / CLIMAX / AFTERMATH / TRANSITION / SLICE
tension_level
target_scene_count
```

迁移建议：

```text
026_outline_pacing.py
```

### 8.3 Architect

`ChapterTaskCard` 建议增加：

```json
{
  "pacing": "BUILDUP",
  "tension_curve": ["low", "medium", "high"],
  "scene_count": 4
}
```

writer 只能按任务卡执行，不自行改节奏职责。

---

## 9. Phase K-5：角色弧线聚合视图

### 9.1 目标

先不加复杂新表，聚合已有数据：

- `CharacterEvent`
- confirmed `WritingMemoryStaging`
- story-recorder 的角色状态变化

### 9.2 API

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

### 9.3 前端

角色详情增加“角色弧线”面板。

---

## 10. 后续暂缓

以下能力有价值，但不要排在 L-1 / L-2 / K-2~K-5 前面：

| 阶段 | 内容 | 暂缓原因 |
|---|---|---|
| K-6 | 风格样本库 | 需要先把资料注入开关和上下文可解释做好 |
| K-7 | 读者反馈闭环 | summarize 反馈需要先明确是否属于作品事实 |
| K-8 | 场景库 / Location | 数据模型价值较高，但不是当前跑偏问题的主因 |
| Workflow 拖拽 | 用户自定义 DAG | 应在规则 workflow 稳定后做，避免复杂度放大 |

---

## 11. 推荐执行顺序

| 顺序 | 阶段 | 内容 | 迁移 | 优先级 |
|---:|---|---|---:|---|
| 0 | K-1 | Opening Anchor 自动提取 | 否 | 已完成 |
| 1 | L-1 | TaskCard Preview 可见可编辑 | 否 | 最高 |
| 2 | L-2 | 嵌入式澄清 | 否 | 高 |
| 3 | K-2 | Story Arc / 分卷分幕 | 是，024 | 高 |
| 4 | K-3 | 伏笔状态追踪 | 是，025 | 高 |
| 5 | K-4 | Outline pacing | 是，026 | 中高 |
| 6 | K-5 | 角色弧线聚合视图 | 否或少量 | 中 |

---

## 12. 全局约束

1. 不展示模型思维链，只展示任务卡、上下文摘要、审稿意见、一致性结果等可解释依据。
2. 不让 LLM 自由调度 workflow DAG。
3. 不新增文件型私有记忆系统。
4. 不绕开 `WritingMemoryStaging` 直接污染正式设定库。
5. 不让资料库内容默认进入 prompt，必须由用户显式勾选。
6. 不让澄清变成自由聊天。
7. 每次最多 3 个澄清问题。
8. 不要把任务卡预览和 final_review 混成一个 interrupt 类型。
9. 任务卡确认前不进入 writer。
10. writer 不得擅自修改任务卡。
11. 所有等待用户的节点必须有 `HumanInterrupt`。
12. 所有 LLM 调用继续走 `LlmCallLog` 和 prompt/context snapshot。

---

## 13. 最小成功版本

第一批只做 L-1。

用户点击“章节生成”后应该看到：

```text
第 2 章生成计划

核心任务：...
开篇承接：...
场景顺序：...
必须出现：...
禁止事项：...
信息揭示：...
节奏：...

[查看上下文]
[取消]
[确认生成]
[保存修改并生成]
```

验收口径：

- 用户能在生成前看见 AI 的计划。
- 用户能修改计划。
- writer 按修改后的计划写。
- 生成后仍走 final_review。
- 审计链完整。
