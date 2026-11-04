<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref, watch } from 'vue'
import { useConversationStore } from '../stores/useConversationStore'
import { useUiStore } from '../stores'
import type { ConversationTaskCard } from '../api/chat'
import type { ProjectMode } from '../api/types'
import ApprovalModal from './ApprovalModal.vue'

/** 重新生成必须使用新幂等键（与 store 同款生成方式，避免后端幂等短路） */
function createIdempotencyKey(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return `key-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

/** AgentPanel 传给对话助手的工作对象上下文 */
export interface ConversationContext {
  projectId: string
  mode: ProjectMode
  chapterId?: string | null
  chapterNum?: number | null
  chapterTitle?: string | null
  currentVersionId?: string | null
  currentDraft?: string
  /** 补锁 3：用户选中的文本与光标所在段落 */
  selection?: string | null
  currentParagraph?: string | null
}

const props = defineProps<{ context: ConversationContext }>()

const emit = defineEmits<{
  confirm: [payload: {
    messageId: string
    taskCard: ConversationTaskCard
    overrides?: Record<string, unknown>
    /** 重新生成时基于的最新正式版本（覆盖任务卡旧 base_version_id） */
    baseVersionId?: string | null
    /** 重新生成时必须使用新幂等键，否则后端幂等短路返回首次结果 */
    idempotencyKey?: string
  }]
  acceptCandidate: [payload: { content: string; executionId: string | null }]
  revertSession: []
}>()

const store = useConversationStore()
onUnmounted(() => store.stopPolling())
const ui = useUiStore()

const inputText = ref('')
const reviseText = ref('')
const clarifyNote = ref('')
const clarificationAnswers = ref<Record<string, string | string[]>>({})
const showCandidateModal = ref(false)
const messagesEl = ref<HTMLElement | null>(null)

const intentMeta: Record<string, { label: string; hint: string; tone: string }> = {
  knowledge_query: { label: '知识查询', hint: '检索知识库并回答', tone: 'info' },
  discussion: { label: '创作讨论', hint: '仅讨论，不修改数据', tone: 'info' },
  rewrite: { label: '修改指令', hint: '生成任务卡，确认后执行', tone: 'action' },
  data_operation: { label: '数据操作', hint: '跨对象变更，需确认', tone: 'warn' },
  flow_control: { label: '流程控制', hint: '撤销 / 采用 / 定稿', tone: 'warn' },
}

const operationLabels: Record<string, string> = {
  chapter_rewrite: '单章局部重写',
  rewrite: '局部重写',
}

const intentShort: Record<string, string> = {
  knowledge_query: '知识查询',
  discussion: '创作讨论',
  rewrite: '修改指令',
  data_operation: '数据操作',
  flow_control: '流程控制',
}

const canSend = computed(() => !!inputText.value.trim() && !store.sending)
const canConfirm = computed(() => store.awaiting === 'task_card_confirm' && !!store.taskCard && !store.sending)
const hasRoundState = computed(() => !!store.changeSet || !!store.candidate || store.awaiting !== 'idle')
const candidateOriginal = computed(() => store.baseDraft ?? props.context.currentDraft ?? '')
const candidateContent = computed(() => store.candidate?.content ?? '')
const currentIntent = computed(() => store.intent ? intentMeta[store.intent] : null)

function wordCount(text: string): number {
  return (text ?? '').replace(/\s+/g, '').length
}

function operationLabel(op: string): string {
  return operationLabels[op] ?? op
}

function stepIntentLabel(intent: string): string {
  return intentShort[intent] ?? intent
}

function stepTarget(target: string | Record<string, unknown> | null | undefined): string {
  if (!target) return ''
  if (typeof target === 'string') return target
  if ('chapter_no' in target && typeof target.chapter_no === 'number') return `第 ${target.chapter_no} 章`
  return JSON.stringify(target)
}

async function send(): Promise<void> {
  const text = inputText.value.trim()
  if (!text || store.sending) return
  inputText.value = ''
  try {
    await store.sendMessage(props.context.projectId, text, contextInput(props.context))
  } catch {
    /* 错误展示在 store.error 中 */
  }
}

function contextInput(ctx: ConversationContext) {
  return {
    chapterNo: ctx.chapterNum ?? null,
    chapterTitle: ctx.chapterTitle ?? null,
    draft: ctx.currentDraft ?? '',
    currentVersionId: ctx.currentVersionId ?? null,
    selection: ctx.selection ?? null,
    currentParagraph: ctx.currentParagraph ?? null,
  }
}

async function submitClarify(): Promise<void> {
  const answers: Record<string, string> = {}
  for (const question of store.clarification?.questions ?? []) {
    const value = clarificationAnswers.value[question.id]
    if (Array.isArray(value)) {
      if (value.length) answers[question.id] = value.join('、')
    } else if (typeof value === 'string' && value.trim()) {
      answers[question.id] = value.trim()
    }
  }
  try {
    await store.submitClarification(props.context.projectId, clarifyNote.value, answers, contextInput(props.context))
    clarifyNote.value = ''
    clarificationAnswers.value = {}
  } catch {
    /* 错误展示在 store.error 中 */
  }
}

function confirmTaskCard(regenerate = false): void {
  if (!store.taskCard || !store.lastMessageId || store.sending) return
  // 修复 B：重新生成必须基于最新正式版本（versionConflict.latestVersionId）+ 新幂等键，
  // 否则后端幂等短路返回首次执行结果，按钮形同虚设。
  const baseVersionId = regenerate && store.versionConflict?.latestVersionId
    ? store.versionConflict.latestVersionId
    : undefined
  const idempotencyKey = regenerate ? createIdempotencyKey() : undefined
  emit('confirm', {
    messageId: store.lastMessageId,
    taskCard: store.taskCard,
    overrides: {},
    baseVersionId,
    idempotencyKey,
  })
}

async function handleCandidateDecision(decision: 'accept' | 'accept_with_mods' | 'compress' | 'reject'): Promise<void> {
  if (!store.executionId || !store.candidate) return
  if (decision === 'accept') {
    try {
      await store.acceptCandidate(store.executionId)
      showCandidateModal.value = false
      emit('acceptCandidate', { content: store.candidate?.content ?? '', executionId: store.executionId })
    } catch {
      showCandidateModal.value = true // 版本冲突时保持弹窗打开，提示重新生成
    }
  } else if (decision === 'reject') {
    try {
      await store.discardCandidate(store.executionId)
      showCandidateModal.value = false
      ui.showToast('候选稿已丢弃', 'info')
    } catch {
      showCandidateModal.value = true
    }
  } else {
    // accept_with_mods / compress → 继续调整
    showCandidateModal.value = false
    reviseText.value = decision === 'compress'
      ? '在保持核心剧情与人物关系不变的前提下压缩篇幅。'
      : reviseText.value
  }
}

async function submitRevise(): Promise<void> {
  const instruction = reviseText.value.trim()
  if (!instruction) return
  try {
    await store.reviseCandidate(instruction)
    reviseText.value = ''
    showCandidateModal.value = false
  } catch {
    /* 错误展示在 store.error 中 */
  }
}

async function discardFromCard(): Promise<void> {
  if (!store.executionId) return
  try {
    await store.discardCandidate(store.executionId)
    ui.showToast('候选稿已丢弃', 'info')
  } catch {
    /* 错误展示在 store.error 中 */
  }
}

async function revertRound(): Promise<void> {
  try {
    await store.revertSession(props.context.projectId)
    emit('revertSession')
  } catch {
    /* 错误展示在 store.error 中 */
  }
}

// 新候选稿到达时自动打开对照弹窗
watch(() => store.awaiting, (value) => {
  if (value === 'candidate_review' && store.candidate) {
    nextTick(() => { showCandidateModal.value = true })
  }
})

// 消息列表自动滚动到底部
watch(() => store.messages.length, () => {
  nextTick(() => {
    messagesEl.value?.scrollTo({ top: messagesEl.value.scrollHeight })
  })
})
</script>

<template>
  <div class="conversation-assistant">
    <div class="ca-header">
      <div class="ca-title">对话助手</div>
      <button class="ca-revert" type="button" :disabled="!hasRoundState" @click="revertRound">撤销本轮</button>
    </div>

    <div v-if="store.summary" class="ca-summary">{{ store.summary }}</div>

    <div ref="messagesEl" class="ca-messages">
      <div v-if="!store.messages.length" class="ca-empty">
        <p>在下方输入自然语言，我可以帮你：</p>
        <ul>
          <li>压缩 / 重写当前章节（生成任务卡，确认后产出候选稿）</li>
          <li>查询设定与角色（知识查询）</li>
          <li>讨论创作方向（不修改数据）</li>
          <li>撤销本轮修改（流程控制）</li>
        </ul>
      </div>
      <div v-for="m in store.messages" :key="m.id" class="ca-message" :class="m.role">
        <div class="ca-bubble">{{ m.content }}</div>
      </div>
      <div v-if="store.sending" class="ca-message assistant">
        <div class="ca-bubble ca-bubble-thinking">思考中…</div>
      </div>
    </div>

    <!-- 意图展示 -->
    <div v-if="currentIntent" class="ca-intent" :class="currentIntent.tone">
      <span class="ca-intent-tag">{{ currentIntent.label }}</span>
      <span class="ca-intent-hint">{{ currentIntent.hint }}</span>
    </div>

    <!-- 复合任务执行计划 -->
    <div v-if="store.plan && store.plan.steps.length" class="ca-plan">
      <div class="ca-section-title">执行计划</div>
      <ol>
        <li v-for="(step, i) in store.plan.steps" :key="i">
          {{ stepIntentLabel(step.intent) }}<template v-if="stepTarget(step.target)"> → {{ stepTarget(step.target) }}</template>
        </li>
      </ol>
    </div>

    <!-- 澄清问题 -->
    <div v-if="store.awaiting === 'clarification' && store.clarification" class="ca-clarify">
      <div class="ca-section-title">需要你确认</div>
      <p v-if="store.clarification.reason" class="ca-clarify-reason">{{ store.clarification.reason }}</p>
      <div v-for="q in store.clarification.questions" :key="q.id" class="ca-question">
        <label class="ca-question-label">
          {{ q.question }}<span v-if="q.required" class="ca-required">必答</span>
        </label>
        <div v-if="q.type === 'single_choice'" class="ca-options">
          <label v-for="opt in (q.options || [])" :key="opt.value" class="ca-option">
            <input v-model="clarificationAnswers[q.id]" type="radio" :value="opt.value" />
            <span>{{ opt.label }}</span>
          </label>
        </div>
        <div v-else-if="q.type === 'multi_choice'" class="ca-options">
          <label v-for="opt in (q.options || [])" :key="opt.value" class="ca-option">
            <input v-model="clarificationAnswers[q.id]" type="checkbox" :value="opt.value" />
            <span>{{ opt.label }}</span>
          </label>
        </div>
        <input v-else v-model="clarificationAnswers[q.id]" type="text" class="ca-input" :placeholder="q.type === 'number' ? '请输入数字' : '补充说明'" />
      </div>
      <textarea v-model="clarifyNote" rows="2" class="ca-textarea" placeholder="或直接补充你的要求…"></textarea>
      <button class="ca-btn ca-btn-primary" type="button" :disabled="store.sending" @click="submitClarify">
        {{ store.sending ? '重新识别中…' : '提交澄清并重新识别' }}
      </button>
    </div>

    <!-- 任务卡预览（只读） -->
    <div v-if="store.taskCard && store.awaiting === 'task_card_confirm'" class="ca-task-card">
      <div class="ca-section-title">任务卡（确认后开始生成候选稿）</div>
      <div class="ca-tc-grid">
        <div class="ca-tc-row"><span class="ca-tc-key">操作</span><span class="ca-tc-val">{{ operationLabel(store.taskCard.operation_type) }}</span></div>
        <div class="ca-tc-row"><span class="ca-tc-key">目标章节</span><span class="ca-tc-val">第 {{ store.taskCard.target?.chapter_no ?? props.context.chapterNum ?? '-' }} 章</span></div>
        <div class="ca-tc-row"><span class="ca-tc-key">风险级别</span><span class="ca-tc-val">{{ store.taskCard.risk_level ?? '-' }}</span></div>
        <div class="ca-tc-row"><span class="ca-tc-key">基于版本</span><span class="ca-tc-val">{{ store.taskCard.base_version_id ?? '当前版本' }}</span></div>
      </div>
      <div v-if="store.taskCard.constraints" class="ca-tc-constraints">
        <div v-if="store.taskCard.constraints.must_preserve?.length" class="ca-tc-line">
          <span class="ca-tc-key">必须保留</span>{{ store.taskCard.constraints.must_preserve.join('、') }}
        </div>
        <div v-if="store.taskCard.constraints.must_not_change?.length" class="ca-tc-line">
          <span class="ca-tc-key">不得修改</span>{{ store.taskCard.constraints.must_not_change.join('、') }}
        </div>
        <div v-if="store.taskCard.constraints.target_word_count" class="ca-tc-line">
          <span class="ca-tc-key">目标字数</span>{{ store.taskCard.constraints.target_word_count }}
        </div>
      </div>
      <!-- P0-3：确认前必须看到 AI 如何理解指令——原始要求 + 解析后的执行指令 -->
      <div v-if="store.taskCard.resolved_instruction" class="ca-tc-resolved">
        <span class="ca-tc-key">AI 解析的执行指令</span>{{ store.taskCard.resolved_instruction }}
      </div>
      <div v-if="store.taskCard.constraints?.user_instruction" class="ca-tc-line ca-tc-original">
        <span class="ca-tc-key">你的原始要求</span>{{ store.taskCard.constraints.user_instruction }}
      </div>
      <div v-if="store.taskCard.mentioned_entities?.length" class="ca-tc-line">
        <span class="ca-tc-key">重点人物</span>{{ store.taskCard.mentioned_entities.join('、') }}
      </div>
      <div v-if="store.taskCard.requirements?.length" class="ca-tc-line">
        <span class="ca-tc-key">具体要求</span>{{ store.taskCard.requirements.join('；') }}
      </div>
      <button class="ca-btn ca-btn-primary" type="button" :disabled="!canConfirm" @click="() => confirmTaskCard()">
        {{ store.sending ? '确认中…' : '确认并生成候选稿' }}
      </button>
    </div>

    <!-- 版本冲突提示 -->
    <div v-if="store.versionConflict" class="ca-conflict">
      当前章节已在其他操作中更新，原候选稿基于旧版本。是否基于最新版本重新生成？
      <button class="ca-btn ca-btn-ghost" type="button" @click="() => confirmTaskCard(true)">基于最新版本重新生成</button>
    </div>

    <!-- 候选稿概览 -->
    <!-- running：并发/超时后正在生成（不得消失，持续轮询中） -->
    <div v-if="store.awaiting === 'running'" class="ca-running" role="status">
      <span class="ca-running-spinner" aria-hidden="true" />
      正在生成候选稿，请稍候…
    </div>

    <div v-if="store.awaiting === 'candidate_review' && store.candidate" class="ca-candidate">
      <div class="ca-candidate-main">
        <div>
          <div class="ca-section-title">候选稿已生成</div>
          <div class="ca-candidate-meta">
            约 {{ wordCount(candidateContent) }} 字 ·
            {{ store.candidate.base_version_id ? `基于版本 ${store.candidate.base_version_id}` : '基于当前版本' }}
          </div>
        </div>
        <button class="ca-btn ca-btn-primary" type="button" @click="showCandidateModal = true">查看对照</button>
      </div>
      <div class="ca-revise-row">
        <input v-model="reviseText" type="text" class="ca-input" placeholder="不满意？输入继续调整的要求…" @keyup.enter="submitRevise" />
        <button class="ca-btn" type="button" :disabled="!reviseText.trim()" @click="submitRevise">继续调整</button>
        <button class="ca-btn ca-btn-ghost" type="button" :disabled="!store.executionId" @click="discardFromCard">丢弃</button>
      </div>
    </div>

    <!-- 错误提示 -->
    <div v-if="store.error" class="ca-error" role="alert">{{ store.error }}</div>

    <!-- 消息输入 -->
    <div class="ca-composer">
      <textarea
        v-model="inputText"
        rows="2"
        class="ca-textarea"
        placeholder="描述你的需求，例如：压缩本章前半段，保留结尾伏笔…"
        @keydown.enter.exact.prevent="send"
      ></textarea>
      <div class="ca-composer-actions">
        <span v-if="store.awaiting === 'task_card_confirm'" class="ca-hint">已生成任务卡，请确认后生成；或继续输入修改需求</span>
        <button class="ca-btn ca-btn-primary" type="button" :disabled="!canSend" @click="send">发送</button>
      </div>
    </div>

    <!-- 候选稿双栏对照（复用现有人工审核弹窗） -->
    <ApprovalModal
      :show="showCandidateModal && !!store.candidate"
      :content="candidateContent"
      :original-content="candidateOriginal"
      mode="full_pipeline"
      :has-h-i-t-l="false"
      :project-mode="props.context.mode"
      :can-finalize="false"
      :finalizing="false"
      @decision="handleCandidateDecision"
    />
  </div>
</template>

<style scoped>
.conversation-assistant {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-height: 0;
}

.ca-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.ca-title {
  font-size: var(--text-sm);
  font-weight: 750;
  color: var(--text);
}
.ca-revert {
  min-height: 28px;
  padding: 0 10px;
  border: 1px solid color-mix(in srgb, var(--border) 82%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
  color: var(--text-secondary);
  font-size: var(--text-xs);
  cursor: pointer;
  transition: color var(--transition), border-color var(--transition);
}
.ca-revert:hover:not(:disabled) {
  color: var(--status-error);
  border-color: color-mix(in srgb, var(--status-error) 46%, var(--border));
}
.ca-revert:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

.ca-summary {
  padding: 9px 11px;
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--accent) 8%, transparent);
  color: var(--text-secondary);
  font-size: 11px;
  line-height: 1.6;
}

.ca-messages {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 340px;
  min-height: 120px;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding-right: 2px;
}
.ca-empty {
  padding: 14px;
  border: 1px dashed color-mix(in srgb, var(--border) 82%, transparent);
  border-radius: 12px;
  color: var(--text-tertiary);
  font-size: 12px;
  line-height: 1.7;
}
.ca-empty p { margin: 0 0 6px; font-weight: 600; }
.ca-empty ul { margin: 0; padding-left: 16px; }

.ca-message {
  display: flex;
}
.ca-message.user {
  justify-content: flex-end;
}
.ca-message.assistant {
  justify-content: flex-start;
}
.ca-bubble {
  max-width: 86%;
  padding: 9px 12px;
  border-radius: 12px;
  font-size: var(--text-xs);
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
.ca-message.user .ca-bubble {
  background: color-mix(in srgb, var(--accent) 16%, var(--bg-panel));
  border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--border));
  color: var(--text);
  border-bottom-right-radius: 4px;
}
.ca-message.assistant .ca-bubble {
  background: color-mix(in srgb, var(--bg-panel) 92%, transparent);
  border: 1px solid color-mix(in srgb, var(--border) 82%, transparent);
  color: var(--text-secondary);
  border-bottom-left-radius: 4px;
}
.ca-bubble-thinking {
  color: var(--text-tertiary);
  font-style: italic;
}

.ca-intent {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 11px;
  border-radius: 10px;
  font-size: 12px;
}
.ca-intent.info { background: color-mix(in srgb, var(--accent) 8%, transparent); border: 1px solid color-mix(in srgb, var(--accent) 26%, var(--border)); }
.ca-intent.action { background: color-mix(in srgb, var(--status-final) 8%, transparent); border: 1px solid color-mix(in srgb, var(--status-final) 30%, var(--border)); }
.ca-intent.warn { background: color-mix(in srgb, var(--status-reviewing) 8%, transparent); border: 1px solid color-mix(in srgb, var(--status-reviewing) 32%, var(--border)); }
.ca-intent-tag {
  flex: 0 0 auto;
  font-weight: 750;
  color: var(--text);
}
.ca-intent-hint {
  color: var(--text-tertiary);
  font-size: 11px;
}

.ca-section-title {
  font-size: var(--text-xs);
  font-weight: 650;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: 7px;
}

.ca-plan,
.ca-task-card,
.ca-clarify,
.ca-candidate {
  padding: 12px 13px;
  border: 1px solid color-mix(in srgb, var(--border) 82%, transparent);
  border-radius: 12px;
  background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
}
.ca-plan ol {
  margin: 0;
  padding-left: 18px;
  color: var(--text-secondary);
  font-size: 12px;
  line-height: 1.9;
}

.ca-clarify-reason {
  margin: 0 0 10px;
  color: var(--status-reviewing);
  font-size: 12px;
}
.ca-question {
  margin-bottom: 10px;
}
.ca-question-label {
  display: block;
  margin-bottom: 6px;
  font-size: 13px;
  font-weight: 650;
  color: var(--text);
}
.ca-required {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 6px;
  background: color-mix(in srgb, var(--status-error) 14%, transparent);
  color: var(--status-error);
  font-size: 10px;
}
.ca-options {
  display: grid;
  gap: 6px;
}
.ca-option {
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 12px;
  color: var(--text-secondary);
  cursor: pointer;
}
.ca-option input {
  margin: 0;
}

.ca-tc-grid {
  display: grid;
  gap: 6px;
  margin-bottom: 9px;
}
.ca-tc-row {
  display: flex;
  gap: 10px;
  font-size: 12px;
}
.ca-tc-key {
  flex: 0 0 64px;
  color: var(--text-tertiary);
  font-size: 11px;
  line-height: 1.6;
}
.ca-tc-val {
  color: var(--text-secondary);
  word-break: break-all;
}
.ca-tc-constraints {
  display: grid;
  gap: 5px;
  margin-bottom: 11px;
  padding: 8px 10px;
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg) 64%, transparent);
}
.ca-tc-line {
  font-size: 12px;
  color: var(--text-secondary);
  line-height: 1.6;
}
.ca-tc-line .ca-tc-key {
  display: inline-block;
  min-width: 64px;
}

/* P0-3：AI 解析后的执行指令是确认闭环的核心，突出展示 */
.ca-tc-resolved {
  margin: 2px 0 6px;
  padding: 6px 8px;
  border-left: 2px solid var(--accent, #576bff);
  border-radius: 4px;
  background: color-mix(in srgb, var(--accent, #576bff) 8%, transparent);
  font-size: 12px;
  color: var(--text-primary);
  line-height: 1.6;
}
.ca-tc-resolved .ca-tc-key {
  display: inline-block;
  min-width: 64px;
  color: var(--accent, #576bff);
}
.ca-tc-original {
  color: var(--text-tertiary);
}

.ca-conflict {
  padding: 11px 13px;
  border: 1px solid color-mix(in srgb, var(--status-error) 46%, var(--border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--status-error) 8%, transparent);
  color: var(--status-error);
  font-size: 12px;
  line-height: 1.7;
}

.ca-candidate-main {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 9px;
}
.ca-candidate-meta {
  color: var(--text-tertiary);
  font-size: 11px;
}
.ca-revise-row {
  display: flex;
  gap: 7px;
  align-items: center;
}

.ca-input {
  flex: 1;
  min-width: 0;
  height: 34px;
  padding: 0 10px;
  border: 1px solid color-mix(in srgb, var(--border) 84%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg) 74%, transparent);
  color: var(--text);
  font-size: 12px;
}
.ca-input:focus,
.ca-textarea:focus {
  outline: none;
  border-color: var(--border-focus);
}
.ca-textarea {
  width: 100%;
  padding: 8px 10px;
  border: 1px solid color-mix(in srgb, var(--border) 84%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg) 74%, transparent);
  color: var(--text);
  font-size: 12px;
  line-height: 1.6;
  resize: vertical;
}

.ca-error {
  padding: 9px 11px;
  border: 1px solid color-mix(in srgb, var(--status-error) 46%, var(--border));
  border-radius: 9px;
  background: color-mix(in srgb, var(--status-error) 8%, transparent);
  color: var(--status-error);
  font-size: 12px;
}

.ca-composer {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.ca-composer-actions {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.ca-hint {
  color: var(--text-tertiary);
  font-size: 11px;
  line-height: 1.5;
}

.ca-btn {
  flex: 0 0 auto;
  min-height: 32px;
  padding: 0 13px;
  border: 1px solid color-mix(in srgb, var(--border) 86%, transparent);
  border-radius: 8px;
  background: color-mix(in srgb, var(--bg-panel) 90%, transparent);
  color: var(--text-secondary);
  font-size: 12px;
  font-weight: 650;
  cursor: pointer;
  transition: color var(--transition), background var(--transition), border-color var(--transition);
}
.ca-btn:hover:not(:disabled) {
  color: var(--text);
  border-color: var(--border-focus);
}
.ca-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.ca-btn-primary {
  background: var(--accent);
  border-color: var(--accent);
  color: var(--text-inverse);
}
.ca-btn-primary:hover:not(:disabled) {
  background: var(--accent-hover);
  border-color: var(--accent-hover);
  color: var(--text-inverse);
}
.ca-btn-ghost {
  background: transparent;
  border-color: transparent;
  color: var(--text-tertiary);
}
.ca-btn-ghost:hover:not(:disabled) {

  color: var(--status-error);
  border-color: color-mix(in srgb, var(--status-error) 40%, var(--border));
}

.ca-running {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 16px;
  border: 1px solid color-mix(in srgb, var(--status-info, #1a73e8) 35%, var(--border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--status-info, #1a73e8) 6%, transparent);
  color: var(--text-secondary, #5f6368);
  font-size: 14px;
}
.ca-running-spinner {
  width: 14px;
  height: 14px;
  border: 2px solid color-mix(in srgb, var(--status-info, #1a73e8) 30%, transparent);
  border-top-color: var(--status-info, #1a73e8);
  border-radius: 50%;
  animation: ca-spin 0.8s linear infinite;
}
@keyframes ca-spin {
  to { transform: rotate(360deg); }
}
</style>
