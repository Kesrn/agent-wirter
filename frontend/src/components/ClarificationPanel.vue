<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import type { ClarificationQuestion } from '../api/types'

const props = defineProps<{
  runId: string
  questions: ClarificationQuestion[]
  round: number
  maxRounds: number
  assumptionsIfSkipped: string[]
}>()

const emit = defineEmits<{
  submit: [answers: Record<string, string>]
  skip: []
}>()

/**
 * 本地 answers 状态：
 * - single_choice / free_text / number → 单值字符串
 * - multi_choice → 以换行分隔的多个 value（每个 option 一行）
 * 这里统一用 Record<string, string>，多选时用 \n 拼接。
 */
const answers = ref<Record<string, string>>({})
const multiAnswers = ref<Record<string, Record<string, boolean>>>({})
const submitting = ref(false)
const showAssumptions = ref(false)

// 初始化 / 重置：当 questions 变化（如新一轮）时清空状态
watch(
  () => props.questions,
  () => {
    answers.value = {}
    multiAnswers.value = {}
    submitting.value = false
    showAssumptions.value = false
  },
  { immediate: true },
)

function setSingleChoice(qid: string, value: string) {
  answers.value[qid] = value
}

function setText(qid: string, value: string) {
  answers.value[qid] = value
}

function setNumber(qid: string, value: string) {
  answers.value[qid] = value
}

function toggleMultiChoice(qid: string, value: string, checked: boolean) {
  if (!multiAnswers.value[qid]) multiAnswers.value[qid] = {}
  multiAnswers.value[qid][value] = checked
  // 同步到 answers（以换行分隔）
  const selected = (props.questions.find(q => q.id === qid)?.options ?? [])
    .filter(o => multiAnswers.value[qid][o.value])
    .map(o => o.value)
  answers.value[qid] = selected.join('\n')
}

/** 校验：所有 required 的问题必须有非空回答 */
const canSubmit = computed(() => {
  if (submitting.value) return false
  for (const q of props.questions) {
    if (!q.required) continue
    const val = answers.value[q.id]
    if (val === undefined || val === null || String(val).trim() === '') return false
  }
  return true
})

const hasQuestions = computed(() => props.questions.length > 0)

function handleSubmit() {
  if (!canSubmit.value || submitting.value) return
  submitting.value = true
  // 收集所有问题的回答（包括非必填但已作答的）
  const payload: Record<string, string> = {}
  for (const q of props.questions) {
    const val = answers.value[q.id]
    if (val !== undefined && val !== null && String(val).trim() !== '') {
      payload[q.id] = val
    }
  }
  emit('submit', payload)
}

function handleSkip() {
  if (submitting.value) return
  submitting.value = true
  emit('skip')
}
</script>

<template>
  <div class="clarification-panel">
    <div class="clar-header">
      <div class="clar-title-row">
        <span class="clar-badge">生成前澄清</span>
        <span class="clar-round">第 {{ round }}/{{ maxRounds }} 轮</span>
      </div>
      <p class="clar-subtitle">
        在正式生成前，请补充以下关键信息。这与生成完成后的「最终审核」不同——回答越明确，生成结果越贴合预期。
      </p>
    </div>

    <div v-if="hasQuestions" class="clar-body">
      <div
        v-for="(q, idx) in questions"
        :key="q.id"
        class="clar-question"
      >
        <div class="q-head">
          <span class="q-index">{{ idx + 1 }}</span>
          <span class="q-text">{{ q.question }}</span>
          <span v-if="q.required" class="q-required">必填</span>
        </div>
        <p v-if="q.reason" class="q-reason">{{ q.reason }}</p>

        <!-- single_choice: radio -->
        <template v-if="q.type === 'single_choice'">
          <div v-if="q.options && q.options.length" class="option-list">
            <label
              v-for="opt in q.options"
              :key="opt.value"
              class="option-item"
              :class="{ selected: answers[q.id] === opt.value }"
            >
              <input
                type="radio"
                :name="`clar-${q.id}`"
                :value="opt.value"
                :checked="answers[q.id] === opt.value"
                @change="setSingleChoice(q.id, opt.value)"
              />
              <span class="option-main">
                <span class="option-label">{{ opt.label }}</span>
                <span v-if="opt.description" class="option-desc">{{ opt.description }}</span>
              </span>
            </label>
          </div>
        </template>

        <!-- multi_choice: checkbox（兜底） -->
        <template v-else-if="q.type === 'multi_choice'">
          <div v-if="q.options && q.options.length" class="option-list">
            <label
              v-for="opt in q.options"
              :key="opt.value"
              class="option-item"
              :class="{ selected: multiAnswers[q.id]?.[opt.value] }"
            >
              <input
                type="checkbox"
                :value="opt.value"
                :checked="!!multiAnswers[q.id]?.[opt.value]"
                @change="(e) => toggleMultiChoice(q.id, opt.value, (e.target as HTMLInputElement).checked)"
              />
              <span class="option-main">
                <span class="option-label">{{ opt.label }}</span>
                <span v-if="opt.description" class="option-desc">{{ opt.description }}</span>
              </span>
            </label>
          </div>
        </template>

        <!-- free_text: textarea -->
        <template v-else-if="q.type === 'free_text'">
          <textarea
            class="q-textarea"
            rows="3"
            :value="answers[q.id] ?? ''"
            placeholder="请输入..."
            @input="(e) => setText(q.id, (e.target as HTMLTextAreaElement).value)"
          ></textarea>
        </template>

        <!-- number: number input（兜底） -->
        <template v-else-if="q.type === 'number'">
          <input
            type="number"
            class="q-number"
            :value="answers[q.id] ?? ''"
            placeholder="请输入数字"
            @input="(e) => setNumber(q.id, (e.target as HTMLInputElement).value)"
          />
        </template>
      </div>
    </div>

    <div v-else class="clar-empty">暂无澄清问题</div>

    <!-- 跳过时展示默认假设 -->
    <div v-if="showAssumptions && assumptionsIfSkipped.length" class="assumptions-box">
      <div class="assumptions-label">跳过将使用以下默认假设：</div>
      <ul class="assumptions-list">
        <li v-for="(a, i) in assumptionsIfSkipped" :key="i">{{ a }}</li>
      </ul>
    </div>

    <div class="clar-footer">
      <button
        type="button"
        class="btn-skip"
        :disabled="submitting"
        @click="handleSkip"
      >
        {{ submitting ? '提交中...' : '跳过，使用默认假设' }}
      </button>
      <button
        type="button"
        class="btn-submit"
        :disabled="!canSubmit"
        @click="handleSubmit"
      >
        {{ submitting ? '提交中...' : '提交回答' }}
      </button>
    </div>
  </div>
</template>

<style scoped>
.clarification-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: var(--sp-4);
  border: 1px solid color-mix(in srgb, var(--accent) 40%, var(--border));
  border-radius: 16px;
  background: color-mix(in srgb, var(--bg-panel) 92%, transparent);
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.035) inset, var(--shadow-sm);
}
.clar-header {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.clar-title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}
.clar-badge {
  font-size: var(--text-sm);
  font-weight: 700;
  color: #fff;
  background: var(--accent);
  padding: 2px 10px;
  border-radius: var(--radius);
}
.clar-round {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-tertiary);
}
.clar-subtitle {
  margin: 0;
  font-size: var(--text-sm);
  color: var(--text-secondary);
  line-height: 1.5;
}
.clar-body {
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.clar-question {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 12px;
  border: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--bg) 60%, transparent);
}
.q-head {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.q-index {
  flex: 0 0 auto;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  background: color-mix(in srgb, var(--accent) 18%, transparent);
  color: var(--accent);
  font-size: 11px;
  font-weight: 700;
}
.q-text {
  flex: 1;
  font-size: var(--text-sm);
  font-weight: 650;
  color: var(--text);
  line-height: 1.4;
}
.q-required {
  flex: 0 0 auto;
  font-size: 10px;
  font-weight: 700;
  color: var(--status-error, #ff4d4f);
  border: 1px solid var(--status-error, #ff4d4f);
  border-radius: 3px;
  padding: 0 4px;
}
.q-reason {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  line-height: 1.5;
}
.option-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 4px;
}
.option-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.option-item:hover {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 6%, transparent);
}
.option-item.selected {
  border-color: var(--accent);
  background: color-mix(in srgb, var(--accent) 10%, transparent);
}
.option-item input {
  margin-top: 3px;
  accent-color: var(--accent);
}
.option-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
}
.option-label {
  font-size: var(--text-sm);
  font-weight: 600;
  color: var(--text);
}
.option-desc {
  font-size: var(--text-xs);
  color: var(--text-secondary);
  line-height: 1.4;
}
.q-textarea,
.q-number {
  width: 100%;
  padding: 8px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg);
  color: var(--text);
  font-size: var(--text-sm);
  font-family: inherit;
  resize: vertical;
}
.q-textarea:focus,
.q-number:focus {
  outline: none;
  border-color: var(--accent);
}
.clar-empty {
  font-size: var(--text-sm);
  color: var(--text-tertiary);
  padding: 8px 0;
}
.assumptions-box {
  padding: 10px 12px;
  border: 1px dashed var(--border);
  border-radius: var(--radius);
  background: color-mix(in srgb, var(--bg) 50%, transparent);
}
.assumptions-label {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-secondary);
  margin-bottom: 4px;
}
.assumptions-list {
  margin: 0;
  padding-left: 18px;
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  line-height: 1.6;
}
.assumptions-list li {
  list-style: disc;
}
.clar-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 4px;
}
.btn-skip {
  padding: 8px 16px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: transparent;
  color: var(--text-secondary);
  font-size: var(--text-sm);
  cursor: pointer;
  transition: background 0.15s;
}
.btn-skip:hover:not(:disabled) {
  background: color-mix(in srgb, var(--border) 20%, transparent);
}
.btn-submit {
  padding: 8px 20px;
  border: none;
  border-radius: var(--radius);
  background: var(--accent);
  color: #fff;
  font-size: var(--text-sm);
  font-weight: 600;
  cursor: pointer;
  transition: filter 0.15s;
}
.btn-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-submit:not(:disabled):hover {
  filter: brightness(1.1);
}
.btn-skip:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
</style>
