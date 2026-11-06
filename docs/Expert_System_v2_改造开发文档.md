# Expert System v2 改造开发文档

面向：GLM / Codex 开发执行

目标：替换旧“大师”专家体系和旧 Skill 设计，保留现有数据库、版本、审计、Human Review、Memory Staging、Context Builder 与 AI Harness 基础设施。

---

## 1. 核心结论

本次不是重做项目，而是在现有平台上做一次：

```text
Expert System v2 + Workflow v2
```

必须推翻的是：

- 旧专家角色：创意大师、残酷大师、情节转折大师、渲染大师、概括者等泛化命名。
- 旧 Skill：一个专家承担过多职责、边界不清、容易互相污染。
- 旧 Workflow：固定 writer/critic 思路不足以支持章节卡、编辑、记录、记忆治理。

必须保留的是：

- 数据库作为唯一事实源。
- `Character / WorldEntry / CharacterEvent / HiddenThread` 等正式设定表。
- `WritingMemoryStaging` 作为 AI 候选记忆暂存区。
- `ChapterVersion` 版本审计。
- `AiRun / AiRunStep / LlmCallLog / HumanInterrupt` Harness 审计链。
- 现有 `Context Builder` 和 confirmed memory 注入机制。

禁止新建 `.crazy_writer_memory/` 文件夹作为正式记忆源。Skill 只能定义专家行为和 prompt，不允许自己维护一套私有记忆目录。

---

## 2. 当前项目基线

当前相关文件：

- `backend/models/expert.py`
- `backend/agents/expert_templates.py`
- `backend/skills/registry.py`
- `backend/skills/runner.py`
- `backend/agents/workflow.py`
- `backend/api/routes.py`
- `backend/services/chapter_context.py`
- `backend/models/ai_run.py`
- `backend/models/ai_run_step.py`
- `backend/models/llm_call_log.py`
- `backend/models/writing_memory_staging.py`
- `frontend/src/views/AgentStudio.vue`
- `frontend/src/components/AgentPanel.vue`

当前旧专家模板在 `backend/agents/expert_templates.py`。

当前 Skill 注入方式：

```text
Expert.skill_dir
→ backend/skills/runner.py 读取 backend/skills/{skill_dir}/SKILL.md
→ build_expert_system_prompt()
→ 拼入 system_prompt
→ 调用 LLM
```

注意：当前 Skill 是 prompt 增强包，不是本地工具执行 runtime。

---

## 3. Expert v2 新专家体系

### 3.1 创作链专家

这些是主流程专家：

| expert_key | 中文名 | 职责 | 替代旧专家 |
|---|---|---|---|
| `chapter-architect` | 章节策划师 | 生成章节任务卡，只规划不写正文 | 情节转折大师的一部分、创意大师的一部分 |
| `chapter-writer` | 正文写手 | 根据章节任务卡写完整正文 | 创意大师 |
| `structural-critic` | 残酷审稿人 | 诊断结构、节奏、人物、同人风险，只给修改指令 | 残酷大师 |
| `narrative-editor` | 专业编辑 | 根据审稿指令修改正文，输出可用修订稿 | 专业编辑 |
| `continuity-checker` | 一致性审校师 | 检查人物、能力、时间线、原著事实、状态冲突 | 现有 consistency_checker |

### 3.2 记忆治理专家

这些属于系统步骤，不建议在前端作为显眼创作专家平铺展示：

| expert_key | 中文名 | 职责 |
|---|---|---|
| `story-recorder` | 剧情记录员 | 从最终正文提取已发生事件、状态变化、关系变化、伏笔、时间线 |
| `memory-curator` | 记忆管理员 | 判断候选记忆是否长期有效、是否冲突、写入 staging 或等待确认 |

第一版可以共用一次模型调用，但代码和 prompt 必须拆成两个逻辑步骤，避免把章节临时状态升级成长期设定。

### 3.3 按需工具专家

| expert_key | 中文名 | 触发方式 |
|---|---|---|
| `canon-researcher` | 原著考据师 | 用户明确要求查原著、核对原著事实时触发 |
| `scene-enhancer` | 场景增强师 | 用户明确选择段落/场景增强时触发 |

`scene-enhancer` 不允许默认增强整章，只处理用户指定范围。

### 3.4 调度层

| key | 中文名 | 实现方式 |
|---|---|---|
| `novel-orchestrator` | 总编调度器 | 第一版用规则引擎，不做自由 LLM Agent |

第一版总编只做：

```text
TaskType -> WorkflowDefinition
```

不要让 LLM 总编自由决定任意调用链，避免成本失控、循环调用、难测试。

---

## 4. 新旧专家映射

| 旧专家 | 处理方式 |
|---|---|
| 创意大师 | 拆分为 `chapter-architect` + `chapter-writer` |
| 残酷大师 | 替换为 `structural-critic` |
| 情节转折大师 | 不再独立常驻，并入 `chapter-architect` 的张力设计能力 |
| 渲染大师 | 不再常驻，改为按需 `scene-enhancer` |
| 专业编辑 | 保留但升级为 `narrative-editor` |
| 概括者 | 升级为 `story-recorder` |

旧 Skill 文件不要物理删除，迁移到 `backend/skills/archive/` 或保留并标记 deprecated。历史 `AiRun` 仍可能需要解释旧 prompt。

---

## 5. Workflow v2 目标

### 5.1 标准章节生成

```text
Context Builder
→ chapter-architect
→ optional Planning Review
→ chapter-writer
→ structural-critic
→ narrative-editor
→ continuity-checker
→ Final Review
→ story-recorder
→ memory-curator
→ WritingMemoryStaging
```

第一版可先实现到 Final Review，Story Recorder / Memory Curator 接入可复用现有 H2/H3 记忆链路逐步替换。

### 5.2 双 Human Review

必须区分两个审核点：

1. `planning_review`
   - 审核章节任务卡。
   - 普通章节可关闭。
   - 关键章节建议开启，例如年度考核、重大改命、主线转折。

2. `final_review`
   - 审核最终正文。
   - 用户确认后才允许进入 Story Recorder 和 Memory Staging。

### 5.3 快捷入口映射

| 旧入口/任务 | 新链路 |
|---|---|
| `generate chapter` | Architect → Writer → Critic → Editor → Continuity |
| `continue` | Architect Lite → Writer → Continuity |
| `enhance` | Scene Enhancer → Narrative Editor |
| `summarize` | Story Recorder |
| `rewrite` | Critic → Editor → Continuity |
| `review` | Critic → Continuity |
| `extract memory` | Story Recorder → Memory Curator → Staging |

`enhance` 必须要求用户指定范围：

- 当前段落
- 当前场景
- 对话
- 战斗动作
- 环境氛围

禁止默认整章增强。

---

## 6. Workflow 定义不要长期写死在 Python 分支里

第一版可以先在 Python 中定义结构化配置，但不要散落到 `routes.py` if/else 里。

建议新增：

```text
backend/services/workflow_definitions.py
backend/services/novel_orchestrator.py
```

示例：

```python
WORKFLOW_DEFINITIONS = {
    "generate_chapter_standard": {
        "version": "v2.0",
        "steps": [
            {"node": "chapter_architect", "expert_key": "chapter-architect", "step_order": 1},
            {"checkpoint": "planning_review", "optional": True, "step_order": 2},
            {"node": "chapter_writer", "expert_key": "chapter-writer", "step_order": 3},
            {"node": "structural_critic", "expert_key": "structural-critic", "step_order": 4},
            {"node": "narrative_editor", "expert_key": "narrative-editor", "step_order": 5},
            {"node": "continuity_checker", "expert_key": "continuity-checker", "step_order": 6},
            {"checkpoint": "final_review", "step_order": 7},
        ],
    },
    "generate_chapter_fast": {
        "version": "v2.0",
        "steps": [
            {"node": "chapter_architect_lite", "expert_key": "chapter-architect", "step_order": 1},
            {"node": "chapter_writer", "expert_key": "chapter-writer", "step_order": 2},
            {"node": "continuity_checker", "expert_key": "continuity-checker", "step_order": 3},
            {"checkpoint": "final_review", "step_order": 4},
        ],
    },
}
```

后续前端拖拽工作流时，可以把这套定义迁移到数据库：

```text
workflow_templates
workflow_nodes
workflow_edges
```

但第一版不要直接做自由 DAG。

---

## 7. 数据模型改造建议

### 7.1 Expert 表版本化

当前 `experts` 已有：

- `name`
- `role_type`
- `skill_dir`
- `system_prompt`
- `workflow_position`
- `is_builtin`
- `is_enabled`

建议新增迁移 `023_expert_system_v2.py`：

```text
experts:
  expert_key       String(80) nullable, indexed
  version          Integer not null default 1
  deprecated       Boolean not null default false
  input_schema     JSON nullable
  output_schema    JSON nullable
```

说明：

- `expert_key` 是稳定机器名，例如 `chapter-writer`。
- `name` 继续作为中文展示名。
- `version` 用于 prompt 升级和历史追溯。
- `deprecated` 标记旧大师。
- `input_schema/output_schema` 用于稳定专家间结构化通信。

### 7.2 AiRun 工作流快照

建议给 `ai_runs` 新增：

```text
workflow_key       String(100) nullable, indexed
workflow_version   String(30) nullable
workflow_snapshot  JSON nullable
expert_snapshot    JSON nullable
```

说明：

- `workflow_snapshot` 保存本次运行实际执行的节点定义。
- `expert_snapshot` 保存每个专家的 `expert_key/version/skill_dir/name`。
- 旧 run 可为空。

### 7.3 AiRunStep 专家快照

建议给 `ai_run_steps` 新增：

```text
node_key        String(100) nullable
expert_key      String(80) nullable
expert_version  Integer nullable
skill_dir       String(100) nullable
```

这样从 run step 可以直接知道每一步用了哪个专家版本。

### 7.4 LlmCallLog

`llm_call_logs` 已有：

- `agent_name`
- `rendered_prompt_snapshot`
- `context_package_snapshot`
- `model_config_snapshot`

可选新增：

```text
expert_key      String(80) nullable
expert_version  Integer nullable
skill_dir       String(100) nullable
workflow_key    String(100) nullable
```

如果不想加列，至少要把这些信息写入 `request` JSON。

---

## 8. 新 Skill 文件

新增以下目录：

```text
backend/skills/novel-orchestrator/SKILL.md
backend/skills/chapter-architect/SKILL.md
backend/skills/chapter-writer/SKILL.md
backend/skills/structural-critic/SKILL.md
backend/skills/narrative-editor/SKILL.md
backend/skills/continuity-checker/SKILL.md
backend/skills/story-recorder/SKILL.md
backend/skills/memory-curator/SKILL.md
backend/skills/canon-researcher/SKILL.md
backend/skills/scene-enhancer/SKILL.md
```

每个 `SKILL.md` 必须包含 frontmatter：

```yaml
---
name: chapter_writer
description: >
  正文写手。严格依据已确认的章节任务卡和项目设定完成小说正文。
  不负责修改大纲，不擅自增加长期设定。
---
```

Skill 文档必须明确：

- 核心职责
- 必须输入
- 输出格式
- 禁止事项
- 权限边界

重点要求：

- `chapter-architect` 输出结构化章节任务卡。
- `chapter-writer` 只根据任务卡写正文。
- `structural-critic` 只诊断，不直接重写。
- `narrative-editor` 允许修改正文，但不能改变已确认剧情结果。
- `continuity-checker` 输出结构化 `GuardrailResult` 兼容格式。
- `story-recorder` 只记录正文明确发生的信息。
- `memory-curator` 只写 staging，不直接写正式表。

---

## 9. 结构化输出契约

### 9.1 ChapterTaskCard

`chapter-architect` 输出：

```json
{
  "chapter_number": 2,
  "chapter_title": "天澜魔法高中",
  "core_task": "让主角确认自己穿越并进入觉醒仪式场景",
  "opening_anchor": "上一章结尾停在主角睁眼感到陌生身体",
  "ending_state": {
    "plot": "主角抵达天澜魔法高中并准备觉醒",
    "character_changes": [],
    "new_information": [],
    "hook": "觉醒仪式即将开始"
  },
  "scenes": [
    {
      "title": "陌生房间醒来",
      "location": "程璇家中",
      "characters": ["程璇"],
      "scene_goal": "确认穿越事实",
      "conflict": "身体陌生、世界异常",
      "must_include": [],
      "must_not_include": [],
      "word_budget": 600
    }
  ],
  "character_goals": [],
  "information_rules": {
    "may_reveal": [],
    "hint_only": [],
    "forbidden": []
  },
  "tension_design": [],
  "word_budget": 2000,
  "forbidden": []
}
```

### 9.2 StructuralCritique

`structural-critic` 输出：

```json
{
  "summary": "总体判断",
  "p0": [],
  "p1": [],
  "p2": [],
  "must_keep": [],
  "edit_instructions": {
    "delete": [],
    "merge": [],
    "move_forward": [],
    "move_later": [],
    "rewrite": [],
    "keep": []
  }
}
```

### 9.3 EditedDraft

`narrative-editor` 输出：

```json
{
  "draft": "完整修订稿正文",
  "edit_report": {
    "deleted": [],
    "merged": [],
    "rewritten": [],
    "kept": []
  }
}
```

### 9.4 Continuity Result

继续复用 Phase G 的 `GuardrailResult`：

```json
{
  "issues": [
    {
      "type": "character|worldbuilding|plot|timeline|other",
      "description": "具体冲突",
      "severity": "info|low|medium|high"
    }
  ],
  "summary": "整体结论",
  "overall_severity": "info|low|medium|high",
  "parse_error": false
}
```

`high` severity 必须阻断自动提交，进入 Human Review。

### 9.5 Story Record

`story-recorder` 输出：

```json
{
  "summary": "一句话摘要",
  "events": [],
  "character_state_changes": [],
  "relationship_changes": [],
  "ability_changes": [],
  "foreshadowing_new": [],
  "foreshadowing_resolved": [],
  "timeline": {},
  "knowledge_state_changes": [],
  "memory_candidates": []
}
```

`memory_candidates` 后续交给 `memory-curator` 和 `WritingMemoryStaging`。

---

## 10. Workflow State v2

建议新增或演进 `CreativeState`：

```python
class CreativeState(TypedDict, total=False):
    project_id: str
    chapter_id: str
    chapter_num: int
    mode: str
    workflow_key: str
    workflow_version: str
    context: str

    chapter_task_card: dict
    draft: str
    structural_critique: dict
    edited_draft: str
    edit_report: dict
    consistency_report: dict
    story_record: dict
    memory_candidates: list[dict]

    revision_count: int
    skill_packs: list[dict]
    llm_config: dict
    harness_run_id: str
```

状态写入规则：

- `chapter-architect` 写 `chapter_task_card`
- `chapter-writer` 写 `draft`
- `structural-critic` 写 `structural_critique`
- `narrative-editor` 写 `draft` 和 `edit_report`
- `continuity-checker` 写 `consistency_report`
- `story-recorder` 写 `story_record` 和 `memory_candidates`
- `memory-curator` 写 `WritingMemoryStaging`

避免多个并行节点写同一 key。若未来并行，必须定义 reducer/merge 策略。

---

## 11. 迁移阶段

### Phase I-1：数据层和 Skill 文件

目标：新增 Expert v2 所需字段、新 Skill 文件、旧专家 deprecated 标记。

任务：

1. 新增 migration `023_expert_system_v2.py`
2. `Expert` 模型加 `expert_key/version/deprecated/input_schema/output_schema`
3. `AiRun` 加 `workflow_key/workflow_version/workflow_snapshot/expert_snapshot`
4. `AiRunStep` 加 `node_key/expert_key/expert_version/skill_dir`
5. 可选：`LlmCallLog` 加专家/workflow 快照字段
6. 新增 10 个 Skill 目录和 `SKILL.md`
7. 更新 `backend/skills/registry.py`
8. 测试模型 import、migration upgrade/downgrade、skill registry 扫描

验收：

- 旧数据迁移不报错
- 新字段 nullable 或有安全默认值
- 旧 run 不受影响
- `scan_skills()` 能找到 v2 skills

### Phase I-2：内置专家模板替换

目标：新项目默认创建 Expert v2 内置专家，旧专家不再作为默认。

任务：

1. 修改 `backend/agents/expert_templates.py`
2. 新增 `BUILTIN_EXPERTS_V2`
3. 旧 `BUILTIN_EXPERTS` 保留为 archive 或 deprecated
4. 创建项目时使用 v2 模板
5. 新增同步函数：为已有项目补齐 v2 内置专家，并把旧内置专家 `deprecated=true`
6. 前端 `AgentStudio.vue` 按分类展示：
   - 创作链专家
   - 记忆治理专家
   - 按需工具专家

验收：

- 新项目只显示 v2 专家
- 旧项目可补齐 v2 专家
- 旧专家不物理删除，历史记录仍可查看

### Phase I-3：规则总编和 Workflow Definition

目标：不要再把工作流选择散落在 `routes.py`。

任务：

1. 新增 `backend/services/workflow_definitions.py`
2. 新增 `backend/services/novel_orchestrator.py`
3. 定义 `TaskType`：
   - `GENERATE_CHAPTER`
   - `CONTINUE`
   - `ENHANCE_SCENE`
   - `REVIEW`
   - `REWRITE`
   - `SUMMARIZE`
   - `EXTRACT_MEMORY`
4. `GenerateRequest.mode` 映射到 workflow key
5. `AiRun` 创建时写入 workflow snapshot

验收：

- 同一请求稳定映射到同一 workflow
- 单测覆盖 mode -> workflow
- 不调用 LLM 做调度

### Phase I-4：LangGraph Workflow v2

目标：实现标准章节生成链路。

任务：

1. 新增 `backend/agents/workflow_v2.py` 或重构 `workflow.py`
2. 实现节点：
   - `chapter_architect_node`
   - `chapter_writer_node`
   - `structural_critic_node`
   - `narrative_editor_node`
   - `continuity_checker_node`
   - `planning_review_node`
   - `final_review_node`
3. 每个节点调用 `build_expert_system_prompt()` 注入对应 Skill
4. 每个节点写独立 `AiRunStep`
5. LLM call log 写入对应 `expert_key/expert_version`
6. `planning_review` 可配置开关
7. `final_review` 复用现有 Human Review 机制

验收：

- 标准链路 run/step/log 完整
- Writer 使用 Architect 的任务卡
- Editor 使用 Critic 的修改指令
- Continuity 使用最终 draft
- High severity 进入 Human Review
- smoke 不坏

### Phase I-5：快捷入口重映射

目标：旧入口不再调用旧大师。

任务：

1. `continue` 改成 `architect-lite -> writer -> continuity`
2. `enhance` 要求前端选择范围，走 `scene-enhancer -> narrative-editor`
3. `summarize` 走 `story-recorder`
4. `review` 走 `structural-critic -> continuity`
5. `rewrite` 走 `structural-critic -> narrative-editor -> continuity`

验收：

- 前端文案不再出现旧“大师”
- `enhance` 不允许默认整章增强
- generation history 能显示 workflow key 和专家步骤

### Phase I-6：剧情记录和记忆治理接入

目标：替换或增强当前 H2 的 FactExtractionAgent。

任务：

1. `story-recorder` 在 final approve 后触发
2. 输出 `Story Record`
3. `memory-curator` 将候选转为 `WritingMemoryStaging`
4. 不直接写正式设定表
5. 用户确认后继续使用 H3 的 confirmed 入库逻辑

验收：

- 章节确认后产生 staging 候选
- staging 带 evidence / chapter_version_id / chapter_sequence_number
- PLOT_FACT 仍不硬塞 CharacterEvent
- Context Builder 继续读取 confirmed memory

---

## 12. 前端改造要求

### 12.1 Agent Studio

旧“内置专家”平铺卡片改为分组：

```text
创作链专家
- 章节策划师
- 正文写手
- 残酷审稿人
- 专业编辑
- 一致性审校师

记忆治理专家
- 剧情记录员
- 记忆管理员

按需工具专家
- 原著考据师
- 场景增强师
```

`novel-orchestrator` 不建议作为普通专家展示，可放在“工作流设置”里。

### 12.2 工作流模式选择

章节生成按钮前可支持：

- 快速模式
- 标准模式
- 精修模式

第一版可以只做标准模式，其他模式后续加。

### 12.3 Planning Review

关键章节可开启章节卡审核：

- 展示 `ChapterTaskCard`
- 用户可 approve / edit / reject
- approve 后进入 writer

第一版普通章节默认跳过。

### 12.4 Final Review

继续复用现有 ApprovalModal，但展示信息应包括：

- 本次 workflow
- 章节任务卡摘要
- 审稿意见摘要
- 一致性风险
- 最终正文

---

## 13. 测试要求

### 后端

必须新增测试：

1. skill registry 能扫描 v2 skills
2. Expert v2 模型字段和迁移
3. 新项目创建 v2 内置专家
4. 旧专家 deprecated 同步逻辑
5. mode -> workflow key 规则调度
6. `chapter-architect` 输出可 parse 的 task card
7. `chapter-writer` prompt 包含 task card，不把前文当续写起点
8. `structural-critic` 输出结构化 critique
9. `narrative-editor` 使用 critique 输出 edited draft
10. `continuity-checker` 兼容 GuardrailResult
11. run/step/log 包含 workflow/expert snapshot
12. final review 后触发 story recorder / staging

继续保留：

- `pytest tests/test_smoke.py`
- harness 相关测试
- migration upgrade/downgrade

### 前端

必须验证：

1. Agent Studio 分组展示
2. 旧专家不再默认显示为主专家
3. 模式选择不会破坏章节生成
4. Planning Review 展示 task card
5. Final Review 正常 approve/reject/revise
6. `vue-tsc -b`
7. `vitest`

---

## 14. 禁止事项

1. 禁止创建 `.crazy_writer_memory/` 作为正式记忆源。
2. 禁止删除旧 Skill 文件导致历史 run 无法解释。
3. 禁止让 LLM 总编自由调用任意专家。
4. 禁止多个专家无限循环审稿。
5. 禁止 story-recorder 或 memory-curator 直接写正式设定表。
6. 禁止 enhance 默认增强整章。
7. 禁止 continuity-checker 直接重写正文。
8. 禁止 editor 擅自改变已确认剧情结果。
9. 禁止 writer 擅自改章节任务卡。
10. 禁止未保存 prompt / workflow / expert 快照的 LLM 调用。

---

## 15. 第一版推荐最小范围

第一版不要一次实现十个 LLM 节点。

推荐上线：

1. `chapter-architect`
2. `chapter-writer`
3. `structural-critic`
4. `narrative-editor`
5. `continuity-checker`
6. `story-recorder`

先不做：

- LLM 自由总编
- 自由拖拽 DAG
- canon-researcher 联网/原著检索工具
- scene-enhancer 独立复杂 UI

规则调度器先实现为 Python deterministic service。

memory-curator 第一版可复用现有 `memory_staging_service.py`，但 prompt 和代码职责要预留拆分点。

---

## 16. 最终验收标准

完成 Expert System v2 后，必须满足：

1. 新建项目默认使用 v2 专家，不再默认使用旧大师体系。
2. 生成章节时先形成章节任务卡，再写正文。
3. 正文写手不会修改章节任务卡和长期设定。
4. 审稿人只输出诊断，编辑器负责修改。
5. 一致性高危问题会阻断进入 Human Review。
6. 用户确认正文后才进入剧情记录和记忆 staging。
7. 所有 run/step/LLM call 能追溯 workflow version、expert version、prompt snapshot、context snapshot。
8. 数据库仍是唯一事实源。
9. 旧 Skill 和旧专家历史记录可追溯。
10. smoke、harness、frontend typecheck、frontend tests 全部通过。

