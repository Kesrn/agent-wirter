# 远程大模型使用本地 Skill 的方案评审

## 1. 背景

当前平台通过远程大模型完成小说创作、审校、润色、转折设计和摘要整理。但远程模型本身看不到本地项目能力，只能接收我们传入的 prompt，上下文质量完全取决于后端怎么组织。

你现在已经在 `backend/skills` 下准备好了 6 个默认专家 skill，再加上一个总控 skill `crazy_writer`。这说明系统已经不只是“通用资料注入”，而是形成了明确分工的专家体系：

- `creative_master`：创意生成、章节续写
- `brutal_critic`：严苛审校
- `plot_twister`：平淡检测、转折设计
- `sensory_renderer`：场景渲染、五感增强
- `professional_editor`：语法、用词、节奏、删冗余
- `summarizer`：摘要、人物志、设定概览
- `crazy_writer`：总控编排，不是单独的第 7 个专家

所以现在更合适的方向不是“通用 Context Pack”一把梭，而是：

> **基础上下文 + 专家 skill 结果包 → 对应专家节点**

---

## 2. 目标

### 2.1 必须满足

1. 远程模型生成时能利用本地项目资料和 skill 结果。
2. 不能让模型直接访问本地文件、数据库或命令。
3. 对不同小说项目保持隔离。
4. 前端能解释“这次生成用了哪些上下文”和“哪个专家跑了哪个 skill”。
5. 后端能记录、调试、复现 skill 执行链路。

### 2.2 暂不追求

1. 第一版不要求模型主动 tool calling。
2. 第一版不要求所有模型厂商都支持 tools。
3. 第一版不做复杂 Agent 自动规划闭环。
4. 不把本地 skill 暴露成无限制执行环境。

---

## 3. 现有 skill 结构的判断

### 3.1 `creative_master`

这是 Writer 节点的主 skill。

特点：
- 负责从零构思、续写章节、扩展情节
- 明确要求加载世界观、人设、大纲、剧情状态
- 输出就是正文草稿

结论：
- 应作为章节生成主引擎
- 属于“产出正文”的核心 skill

### 3.2 `brutal_critic`

这是 Critic 节点的标准 skill。

特点：
- 结构化评分
- 问题分级
- 给出具体修改建议
- 不负责直接改写正文

结论：
- 应绑定审校节点
- 输入正文，输出审校报告

### 3.3 `plot_twister`

这是可选的转折增强 skill。

特点：
- 检测平淡
- 设计反转
- 强调铺垫与可行性

结论：
- 不适合每次都跑
- 更像按需触发的剧情增强器
- 可在 Writer 前后作为可选增强

### 3.4 `sensory_renderer`

这是渲染大师。

特点：
- 五感描写
- 氛围营造
- 只增强表达，不改剧情

结论：
- 适合放在 Writer 之后
- 作为正文增强和润色前置

### 3.5 `professional_editor`

这是专业编辑。

特点：
- 语法、用词、节奏、删冗余
- 明确只做文字精修

结论：
- 适合做最后一道文本精修
- 放在渲染之后或正文输出前

### 3.6 `summarizer`

这是归档和结构化整理 skill。

特点：
- 章节摘要
- 人物志
- 设定概览
- 时间线整理

结论：
- 更适合创作完成后的归档
- 不适合作为正文生成前的核心步骤

### 3.7 `crazy_writer`

这是总控工作流，不是普通单点 skill。

它把 6 个子 skill 编排成一个完整创作链：

1. creative_master
2. sensory_renderer
3. brutal_critic
4. plot_twister
5. professional_editor
6. summarizer

所以系统应该理解成：

```text
总控 skill crazy_writer
  ↓
多个专精 skill
  ↓
各自服务不同专家节点
```

---

## 4. 推荐方案

### 4.1 核心思路

推荐采用 **D 混合方案**，但第一阶段的实现重点不是“通用上下文包”，而是：

> **基础上下文包 + 专家 skill pack**

也就是：

```text
基础创作上下文
+ 当前专家对应 skill 的执行结果
→ 进入对应专家节点
```

### 4.2 推荐顺序

```text
第一阶段：专家级 skill 预执行 / 预注入
第二阶段：规则型 Planner
第三阶段：受控 Tool Calling
```

---

## 5. 为什么要这样改

因为你已经明确有 6 个专家 skill，这些 skill 不只是“资料读取工具”，而是已经形成了职责分工：

- Writer 负责写
- Critic 负责挑问题
- Editor 负责精修
- Renderer 负责增强画面
- Twister 负责加转折
- Summarizer 负责归档

如果还只做一份通用 Context Pack，就会把这些能力压扁成“统一上下文”，浪费 skill 本身的角色分工。

更合理的模型是：

```text
专家节点 = 基础上下文 + 专家 skill 结果 + 当前草稿/目标
```

---

## 6. 第一阶段：专家级 Skill Pack

### 6.1 目标

让不同专家节点在执行前获得各自专属的 skill 结果，而不是所有节点共用同一份上下文。

### 6.2 推荐映射

| 专家节点 | 对应 skill | 作用 |
|---|---|---|
| Writer | `creative_master` | 生成正文、续写章节 |
| Critic | `brutal_critic` | 审校评分、问题清单 |
| Renderer | `sensory_renderer` | 场景渲染、五感增强 |
| Editor | `professional_editor` | 语法、节奏、删冗余 |
| Twister | `plot_twister` | 平淡检测、反转设计 |
| Summarizer | `summarizer` | 摘要、人物志、设定归档 |

### 6.3 需要保留的基础上下文

不管哪个专家节点，都应保留一份基础上下文：

- 当前 project 的大纲
- 当前章节信息
- 角色资料
- 世界观设定
- 暗线信息
- 前文摘要或最近章节片段
- 用户手动选择的素材

### 6.4 Skill Pack 的结构

建议每个专家 skill 输出一个结构化结果：

```python
class ExpertSkillResult(TypedDict):
    expert_id: str
    skill_name: str
    content: str
    sources: list[dict]
    warnings: list[str]
```

例如：

- Writer skill pack：本章大纲 + 角色约束 + 暗线要求 + 目标字数
- Critic skill pack：评分维度 + 已知问题点 + 修改建议
- Editor skill pack：待修正文 + 需要重点处理的语法/节奏问题
- Renderer skill pack：需要增强的场景片段 + 氛围目标
- Twister skill pack：平淡位置 + 可用反转方案
- Summarizer skill pack：本章输出摘要与人物状态变更

### 6.5 建议 prompt 组合方式

```text
## 基础上下文
{base_context}

## 当前专家 Skill 结果
{expert_skill_pack}

## 当前草稿 / 待处理内容
{draft}

## 任务
请以当前专家身份完成任务。
```

---

## 7. 后端落地建议

### 7.1 新增概念：Expert Skill Runner

建议新增一个后端层，负责按专家执行 skill：

```text
backend/services/expert_skill_runner.py
```

职责：

1. 根据专家类型选择 skill。
2. 读取基础上下文。
3. 执行对应 skill。
4. 生成 Skill Pack。
5. 返回给工作流节点使用。

### 7.2 推荐流程

```text
用户发起生成
  ↓
ContextLoader 加载基础上下文
  ↓
ExpertSkillRunner 按专家执行对应 skill
  ↓
专家节点读取 base_context + expert_skill_pack
  ↓
远程模型生成结果
```

### 7.3 输出记录

建议记录每次专家执行的元信息：

```json
{
  "generation_id": "...",
  "project_id": "...",
  "chapter_id": "...",
  "expert": "writer",
  "skill": "creative_master",
  "sources": [...],
  "warnings": [],
  "token_estimate": 2800
}
```

这样后续排查“为什么没按大纲写”时，可以直接看：

1. 基础上下文是否正确
2. Writer skill 是否真的执行
3. skill 结果是否被裁剪
4. prompt 是否弱化了约束
5. 模型是否不遵守

---

## 8. 运行顺序建议

### 8.1 Writer 路径

推荐：

```text
基础上下文
→ creative_master
→ sensory_renderer（可选增强）
→ professional_editor
→ 输出正文
```

### 8.2 Critic 路径

推荐：

```text
待审校正文
→ brutal_critic
→ 输出评分与问题清单
```

### 8.3 转折路径

推荐：

```text
情节摘要 / 当前草稿
→ plot_twister
→ 输出平淡点与反转建议
```

### 8.4 归档路径

推荐：

```text
已完成内容
→ summarizer
→ 输出摘要 / 人物志 / 设定概览
```

---

## 9. 前端落地建议

第一阶段前端不需要暴露复杂 tool calling，而是展示“这次用了哪些 skill”。

建议前端显示：

- 当前章节使用了哪些基础上下文
- Writer / Critic / Editor / Renderer / Twister / Summarizer 哪些 skill 已执行
- 哪些 skill 的结果被裁剪或缺失
- 上下文包是否过长

这样用户能直观看到：

```text
Writer：creative_master 已执行
Critic：brutal_critic 已执行
Editor：professional_editor 已执行
```

---

## 10. 第二阶段：规则型 Planner

等专家 skill pack 稳定后，再引入规则型 planner。

### 10.1 作用

Planner 决定：

- 当前模式该跑哪些 skill
- 哪些 skill 需要跳过
- 哪些上下文优先级更高
- 上下文过长时如何裁剪

### 10.2 例子

```python
if mode == "full_pipeline":
    use = ["creative_master", "sensory_renderer", "professional_editor", "brutal_critic"]
elif mode == "enhance":
    use = ["sensory_renderer", "professional_editor"]
elif mode == "continue":
    use = ["creative_master", "plot_twister"]
elif mode == "summarize":
    use = ["summarizer"]
```

### 10.3 为什么第二阶段再做

因为你现在已经有固定分工 skill 了，先把 skill pack 和节点接稳，比一上来做复杂 planner 更重要。

---

## 11. 第三阶段：受控 Tool Calling

最后才考虑让支持 tools 的模型主动请求 skill。

### 11.1 保留降级路径

对于不支持 tools 的 provider，自动降级回 pre-injection。

### 11.2 安全限制

- 只能调用白名单 skill
- 必须校验参数 schema
- 单次生成限制调用次数
- 严禁 shell / 任意文件 / 任意 SQL
- 所有 tool call 必须记录日志
- 所有 skill 必须强制 `project_id` 隔离

---

## 12. 推荐结论

最终推荐仍然是 **D 混合方案**，但结合你现在的 skill 目录，第一阶段应该改成：

> **基础上下文包 + 专家级 Skill Pack 预注入**

不是先做通用上下文包，而是先把 6 个默认专家 skill 跑通，并接到对应专家节点上。

### 最终落地顺序

```text
第一阶段：基础上下文 + 专家 skill pack
第二阶段：规则型 Planner
第三阶段：受控 Tool Calling
```

这样既能马上利用你已经写好的 skill，又能保留未来演进到 planner 和 tool calling 的空间。

---

## 13. 最终一句话

**你的 skill 不是附属品，而是工作流本身的一部分；方案应该从“通用上下文注入”升级为“专家级 skill 驱动的创作链”。**
