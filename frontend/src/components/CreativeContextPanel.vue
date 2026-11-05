<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  useChapterStore,
  useDocumentStore,
  useConversationStore,
  useExpertStore,
  useCharacterStore,
  useWorldEntryStore,
  useHiddenThreadStore,
  useOutlineStore,
  useChapterVersionStore,
  useDocumentRevisionStore,
} from '../stores'
import type { ProjectMode, WritingUnit } from '../api/types'
import AgentWorkflow from './AgentWorkflow.vue'

/**
 * 新版对话式创作右侧上下文面板（对应原型 studio-context）：
 * 只做只读展示与“快捷能力”入口，不复制 AgentPanel 的生成逻辑——
 * 快捷能力按钮通过 quickAction/cancel 事件上抛，由 ConversationCreativeWorkspace
 * 转发到现有 AgentPanel 执行宿主（同一 SSE / 任务卡 / 审核闭环）。
 */

const props = defineProps<{
  projectId: string
  mode: ProjectMode
  /** 当前正式版本号（由壳层统一加载版本 store 后传入，避免重复请求） */
  latestVersionNumber?: number | null
}>()

const emit = defineEmits<{
  quickAction: [key: 'generate' | 'turn' | 'enhance' | 'summarize']
  cancel: []
}>()

const chapterStore = useChapterStore()
const documentStore = useDocumentStore()
const conversationStore = useConversationStore()
const expertStore = useExpertStore()
const characterStore = useCharacterStore()
const worldEntryStore = useWorldEntryStore()
const hiddenThreadStore = useHiddenThreadStore()
const outlineStore = useOutlineStore()
const versionStore = useChapterVersionStore()
const revisionStore = useDocumentRevisionStore()

const isNovel = computed(() => props.mode === 'novel')
const currentUnit = computed(() => isNovel.value
  ? chapterStore.currentChapterForProject(props.projectId)
  : documentStore.currentDocumentForProject(props.projectId))
const currentUnitPosition = computed(() => isNovel.value
  ? chapterStore.currentChapterNum
  : documentStore.currentDocumentPosition)

function unitPosition(unit: WritingUnit): number {
  return 'position' in unit ? unit.position : unit.chapter_num
}

const unitLabel = computed(() => isNovel.value ? '章节' : '稿件')
const unitOrdinal = computed(() => currentUnit.value
  ? (isNovel.value ? `第 ${currentUnitPosition.value} 章` : `第 ${currentUnitPosition.value} 篇`)
  : `未选择${unitLabel.value}`)
const unitTitle = computed(() => currentUnit.value?.title?.trim() || unitOrdinal.value)
const unitStatusLabel = computed(() => {
  const status = currentUnit.value?.status
  if (status === 'final') return '终稿'
  if (status === 'reviewing') return '审核中'
  if (status === 'revision') return '修订'
  return '草稿'
})
const wordCount = computed(() => (currentUnit.value?.draft ?? '').replace(/\s+/g, '').length)

/** 正式版本号：现有版本 store（后端按 version_number desc 返回，[0] 为最新） */
const latestVersionNumber = computed(() => isNovel.value
  ? versionStore.versions[0]?.versionNumber
  : revisionStore.revisions[0]?.versionNumber)

watch([() => props.projectId, currentUnitPosition], () => {
  const unit = currentUnit.value
  if (!unit) return
  if (isNovel.value) {
    void versionStore.loadVersions(props.projectId, unitPosition(unit))
  } else {
    void revisionStore.loadRevisions(props.projectId, unit.id)
  }
}, { immediate: true })

/** 已锁定约束：读对话任务卡确认的 must_preserve / must_not_change（conversationStore） */
const lockedConstraints = computed(() => {
  const constraints = conversationStore.taskCard?.constraints
  return {
    mustPreserve: constraints?.must_preserve ?? [],
    mustNotChange: constraints?.must_not_change ?? [],
    targetWordCount: constraints?.target_word_count ?? null,
  }
})

/** 资料来源统计：纯 store 读取（角色/设定/暗线/大纲），不复制 contextStats API 逻辑 */
const sourceStats = computed(() => ({
  characters: characterStore.charactersForProject(props.projectId).length,
  worldEntries: worldEntryStore.entriesForProject(props.projectId).length,
  hiddenThreads: hiddenThreadStore.threadsForProject(props.projectId).length,
  outlines: outlineStore.entriesForProject(props.projectId).length,
}))

const projectState = computed(() => expertStore.getState(props.projectId))
const generating = computed(() => projectState.value.isGenerating)
const generationError = computed(() => projectState.value.generationError)
const hasVersionConflict = computed(() => !!conversationStore.versionConflict)

/** 对话执行链路（只读）：意图识别 → 任务卡确认 → 生成候选稿 → 人工审核 */
type FlowStatus = 'done' | 'active' | 'pending'
const flowSteps = computed<Array<{ label: string; status: FlowStatus }>>(() => {
  const awaiting = conversationStore.awaiting
  const hasIntent = !!conversationStore.intent || conversationStore.messages.length > 0
  const hasTaskCard = !!conversationStore.taskCard
  const hasCandidate = !!conversationStore.candidate
  const applied = conversationStore.changeSet?.status === 'applied' || conversationStore.candidate?.status === 'applied'
  const stepStatus = (done: boolean, active: boolean): FlowStatus => done ? 'done' : (active ? 'active' : 'pending')
  return [
    { label: '意图识别', status: stepStatus(hasIntent, false) },
    { label: '任务卡确认', status: stepStatus(hasTaskCard, awaiting === 'task_card_confirm' || awaiting === 'clarification') },
    { label: '生成候选稿', status: stepStatus(hasCandidate || applied, generating.value || awaiting === 'candidate_review') },
    { label: '人工审核', status: stepStatus(applied, awaiting === 'candidate_review') },
  ]
})

interface QuickActionDef { key: 'generate' | 'turn' | 'enhance' | 'summarize'; novelLabel: string; articleLabel: string; primary?: boolean }
const QUICK_ACTIONS: QuickActionDef[] = [
  { key: 'generate', novelLabel: '章节生成', articleLabel: '生成内容', primary: true },
  { key: 'turn', novelLabel: '转折建议', articleLabel: '标题建议' },
  { key: 'enhance', novelLabel: '润色', articleLabel: '改写优化' },
  { key: 'summarize', novelLabel: '读者视角', articleLabel: '受众视角' },
]

function quickActionLabel(action: QuickActionDef): string {
  return isNovel.value ? action.novelLabel : action.articleLabel
}

function triggerQuickAction(key: QuickActionDef['key']): void {
  if (!currentUnit.value || generating.value) return
  emit('quickAction', key)
}

function triggerCancel(): void {
  emit('cancel')
}

const progressEl = ref<HTMLElement | null>(null)
function scrollToProgress(): void {
  progressEl.value?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
}
</script>

<template>
  <aside class="ccw-context" aria-label="当前任务上下文">
    <div class="ccw-context-head">
      <strong>当前上下文</strong>
      <span>自动按任务装载</span>
    </div>

    <!-- 操作对象：当前章节 / 版本 -->
    <section class="ccw-context-section">
      <div class="ccw-context-label">操作对象</div>
      <div class="ccw-context-value">{{ unitOrdinal }} · {{ unitTitle }}</div>
      <div class="ccw-context-note">
        <template v-if="isNovel">正式版本 v{{ latestVersionNumber ?? '—' }}</template>
        <template v-else>修订 v{{ latestVersionNumber ?? '—' }}</template>
        <template v-if="wordCount"> · {{ wordCount.toLocaleString('zh-CN') }} 字</template>
        <span class="ccw-badge">{{ unitStatusLabel }}</span>
      </div>
      <div v-if="hasVersionConflict" class="ccw-conflict-note">
        当前章节已在其他操作中更新，请在对话中基于最新版本重新生成。
      </div>
    </section>

    <!-- 快捷能力：映射 AgentPanel 现有 handleGenerate / handleQuickAction -->
    <section class="ccw-context-section">
      <div class="ccw-context-label">快捷能力</div>
      <div class="ccw-quick-grid">
        <button
          v-for="action in QUICK_ACTIONS"
          :key="action.key"
          type="button"
          class="ccw-quick-action"
          :class="{ primary: action.primary }"
          :disabled="!currentUnit || generating"
          @click="triggerQuickAction(action.key)"
        >
          {{ quickActionLabel(action) }}
        </button>
        <button
          v-if="generating"
          type="button"
          class="ccw-quick-action ccw-cancel-action"
          @click="triggerCancel"
        >
          取消当前任务
        </button>
        <button
          v-else
          type="button"
          class="ccw-quick-action"
          :disabled="!projectState.workflowSteps.length && !flowSteps.some(s => s.status === 'active')"
          @click="scrollToProgress"
        >
          工作流进度
        </button>
      </div>
    </section>

    <!-- 已锁定约束：对话任务卡确认后自动锁定 -->
    <section class="ccw-context-section">
      <div class="ccw-context-label">已锁定约束</div>
      <template v-if="lockedConstraints.mustPreserve.length || lockedConstraints.mustNotChange.length">
        <div v-for="item in lockedConstraints.mustPreserve" :key="`p-${item}`" class="ccw-context-row">
          <span>必须保留 · {{ item }}</span>
          <i class="ccw-ok"></i>
        </div>
        <div v-for="item in lockedConstraints.mustNotChange" :key="`n-${item}`" class="ccw-context-row">
          <span>不得修改 · {{ item }}</span>
          <i class="ccw-ok"></i>
        </div>
        <div v-if="lockedConstraints.targetWordCount" class="ccw-context-row">
          <span>目标字数 · {{ lockedConstraints.targetWordCount }}</span>
          <i class="ccw-ok"></i>
        </div>
      </template>
      <div v-else class="ccw-context-note">暂无已确认约束。对话确认任务卡后，必须保留 / 不得修改项会自动锁定在这里。</div>
    </section>

    <!-- 本次调用资料 -->
    <section class="ccw-context-section">
      <div class="ccw-context-label">本次调用资料</div>
      <div class="ccw-context-row"><span>本章正式正文</span><i class="ccw-ok"></i></div>
      <div class="ccw-context-row"><span>人物档案 · {{ sourceStats.characters }} 条</span><i class="ccw-ok"></i></div>
      <div class="ccw-context-row"><span>世界观设定 · {{ sourceStats.worldEntries }} 条</span><i class="ccw-ok"></i></div>
      <div class="ccw-context-row"><span>暗线 · {{ sourceStats.hiddenThreads }} 条</span><i class="ccw-ok"></i></div>
      <div class="ccw-context-note">只加载当前任务需要的内容，不把整段聊天历史全部塞进提示词。</div>
    </section>

    <!-- 执行链路 -->
    <section ref="progressEl" class="ccw-context-section">
      <div class="ccw-context-label">执行链路</div>
      <div class="ccw-flow">
        <div
          v-for="step in flowSteps"
          :key="step.label"
          class="ccw-flow-step"
          :class="step.status"
        >
          <span class="ccw-flow-dot">{{ step.status === 'done' ? '✓' : '·' }}</span>
          <span>{{ step.label }}</span>
        </div>
      </div>
      <div v-if="generationError" class="ccw-flow-error" role="alert">{{ generationError }}</div>
      <AgentWorkflow v-if="projectState.workflowSteps.length" :project-id="projectId" :mode="mode" />
    </section>
  </aside>
</template>

<style scoped>
.ccw-context {
  min-width: 0;
  min-height: 0;
  overflow-y: auto;
  padding: 18px 15px;
  border-left: 1px solid var(--border);
  background: var(--bg-sidebar);
  overscroll-behavior: contain;
}
.ccw-context-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin: 2px 2px 14px;
}
.ccw-context-head strong {
  font-size: 12px;
  font-weight: 750;
  color: var(--text);
}
.ccw-context-head span {
  color: var(--text-tertiary);
  font-size: 9px;
}
.ccw-context-section {
  padding: 13px;
  border: 1px solid var(--border);
  border-radius: var(--radius-lg, 12px);
  background: var(--bg-hover);
}
.ccw-context-section + .ccw-context-section {
  margin-top: 10px;
}
.ccw-context-label {
  margin-bottom: 8px;
  color: var(--text-tertiary);
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.08em;
}
.ccw-context-value {
  color: var(--text);
  font-size: 12px;
  font-weight: 650;
  line-height: 1.5;
  overflow-wrap: anywhere;
}
.ccw-context-note {
  margin-top: 5px;
  color: var(--text-secondary);
  font-size: 9px;
  line-height: 1.55;
}
.ccw-badge {
  display: inline-block;
  margin-left: 6px;
  padding: 2px 6px;
  border-radius: 6px;
  background: var(--accent-subtle);
  color: var(--accent);
  font-size: 9px;
}
.ccw-conflict-note {
  margin-top: 8px;
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  background: var(--status-error-bg);
  color: var(--status-error);
  font-size: 9px;
  line-height: 1.5;
}
.ccw-quick-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 6px;
}
.ccw-quick-action {
  min-height: 32px;
  padding: 8px 6px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  color: var(--text-secondary);
  background: var(--bg);
  font-size: 9px;
  font-weight: 650;
  cursor: pointer;
  transition: border-color 120ms ease, color 120ms ease, background 120ms ease;
}
.ccw-quick-action:hover:not(:disabled) {
  border-color: var(--accent);
  color: var(--text);
  background: var(--bg-hover);
}
.ccw-quick-action:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}
.ccw-quick-action.primary {
  grid-column: 1 / -1;
  border-color: var(--accent);
  color: var(--text-inverse);
  background: var(--accent);
}
.ccw-quick-action.primary:hover:not(:disabled) {
  opacity: 0.9;
}
.ccw-cancel-action {
  grid-column: 1 / -1;
  border-color: var(--status-error);
  color: var(--status-error);
  background: var(--status-error-bg);
}
.ccw-cancel-action:hover:not(:disabled) {
  opacity: 0.9;
}
.ccw-context-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 7px 0;
  color: var(--text-secondary);
  font-size: 10px;
}
.ccw-context-row + .ccw-context-row {
  border-top: 1px solid var(--border);
}
.ccw-ok {
  width: 6px;
  height: 6px;
  flex: 0 0 auto;
  border-radius: 50%;
  background: var(--status-final);
  box-shadow: 0 0 0 3px var(--status-final-bg);
}
.ccw-flow-step {
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px 0;
  color: var(--text-tertiary);
  font-size: 9px;
}
.ccw-flow-step + .ccw-flow-step::before {
  position: absolute;
  top: -7px;
  left: 7px;
  width: 1px;
  height: 8px;
  background: var(--border);
  content: '';
}
.ccw-flow-dot {
  width: 15px;
  height: 15px;
  display: grid;
  place-items: center;
  flex: 0 0 auto;
  border-radius: 50%;
  color: var(--text-inverse);
  background: var(--status-final);
  font-size: 8px;
}
.ccw-flow-step.active {
  color: var(--text);
}
.ccw-flow-step.active .ccw-flow-dot {
  background: var(--accent);
  box-shadow: 0 0 0 4px var(--accent-subtle);
}
.ccw-flow-step.pending .ccw-flow-dot {
  color: var(--text-tertiary);
  background: var(--bg-hover);
}
.ccw-flow-error {
  margin-top: 8px;
  padding: 7px 9px;
  border: 1px solid var(--border);
  border-radius: var(--radius-sm, 6px);
  background: var(--status-error-bg);
  color: var(--status-error);
  font-size: 9px;
  line-height: 1.5;
}
.ccw-context :deep(.wf-container) {
  margin-top: 10px;
}
</style>
