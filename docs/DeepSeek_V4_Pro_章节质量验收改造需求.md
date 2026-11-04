# DeepSeek V4 Pro 实现任务书：章节生成质量验收门与有效编辑闭环

## 一、你的角色与任务

你是本项目的高级 Python/FastAPI/LangGraph/Vue 工程师。请直接在以下仓库中完成代码改造：

```text
/Users/zcx/ai-creative-platform
```

目标不是单纯优化提示词，而是给小说章节生成工作流增加一套**可验证、可重试、不可假完成**的质量验收机制。

开始前必须：

1. 阅读当前工作区 `git diff` 和 `git status`。
2. 保留当前所有未提交修改，不得执行 `git reset`、`git checkout .`、覆盖文件或回滚已有修复。
3. 重点理解现有 Workflow V2、任务卡审核、DeepSeek keepalive、最终人工审核弹窗和素材白名单逻辑。
4. 先给出简短实施计划，再直接完成代码、测试和验证。

---

## 二、问题背景与可复现证据

当前一次真实章节生成出现以下结果：

```text
目标字数：约 2000
实际候选正文：约 3999 个非空白字符
```

Writer 初稿包含内部规划标记：

```text
【环境与状态铺垫 — 增强开始】
【增强结束】
【陈浩打游戏与互动 — 增强开始】
```

将这些内部标记移除后，Writer 初稿与 Editor 候选稿的对比结果是：

```text
初稿长度：3999
候选稿长度：3999
SequenceMatcher 相似度：1.0
```

即 Editor 没有进行任何实质修改，但系统仍然显示：

```text
审校成功
一致性检查成功
编辑后的候选稿
```

当前系统的根本问题是：

```text
节点成功 = 模型返回非空字符串
```

而不是：

```text
节点成功 = 输出满足任务卡、字数、格式和编辑要求
```

因此会出现“流程全绿、内容却不合格”的假成功。

---

## 三、总体目标

将当前工作流：

```text
chapter_writer
→ structural_critic
→ narrative_editor
→ continuity_checker
→ human_review
```

改造成：

```text
chapter_writer
→ draft_validator
→ structural_critic
→ narrative_editor
→ edited_draft_validator
→ continuity_checker
→ final_quality_gate
→ human_review
```

要求：

1. 模型返回内容不再自动视为成功。
2. 字数、内部标记、正文格式等客观规则必须由普通代码确定性验证。
3. Writer 或 Editor 输出不合格时允许有限次数自动重试。
4. Editor 有修改任务时，必须证明它进行了实质修改。
5. 最终质量门未通过时：
   - 不得标记 AiRun 为 `COMPLETED`；
   - 不得发送假 `done`；
   - 不得创建可供用户误用的候选版本；
   - 不得进入人工审核弹窗。
6. 前端必须显示当前质量检查、重试和失败原因。

---

## 四、后端确定性质量检查

建议新增独立模块：

```text
backend/services/chapter_quality.py
```

不要把全部规则继续堆进 `workflow_v2.py` 或 `routes.py`。

### 4.1 建议的数据结构

可以使用 dataclass、TypedDict 或 Pydantic，但输出必须稳定、可序列化：

```python
class QualityIssue:
    code: str
    severity: str       # ERROR / WARNING
    message: str
    evidence: str | None

class QualityReport:
    passed: bool
    stage: str          # draft / edited / final
    target_chars: int
    actual_chars: int
    min_chars: int
    max_chars: int
    similarity: float | None
    changed_chars: int | None
    issues: list[QualityIssue]
    metrics: dict
```

### 4.2 字数硬约束

项目当前前端字数统计是去除空白后的字符数，后端必须使用相同口径：

```python
def count_non_space_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text or ""))
```

默认容差：

```python
min_chars = int(target_words * 0.8)
max_chars = int(target_words * 1.2)
```

例如目标 2000，只接受 1600～2400。

要求：

- `target_words <= 0` 时可以跳过长度硬检查；
- 长度不足或超长必须返回 `ERROR`；
- 不得仅依赖 Prompt 里的“约 2000 字”。

### 4.3 内部元信息禁止规则

正文中禁止出现规划、审校和增强标记。至少检测：

```python
FORBIDDEN_PATTERNS = [
    r"【[^】]*(?:增强开始|增强结束|开始|结束)[^】]*】",
    r"(^|\n)\s*#{1,6}\s*(?:场景|写作说明|修改说明|审校意见|任务卡)",
    r"(^|\n)\s*(?:场景目标|必须包含|禁止包含|word_budget)\s*[:：]",
    r"(?:以下是|下面是).{0,20}(?:正文|修改稿|候选稿)",
]
```

要求：

- 检测结果必须保留匹配 evidence；
- 后端应提供统一的正文清理函数，但不能把“清理成功”当作质量通过；
- 如果模型输出内部标记，应将问题反馈给重试节点，而不是只在前端静默删除。

### 4.4 Markdown 标题与正文边界

章节标题已经由章节数据和编辑器单独管理。生成正文不应再次包含：

```markdown
# 第一章 标题
```

建议：

- 检测开头 Markdown 标题；
- 后端可以安全去掉重复章节标题；
- QualityReport 中记录 `duplicate_heading_removed`；
- 不允许标题清理后正文为空。

### 4.5 完整结尾检查

正文最后一句必须自然结束，至少检查末尾标点：

```python
VALID_ENDINGS = ("。", "！", "？", "…", "。”", "！”", "？”")
```

禁止明显截断状态，例如：

```text
最后停在逗号、冒号、破折号、未闭合引号、未完成 Markdown 代码块
```

### 4.6 重复与扩写膨胀指标

先实现稳定、可测试的确定性指标，不要求一次解决所有文学判断：

- 重复段落检测；
- 连续重复句检测；
- 高频 4-gram 或 6-gram 重复率；
- 相同感官关键词在短距离内重复出现的警告；
- 单个任务卡场景占总篇幅比例过高时给 `WARNING`。

这类指标初期可以只生成 Warning，不阻断最终流程；长度和内部标记必须是 Error。

---

## 五、Writer 重试机制

### 5.1 状态字段

在 `CreativeStateV2` 中增加独立字段，不得复用用户修订次数 `revision_count`：

```python
writer_retry_count: int
writer_max_retries: int       # 默认 1
writer_validation_errors: list[dict]
draft_quality_report: dict
```

### 5.2 Writer 节点行为

`chapter_writer_node` 首次生成时使用正常 Prompt。

重试时必须把确定性错误明确传入 Prompt，例如：

```text
上一次输出未通过系统验收：
1. 目标 2000 字，实际 3999 字，必须压缩到 1600～2400 字。
2. 输出包含内部标记“【环境与状态铺垫 — 增强开始】”。
3. 禁止输出场景标题、增强标签和修改说明。

请重新输出完整纯正文，不要解释。
```

路由规则：

```text
Draft 通过 → structural_critic
Draft 失败且 retry 未耗尽 → chapter_writer
Draft 失败且 retry 已耗尽 → workflow error
```

不得无限循环。

---

## 六、Critic 必须输出可执行结构

`structural_critic` 不能只输出泛泛评价。

建议统一为：

```json
{
  "summary": "...",
  "requires_revision": true,
  "issues": [
    {
      "id": "E1",
      "severity": "P1",
      "type": "pacing",
      "target": "宿舍环境描写",
      "problem": "环境描写占比过高，核心事件推进过慢",
      "instruction": "将环境描写压缩到400字以内",
      "verifiable": true
    }
  ],
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

要求：

- 增加 `requires_revision`；
- 每条可执行问题必须有稳定 ID；
- Critic 必须看到 draft 的 QualityReport；
- 超长、内部标记等确定性错误不能被 Critic 忽略。

---

## 七、Editor 有效性约束

### 7.1 状态字段

```python
editor_retry_count: int
editor_max_retries: int       # 默认 2
editor_validation_errors: list[dict]
edited_quality_report: dict
edit_effectiveness_report: dict
```

### 7.2 Editor Prompt

必须包含：

- 目标字数与允许范围；
- 初稿正文；
- Critic 的结构化 issues；
- 上一次 Editor 验收失败原因；
- 明确禁止输出说明、标题、任务卡字段和增强标签；
- 明确要求输出完整正文。

### 7.3 文本相似度

使用标准库即可：

```python
from difflib import SequenceMatcher
```

规范化后比较：

```python
similarity = SequenceMatcher(
    None,
    normalize_for_comparison(initial_draft),
    normalize_for_comparison(edited_draft),
).ratio()
```

必须区分两种情况：

#### Critic 判定无需修改

```text
requires_revision = false
```

此时 Editor 可以跳过，或者允许相似度为 1.0，但前端文案必须显示：

```text
审校通过，无需改写
```

不能伪装成“编辑后的候选稿”。

#### Critic 判定需要修改

```text
requires_revision = true
```

至少满足：

```python
similarity <= 0.98
changed_chars >= max(80, int(len(initial) * 0.03))
```

仅删除内部标签不能算有效编辑。`changed_chars` 应基于清理后的正文计算。

如果不满足：

```text
Editor 未落实审校意见
```

进入 Editor 重试，达到上限后流程失败。

### 7.4 指令落实检查

至少实现一个结构化的 effectiveness report：

```json
{
  "requires_revision": true,
  "similarity": 0.91,
  "changed_chars": 420,
  "instruction_results": [
    {
      "id": "E1",
      "applied": true,
      "evidence": "环境段落已明显压缩"
    }
  ],
  "passed": true
}
```

可先用确定性检查判断整体修改量，再让 LLM 只负责判断各指令是否落实。LLM 判断失败时应降级，不得让整个流程无响应。

---

## 八、最终质量门

在进入 `human_review` 前增加 `final_quality_gate`。

通过条件至少包括：

```python
final_report.passed is True
no ERROR issues
content is not empty
length within allowed range
no forbidden metadata
ending is complete
```

如果 Critic 要求修改，还要满足：

```python
edit_effectiveness_report.passed is True
```

失败时必须：

1. AiRun 标记为 `FAILED`，或保留明确的可重试失败状态；
2. 记录 error_message；
3. SSE 发送 `quality_check` 后发送 `error`；
4. 不发送 `done`；
5. 不发送 `WAITING_HUMAN`；
6. 不创建空白或不合格的 GenerationRecord；
7. 不把进度条全部涂绿。

`backend/api/routes.py` 必须保留最终保护，即使 LangGraph 路由出现异常，也不能绕过质量门直接完成。

---

## 九、SSE 事件和前端展示

新增 SSE 事件：

```text
quality_check
```

建议 payload：

```json
{
  "stage": "draft|edited|final",
  "status": "running|passed|retrying|failed",
  "target_chars": 2000,
  "actual_chars": 3999,
  "min_chars": 1600,
  "max_chars": 2400,
  "retry_count": 1,
  "max_retries": 2,
  "issues": [
    {
      "code": "CONTENT_TOO_LONG",
      "severity": "ERROR",
      "message": "正文超过最大长度"
    }
  ]
}
```

前端涉及文件预计包括：

```text
frontend/src/api/types.ts
frontend/src/components/AgentPanel.vue
frontend/src/components/AgentWorkflow.vue
frontend/src/components/ApprovalModal.vue
```

前端要求：

1. Workflow 增加“质量验收”状态，或在现有流程中明确显示质量检查。
2. 重试时显示：

   ```text
   字数验收失败，正在要求 Writer 第 1/1 次重写
   ```

3. 失败时显示具体原因，不能显示 100% 成功。
4. ApprovalModal 接收最终 QualityReport，并展示简短摘要：

   ```text
   目标 2000 字 / 实际 2180 字
   内部标记检查：通过
   编辑有效性：修改 312 字，相似度 0.89
   ```

5. 如果 `requires_revision=false` 且正文未改，标题应显示：

   ```text
   审校通过，无需改写
   ```

6. 不得只靠前端 `cleanGeneratedContent()` 判断正文合格；前端清理只用于兼容展示。

---

## 十、日志与可观测性

沿用当前已有的：

```text
GENERATION_TRACE
GENERATION_UI_TRACE
```

增加质量日志，但不得记录完整正文、API Key 或 Authorization：

```text
QUALITY_TRACE stage=draft run_id=... target=2000 actual=3999 passed=false issues=[CONTENT_TOO_LONG,METADATA_LEAK]
QUALITY_TRACE stage=editor run_id=... similarity=1.0 changed_chars=0 passed=false
QUALITY_TRACE stage=final run_id=... passed=true
```

AiRun 或 AiRunStep 的 output snapshot 中保存摘要指标：

```json
{
  "target_chars": 2000,
  "actual_chars": 2180,
  "similarity": 0.89,
  "quality_passed": true
}
```

---

## 十一、兼容性和不可破坏项

当前工作区已有多项未提交修复，必须保留：

1. DeepSeek 慢模型使用非取消型 SSE keepalive，不能恢复成：

   ```python
   asyncio.wait_for(iterator.__anext__(), timeout=25)
   ```

2. 任务卡确认后正文为空时不得假完成。
3. 最终 SSE 已携带候选正文 content。
4. 创作素材选择器是 full_pipeline 的严格白名单。
5. 世界设定已有 `chapter_sequence_number` 迁移和章节归属规则。
6. 最后一轮澄清允许提交答案，并转入任务卡确认。
7. 现有人工审核、定稿本章、生成历史和写作记忆功能不得回归。
8. 不得修改客户端 Clash、系统代理或其他无关配置。

---

## 十二、测试要求

### 12.1 后端单元测试

新增 `chapter_quality` 测试，至少覆盖：

1. 目标 2000，实际 3999 → 长度失败；
2. 目标 2000，实际 2100 → 长度通过；
3. 包含 `【增强开始】` → 元信息失败；
4. 包含 Markdown 场景标题 → 失败；
5. 结尾停在逗号 → 失败；
6. Critic 要求修改且 similarity=1.0 → Editor 有效性失败；
7. Critic 无需修改且 similarity=1.0 → 可以通过；
8. 只删除增强标签，其余正文完全相同 → 不算有效编辑；
9. changed_chars 达标且所有硬规则通过 → 通过。

### 12.2 Workflow 测试

至少覆盖：

1. Writer 首次超长，第二次重写通过；
2. Writer 重试耗尽 → error，无 done；
3. Editor 未修改，第二次修改通过；
4. Editor 重试耗尽 → 不进入 human_review；
5. 最终质量通过 → 正常进入人工审核；
6. 不合格正文不创建 GenerationRecord；
7. 不合格正文不把 AiRun 标成 COMPLETED；
8. DeepSeek 风格慢 Provider 心跳不会取消生成；
9. 原有 task_card_review / clarification / revise 流程继续通过。

### 12.3 前端测试

至少覆盖：

1. `quality_check` SSE 被正确解析；
2. retrying 显示重试次数；
3. failed 显示具体 issues；
4. 失败时进度不是 100%；
5. QualityReport 传入 ApprovalModal；
6. “无需改写”和“编辑后的候选稿”文案正确区分。

### 12.4 必须执行的验证命令

根据实际项目脚本执行，至少包括：

```bash
cd /Users/zcx/ai-creative-platform/backend
source venv/bin/activate
pytest -q tests/test_chapter_quality.py
pytest -q tests/test_workflow_v2.py tests/test_task_card_review.py tests/test_sse_keepalive.py
pytest -q tests/test_smoke.py

cd /Users/zcx/ai-creative-platform/frontend
npm test -- --run
npm run build
```

还需要进行一次浏览器运行时验证：

- 正常合格生成可进入人工审核；
- 超长或含内部标记的模拟结果会触发重试；
- 重试耗尽显示错误，不出现假完成弹窗；
- 浏览器控制台无新增错误。

---

## 十三、验收标准

以下条件全部满足才算完成：

- [ ] 目标 2000 字时，3999 字正文不能进入人工审核；
- [ ] `【增强开始/结束】` 等内部标记不能进入最终候选稿；
- [ ] Critic 要求修改时，完全相同的 Editor 输出不能通过；
- [ ] 只删除内部标签不算有效编辑；
- [ ] Writer/Editor 重试均有上限；
- [ ] 重试耗尽不会发送假 done；
- [ ] 不合格内容不会创建 GenerationRecord；
- [ ] AiRun 状态能准确反映失败原因；
- [ ] 前端能显示质量指标和重试原因；
- [ ] 旧功能和现有修复全部通过回归测试；
- [ ] 提交最终总结：修改文件、架构变化、测试结果、未解决风险。

---

## 十四、实现原则

1. 硬规则优先用确定性代码，不要把所有判断都交给另一个 LLM。
2. LLM 负责文学语义判断，代码负责字数、格式、标记、相似度和状态机安全。
3. 不要通过简单截断正文满足字数；必须让 Writer/Editor 自然重写。
4. 不要为了让测试通过而降低阈值或绕过质量门。
5. 任何异常都不能落入“空正文 + COMPLETED + done”的假成功状态。
6. 保持修改可观察、可回滚，并在最终回复中提供清晰的验证证据。
