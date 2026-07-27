# 任务卡预览界面改进 - 永远显示用户补充区

## 问题背景

之前的实现是"按需澄清"：
- AI 判断 `needs_clarification: true` 时才显示澄清问题
- 如果 AI 认为信息足够（`needs_clarification: false`），用户直接看到任务卡确认界面
- **问题**：用户无法在不需要澄清时补充自己的特殊要求或方向偏好

这种设计在小说生成场景下不够稳定，用户体验不一致。

## 改进方案

**新的交互流程**：
1. **永远显示"用户补充要求"输入区**（不管 AI 是否判断需要澄清）
2. AI 判断的澄清问题作为可选项，按需显示在补充区下方
3. 用户可以：
   - 只填补充要求就直接生成
   - 回答 AI 问题并刷新任务卡
   - 跳过 AI 问题，按当前任务卡生成

## 修改的文件

### 1. `frontend/src/components/TaskCardReviewPanel.vue`

#### 脚本部分变更

**添加新状态**：
```typescript
// 用户补充要求（永远显示）
const userNote = ref('')

const hasAIQuestions = computed(() =>
  Boolean(props.clarification?.needs_clarification &&
         !clarificationHidden.value &&
         (props.clarification?.questions || []).length > 0)
)
```

**更新 emit 签名**：
```typescript
emit: {
  approved: [taskCard: TaskCardPayload, userNote?: string]  // 添加 userNote 参数
  // ... 其他保持不变
}
```

**更新提交逻辑**：
```typescript
function handleApprove() {
  emit('approved', editableTaskCard.value, userNote.value.trim() || undefined)
}
```

#### 模板部分变更

**新的UI结构**：
```vue
<div class="card-scroll">
  <!-- 1. 用户补充要求区（永远显示） -->
  <div class="user-note-section">
    <label class="user-note-label">
      补充要求 / 方向确认
      <span class="optional-badge">选填</span>
    </label>
    <textarea
      v-model="userNote"
      rows="3"
      placeholder="例如：本章重点写XX情节、注意YY伏笔、保持ZZ风格..."
    ></textarea>
    <p class="user-note-hint">
      💡 这里可以补充任何对本章的特殊要求、风格偏好或重点提示
    </p>
  </div>

  <!-- 2. AI 判断的澄清问题（按需显示） -->
  <div v-if="hasAIQuestions" class="clarification-section">
    <div class="clarification-kicker">{{ clarificationRoundLabel }}</div>
    <h4>AI 认为以下信息可能影响本章质量</h4>
    <p class="clarification-hint">回答后 AI 会重新判断并更新任务卡。也可以跳过直接生成。</p>
    <!-- AI 问题列表 -->
  </div>

  <!-- 3. 任务卡详情（表单视图） -->
  <div class="card-body field-view">
    <!-- 章节标题、场景划分等 -->
  </div>
</div>
```

**按钮逻辑变更**：
```vue
<div class="card-actions">
  <button class="btn-cancel" @click="handleReject">取消生成</button>

  <!-- 如果有 AI 问题，显示"回答问题"按钮 -->
  <button
    v-if="hasAIQuestions"
    class="btn-refresh-card"
    :disabled="!canSubmitClarification"
    @click="submitClarification"
  >
    回答 AI 问题并刷新任务卡
  </button>

  <!-- 生成按钮永远显示，文案根据是否有问题变化 -->
  <button class="btn-generate" @click="handleApprove">
    {{ hasAIQuestions ? '跳过问题，按当前任务卡生成' : '按此任务卡生成' }}
  </button>
</div>
```

#### 样式变更

新增样式：
```css
.user-note-section {
  padding: var(--sp-3) 0;
  border-bottom: 1px solid color-mix(in srgb, var(--border, #2a3342) 70%, transparent);
}

.user-note-label {
  display: block;
  font-size: 14px;
  font-weight: 600;
  margin-bottom: var(--sp-2);
}

.optional-badge {
  font-size: 11px;
  background: color-mix(in srgb, var(--text-tertiary, #94a3b8) 20%, transparent);
  color: var(--text-tertiary, #94a3b8);
  padding: 2px 6px;
  border-radius: 4px;
  margin-left: 8px;
}

.user-note-hint {
  font-size: 12px;
  color: var(--text-tertiary, #94a3b8);
  margin: var(--sp-1) 0 0 0;
}
```

### 2. `frontend/src/components/AgentPanel.vue`

**更新事件处理器**：
```typescript
async function handleTaskCardApproved(taskCard: TaskCardPayload, userNote?: string) {
  showTaskCardReview.value = false
  const threadId = hitlThreadId.value
  if (!threadId) {
    ui.showToast('缺少 thread_id，无法继续生成', 'error')
    return
  }
  hitlResuming = true
  expertStore.setGenerating(pid.value, true)
  expertStore.updateStepStatus(pid.value, 'writer', 'running')
  currentAbort = new AbortController()
  try {
    const requestBody = userNote ? { user_note: userNote } : undefined
    await api.resumeGeneration(
      pid.value,
      threadId,
      'approve_task_card',
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
      undefined,
      currentAbort.signal,
      props.mode,
      JSON.stringify(taskCard),
      requestBody,  // 传递 user_note
    )
  } catch (e: unknown) {
    // ... 错误处理
  }
}
```

### 3. `backend/api/routes.py`

**approve_task_card 处理逻辑**：
```python
# 在 4701-4706 行后添加
update_state: dict = {
    "task_card_reviewed": True,
}
if modified_card:
    update_state["modified_task_card"] = modified_card
    update_state["chapter_task_card"] = modified_card

# 从 body_data 读取用户补充要求
user_note = body_data.get("user_note", "")
if user_note:
    update_state["user_note"] = user_note  # 传递给 chapter_writer_node
```

**workflow 使用**：
在 `backend/agents/workflow_v2.py` 的 `chapter_writer_node` 中，`user_note` 已经从 state 中读取并用于生成 prompt：
```python
user_note = state.get("user_note", "")
if user_note:
    direction_block += f"\n## 用户补充要求\n{user_note}\n"
```

## 交互流程对比

### 旧流程（按需澄清）

```
用户发起生成
  ↓
AI 判断是否需要澄清
  ↓
需要 → 显示澄清问题（只有问题）
  ↓
用户回答 → AI 重新判断 → 继续循环或进入任务卡确认
  ↓
不需要 → 直接显示任务卡确认（用户无法补充要求）
  ↓
用户确认 → 开始生成
```

**问题**：
- 如果 AI 判断不需要澄清，用户没有机会补充特殊要求
- 体验不一致，有时能补充有时不能

### 新流程（永远可补充）

```
用户发起生成
  ↓
AI 生成任务卡 + 判断是否需要澄清
  ↓
显示界面：
  ┌─────────────────────────┐
  │ 用户补充要求（永远显示） │  ← 用户可以随时填写
  ├─────────────────────────┤
  │ AI 澄清问题（按需显示）  │  ← AI 认为需要时才显示
  ├─────────────────────────┤
  │ 任务卡详情（可编辑）     │
  └─────────────────────────┘
  ↓
用户可选择：
  1. 只填补充要求，直接生成
  2. 回答 AI 问题 → 刷新任务卡 → 重新判断 → 继续循环
  3. 跳过 AI 问题，按当前任务卡生成
  ↓
开始生成（user_note 传递给 chapter_writer）
```

**优势**：
- ✅ 用户永远可以补充特殊要求
- ✅ 体验一致，不会因为 AI 判断而变化
- ✅ AI 问题作为可选增强，不阻塞用户
- ✅ 用户有更多控制权

## 使用场景举例

### 场景 1：AI 认为信息足够

```
[用户补充要求区]
例如：本章重点写主角内心挣扎，节奏要慢一些

[任务卡]
章节标题：抉择时刻
核心任务：主角在两难选择中做出决定
...

按钮：[按此任务卡生成]
```

用户可以直接在补充区输入要求，点击生成。

### 场景 2：AI 认为缺少关键信息

```
[用户补充要求区]
例如：本章要为后续伏笔埋下线索

[AI 澄清问题]
第 1/2 轮澄清
AI 认为以下信息可能影响本章质量
1. 这一章最重要的推进目标是什么？
   ○ 适应新环境
   ● 触发冲突
原因：章节目标决定 writer 的事件选择

按钮：[回答 AI 问题并刷新任务卡] [跳过问题，按当前任务卡生成]
```

用户可以：
- 回答问题 → AI 根据回答更新任务卡
- 或跳过问题，直接用当前任务卡生成

## 技术细节

### 状态传递链

```
TaskCardReviewPanel (userNote)
  ↓ emit('approved', taskCard, userNote)
AgentPanel (handleTaskCardApproved)
  ↓ api.resumeGeneration(..., { user_note: userNote })
Backend routes.py (approve_task_card)
  ↓ update_state["user_note"] = user_note
LangGraph State (workflow_v2.py)
  ↓ chapter_writer_node 读取 state.get("user_note")
Prompt 构建
  ↓ ## 用户补充要求\n{user_note}
LLM 生成
```

### 兼容性

- ✅ 如果用户不填 `userNote`，行为与之前一致
- ✅ 旧的澄清逻辑完全保留，只是 UI 调整
- ✅ 后端已经支持 `user_note`，只是现在从前端新入口传入

## 未来扩展方向

1. **模式配置**：
   - 快速模式：不强制澄清
   - 计划模式：每次至少问 1 轮
   - 严格模式：最多 3-5 轮，直到 AI 判断足够明确

2. **历史记忆**：
   - 记住用户常用的补充要求
   - 提供"常用要求"快捷选项

3. **智能建议**：
   - 根据上下文，在补充区预填建议的方向

## 总结

这个改进让任务卡预览界面的交互更稳定、更可预测：
- 用户永远可以补充自己的要求（不依赖 AI 判断）
- AI 问题作为可选增强，不阻塞主流程
- 更接近 "Codex 计划模式"的体验
- 提升了用户对生成过程的控制感
