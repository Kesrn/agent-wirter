<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { ClarificationEmbedded, ClarificationQuestion, TaskCardClarificationStatus, TaskCardContextItem, TaskCardContextSummary, TaskCardPayload } from '../api/types'

const props = defineProps<{
  taskCard: TaskCardPayload
  projectId: string
  threadId: string
  clarification?: ClarificationEmbedded | null
  clarificationStatus?: TaskCardClarificationStatus | null
  contextSummary?: TaskCardContextSummary | null
}>()

const emit = defineEmits<{
  approved: [taskCard: TaskCardPayload, userNote?: string]
  rejected: []
  clarificationRefresh: [answers: Record<string, string>, userNote?: string]
  contextRefresh: [excludedKeys: string[]]
}>()

const editableTaskCard = ref<TaskCardPayload>(JSON.parse(JSON.stringify(props.taskCard)))
const rawJsonEdit = ref(false)
const jsonText = ref(JSON.stringify(editableTaskCard.value, null, 2))
const parseError = ref('')
const userNote = ref('')
const approving = ref(false)
const userNoteInput = ref<HTMLTextAreaElement | null>(null)
const clarificationAnswers = ref<Record<string, string | string[]>>({})
const clarificationError = ref('')
const excludedContextKeys = ref<string[]>([])
const initialExcludedContextKeys = ref<string[]>([])

// 后端会保留已回答的问题用于审计；前端只渲染仍待回答的问题，避免旧状态或
// 重规划事件意外携带 questions 时重复出现同一组澄清项。
const hasPendingClarification = computed(() => Boolean(props.clarification?.needs_clarification))
const clarificationQuestions = computed(() =>
  hasPendingClarification.value ? (props.clarification?.questions || []).slice(0, 3) : [],
)
const requiresClarificationAnswer = computed(() =>
  hasPendingClarification.value && Boolean(props.clarification?.require_answer),
)
const clarificationNotice = computed(() => {
  if (props.clarificationStatus === 'not_needed') {
    return {
      title: 'AI 澄清分析完成',
      message: 'AI 已分析当前章节的上下文与任务要求，无需进一步澄清。请确认任务卡后开始编写章节。',
    }
  }
  if (props.clarificationStatus === 'answered') {
    return {
      title: '澄清结果已应用',
      message: 'AI 已根据你的回答重新规划任务卡。请确认任务卡后开始编写章节。',
    }
  }
  if (props.clarificationStatus === 'skipped') {
    return {
      title: '已跳过生成前澄清',
      message: '本次将按当前大纲与上下文生成。请确认任务卡后开始编写章节。',
    }
  }
  return null
})

function answerText(value: string | string[] | undefined): string {
  return Array.isArray(value) ? value.join(', ') : (value || '')
}

function hasAnswer(question: ClarificationQuestion): boolean {
  return answerText(clarificationAnswers.value[question.id]).trim().length > 0
}

const missingRequiredQuestion = computed(() =>
  clarificationQuestions.value.find(question => question.required && !hasAnswer(question)),
)

const contextGroups = computed(() => {
  const summary = props.contextSummary
  if (!summary) return []
  const groups: Array<{ key: string; label: string; items: TaskCardContextItem[] }> = [
    { key: 'outlines', label: '章节大纲', items: summary.outlines || [] },
    { key: 'story_arcs', label: '长线结构', items: summary.story_arcs || [] },
    { key: 'characters', label: '人物状态', items: summary.characters || [] },
    { key: 'hidden_threads', label: '活跃伏笔', items: summary.hidden_threads || [] },
    { key: 'world_entries', label: '世界设定', items: summary.world_entries || [] },
    { key: 'confirmed_memories', label: '已确认记忆', items: summary.confirmed_memories || [] },
    { key: 'knowledge_sources', label: '资料来源', items: summary.knowledge_sources || [] },
  ]
  return groups.filter(group => group.items.length)
})

const contextItemCount = computed(() =>
  contextGroups.value.reduce((total, group) => total + group.items.length, 0)
    + (props.contextSummary?.previous_chapter_ending ? 1 : 0),
)

function normalizeKeys(keys: string[]): string[] {
  return [...new Set(keys)].sort()
}

watch(() => props.contextSummary, (summary) => {
  const keys = normalizeKeys(summary?.excluded_context_keys || [])
  excludedContextKeys.value = keys
  initialExcludedContextKeys.value = keys
}, { immediate: true })

const contextSelectionChanged = computed(() =>
  JSON.stringify(normalizeKeys(excludedContextKeys.value)) !== JSON.stringify(initialExcludedContextKeys.value),
)

function contextItemKey(item: TaskCardContextItem): string {
  return item.key || `${item.source_type}:${item.id || item.label}`
}

function contextIncluded(key: string): boolean {
  return !excludedContextKeys.value.includes(key)
}

function setContextIncluded(key: string, included: boolean) {
  const keys = new Set(excludedContextKeys.value)
  if (included) keys.delete(key)
  else keys.add(key)
  excludedContextKeys.value = normalizeKeys([...keys])
}

function handleContextRefresh() {
  if (approving.value || !contextSelectionChanged.value) return
  approving.value = true
  emit('contextRefresh', normalizeKeys(excludedContextKeys.value))
}

const contextSourceLabels: Record<string, string> = {
  current_outline: '本章大纲',
  selected_outline: '用户选择大纲',
  character: '人物资料',
  hidden_thread: '伏笔资料',
  world_entry: '世界设定',
  confirmed_memory: '已确认记忆',
  knowledge_rule: '知识库规则',
  knowledge_source: '知识库检索',
}

function contextSourceLabel(sourceType: string): string {
  if (sourceType.startsWith('story_arc:')) return `长线结构 · ${sourceType.split(':')[1].toUpperCase()}`
  return contextSourceLabels[sourceType] || sourceType
}

watch(() => props.taskCard, (newCard) => {
  editableTaskCard.value = JSON.parse(JSON.stringify(newCard))
  jsonText.value = JSON.stringify(editableTaskCard.value, null, 2)
  rawJsonEdit.value = false
  parseError.value = ''
  userNote.value = ''
  approving.value = false
  clarificationAnswers.value = {}
  clarificationError.value = ''
})

const infoRules = computed(() => editableTaskCard.value.information_rules || {})

function addScene() {
  const scenes = editableTaskCard.value.scenes || []
  scenes.push({ title: '', location: '', scene_goal: '', conflict: '', word_budget: 0 })
  editableTaskCard.value.scenes = scenes
}

function removeScene(index: number) {
  const scenes = [...(editableTaskCard.value.scenes || [])]
  scenes.splice(index, 1)
  editableTaskCard.value.scenes = scenes
}

function toggleJsonEdit() {
  if (rawJsonEdit.value) {
    applyRawJson()
  } else {
    jsonText.value = JSON.stringify(editableTaskCard.value, null, 2)
    rawJsonEdit.value = true
  }
}

function applyRawJson(): boolean {
  try {
    const parsed: unknown = JSON.parse(jsonText.value)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      throw new Error('任务卡 JSON 必须是对象')
    }
    editableTaskCard.value = parsed as TaskCardPayload
    parseError.value = ''
    rawJsonEdit.value = false
    return true
  } catch (e: unknown) {
    parseError.value = e instanceof Error ? e.message : 'JSON 格式错误'
    return false
  }
}

function handleApprove() {
  if (approving.value || (rawJsonEdit.value && !applyRawJson())) return
  if (contextSelectionChanged.value) {
    clarificationError.value = '上下文选择已修改，请先点击“应用选择并重新规划”。'
    return
  }
  const hasClarificationAnswer = Object.values(clarificationAnswers.value)
    .some(value => answerText(value).trim())
  if (hasClarificationAnswer) {
    clarificationError.value = '已填写澄清回答，请先点击“回答后重新规划”。'
    return
  }
  if (requiresClarificationAnswer.value && missingRequiredQuestion.value && !userNote.value.trim()) {
    clarificationError.value = `请先回答“${missingRequiredQuestion.value.question}”，或填写补充要求。`
    return
  }
  clarificationError.value = ''
  approving.value = true
  emit('approved', editableTaskCard.value, userNote.value.trim() || undefined)
}

function handleClarificationRefresh() {
  if (approving.value) return
  const answers = Object.fromEntries(
    Object.entries(clarificationAnswers.value)
      .map(([key, value]) => [key, answerText(value).trim()])
      .filter(([, value]) => value),
  ) as Record<string, string>
  if (!Object.keys(answers).length && !userNote.value.trim()) {
    clarificationError.value = '请至少回答一个问题或填写补充要求。'
    return
  }
  clarificationError.value = ''
  approving.value = true
  emit('clarificationRefresh', answers, userNote.value.trim() || undefined)
}

function handleReject() {
  if (approving.value) return
  emit('rejected')
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.preventDefault()
    handleReject()
  }
}

onMounted(() => {
  window.addEventListener('keydown', handleKeydown)
  nextTick(() => userNoteInput.value?.focus())
})

onBeforeUnmount(() => window.removeEventListener('keydown', handleKeydown))
</script>

<template>
  <div class="task-card-review-overlay">
    <section
      class="task-card-review"
      role="dialog"
      aria-modal="true"
      aria-labelledby="task-card-review-title"
    >
    <div class="card-header">
      <h3 id="task-card-review-title">章节任务卡</h3>
      <p class="card-subtitle">
        AI 已规划好本章结构。请在下方补充关键要求或调整任务卡，然后点击生成。
      </p>
    </div>

    <div class="card-scroll">
      <section v-if="clarificationNotice" class="clarification-status-notice" role="status">
        <strong>{{ clarificationNotice.title }}</strong>
        <p>{{ clarificationNotice.message }}</p>
      </section>

      <div class="user-note-section">
        <div class="user-note-header">
          <label class="user-note-label" for="task-card-user-note">
            补充要求 / 方向确认
            <span class="optional-badge">选填</span>
          </label>
        </div>
        <textarea
          v-model="userNote"
          ref="userNoteInput"
          id="task-card-user-note"
          rows="3"
          class="form-textarea"
          placeholder="例如：本章重点写XX情节、注意YY伏笔、保持ZZ风格..."
        ></textarea>
        <p class="user-note-hint">这里可以补充任何对本章的特殊要求、风格偏好或重点提示。</p>
      </div>

      <section v-if="clarificationQuestions.length" class="clarification-section" aria-labelledby="task-card-clarification-title">
        <div class="section-divider"></div>
        <h4 id="task-card-clarification-title">可选澄清</h4>
        <p class="clarification-hint">回答后会重新规划任务卡，并停留在这里供你确认。</p>
        <div v-for="question in clarificationQuestions" :key="question.id" class="clarification-question">
          <label class="question-label" :for="`task-card-question-${question.id}`">
            {{ question.question }}
            <span v-if="question.required" class="required-badge">必答</span>
          </label>
          <p v-if="question.reason" class="question-reason">{{ question.reason }}</p>
          <div v-if="question.type === 'single_choice'" class="question-options">
            <label v-for="option in (question.options || [])" :key="option.value" class="option-label">
              <input v-model="clarificationAnswers[question.id]" type="radio" :name="`task-card-question-${question.id}`" :value="option.value" />
              <span>{{ option.label }}</span>
            </label>
          </div>
          <div v-else-if="question.type === 'multi_choice'" class="question-options">
            <label v-for="option in (question.options || [])" :key="option.value" class="option-label">
              <input v-model="clarificationAnswers[question.id]" type="checkbox" :value="option.value" />
              <span>{{ option.label }}</span>
            </label>
          </div>
          <input
            v-else-if="question.type === 'number'"
            :id="`task-card-question-${question.id}`"
            v-model="clarificationAnswers[question.id]"
            type="number"
            class="form-input form-input-sm"
          />
          <textarea
            v-else
            :id="`task-card-question-${question.id}`"
            v-model="clarificationAnswers[question.id]"
            rows="2"
            class="form-textarea"
          ></textarea>
        </div>
        <p v-if="clarificationError" class="clarification-error" role="alert">{{ clarificationError }}</p>
        <div class="clarification-actions">
          <button type="button" class="btn-refresh-card" :disabled="approving" @click="handleClarificationRefresh">
            {{ approving ? '重新规划中…' : '回答后重新规划' }}
          </button>
          <span class="clarification-skip-hint">也可以直接按下方按钮，按当前任务卡生成</span>
        </div>
      </section>

      <details v-if="contextItemCount" class="context-preview">
        <summary>查看本次上下文 <span>{{ contextItemCount }} 项</span></summary>
        <div class="context-preview-body">
          <section v-if="contextSummary?.previous_chapter_ending" class="context-group">
            <h4>前章结尾</h4>
            <label class="context-row context-toggle-row">
              <input
                type="checkbox"
                :checked="contextIncluded('previous_chapter_ending')"
                @change="setContextIncluded('previous_chapter_ending', ($event.target as HTMLInputElement).checked)"
              />
              <span class="previous-ending">{{ contextSummary.previous_chapter_ending }}</span>
            </label>
          </section>
          <section v-for="group in contextGroups" :key="group.key" class="context-group">
            <h4>{{ group.label }}</h4>
            <div class="context-rows">
              <label v-for="item in group.items" :key="contextItemKey(item)" class="context-row context-toggle-row">
                <input
                  type="checkbox"
                  :checked="contextIncluded(contextItemKey(item))"
                  @change="setContextIncluded(contextItemKey(item), ($event.target as HTMLInputElement).checked)"
                />
                <div class="context-row-main">
                  <strong>{{ item.label }}</strong>
                  <span v-if="item.detail">{{ item.detail }}</span>
                </div>
                <div class="context-row-meta">
                  <span>{{ contextSourceLabel(item.source_type) }}</span>
                  <span v-if="item.selected" class="selected-source">用户选择</span>
                  <span v-if="item.chapter_sequence_number">第 {{ item.chapter_sequence_number }} 章</span>
                </div>
              </label>
            </div>
          </section>
          <div class="context-refresh-actions">
            <button type="button" class="btn-refresh-card" :disabled="approving || !contextSelectionChanged" @click="handleContextRefresh">
              {{ approving ? '重新规划中…' : '应用选择并重新规划' }}
            </button>
            <span>取消勾选只影响本次生成，不会删除正式资料。</span>
          </div>
        </div>
      </details>

      <!-- 任务卡详情（表单视图） -->
      <div v-if="!rawJsonEdit" class="card-body field-view">
      <!-- 章节信息 -->
      <div class="field-group">
        <label>章节标题</label>
        <input v-model="editableTaskCard.chapter_title" type="text" class="form-input" />
      </div>

      <div class="field-group">
        <label>核心任务</label>
        <textarea v-model="editableTaskCard.core_task" rows="2" class="form-textarea"></textarea>
      </div>

      <div class="field-group">
        <label>开篇锚点（如何承接上一章结尾）</label>
        <textarea v-model="editableTaskCard.opening_anchor" rows="2" class="form-textarea"></textarea>
      </div>

      <!-- 场景划分 -->
      <div class="field-group">
        <label>场景划分</label>
        <div v-for="(scene, idx) in (editableTaskCard.scenes || [])" :key="idx" class="scene-item">
          <div class="scene-header">
            <span>场景 {{ idx + 1 }}</span>
            <button type="button" class="btn-icon-danger" :aria-label="`移除场景 ${idx + 1}`" @click="removeScene(idx)">✕</button>
          </div>
          <div class="scene-fields">
            <input v-model="scene.title" type="text" placeholder="场景标题" class="form-input form-input-sm" />
            <input v-model="scene.location" type="text" placeholder="地点" class="form-input form-input-sm" />
            <textarea v-model="scene.scene_goal" rows="1" placeholder="场景目标" class="form-textarea form-textarea-sm"></textarea>
            <input v-model="scene.conflict" type="text" placeholder="冲突" class="form-input form-input-sm" />
            <input v-model.number="scene.word_budget" type="number" placeholder="字数预算" class="form-input form-input-sm" min="0" />
          </div>
        </div>
        <button type="button" class="btn-add-scene" @click="addScene">+ 添加场景</button>
      </div>

      <!-- 信息规则 -->
      <div class="field-group">
        <label>禁止揭示</label>
        <textarea
          :value="(infoRules.forbidden || []).join(', ')"
          @input="(e: Event) => { const v = (e.target as HTMLTextAreaElement).value; infoRules.forbidden = v ? v.split(',').map(s => s.trim()).filter(Boolean) : []; }"
          placeholder="用逗号分隔"
          rows="2"
          class="form-textarea"
        ></textarea>
      </div>
      <div class="field-group">
        <label>仅暗示（不可明确揭示）</label>
        <textarea
          :value="(infoRules.hint_only || []).join(', ')"
          @input="(e: Event) => { const v = (e.target as HTMLTextAreaElement).value; infoRules.hint_only = v ? v.split(',').map(s => s.trim()).filter(Boolean) : []; }"
          placeholder="用逗号分隔"
          rows="2"
          class="form-textarea"
        ></textarea>
      </div>

      <!-- 禁止事项 -->
      <div class="field-group">
        <label>禁止事项</label>
        <textarea
          :value="(editableTaskCard.forbidden || []).join(', ')"
          @input="(e: Event) => { const v = (e.target as HTMLTextAreaElement).value; editableTaskCard.forbidden = v ? v.split(',').map(s => s.trim()).filter(Boolean) : []; }"
          placeholder="用逗号分隔"
          rows="2"
          class="form-textarea"
        ></textarea>
      </div>

      <!-- 字数预算 -->
      <div class="field-group">
        <label>总字数预算</label>
        <input v-model.number="editableTaskCard.word_budget" type="number" class="form-input form-input-sm" min="0" />
      </div>

      <button type="button" class="btn-toggle-json" @click="toggleJsonEdit">JSON 编辑</button>
      </div>

      <div v-else class="card-body json-edit">
      <div class="field-group">
        <label>任务卡 JSON（直接编辑原始 JSON）</label>
        <textarea v-model="jsonText" rows="20" class="form-textarea json-area" spellcheck="false"></textarea>
        <div v-if="parseError" class="parse-error">{{ parseError }}</div>
        <button type="button" class="btn-toggle-json" @click="toggleJsonEdit">返回表单视图</button>
      </div>
      </div>

    </div>

    <div class="card-actions">
      <button type="button" class="btn-cancel" :disabled="approving" @click="handleReject">取消生成</button>
      <button type="button" class="btn-generate" :disabled="approving" @click="handleApprove">
        {{ approving ? '提交中…' : '按此任务卡生成' }}
      </button>
    </div>
    </section>
  </div>
</template>

<style scoped>
.task-card-review-overlay {
  position: fixed;
  inset: 0;
  z-index: 4500;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 32px;
  background: color-mix(in srgb, #020617 62%, transparent);
  backdrop-filter: blur(10px);
  /* M-2: 修复 agent-hidden 时 visibility:hidden 继承导致弹窗不可见 */
  visibility: visible;
}

.task-card-review {
  width: min(960px, calc(100vw - 48px));
  max-height: min(88vh, 920px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid color-mix(in srgb, var(--border, #2a3342) 88%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-panel, #111827) 96%, #020617);
  box-shadow:
    0 26px 80px rgba(0, 0, 0, 0.45),
    0 1px 0 rgba(255, 255, 255, 0.06) inset;
}

.card-header {
  flex: 0 0 auto;
  padding: 22px 26px 18px;
  border-bottom: 1px solid color-mix(in srgb, var(--border, #2a3342) 88%, transparent);
}

.card-scroll {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: 22px 26px 18px;
}

.card-header h3 {
  margin: 0 0 var(--sp-1) 0;
  font-size: 20px;
  font-weight: 800;
  color: var(--text, #f8fafc);
}
.card-subtitle {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--text-tertiary, #94a3b8);
}

.field-group {
  margin-bottom: var(--sp-3);
}
.field-group label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-secondary, #cbd5e1);
  margin-bottom: var(--sp-1);
}

.form-input {
  width: 100%;
  padding: 6px 10px;
  border: 1px solid color-mix(in srgb, var(--border, #2a3342) 88%, transparent);
  border-radius: 6px;
  background: color-mix(in srgb, var(--bg, #0b1120) 82%, transparent);
  color: var(--text, #f8fafc);
  font-size: 13px;
}
.form-input-sm {
  width: 100%;
}
.form-textarea {
  width: 100%;
  padding: 6px 10px;
  border: 1px solid color-mix(in srgb, var(--border, #2a3342) 88%, transparent);
  border-radius: 6px;
  background: color-mix(in srgb, var(--bg, #0b1120) 82%, transparent);
  color: var(--text, #f8fafc);
  font-size: 13px;
  resize: vertical;
}
.form-textarea-sm {
  min-height: 32px;
}

.scene-item {
  border: 1px solid color-mix(in srgb, var(--border, #2a3342) 78%, transparent);
  border-radius: 8px;
  padding: var(--sp-2);
  margin-bottom: var(--sp-2);
  background: color-mix(in srgb, var(--bg, #0b1120) 52%, transparent);
}
.scene-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 13px;
  font-weight: 600;
  margin-bottom: var(--sp-1);
}
.scene-fields {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.btn-icon-danger {
  background: none;
  border: none;
  color: var(--status-error, #ef4444);
  cursor: pointer;
  font-size: 14px;
  padding: 0 4px;
}

.btn-add-scene {
  background: none;
  border: 1px dashed color-mix(in srgb, var(--border, #2a3342) 90%, transparent);
  border-radius: 6px;
  color: var(--accent, #3b82f6);
  cursor: pointer;
  font-size: 13px;
  padding: 8px 12px;
  width: 100%;
}

.btn-toggle-json {
  background: none;
  border: none;
  color: var(--text-tertiary, #94a3b8);
  cursor: pointer;
  font-size: 12px;
  text-decoration: underline;
  padding: 0;
}

.card-actions {
  flex: 0 0 auto;
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  padding: 16px 26px;
  border-top: 1px solid color-mix(in srgb, var(--border, #2a3342) 88%, transparent);
  background: color-mix(in srgb, var(--bg-panel, #111827) 92%, #020617);
}

.btn-cancel {
  min-height: 38px;
  padding: 0 20px;
  border: 1px solid color-mix(in srgb, var(--border, #2a3342) 90%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-panel, #111827) 88%, transparent);
  color: var(--text-secondary, #cbd5e1);
  cursor: pointer;
  font-size: 14px;
}
.btn-cancel:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.btn-generate {
  min-height: 38px;
  padding: 0 22px;
  border: none;
  border-radius: 8px;
  background: var(--accent, #3b82f6);
  color: var(--text-inverse, #fff);
  cursor: pointer;
  font-size: 14px;
  font-weight: 700;
}
.btn-generate:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.json-area {
  font-family: monospace;
  font-size: 12px;
}
.parse-error {
  color: var(--status-error, #ef4444);
  font-size: 12px;
  margin-top: var(--sp-1);
}

.user-note-section {
  padding: var(--sp-3) 0;
  border-bottom: 1px solid color-mix(in srgb, var(--border, #2a3342) 70%, transparent);
}
.clarification-status-notice {
  margin-top: var(--sp-3);
  padding: 12px 14px;
  border: 1px solid color-mix(in srgb, var(--color-accent, #60a5fa) 42%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--color-accent, #60a5fa) 10%, transparent);
  color: var(--text-secondary, #cbd5e1);
  font-size: 13px;
  line-height: 1.6;
}
.clarification-status-notice strong { color: var(--text, #f8fafc); }
.clarification-status-notice p { margin: 3px 0 0; }
.user-note-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: var(--sp-2);
}
.user-note-header .user-note-label {
  margin-bottom: 0;
}
.user-note-label {
  display: block;
  font-size: 14px;
  font-weight: 600;
  margin-bottom: var(--sp-2);
  color: var(--text, #f8fafc);
}
.optional-badge {
  font-size: 11px;
  font-weight: 400;
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
  line-height: 1.5;
}

.clarification-section {
  margin-top: 20px;
  padding-top: 4px;
}
.section-divider {
  border-top: 1px solid color-mix(in srgb, var(--border, #2a3342) 88%, transparent);
  margin-bottom: 16px;
}
.clarification-section h4 {
  margin: 0 0 6px;
  color: var(--text, #f8fafc);
  font-size: 15px;
}
.clarification-hint,
.question-reason,
.clarification-skip-hint {
  color: var(--text-muted, #94a3b8);
  font-size: 12px;
}
.clarification-hint { margin: 0 0 14px; }
.clarification-question { margin: 0 0 14px; }
.question-label { display: block; margin-bottom: 7px; font-weight: 700; }
.question-reason { margin: -2px 0 8px; }
.question-options { display: grid; gap: 7px; }
.option-label { display: flex; gap: 8px; align-items: flex-start; }
.required-badge { color: var(--color-danger, #f87171); font-size: 11px; }
.clarification-error { margin: 8px 0; color: var(--color-danger, #f87171); font-size: 12px; }
.clarification-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.btn-refresh-card {
  border: 1px solid var(--color-accent, #60a5fa);
  border-radius: 6px;
  padding: 8px 12px;
  background: transparent;
  color: var(--color-accent, #60a5fa);
  cursor: pointer;
}
.btn-refresh-card:disabled { cursor: not-allowed; opacity: .55; }

.context-preview {
  margin: 20px 0 4px;
  border-top: 1px solid color-mix(in srgb, var(--border, #2a3342) 88%, transparent);
  border-bottom: 1px solid color-mix(in srgb, var(--border, #2a3342) 70%, transparent);
}
.context-preview summary {
  padding: 13px 0;
  color: var(--text, #f8fafc);
  cursor: pointer;
  font-size: 14px;
  font-weight: 700;
}
.context-preview summary span { margin-left: 6px; color: var(--text-muted, #94a3b8); font-weight: 400; }
.context-preview-body { padding: 0 0 14px; }
.context-group + .context-group { margin-top: 16px; }
.context-group h4 { margin: 0 0 7px; font-size: 12px; color: var(--text-muted, #94a3b8); }
.previous-ending { margin: 0; white-space: pre-wrap; line-height: 1.6; }
.context-rows { display: grid; gap: 8px; }
.context-row { display: flex; justify-content: space-between; gap: 14px; padding: 8px 0; border-bottom: 1px solid color-mix(in srgb, var(--border, #2a3342) 48%, transparent); }
.context-toggle-row { cursor: pointer; align-items: flex-start; }
.context-toggle-row > input { flex: 0 0 auto; margin-top: 3px; }
.context-row-main { min-width: 0; display: grid; gap: 3px; }
.context-row-main strong { font-size: 13px; }
.context-row-main span { color: var(--text-secondary, #cbd5e1); font-size: 12px; overflow-wrap: anywhere; }
.context-row-meta { flex: 0 0 auto; display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; color: var(--text-muted, #94a3b8); font-size: 11px; }
.selected-source { color: var(--color-accent, #60a5fa); }
.context-refresh-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; margin-top: 14px; color: var(--text-muted, #94a3b8); font-size: 11px; }

@media (max-width: 720px) {
  .task-card-review-overlay {
    align-items: stretch;
    padding: 14px;
  }

  .task-card-review {
    width: 100%;
    max-height: calc(100vh - 28px);
  }

  .card-header,
  .card-scroll,
  .card-actions {
    padding-left: 16px;
    padding-right: 16px;
  }

  .card-actions {
    flex-direction: column-reverse;
  }

  .btn-cancel,
  .btn-generate {
    width: 100%;
  }

  .clarification-actions { align-items: stretch; flex-direction: column; }
  .btn-refresh-card { width: 100%; }
  .context-row { flex-direction: column; }
  .context-row-meta { justify-content: flex-start; }
  .context-refresh-actions { align-items: stretch; flex-direction: column; }
}
</style>
