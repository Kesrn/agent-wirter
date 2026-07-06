<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import type { TaskCardPayload, ClarificationEmbedded, ClarificationQuestion } from '../api/types'

const props = defineProps<{
  taskCard: TaskCardPayload
  projectId: string
  threadId: string
  clarification?: ClarificationEmbedded | null
}>()

const emit = defineEmits<{
  approved: [taskCard: TaskCardPayload]
  rejected: []
  clarificationAnswered: [answers: Record<string, string>, questions: ClarificationQuestion[]]
  clarificationSkipped: []
}>()

const editableTaskCard = ref<TaskCardPayload>(JSON.parse(JSON.stringify(props.taskCard)))
const rawJsonEdit = ref(false)
const jsonText = ref(JSON.stringify(editableTaskCard.value, null, 2))
const parseError = ref('')

// L-2: 澄清
const clarificationAnswers = ref<Record<string, string | string[] | number>>({})
const clarificationHidden = ref(false)

function isAnswered(value: unknown): boolean {
  if (value === undefined || value === null) return false
  if (typeof value === 'string') return value.trim().length > 0
  if (Array.isArray(value)) return value.length > 0
  if (typeof value === 'number') return true
  return false
}

const canSubmitClarification = computed(() => {
  const qs = props.clarification?.questions || []
  if (!qs.length) return false
  return qs.every(q => !q.required || isAnswered(clarificationAnswers.value[q.id]))
})

watch(() => props.taskCard, (newCard) => {
  editableTaskCard.value = JSON.parse(JSON.stringify(newCard))
  jsonText.value = JSON.stringify(editableTaskCard.value, null, 2)
  rawJsonEdit.value = false
  parseError.value = ''
  clarificationAnswers.value = {}
  clarificationHidden.value = false
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
    // 从 JSON 解析回 task card
    try {
      const parsed = JSON.parse(jsonText.value)
      editableTaskCard.value = parsed
      parseError.value = ''
      rawJsonEdit.value = false
    } catch (e: unknown) {
      parseError.value = e instanceof Error ? e.message : 'JSON 格式错误'
    }
  } else {
    jsonText.value = JSON.stringify(editableTaskCard.value, null, 2)
    rawJsonEdit.value = true
  }
}

function handleApprove() {
  emit('approved', editableTaskCard.value)
}

function handleReject() {
  emit('rejected')
}

// L-2: 澄清
function submitClarification() {
  const qs = props.clarification?.questions || []
  // 将所有值转为字符串（multi_choice 用逗号拼接，number 转字符串）
  const stringAnswers: Record<string, string> = {}
  for (const [key, val] of Object.entries(clarificationAnswers.value)) {
    if (Array.isArray(val)) stringAnswers[key] = val.join(', ')
    else if (val !== undefined && val !== null) stringAnswers[key] = String(val)
  }
  emit('clarificationAnswered', stringAnswers, qs)
}

function skipClarification() {
  clarificationHidden.value = true
  emit('clarificationSkipped')
}
</script>

<template>
  <div class="task-card-review">
    <div class="card-header">
      <h3>📋 章节任务卡</h3>
      <p class="card-subtitle">AI 已规划好本章结构，如需修改请在下方调整，然后点击生成。</p>
    </div>

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
            <button class="btn-icon-danger" @click="removeScene(idx)" title="移除场景">✕</button>
          </div>
          <div class="scene-fields">
            <input v-model="scene.title" type="text" placeholder="场景标题" class="form-input form-input-sm" />
            <input v-model="scene.location" type="text" placeholder="地点" class="form-input form-input-sm" />
            <textarea v-model="scene.scene_goal" rows="1" placeholder="场景目标" class="form-textarea form-textarea-sm"></textarea>
            <input v-model="scene.conflict" type="text" placeholder="冲突" class="form-input form-input-sm" />
            <input v-model.number="scene.word_budget" type="number" placeholder="字数预算" class="form-input form-input-sm" min="0" />
          </div>
        </div>
        <button class="btn-add-scene" @click="addScene">+ 添加场景</button>
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

      <button class="btn-toggle-json" @click="toggleJsonEdit">JSON 编辑</button>
    </div>

    <div v-else class="card-body json-edit">
      <div class="field-group">
        <label>任务卡 JSON（直接编辑原始 JSON）</label>
        <textarea v-model="jsonText" rows="20" class="form-textarea json-area" spellcheck="false"></textarea>
        <div v-if="parseError" class="parse-error">{{ parseError }}</div>
        <button class="btn-toggle-json" @click="toggleJsonEdit">返回表单视图</button>
      </div>
    </div>

    <!-- L-2: 澄清区 -->
    <div v-if="clarification?.needs_clarification && !clarificationHidden" class="clarification-section">
      <div class="section-divider"></div>
      <h4>💡 以下问题可能影响本章方向</h4>
      <p class="clarification-hint">回答后可刷新任务卡，也可直接跳过。</p>
      <div v-for="q in (clarification?.questions || [])" :key="q.id" class="clarification-question">
        <p class="q-text">
          {{ q.question }} <span v-if="q.required" class="required-badge">必答</span>
        </p>
        <p v-if="q.reason" class="q-reason">原因：{{ q.reason }}</p>
        <!-- single_choice -->
        <div v-if="q.type === 'single_choice'" class="q-options">
          <label v-for="opt in (q.options || [])" :key="opt.value" class="option-label">
            <input type="radio" :value="opt.value" v-model="clarificationAnswers[q.id]" />
            {{ opt.label }}
            <span v-if="opt.description" class="option-desc"> — {{ opt.description }}</span>
          </label>
        </div>
        <!-- multi_choice -->
        <div v-else-if="q.type === 'multi_choice'" class="q-options">
          <label v-for="opt in (q.options || [])" :key="opt.value" class="option-label">
            <input type="checkbox" :value="opt.value" v-model="clarificationAnswers[q.id]" />
            {{ opt.label }}
          </label>
        </div>
        <!-- free_text -->
        <textarea v-else-if="q.type === 'free_text'" v-model="clarificationAnswers[q.id]" rows="2" class="form-textarea" />
        <!-- number -->
        <input v-else-if="q.type === 'number'" v-model.number="clarificationAnswers[q.id]" type="number" class="form-input form-input-sm" />
      </div>
      <div class="clarification-actions">
        <button class="btn-skip-clarification" @click="skipClarification">跳过澄清</button>
        <button class="btn-refresh-card" :disabled="!canSubmitClarification" @click="submitClarification">
          提交回答并刷新任务卡
        </button>
      </div>
    </div>

    <div class="card-actions">
      <button class="btn-cancel" @click="handleReject">取消生成</button>
      <button class="btn-generate" @click="handleApprove">按此任务卡生成</button>
    </div>
  </div>
</template>

<style scoped>
.task-card-review {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: var(--sp-4);
  background: var(--color-surface);
  margin-bottom: var(--sp-4);
}

.card-header h3 {
  margin: 0 0 var(--sp-1) 0;
  font-size: 16px;
}
.card-subtitle {
  margin: 0 0 var(--sp-4) 0;
  font-size: 13px;
  color: var(--color-text-muted);
}

.field-group {
  margin-bottom: var(--sp-3);
}
.field-group label {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  margin-bottom: var(--sp-1);
}

.form-input {
  width: 100%;
  padding: 6px 10px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  font-size: 13px;
}
.form-input-sm {
  width: 100%;
}
.form-textarea {
  width: 100%;
  padding: 6px 10px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  font-size: 13px;
  resize: vertical;
}
.form-textarea-sm {
  min-height: 32px;
}

.scene-item {
  border: 1px dashed var(--color-border);
  border-radius: 4px;
  padding: var(--sp-2);
  margin-bottom: var(--sp-2);
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
  color: var(--color-danger);
  cursor: pointer;
  font-size: 14px;
  padding: 0 4px;
}

.btn-add-scene {
  background: none;
  border: 1px dashed var(--color-border);
  border-radius: 4px;
  color: var(--color-primary);
  cursor: pointer;
  font-size: 13px;
  padding: 4px 12px;
  width: 100%;
}

.btn-toggle-json {
  background: none;
  border: none;
  color: var(--color-text-muted);
  cursor: pointer;
  font-size: 12px;
  text-decoration: underline;
  padding: 0;
}

.card-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  padding-top: var(--sp-4);
  border-top: 1px solid var(--color-border);
}

.btn-cancel {
  padding: 8px 20px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-surface);
  cursor: pointer;
  font-size: 14px;
}
.btn-generate {
  padding: 8px 20px;
  border: none;
  border-radius: 4px;
  background: var(--color-primary);
  color: #fff;
  cursor: pointer;
  font-size: 14px;
}

.json-area {
  font-family: monospace;
  font-size: 12px;
}
.parse-error {
  color: var(--color-danger);
  font-size: 12px;
  margin-top: var(--sp-1);
}

/* L-2: 澄清区 */
.clarification-section {
  padding: var(--sp-3) 0;
}
.section-divider {
  border-top: 1px dashed var(--color-border);
  margin-bottom: var(--sp-3);
}
.clarification-section h4 {
  margin: 0 0 var(--sp-1) 0;
  font-size: 14px;
}
.clarification-hint {
  font-size: 12px;
  color: var(--color-text-muted);
  margin: 0 0 var(--sp-3) 0;
}
.clarification-question {
  margin-bottom: var(--sp-3);
}
.q-text {
  font-size: 13px;
  font-weight: 500;
  margin: 0 0 var(--sp-1) 0;
}
.q-reason {
  font-size: 12px;
  color: var(--color-text-muted);
  margin: 0 0 var(--sp-1) 0;
}
.required-badge {
  font-size: 10px;
  background: var(--color-danger);
  color: #fff;
  padding: 1px 5px;
  border-radius: 3px;
}
.q-options {
  display: flex;
  flex-direction: column;
  gap: var(--sp-1);
}
.option-label {
  font-size: 13px;
  cursor: pointer;
  display: flex;
  align-items: flex-start;
  gap: 4px;
}
.option-desc {
  font-size: 12px;
  color: var(--color-text-muted);
}
.clarification-actions {
  display: flex;
  justify-content: flex-end;
  gap: var(--sp-2);
  padding-top: var(--sp-2);
}
.btn-skip-clarification {
  padding: 6px 14px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-surface);
  cursor: pointer;
  font-size: 13px;
}
.btn-refresh-card {
  padding: 6px 14px;
  border: none;
  border-radius: 4px;
  background: var(--color-primary);
  color: #fff;
  cursor: pointer;
  font-size: 13px;
}
.btn-refresh-card:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
