<script setup lang="ts">
import { ref, computed, watch } from 'vue'
import { useChapterStore, useDocumentStore, useExpertStore, useUiStore, useOutlineStore, useCharacterStore, useWorldEntryStore, useHiddenThreadStore, useGenerationHistoryStore, friendlyError } from '../stores'
import type { WorkflowStep, SSEEnvelope, GenerateMode, ProjectMode, ArticleGenerateParams, WritingUnit } from '../api/types'
import type { AgentStartPayload, AgentOutputPayload, AgentDonePayload, ProgressPayload, ErrorPayload, WriterOutputPayload, CriticOutputPayload, ConsistencyCheckPayload, EnhanceDirectionsPayload, TurnSuggestionsPayload, RevisionSuggestionsPayload, SkillPackPayload, ArticleReviewPayload, GenerationRecordPayload, RunCreatedPayload, ClarificationRequiredPayload, ClarificationEmbedded, TaskCardClarificationStatus, TaskCardContextSummary, TaskCardPayload, TaskCardReviewRequiredPayload, PreGenerationMode } from '../api/types'
import { api } from '../api/client'
import ApprovalModal from './ApprovalModal.vue'
import AgentWorkflow from './AgentWorkflow.vue'
import ContextPicker from './ContextPicker.vue'
import EnhancePicker from './EnhancePicker.vue'
import TurnPicker from './TurnPicker.vue'
import RevisionSuggestionPicker from './RevisionSuggestionPicker.vue'
import ArticleParamsPicker from './ArticleParamsPicker.vue'
import DirectionPicker from './DirectionPicker.vue'
import MemoryStagingPanel from './MemoryStagingPanel.vue'
import ClarificationPanel from './ClarificationPanel.vue'
import TaskCardReviewPanel from './TaskCardReviewPanel.vue'

const props = defineProps<{ projectId: string; mode: ProjectMode }>()
const chapterStore = useChapterStore()
const documentStore = useDocumentStore()
const expertStore = useExpertStore()
const outlineStore = useOutlineStore()
const characterStore = useCharacterStore()
const worldEntryStore = useWorldEntryStore()
const hiddenThreadStore = useHiddenThreadStore()
const generationHistoryStore = useGenerationHistoryStore()
const ui = useUiStore()

const pid = computed(() => props.projectId)
const projectState = computed(() => expertStore.getState(pid.value))
const isNovel = computed(() => props.mode === 'novel')
const currentWritingUnit = computed(() => isNovel.value
  ? chapterStore.currentChapterForProject(pid.value)
  : documentStore.currentDocumentForProject(pid.value))
const currentUnitPosition = computed(() => isNovel.value
  ? chapterStore.currentChapterNum
  : documentStore.currentDocumentPosition)
const currentWritingUnitWordCount = computed(() => {
  const draft = currentWritingUnit.value?.draft ?? ''
  return draft.replace(/\s+/g, '').length || 1200
})
const initialDraftPreview = computed(() => cleanGeneratedContent(projectState.value.initialDraft))
const candidateDraftPreview = computed(() => cleanGeneratedContent(projectState.value.finalDraft))
const panelTitle = computed(() => isNovel.value ? '章节助手' : '内容助手')
const unitTypeLabel = computed(() => isNovel.value ? '当前章节' : '当前稿件')
const currentUnitTitle = computed(() => currentWritingUnit.value?.title?.trim() || (isNovel.value ? '未选择章节' : '未选择稿件'))
const currentUnitOrdinal = computed(() => currentWritingUnit.value ? (isNovel.value ? `第 ${currentUnitPosition.value} 章` : `第 ${currentUnitPosition.value} 篇`) : '未选择')
const reviewComments = computed(() => {
  if (!isNovel.value || !currentWritingUnit.value) return []
  return chapterStore.reviewCommentsForProjectChapter(pid.value, currentUnitPosition.value)
})

function unitPosition(unit: WritingUnit): number {
  return 'position' in unit ? unit.position : unit.chapter_num
}

const showApproval = ref(false)
const approvalContent = ref('')
const finalizingChapter = ref(false)
const showContextPicker = ref(false)
const showEnhancePicker = ref(false)
const showTurnPicker = ref(false)
const showRevisionPicker = ref(false)
const enhanceDirections = ref<string[]>([])
const turnSuggestions = ref<string[]>([])
const revisionDirections = ref<string[]>([])
const revisionCount = ref(0)
const maxRevisions = ref(3)
const pendingMode = ref<GenerateMode>('full_pipeline')
const canFinalizeCandidate = computed(() =>
  isNovel.value
  && !!currentWritingUnit.value
  && pendingMode.value !== 'summarize'
  && !!candidateDraftPreview.value,
)
const showArticleParams = ref(false)
const latestGenerationRecordId = ref<string | null>(null)
const latestRunId = ref<string | null>(null)

// ─── Clarification Loop (生成前澄清) ───
// 收到 clarification_required SSE 事件后暂存 payload，用于渲染 ClarificationPanel
const clarificationState = ref<ClarificationRequiredPayload | null>(null)

// L-1: 任务卡预览 — full_pipeline 默认开启
const planningReview = ref(true)
const taskCardState = ref<TaskCardPayload | null>(null)
const showTaskCardReview = ref(false)
const taskCardReviewKey = ref(0)
const embeddedClarification = ref<ClarificationEmbedded | null>(null)
const taskCardClarificationStatus = ref<TaskCardClarificationStatus | null>(null)
const taskCardContextSummary = ref<TaskCardContextSummary | null>(null)
// M-1: 生成前交互模式 — 会话级配置
// M-2: 持久化到 localStorage，按 projectId 分隔
const _modeKey = computed(() => `pre_gen_mode_${pid.value}`)
const _roundsKey = computed(() => `pre_gen_rounds_${pid.value}`)
function _loadMode(): PreGenerationMode {
  try {
    const v = localStorage.getItem(_modeKey.value)
    if (v === 'FAST' || v === 'PLANNING' || v === 'STRICT') return v
  } catch { /* 隐私模式 */ }
  return 'PLANNING'
}
function _loadRounds(): number {
  try {
    const v = parseInt(localStorage.getItem(_roundsKey.value) || '3', 10)
    return isNaN(v) || v < 1 || v > 10 ? 3 : v
  } catch { /* 隐私模式 */ }
  return 3
}
const preGenerationMode = ref<PreGenerationMode>(_loadMode())
const maxClarificationRounds = ref(_loadRounds())
// M-2: watch 写入 localStorage
watch(preGenerationMode, (v) => {
  try { localStorage.setItem(_modeKey.value, v) } catch { /* ignore */ }
})
watch(maxClarificationRounds, (v) => {
  try { localStorage.setItem(_roundsKey.value, String(v)) } catch { /* ignore */ }
})
// M-2: projectId 变化时重新加载
watch(pid, () => {
  preGenerationMode.value = _loadMode()
  maxClarificationRounds.value = _loadRounds()
})

// ─── Chapter context stats ───
interface ChapterContextStats {
  stats: { characters: number; events: number; hidden_threads: number; world_entries: number; sources: number }
  chapter_goal: { outline: string; light_line: string }
}
const contextStats = ref<ChapterContextStats | null>(null)

async function fetchContextStats() {
  if (!isNovel.value) { contextStats.value = null; return }
  const seq = currentUnitPosition.value
  if (!seq) { contextStats.value = null; return }
  try {
    contextStats.value = await api.getChapterContext(pid.value, seq)
  } catch {
    contextStats.value = null
  }
}

watch([() => props.projectId, currentUnitPosition], () => {
  contextStats.value = null
  fetchContextStats()
  if (isNovel.value && currentUnitPosition.value) {
    chapterStore.loadReviewNotes(pid.value, currentUnitPosition.value)
  }
}, { immediate: true })

// ─── Direction picker state ───
const showDirectionPicker = ref(false)
const directionOptions = ref<Array<{ id: string; title: string; description: string; risk: string }>>([])
const directionLoading = ref(false)
let pendingContextPick: {
  outlineIds: string[]; characterIds: string[]; worldEntryIds: string[]; hiddenThreadIds: string[]; targetWords: number; userNote: string; includeKnowledgeSources: boolean
} | null = null

async function fetchAndShowDirections() {
  if (!isNovel.value) return
  const seq = currentUnitPosition.value
  if (!seq) return
  showDirectionPicker.value = true
  directionOptions.value = []
  directionLoading.value = true
  try {
    const selectedIds = pendingContextPick ? {
      selected_outline_ids: pendingContextPick.outlineIds.length ? pendingContextPick.outlineIds : undefined,
      selected_character_ids: pendingContextPick.characterIds.length ? pendingContextPick.characterIds : undefined,
      selected_world_entry_ids: pendingContextPick.worldEntryIds.length ? pendingContextPick.worldEntryIds : undefined,
      selected_hidden_thread_ids: pendingContextPick.hiddenThreadIds.length ? pendingContextPick.hiddenThreadIds : undefined,
    } : undefined
    const result = await api.getChapterDirections(pid.value, seq, selectedIds)
    directionOptions.value = result.options ?? []
  } catch {
    directionOptions.value = []
  } finally {
    directionLoading.value = false
  }
}

function handleDirectionConfirm(_directionId: string, directionTitle: string, userNote: string) {
  showDirectionPicker.value = false
  if (!pendingContextPick) return
  const combinedUserNote = [pendingContextPick.userNote, userNote.trim()]
    .filter(Boolean)
    .join('\n')
  expertStore.startGenerating(pid.value)
  expertStore.setWorkflowSteps(pid.value, defaultWorkflow.map(s => ({ ...s, status: 'pending' as const })))
  runGenerateStream(
    pendingContextPick.outlineIds,
    pendingContextPick.characterIds,
    pendingContextPick.worldEntryIds,
    pendingContextPick.hiddenThreadIds,
    pendingContextPick.targetWords,
    undefined, // enhanceDirection
    undefined, // turnDirection
    combinedUserNote,
    directionTitle, // selectedDirection
    pendingContextPick.includeKnowledgeSources,
  )
  pendingContextPick = null
}

function handleDirectionSkip() {
  showDirectionPicker.value = false
  if (!pendingContextPick) return
  expertStore.startGenerating(pid.value)
  expertStore.setWorkflowSteps(pid.value, defaultWorkflow.map(s => ({ ...s, status: 'pending' as const })))
  runGenerateStream(
    pendingContextPick.outlineIds,
    pendingContextPick.characterIds,
    pendingContextPick.worldEntryIds,
    pendingContextPick.hiddenThreadIds,
    pendingContextPick.targetWords,
    undefined,
    undefined,
    pendingContextPick.userNote,
    undefined,
    pendingContextPick.includeKnowledgeSources,
  )
  pendingContextPick = null
}

function handleDirectionCancel() {
  showDirectionPicker.value = false
  pendingContextPick = null
}

// ─── Mode-aware labels ───
const GENERATE_LABEL = computed(() => isNovel.value ? '章节生成' : '生成内容')

interface QuickActionDef { key: string; novelLabel: string; articleLabel: string; mode: GenerateMode }
const QUICK_ACTIONS: QuickActionDef[] = [
  { key: 'turn', novelLabel: '转折建议', articleLabel: '标题建议', mode: 'continue' },
  { key: 'enhance', novelLabel: '润色', articleLabel: '改写优化', mode: 'enhance' },
  { key: 'summarize', novelLabel: '读者视角', articleLabel: '受众视角', mode: 'summarize' },
]

function quickActionLabel(action: QuickActionDef): string {
  return isNovel.value ? action.novelLabel : action.articleLabel
}

/** Abort controller for cancelling in-flight SSE requests */
let currentAbort: AbortController | null = null

/**
 * M-2: 创建 SSE 看门狗 — 首事件超时 + idle 超时
 * @param onTimeout 超时回调，reason 区分首事件超时还是 idle 超时
 * @param firstEventMs 首事件超时（默认 20s）
 * @param idleMs idle 超时（默认 90s）
 */
function createSSEWatchdog(
  onTimeout: (reason: 'first_event' | 'idle') => void,
  firstEventMs = 20000,
  idleMs = 90000,
) {
  let receivedFirst = false
  let timer: number | undefined
  const arm = () => {
    timer = window.setTimeout(() => {
      onTimeout(receivedFirst ? 'idle' : 'first_event')
    }, receivedFirst ? idleMs : firstEventMs)
  }
  return {
    onEvent: () => {
      if (!receivedFirst) receivedFirst = true
      if (timer) window.clearTimeout(timer)
      arm()
    },
    start: arm,
    clear: () => { if (timer) window.clearTimeout(timer) },
  }
}

/** M-2: SSE 超时通用处理 — abort + toast + 停止生成 */
function handleSSETimeout(reason: 'first_event' | 'idle', baseMsg: string) {
  if (currentAbort && !currentAbort.signal.aborted) {
    currentAbort.abort()
  }
  if (showTaskCardReview.value) {
    taskCardReviewKey.value += 1
  }
  const suffix = reason === 'first_event' ? '未收到响应' : '90 秒无新事件，可能已断开'
  const msg = `${baseMsg}：${suffix}。请重试或重启后端服务。`
  expertStore.appendOutput(pid.value, `\n\n[错误] ${msg}`)
  expertStore.stopGenerating(pid.value)
  ui.showToast(msg, 'error')
}
/** Thread ID for HITL resume (set when workflow pauses at human_review) */
const hitlThreadId = ref<string | null>(null)
/** Whether we're currently in a HITL resume flow (prevent re-showing approval modal on done) */
let hitlResuming = false

const defaultWorkflow: WorkflowStep[] = [
  { id: 'context', name: 'ContextLoader', status: 'pending', expert_id: 'system' },
  { id: 'writer', name: 'Writer', status: 'pending', expert_id: 'renderer' },
  { id: 'critic', name: 'Critic', status: 'pending', expert_id: 'cruel' },
  { id: 'consistency', name: 'ConsistencyChecker', status: 'pending', expert_id: 'editor' },
  { id: 'review', name: 'HumanReview', status: 'pending', expert_id: 'user' },
]

const articleWorkflow: WorkflowStep[] = [
  { id: 'writer', name: 'ContentWriter', status: 'pending', expert_id: 'content_writer' },
  { id: 'critic', name: 'StructureReview', status: 'pending', expert_id: 'structure_review' },
  { id: 'consistency', name: 'AudienceReview', status: 'pending', expert_id: 'audience_review' },
  { id: 'platform', name: 'PlatformReview', status: 'pending', expert_id: 'platform_review' },
  { id: 'review', name: 'RiskReview', status: 'pending', expert_id: 'risk_review' },
]

function agentToStepId(agent: string): string | null {
  const map: Record<string, string> = {
    writer: 'writer',
    chapter_writer: 'writer',
    content_writer: 'writer',
    critic: 'critic',
    structure_review: 'critic',
    consistency_checker: 'consistency',
    audience_review: 'consistency',
    platform_review: 'platform',
    context_loader: 'context',
    human_review: 'review',
    risk_review: 'review',
  }
  return map[agent] ?? null
}

function resetWorkflowForMode() {
  const workflow = isNovel.value ? defaultWorkflow : articleWorkflow
  expertStore.setWorkflowSteps(pid.value, workflow.map(s => ({ ...s, status: 'pending' as const })))
}

function articleReviewTitle(type: string): string {
  const labels: Record<string, string> = {
    structure: '结构/标题检查',
    audience: '受众匹配检查',
    platform: '平台/CTA 检查',
    risk: '风险/事实性提醒',
  }
  return labels[type] ?? '文章审校'
}

function reviewSourceLabel(sourceType?: string): string {
  if (!sourceType) return '审校'
  const labels: Record<string, string> = {
    critic_output: '审校意见',
    consistency_check: '一致性检查',
    critic: '审校意见',
    consistency: '一致性检查',
  }
  return labels[sourceType] ?? expertStore.getExpertName(sourceType)
}

function reviewCommentAuthor(comment: { expert_id: string; source_type?: string }): string {
  if (comment.expert_id && comment.expert_id !== comment.source_type) {
    return expertStore.getExpertName(comment.expert_id)
  }
  return reviewSourceLabel(comment.source_type || comment.expert_id)
}

async function saveReviewNoteFromStream(sourceType: 'critic_output' | 'consistency_check', content: string, severity: 'info' | 'warning' | 'critical' = 'warning') {
  if (!isNovel.value || !content.trim() || !currentUnitPosition.value) return
  await chapterStore.createReviewNote(pid.value, currentUnitPosition.value, {
    source_type: sourceType,
    severity,
    content: content.trim(),
    resolved: false,
    metadata: { expert_id: sourceType === 'critic_output' ? 'cruel' : 'editor' },
  })
  await chapterStore.loadReviewNotes(pid.value, currentUnitPosition.value)
}

async function resolveReviewComment(commentId: string) {
  if (!isNovel.value || !currentUnitPosition.value) return
  try {
    await chapterStore.markReviewCommentResolved(pid.value, currentUnitPosition.value, commentId)
    ui.showToast('已标记解决', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '标记审核意见失败'), 'error')
  }
}

function formatReviewResult(result: unknown): string {
  if (!result || typeof result !== 'object') return String(result ?? '')
  const record = result as Record<string, unknown>
  const parts: string[] = []
  for (const [key, value] of Object.entries(record)) {
    if (Array.isArray(value)) {
      if (value.length) parts.push(`${key}: ${value.join('；')}`)
    } else if (value !== undefined && value !== null && value !== '') {
      parts.push(`${key}: ${String(value)}`)
    }
  }
  return parts.join('\n')
}

function cleanGeneratedContent(content: string): string {
  if (!content) return ''
  let result = content
    .replace(/【[^】]*?(?:视觉|听觉|嗅觉|味觉|触觉|心理|氛围|意象)[^】]*?】\s*/g, '')
    .replace(/(\*\*|__)(.*?)\1/g, '$2')
  result = result
    .replace(
      /(^|\n)\s*(?:#{1,6}\s*)?(?:📌|💡|📝|⚠️|🎯|✨|🔍|📖)?\s*(?:本章要点|后续建议|下章预告|写作笔记|注意事项|创作目标|亮点|改进|总结|关键|核心|建议|方向|伏笔|提示)[^\n]*(?:\n(?!\s*\n)(?!\s*#{1,6}\s*)[^\n]+)*/g,
      '',
    )
    .replace(/(^|\n)\s*##\s*(?:输出格式|创作方向|写作方向|改进方向|审校意见|编辑建议)[^\n]*(?:\n(?!\s*\n)(?!##)[^\n]+)*/g, '')
    .replace(/(^|\n)\s*-{3,}\s*(?=\n|$)/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
  return result
}

function applyCandidateToEditor(content: string, fallbackMessage = '没有可应用的生成内容'): boolean {
  const unit = currentWritingUnit.value
  if (!unit || !content) {
    ui.showToast(fallbackMessage, 'error')
    return false
  }
  if (isNovel.value) {
    chapterStore.updateDraft(pid.value, content)
  } else {
    documentStore.updateDraft(pid.value, content)
  }
  return true
}

function refreshGenerationHistory() {
  const unit = currentWritingUnit.value
  if (!unit) return
  if (isNovel.value) {
    void generationHistoryStore.loadChapterRecords(pid.value, unitPosition(unit))
  } else {
    void generationHistoryStore.loadDocumentRecords(pid.value, unit.id)
  }
}

async function closePausedWorkflow(threadId: string) {
  try {
    await api.resumeGeneration(pid.value, threadId, 'reject', () => undefined, undefined, undefined, props.mode)
  } catch {
    // 这里只是释放后端暂停态；失败不影响本地候选稿进入编辑器。
  }
}

// ─── Generate (full pipeline) ───

function handleGenerate() {
  pendingMode.value = 'full_pipeline'
  latestGenerationRecordId.value = null
  latestRunId.value = null
  embeddedClarification.value = null
  taskCardClarificationStatus.value = null
  taskCardContextSummary.value = null
  clarificationState.value = null
  showTaskCardReview.value = false
  taskCardState.value = null
  hitlThreadId.value = null
  if (isNovel.value) {
    showContextPicker.value = true
  } else {
    showArticleParams.value = true
  }
}

const articleParams = ref<ArticleGenerateParams | null>(null)

function handleArticleConfirm(params: ArticleGenerateParams) {
  articleParams.value = params
  showArticleParams.value = false
  expertStore.startGenerating(pid.value)
  if (pendingMode.value === 'enhance') {
    runEnhanceDirections()
  } else if (pendingMode.value === 'continue') {
    runTurnSuggestions()
  } else {
    resetWorkflowForMode()
    runGenerateStream()
  }
}

function handleArticleCancel() {
  showArticleParams.value = false
}

function handleContextConfirm(outlineIds: string[], characterIds: string[], worldEntryIds: string[], hiddenThreadIds: string[], targetWords: number, userNote: string, includeKnowledgeSources: boolean) {
  showContextPicker.value = false
  pendingContextPick = { outlineIds, characterIds, worldEntryIds, hiddenThreadIds, targetWords, userNote, includeKnowledgeSources }
  // full_pipeline 直接生成，不走 DirectionPicker（v2 由 Clarification Loop 负责生成前提问）
  if (pendingMode.value === 'full_pipeline') {
    expertStore.startGenerating(pid.value)
    expertStore.setWorkflowSteps(pid.value, defaultWorkflow.map(s => ({ ...s, status: 'pending' as const })))
    runGenerateStream(outlineIds, characterIds, worldEntryIds, hiddenThreadIds, targetWords, undefined, undefined, userNote, undefined, includeKnowledgeSources)
    pendingContextPick = null
  } else {
    fetchAndShowDirections()
  }
}

function handleContextCancel() {
  showContextPicker.value = false
}

// ─── Quick actions ───

function handleQuickAction(key: string) {
  const action = QUICK_ACTIONS.find(a => a.key === key)
  if (!action) return
  pendingMode.value = action.mode
  latestGenerationRecordId.value = null
  latestRunId.value = null
  clarificationState.value = null
  showTaskCardReview.value = false
  taskCardState.value = null
  hitlThreadId.value = null
  if (!isNovel.value) {
    showArticleParams.value = true
    return
  }
  expertStore.startGenerating(pid.value)
  if (action.mode === 'enhance') {
    runEnhanceDirections()
  } else if (action.mode === 'continue') {
    runTurnSuggestions()
  } else if (action.mode === 'summarize') {
    runGenerateStream()
  }
}

// ─── Enhance flow ───

async function runEnhanceDirections() {
  const unit = currentWritingUnit.value
  if (!unit) {
    expertStore.stopGenerating(pid.value)
    ui.showToast(`请先选择一个${isNovel.value ? '章节' : '稿件'}`, 'error')
    return
  }
  currentAbort = new AbortController()
  const watchdog = createSSEWatchdog(
    (reason) => handleSSETimeout(reason, isNovel.value ? '获取润色方向无响应' : '获取改写方向无响应'),
  )
  watchdog.start()
  try {
    await api.generateStream(
      pid.value,
      {
        document_id: isNovel.value ? undefined : unit.id,
        chapter_id: isNovel.value ? unit.id : undefined,
        chapter_num: unitPosition(unit),
        mode: 'enhance',
        ...(isNovel.value ? {} : articleParams.value ?? {}),
      },
      (envelope: SSEEnvelope) => { watchdog.onEvent(); handleSSEEvent(envelope) },
      currentAbort.signal,
      props.mode,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, isNovel.value ? '获取润色方向失败' : '获取改写方向失败')
    expertStore.appendOutput(pid.value, `\n[错误] ${msg}`)
    expertStore.stopGenerating(pid.value)
    ui.showToast(msg, 'error')
  } finally {
    watchdog.clear()
  }
}

function handleEnhanceConfirm(direction: string, userNote: string, targetWords: number) {
  showEnhancePicker.value = false
  expertStore.startGenerating(pid.value)
  runGenerateStream(undefined, undefined, undefined, undefined, targetWords, direction, undefined, userNote)
}

// ─── Turn flow ───

async function runTurnSuggestions() {
  const unit = currentWritingUnit.value
  if (!unit) {
    expertStore.stopGenerating(pid.value)
    ui.showToast(`请先选择一个${isNovel.value ? '章节' : '稿件'}`, 'error')
    return
  }
  currentAbort = new AbortController()
  const watchdog = createSSEWatchdog(
    (reason) => handleSSETimeout(reason, isNovel.value ? '获取转折建议无响应' : '获取内容方向无响应'),
  )
  watchdog.start()
  try {
    await api.generateStream(
      pid.value,
      {
        document_id: isNovel.value ? undefined : unit.id,
        chapter_id: isNovel.value ? unit.id : undefined,
        chapter_num: unitPosition(unit),
        mode: 'continue',
        ...(isNovel.value ? {} : articleParams.value ?? {}),
      },
      (envelope: SSEEnvelope) => { watchdog.onEvent(); handleSSEEvent(envelope) },
      currentAbort.signal,
      props.mode,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, isNovel.value ? '获取转折建议失败' : '获取内容方向失败')
    expertStore.appendOutput(pid.value, `\n[错误] ${msg}`)
    expertStore.stopGenerating(pid.value)
    ui.showToast(msg, 'error')
  } finally {
    watchdog.clear()
  }
}

function handleTurnConfirm(direction: string, userNote: string) {
  showTurnPicker.value = false
  expertStore.startGenerating(pid.value)
  runGenerateStream(undefined, undefined, undefined, undefined, undefined, undefined, direction, userNote)
}

// ─── Revision flow ───

async function requestRevisionDirections() {
  if (!hitlThreadId.value) return
  const threadId = hitlThreadId.value
  pendingMode.value = 'full_pipeline'
  hitlResuming = true
  currentAbort = new AbortController()
  const watchdog = createSSEWatchdog(
    (reason) => handleSSETimeout(reason, '获取修改方向无响应'),
  )
  watchdog.start()
  try {
    await api.resumeGeneration(
      pid.value,
      threadId,
      'review',
      (envelope: SSEEnvelope) => { watchdog.onEvent(); handleSSEEvent(envelope) },
      undefined,
      currentAbort.signal,
      props.mode,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, '获取修改方向失败')
    expertStore.appendOutput(pid.value, `\n[错误] ${msg}`)
    ui.showToast(msg, 'error')
  } finally {
    watchdog.clear()
    hitlResuming = false
  }
}

async function handleRevisionConfirm(direction: string, userNote: string) {
  showRevisionPicker.value = false
  if (!hitlThreadId.value) return
  const feedback = userNote.trim() ? `${direction}\n补充说明：${userNote.trim()}` : direction
  const nextRevisionCount = revisionCount.value + 1

  expertStore.startGenerating(pid.value)
  expertStore.setWorkflowSteps(pid.value, defaultWorkflow.map((s) => ({
    ...s,
    status: s.id === 'context' ? 'success' as const : 'pending' as const,
  })))
  expertStore.setRevisionInfo(pid.value, nextRevisionCount, maxRevisions.value)

  currentAbort = new AbortController()
  const watchdog = createSSEWatchdog(
    (reason) => handleSSETimeout(reason, '修改无响应'),
  )
  watchdog.start()
  try {
    await api.resumeGeneration(
      pid.value,
      hitlThreadId.value!,
      'revise',
      (envelope: SSEEnvelope) => { watchdog.onEvent(); handleSSEEvent(envelope) },
      feedback,
      currentAbort.signal,
      props.mode,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, '修改失败')
    expertStore.appendOutput(pid.value, `\n[错误] ${msg}`)
    expertStore.stopGenerating(pid.value)
    ui.showToast(msg, 'error')
  } finally {
    watchdog.clear()
  }
}

// ─── Core generate stream ───

async function runGenerateStream(
  selectedOutlineIds?: string[],
  selectedCharacterIds?: string[],
  selectedWorldEntryIds?: string[],
  selectedHiddenThreadIds?: string[],
  targetWords?: number,
  enhanceDirection?: string,
  turnDirection?: string,
  userNote?: string,
  selectedDirection?: string,
  includeKnowledgeSources?: boolean,
) {
  const unit = currentWritingUnit.value
  if (!unit) {
    expertStore.stopGenerating(pid.value)
    ui.showToast(`请先选择一个${isNovel.value ? '章节' : '稿件'}`, 'error')
    return
  }

  currentAbort = new AbortController()
  latestGenerationRecordId.value = null
  latestRunId.value = null
  // M-2: 用 SSE 看门狗替换原 noEventTimer，增加 idle 超时
  const watchdog = createSSEWatchdog(
    (reason) => handleSSETimeout(reason, '生成连接超时'),
    20000,
    90000,
  )
  watchdog.start()

  try {
    await api.generateStream(
      pid.value,
      {
        document_id: isNovel.value ? undefined : unit.id,
        chapter_id: isNovel.value ? unit.id : undefined,
        chapter_num: unitPosition(unit),
        mode: pendingMode.value,
        selected_outline_ids: selectedOutlineIds,
        selected_character_ids: selectedCharacterIds,
        selected_world_entry_ids: selectedWorldEntryIds,
        selected_hidden_thread_ids: selectedHiddenThreadIds,
        include_knowledge_sources: includeKnowledgeSources,
        target_words: targetWords,
        enhance_direction: enhanceDirection,
        turn_direction: turnDirection,
        user_note: userNote,
        selected_direction: selectedDirection,
        planning_review: planningReview.value || undefined,
        // M-1: 生成前交互模式
        pre_generation_mode: preGenerationMode.value,
        max_clarification_rounds: maxClarificationRounds.value,
        ...(articleParams.value ?? {}),
      },
      (envelope: SSEEnvelope) => {
        watchdog.onEvent()
        handleSSEEvent(envelope)
      },
      currentAbort.signal,
      props.mode,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, '生成失败')
    expertStore.appendOutput(pid.value, `\n\n[错误] ${msg}`)
    expertStore.stopGenerating(pid.value)
    ui.showToast(msg, 'error')
  } finally {
    watchdog.clear()
  }
}

// ─── SSE event handling ───

function handleSSEEvent(envelope: SSEEnvelope) {
  const { event, data } = envelope

  switch (event) {
    case 'progress': {
      const payload = data as unknown as ProgressPayload
      expertStore.appendOutput(pid.value, `[系统] ${payload.message}\n`)
      // HITL: 当后端暂停在 human_review 时，发送带 thread_id 的 progress 事件
      const threadId = (data as Record<string, unknown>).thread_id as string | undefined
      if (threadId && pendingMode.value === 'full_pipeline') {
        hitlThreadId.value = threadId
        expertStore.stopGenerating(pid.value)
        expertStore.updateStepStatus(pid.value, 'review', 'running')
        const state = expertStore.getState(pid.value)
        approvalContent.value = cleanGeneratedContent(state.finalDraft)
        showApproval.value = true
      }
      break
    }
    case 'agent_start': {
      const payload = data as unknown as AgentStartPayload
      const stepId = agentToStepId(payload.agent)
      if (stepId) expertStore.updateStepStatus(pid.value, stepId, 'running')
      expertStore.appendOutput(pid.value, `\n[${payload.agent}] 开始...\n`)
      break
    }
    case 'agent_output': {
      const payload = data as unknown as AgentOutputPayload
      expertStore.appendOutput(pid.value, payload.token)
      break
    }
    case 'agent_done': {
      const payload = data as unknown as AgentDonePayload
      const stepId = agentToStepId(payload.agent)
      if (stepId) expertStore.updateStepStatus(pid.value, stepId, 'success')
      break
    }
    case 'writer_output': {
      const payload = data as unknown as WriterOutputPayload
      if (payload.token) {
        expertStore.appendDraft(pid.value, payload.token)
        if (!expertStore.getState(pid.value).initialDraft) {
          expertStore.appendInitialDraft(pid.value, payload.token)
        }
      } else if (payload.content) {
        const state = expertStore.getState(pid.value)
        if (payload.initial_draft) {
          expertStore.setInitialDraft(pid.value, payload.initial_draft)
        } else if (!state.initialDraft) {
          expertStore.setInitialDraft(pid.value, payload.content)
        }
        // chain_end 返回的是完整正文，不是增量；设置而非追加可避免和流式 token 重复。
        expertStore.setDraft(pid.value, payload.content)
      }
      expertStore.updateStepStatus(pid.value, 'writer', 'running')
      break
    }
    case 'content_output': {
      const payload = data as unknown as WriterOutputPayload
      if (payload.token) {
        expertStore.appendDraft(pid.value, payload.token)
        if (!expertStore.getState(pid.value).initialDraft) {
          expertStore.appendInitialDraft(pid.value, payload.token)
        }
      } else if (payload.content) {
        if (!expertStore.getState(pid.value).initialDraft) {
          expertStore.setInitialDraft(pid.value, payload.content)
        }
        expertStore.setDraft(pid.value, payload.content)
      }
      expertStore.updateStepStatus(pid.value, 'writer', 'running')
      break
    }
    case 'editor_output': {
      const payload = data as unknown as WriterOutputPayload
      if (payload.content) {
        expertStore.setDraft(pid.value, payload.content)
      } else if (payload.token) {
        expertStore.appendDraft(pid.value, payload.token)
      }
      break
    }
    case 'architect_output': {
      // L-1: 先缓存任务卡；真正进入可操作的人工等待态时由
      // task_card_review_required 事件展示面板，因为那时才有 thread_id。
      const taskCard = (data as Record<string, unknown>).task_card as TaskCardPayload | undefined
      if (taskCard && planningReview.value) {
        taskCardState.value = taskCard
      }
      break
    }
    case 'task_card_review_required': {
      // L-2：任务卡是唯一的生成前暂停点，澄清问题随 payload 一起展示。
      const payload = data as unknown as TaskCardReviewRequiredPayload
      clarificationState.value = null
      if (payload.task_card) {
        taskCardState.value = payload.task_card
        showTaskCardReview.value = true
      }
      embeddedClarification.value = payload.clarification ?? null
      taskCardClarificationStatus.value = payload.clarification_status ?? null
      taskCardContextSummary.value = payload.context_summary ?? null
      if (payload.thread_id) hitlThreadId.value = payload.thread_id
      // M-1: 同步当前 run 的模式（resume 时后端可能返回不同模式）
      if (payload.pre_generation_mode) preGenerationMode.value = payload.pre_generation_mode
      expertStore.stopGenerating(pid.value)
      expertStore.updateStepStatus(pid.value, 'writer', 'pending')
      expertStore.appendOutput(pid.value, '[系统] 澄清已完成，章节任务卡已生成，等待你确认后继续写作。\n')
      break
    }
    case 'critic_output': {
      const payload = data as unknown as CriticOutputPayload
      const text = payload.critiques?.join('\n') ?? ''
      if (text) expertStore.appendOutput(pid.value, `\n[审校意见]\n${text}\n`)
      saveReviewNoteFromStream('critic_output', text, 'warning')
      expertStore.updateStepStatus(pid.value, 'critic', 'success')
      break
    }
    case 'consistency_check': {
      const payload = data as unknown as ConsistencyCheckPayload
      const gr = payload.guardrail_result
      // 结构化渲染：如果有 issues，按 severity 排列
      if (gr && !gr.parse_error && gr.issues && gr.issues.length) {
        const severityLabel: Record<string, string> = { high: '严重', medium: '中等', low: '轻微', info: '提示' }
        const lines = gr.issues.map(i => `  • [${severityLabel[i.severity ?? 'info'] ?? '提示'}] ${i.type ?? '未知'}: ${i.description ?? ''}`)
        const header = gr.summary ? `[一致性检查] ${gr.summary}（${severityLabel[gr.overall_severity ?? 'info'] ?? '提示'}）` : '[一致性检查]'
        expertStore.appendOutput(pid.value, `\n${header}\n${lines.join('\n')}\n`)
        // severity 映射：high → critical, medium → warning, low/info → info
        const maxSeverity = gr.overall_severity === 'high' ? 'critical' : (gr.overall_severity === 'medium' ? 'warning' : 'info')
        saveReviewNoteFromStream('consistency_check', payload.report || gr.summary || '一致性检查完成', maxSeverity)
      } else if (payload.report) {
        expertStore.appendOutput(pid.value, `\n[一致性检查]\n${payload.report}\n`)
        saveReviewNoteFromStream('consistency_check', payload.report, 'info')
      }
      expertStore.updateStepStatus(pid.value, 'consistency', 'success')
      break
    }
    case 'enhance_directions': {
      const payload = data as unknown as EnhanceDirectionsPayload
      enhanceDirections.value = payload.directions
      showEnhancePicker.value = true
      expertStore.stopGenerating(pid.value)
      break
    }
    case 'turn_suggestions': {
      const payload = data as unknown as TurnSuggestionsPayload
      turnSuggestions.value = payload.suggestions
      showTurnPicker.value = true
      expertStore.stopGenerating(pid.value)
      break
    }
    case 'content_suggestions': {
      const payload = data as unknown as TurnSuggestionsPayload
      turnSuggestions.value = payload.suggestions
      showTurnPicker.value = true
      expertStore.stopGenerating(pid.value)
      break
    }
    case 'article_review': {
      const payload = data as unknown as ArticleReviewPayload
      const body = formatReviewResult(payload.result)
      if (body) {
        expertStore.appendOutput(pid.value, `\n[${articleReviewTitle(payload.review_type)}]\n${body}\n`)
      }
      break
    }
    case 'revision_suggestions': {
      const payload = data as unknown as RevisionSuggestionsPayload
      revisionDirections.value = payload.directions
      revisionCount.value = payload.revision_count
      maxRevisions.value = payload.max_revisions
      expertStore.setRevisionInfo(pid.value, payload.revision_count, payload.max_revisions)
      showRevisionPicker.value = true
      break
    }
    case 'skill_pack': {
      const payload = data as unknown as SkillPackPayload
      expertStore.setExpertSkillPack(pid.value, payload)
      break
    }
    case 'generation_record': {
      const payload = data as unknown as GenerationRecordPayload
      latestGenerationRecordId.value = payload.id
      generationHistoryStore.upsertCandidate(pid.value, payload.id)
      refreshGenerationHistory()
      break
    }
    case 'run_created': {
      const payload = data as unknown as RunCreatedPayload
      latestRunId.value = payload.run_id
      break
    }
    case 'clarification_required': {
      // 生成前澄清：暂存 payload，停止"生成中"转圈，等待用户补充信息
      const payload = data as unknown as ClarificationRequiredPayload
      clarificationState.value = payload
      if (payload.thread_id) hitlThreadId.value = payload.thread_id
      if (payload.pre_generation_mode) preGenerationMode.value = payload.pre_generation_mode
      expertStore.appendOutput(pid.value, `[系统] 需要补充信息（第 ${payload.round}/${payload.max_rounds} 轮澄清）\n`)
      expertStore.stopGenerating(pid.value)
      break
    }
    case 'done': {
      const state = expertStore.getState(pid.value)
      for (const step of state.workflowSteps) {
        if (step.status === 'pending' || step.status === 'running') {
          expertStore.updateStepStatus(pid.value, step.id, 'success')
        }
      }
      expertStore.stopGenerating(pid.value)

      // HITL resume 返回的 done 不再弹审批框（已在 handleDecision 中处理）
      if (!hitlResuming && state.finalDraft) {
        approvalContent.value = cleanGeneratedContent(state.finalDraft)
        showApproval.value = true
      }
      break
    }
    case 'error': {
      const payload = data as unknown as ErrorPayload
      expertStore.appendOutput(pid.value, `\n[错误] ${payload.message}`)
      expertStore.stopGenerating(pid.value)
      ui.showToast(payload.message, 'error')
      break
    }
  }
}

// ─── Approval ───

// ─── Clarification Loop (生成前澄清) ───
async function handleClarificationSubmit(answers: Record<string, string>) {
  if (hitlResuming) return
  const threadId = hitlThreadId.value || clarificationState.value?.thread_id
  if (!threadId) {
    ui.showToast('缺少 thread_id，无法提交澄清回答', 'error')
    clarificationState.value = null
    return
  }
  hitlResuming = true
  expertStore.setGenerating(pid.value, true)
  const abort = new AbortController()
  currentAbort = abort
  const watchdog = createSSEWatchdog(
    (reason) => handleSSETimeout(reason, '提交澄清后无响应'),
  )
  watchdog.start()
  try {
    await api.resumeGeneration(
      pid.value,
      threadId,
      'submit_clarification',
      (envelope: SSEEnvelope) => { watchdog.onEvent(); handleSSEEvent(envelope) },
      undefined,
      abort.signal,
      props.mode,
      undefined,
      { clarification_answers: answers },
    )
  } catch (e: unknown) {
    if (abort.signal.aborted) return
    expertStore.appendOutput(pid.value, `\n[错误] 提交澄清回答失败：${friendlyError(e, '请重试')}`)
    expertStore.stopGenerating(pid.value)
    ui.showToast(friendlyError(e, '提交澄清回答失败'), 'error')
  } finally {
    watchdog.clear()
    hitlResuming = false
    if (currentAbort === abort) currentAbort = null
  }
}

async function handleClarificationSkip() {
  if (hitlResuming) return
  const threadId = hitlThreadId.value || clarificationState.value?.thread_id
  if (!threadId) {
    ui.showToast('缺少 thread_id，无法跳过澄清', 'error')
    clarificationState.value = null
    return
  }
  hitlResuming = true
  expertStore.setGenerating(pid.value, true)
  const abort = new AbortController()
  currentAbort = abort
  const watchdog = createSSEWatchdog(
    (reason) => handleSSETimeout(reason, '跳过澄清后无响应'),
  )
  watchdog.start()
  try {
    await api.resumeGeneration(
      pid.value,
      threadId,
      'skip_clarification',
      (envelope: SSEEnvelope) => { watchdog.onEvent(); handleSSEEvent(envelope) },
      undefined,
      abort.signal,
      props.mode,
    )
  } catch (e: unknown) {
    if (abort.signal.aborted) return
    expertStore.appendOutput(pid.value, `\n[错误] 跳过澄清失败：${friendlyError(e, '请重试')}`)
    expertStore.stopGenerating(pid.value)
    ui.showToast(friendlyError(e, '跳过澄清失败'), 'error')
  } finally {
    watchdog.clear()
    hitlResuming = false
    if (currentAbort === abort) currentAbort = null
  }
}

// ─── L-1: Task Card Review ───

async function handleTaskCardApproved(taskCard: TaskCardPayload, userNote?: string) {
  if (hitlResuming) return  // M-2: 防重复
  const threadId = hitlThreadId.value
  if (!threadId) {
    ui.showToast('缺少 thread_id，无法继续生成', 'error')
    return
  }
  hitlResuming = true
  expertStore.setGenerating(pid.value, true)
  expertStore.updateStepStatus(pid.value, 'writer', 'running')
  const abort = new AbortController()
  currentAbort = abort
  const watchdog = createSSEWatchdog(
    (reason) => handleSSETimeout(reason, '任务卡确认后无响应'),
  )
  watchdog.start()
  let writerStarted = false
  let resumeError: string | null = null
  try {
    const requestBody = userNote ? { user_note: userNote } : undefined
    await api.resumeGeneration(
      pid.value,
      threadId,
      'approve_task_card',
      (envelope: SSEEnvelope) => {
        watchdog.onEvent()
        if (envelope.event === 'agent_start') {
          const payload = envelope.data as unknown as AgentStartPayload
          if (agentToStepId(payload.agent) === 'writer') {
            writerStarted = true
            showTaskCardReview.value = false
            taskCardState.value = null
            embeddedClarification.value = null
            taskCardClarificationStatus.value = null
            taskCardContextSummary.value = null
          }
        } else if (envelope.event === 'error') {
          const payload = envelope.data as unknown as ErrorPayload
          resumeError = payload.message || '任务卡确认失败'
        }
        handleSSEEvent(envelope)
      },
      undefined,
      abort.signal,
      props.mode,
      JSON.stringify(taskCard),
      requestBody,
    )
    if (!writerStarted) {
      throw new Error(resumeError || '任务卡确认未能启动正文创作，请重试')
    }
  } catch (e: unknown) {
    if (abort.signal.aborted) return
    const msg = friendlyError(e, '任务卡确认失败')
    // SSE error 已由 handleSSEEvent 呈现；这里只处理网络/空流等未产生 SSE 错误的失败。
    if (!resumeError) {
      expertStore.appendOutput(pid.value, `\n[错误] ${msg}`)
      ui.showToast(msg, 'error')
    }
    expertStore.stopGenerating(pid.value)
  } finally {
    watchdog.clear()
    hitlResuming = false
    if (currentAbort === abort) currentAbort = null
    if (!writerStarted) {
      showTaskCardReview.value = true
      taskCardReviewKey.value += 1
    }
  }
}

async function handleTaskCardClarificationRefresh(answers: Record<string, string>, userNote?: string) {
  if (hitlResuming) return
  const threadId = hitlThreadId.value
  if (!threadId) {
    ui.showToast('缺少 thread_id，无法重新规划任务卡', 'error')
    return
  }
  hitlResuming = true
  expertStore.setGenerating(pid.value, true)
  const abort = new AbortController()
  currentAbort = abort
  const watchdog = createSSEWatchdog(
    (reason) => handleSSETimeout(reason, '任务卡重新规划后无响应'),
  )
  watchdog.start()
  let refreshed = false
  try {
    await api.resumeGeneration(
      pid.value,
      threadId,
      'refresh_task_card',
      (envelope: SSEEnvelope) => {
        watchdog.onEvent()
        if (envelope.event === 'task_card_review_required') {
          const payload = envelope.data as unknown as TaskCardReviewRequiredPayload
          refreshed = true
          taskCardState.value = payload.task_card
          embeddedClarification.value = payload.clarification ?? null
          taskCardClarificationStatus.value = payload.clarification_status ?? null
          taskCardContextSummary.value = payload.context_summary ?? null
          showTaskCardReview.value = true
          expertStore.stopGenerating(pid.value)
        }
        handleSSEEvent(envelope)
      },
      undefined,
      abort.signal,
      props.mode,
      undefined,
      { clarification_answers: answers, user_note: userNote || '' },
    )
    if (!refreshed) throw new Error('任务卡重新规划未返回新的任务卡，请重试')
  } catch (e: unknown) {
    if (!abort.signal.aborted) {
      const msg = friendlyError(e, '任务卡重新规划失败')
      expertStore.appendOutput(pid.value, `\n[错误] ${msg}`)
      ui.showToast(msg, 'error')
      taskCardReviewKey.value += 1
    }
  } finally {
    watchdog.clear()
    hitlResuming = false
    expertStore.stopGenerating(pid.value)
    if (currentAbort === abort) currentAbort = null
  }
}

async function handleTaskCardContextRefresh(excludedKeys: string[]) {
  if (hitlResuming) return
  const threadId = hitlThreadId.value
  if (!threadId) {
    ui.showToast('缺少 thread_id，无法应用上下文选择', 'error')
    return
  }
  hitlResuming = true
  expertStore.setGenerating(pid.value, true)
  const abort = new AbortController()
  currentAbort = abort
  const watchdog = createSSEWatchdog(
    (reason) => handleSSETimeout(reason, '上下文重新加载后无响应'),
  )
  watchdog.start()
  let refreshed = false
  try {
    await api.resumeGeneration(
      pid.value,
      threadId,
      'refresh_task_card_context',
      (envelope: SSEEnvelope) => {
        watchdog.onEvent()
        if (envelope.event === 'task_card_review_required') {
          const payload = envelope.data as unknown as TaskCardReviewRequiredPayload
          refreshed = true
          taskCardState.value = payload.task_card
          embeddedClarification.value = payload.clarification ?? null
          taskCardClarificationStatus.value = payload.clarification_status ?? null
          taskCardContextSummary.value = payload.context_summary ?? null
          showTaskCardReview.value = true
          expertStore.stopGenerating(pid.value)
        }
        handleSSEEvent(envelope)
      },
      undefined,
      abort.signal,
      props.mode,
      undefined,
      { excluded_context_keys: excludedKeys },
    )
    if (!refreshed) throw new Error('上下文应用后未返回新的任务卡，请重试')
  } catch (e: unknown) {
    if (!abort.signal.aborted) {
      const msg = friendlyError(e, '应用上下文选择失败')
      expertStore.appendOutput(pid.value, `\n[错误] ${msg}`)
      ui.showToast(msg, 'error')
      taskCardReviewKey.value += 1
    }
  } finally {
    watchdog.clear()
    hitlResuming = false
    expertStore.stopGenerating(pid.value)
    if (currentAbort === abort) currentAbort = null
  }
}

async function handleTaskCardRejected() {
  if (hitlResuming) return
  hitlResuming = true
  if (currentAbort && !currentAbort.signal.aborted) {
    currentAbort.abort()
  }
  const threadId = hitlThreadId.value
  if (threadId) {
    const abort = new AbortController()
    currentAbort = abort
    try {
      let rejected = false
      await api.resumeGeneration(
        pid.value,
        threadId,
        'reject_task_card',
        (envelope: SSEEnvelope) => {
          if (envelope.event === 'done') {
            rejected = true
            return
          }
          handleSSEEvent(envelope)
        },
        undefined,
        abort.signal,
        props.mode,
      )
      if (!rejected) throw new Error('取消任务卡未得到确认，请重试')
      showTaskCardReview.value = false
      taskCardState.value = null
      embeddedClarification.value = null
      taskCardClarificationStatus.value = null
      taskCardContextSummary.value = null
    } catch (e: unknown) {
      if (!abort.signal.aborted) {
        ui.showToast(friendlyError(e, '取消任务卡失败'), 'error')
        showTaskCardReview.value = true
        taskCardReviewKey.value += 1
      }
    } finally {
      if (currentAbort === abort) currentAbort = null
    }
  }
  expertStore.stopGenerating(pid.value)
  if (!threadId) {
    showTaskCardReview.value = false
    taskCardState.value = null
  }
  hitlResuming = false
}

async function handleDecision(decision: 'accept' | 'accept_with_mods' | 'reject') {
  showApproval.value = false
  const state = expertStore.getState(pid.value)
  const candidateContent = cleanGeneratedContent(state.finalDraft || approvalContent.value)

  if (decision === 'accept_with_mods') {
    if (hitlThreadId.value) {
      await requestRevisionDirections()
    } else {
      if (applyCandidateToEditor(candidateContent, '没有可修改的生成内容')) {
        if (latestGenerationRecordId.value) {
          generationHistoryStore.updateRecordStatus(pid.value, latestGenerationRecordId.value, 'applied')
        }
        ui.showToast('当前生成没有可恢复工作流，已放入编辑器，请修改后手动保存', 'info')
      }
      latestGenerationRecordId.value = null
      expertStore.stopGenerating(pid.value)
    }
    return
  }

  if (!hitlThreadId.value) {
    // 没有 HITL thread_id，走本地逻辑（兼容旧流程）
    expertStore.stopGenerating(pid.value)
    if (decision === 'accept') {
      if (applyCandidateToEditor(candidateContent)) {
        if (latestGenerationRecordId.value) {
          generationHistoryStore.updateRecordStatus(pid.value, latestGenerationRecordId.value, 'applied')
        }
        ui.showToast('已应用到编辑器，请确认后保存', 'success')
      }
    } else if (latestGenerationRecordId.value) {
      generationHistoryStore.updateRecordStatus(pid.value, latestGenerationRecordId.value, 'discarded')
    }
    latestGenerationRecordId.value = null
    return
  }

  // HITL 流程：前端采纳只写入编辑器草稿，不隐式落库。
  hitlResuming = true

  if (decision === 'reject') {
    // 拒绝：只需通知后端终止，不落库
    try {
      await api.resumeGeneration(pid.value, hitlThreadId.value!, 'reject', (envelope: SSEEnvelope) => handleSSEEvent(envelope), undefined, undefined, props.mode)
    } catch {
      // 即使通知失败也不影响（后端状态会过期）
    }
    hitlThreadId.value = null
    hitlResuming = false
    if (latestGenerationRecordId.value) {
      generationHistoryStore.updateRecordStatus(pid.value, latestGenerationRecordId.value, 'discarded')
    }
    latestGenerationRecordId.value = null
    expertStore.stopGenerating(pid.value)
    return
  }

  if (applyCandidateToEditor(candidateContent)) {
    if (latestGenerationRecordId.value) {
      generationHistoryStore.updateRecordStatus(pid.value, latestGenerationRecordId.value, 'applied')
    }
    const threadId = hitlThreadId.value
    if (threadId) await closePausedWorkflow(threadId)
    ui.showToast('已应用到编辑器，请确认后保存', 'success')
  }
  hitlThreadId.value = null
  latestGenerationRecordId.value = null
  hitlResuming = false
  expertStore.stopGenerating(pid.value)
}

async function handleFinalizeChapter() {
  if (!isNovel.value || !currentWritingUnit.value) {
    ui.showToast('当前不是可定稿的小说章节', 'error')
    return
  }

  const state = expertStore.getState(pid.value)
  const candidateContent = cleanGeneratedContent(state.finalDraft || approvalContent.value)
  if (!candidateContent) {
    ui.showToast('没有可定稿的章节正文', 'error')
    return
  }

  finalizingChapter.value = true
  try {
    await chapterStore.finalizeCurrentChapter(pid.value, candidateContent)
    expertStore.setDraft(pid.value, candidateContent)
    approvalContent.value = candidateContent
    if (latestGenerationRecordId.value) {
      generationHistoryStore.updateRecordStatus(pid.value, latestGenerationRecordId.value, 'applied')
    }
    const threadId = hitlThreadId.value
    if (threadId) await closePausedWorkflow(threadId)
    hitlThreadId.value = null
    latestGenerationRecordId.value = null
    showApproval.value = false
    expertStore.stopGenerating(pid.value)
    ui.showToast('本章已定稿，下一章创作会自动使用这版正文衔接', 'success')
  } catch (e: unknown) {
    ui.showToast(friendlyError(e, '章节定稿失败'), 'error')
  } finally {
    finalizingChapter.value = false
  }
}

// ─── Test expert ───

async function testExpert(expertId: string, sampleText: string) {
  expertStore.startGenerating(pid.value)
  currentAbort = new AbortController()
  try {
    await api.testExpertStream(
      pid.value,
      expertId,
      { test_text: sampleText },
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
      currentAbort.signal,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, '测试失败')
    expertStore.appendOutput(pid.value, `\n[错误] ${msg}`)
    expertStore.stopGenerating(pid.value)
    ui.showToast(msg, 'error')
  }
}

function cancelStream() {
  if (currentAbort) {
    currentAbort.abort()
    currentAbort = null
  }
  hitlThreadId.value = null
  hitlResuming = false
  showApproval.value = false
  showContextPicker.value = false
  showArticleParams.value = false
  showEnhancePicker.value = false
  showTurnPicker.value = false
  showRevisionPicker.value = false
  showTaskCardReview.value = false
  taskCardState.value = null
  // M-2: 清理 pre-generation ClarificationPanel 状态
  clarificationState.value = null
  expertStore.stopGenerating(pid.value, true)
}

defineExpose({ testExpert, cancelStream })
</script>

<template>
  <div class="agent-panel">
    <section class="agent-command">
      <div class="command-top">
        <div class="command-heading">
          <span class="command-kicker">{{ unitTypeLabel }}</span>
          <h3>{{ panelTitle }}</h3>
        </div>
        <router-link :to="`/projects/${projectId}/experts`" class="link-configure">配置</router-link>
      </div>

      <div class="unit-context" :class="{ muted: !currentWritingUnit }">
        <span class="unit-ordinal">{{ currentUnitOrdinal }}</span>
        <span class="unit-title">{{ currentUnitTitle }}</span>
      </div>

      <div v-if="isNovel && contextStats" class="context-stats">
        已加载：角色 {{ contextStats.stats.characters }} · 事件 {{ contextStats.stats.events }} · 暗线 {{ contextStats.stats.hidden_threads }} · 设定 {{ contextStats.stats.world_entries }}
      </div>

      <div class="primary-action">
        <button
          v-if="projectState.isGenerating"
          class="btn-cancel-stream"
          @click="cancelStream"
        >
          取消
        </button>
        <button
          v-else
          class="btn-generate"
          :disabled="!currentWritingUnit"
          @click="handleGenerate"
        >
          {{ GENERATE_LABEL }}
        </button>
      </div>

      <!-- M-1: 生成前交互模式配置（仅小说 full_pipeline 有效） -->
      <div v-if="isNovel && planningReview" class="pre-gen-config">
        <span class="pre-gen-label">生成前交互</span>
        <label class="pre-gen-option">
          <input type="radio" value="FAST" v-model="preGenerationMode" />
          <span>快速</span>
        </label>
        <label class="pre-gen-option">
          <input type="radio" value="PLANNING" v-model="preGenerationMode" />
          <span>计划</span>
        </label>
        <label class="pre-gen-option">
          <input type="radio" value="STRICT" v-model="preGenerationMode" />
          <span>严格</span>
        </label>
        <label class="pre-gen-rounds">
          最大澄清轮数
          <input type="number" min="1" max="10" v-model.number="maxClarificationRounds" class="rounds-input" />
        </label>
      </div>
    </section>

    <div class="quick-actions">
      <button
        v-for="action in QUICK_ACTIONS"
        :key="action.key"
        :disabled="!currentWritingUnit || projectState.isGenerating"
        @click="handleQuickAction(action.key)"
      >
        {{ quickActionLabel(action) }}
      </button>
    </div>

    <!-- L-1/L-2: 任务卡预览 + 嵌入式澄清。放在进度上方，避免人工等待态被藏在下方。 -->
    <TaskCardReviewPanel
      v-if="showTaskCardReview && taskCardState"
      :key="taskCardReviewKey"
      :task-card="taskCardState"
      :project-id="projectId"
      :thread-id="hitlThreadId ?? ''"
      :clarification="embeddedClarification"
      :clarification-status="taskCardClarificationStatus"
      :context-summary="taskCardContextSummary"
      @approved="handleTaskCardApproved"
      @rejected="handleTaskCardRejected"
      @clarification-refresh="handleTaskCardClarificationRefresh"
      @context-refresh="handleTaskCardContextRefresh"
    />

    <!-- Workflow progress -->
    <section v-if="pendingMode === 'full_pipeline'" class="workflow-section">
      <AgentWorkflow :project-id="projectId" :mode="props.mode" />
    </section>

    <!-- 写作记忆 staging -->
    <MemoryStagingPanel v-if="isNovel" :project-id="projectId" />

    <!-- 生成前澄清（区别于生成后的最终审核） -->
    <ClarificationPanel
      v-if="clarificationState"
      :run-id="clarificationState.run_id"
      :questions="clarificationState.questions"
      :round="clarificationState.round"
      :max-rounds="clarificationState.max_rounds"
      :assumptions-if-skipped="clarificationState.assumptions_if_skipped"
      :pre-generation-mode="clarificationState.pre_generation_mode ?? preGenerationMode"
      @submit="handleClarificationSubmit"
      @skip="handleClarificationSkip"
    />

    <!-- Review comments -->
    <div v-if="reviewComments.length" class="review-section">
      <div class="section-label">审核意见</div>
      <div
        v-for="comment in reviewComments"
        :key="comment.id"
        class="review-comment"
        :class="comment.severity"
      >
        <div class="comment-main">
          <span class="comment-expert">{{ reviewCommentAuthor(comment) }}</span>
          <span class="comment-text">{{ comment.comment }}</span>
        </div>
        <button
          v-if="!comment.resolved"
          class="resolve-comment-btn"
          type="button"
          @click="resolveReviewComment(comment.id)"
        >
          解决
        </button>
        <span v-else class="comment-resolved">已解决</span>
      </div>
    </div>

    <!-- Stream output (agent logs, critiques, etc.) -->
    <div v-if="projectState.streamOutput" class="stream-section">
      <div class="section-label">输出</div>
      <div class="stream-output">
        <pre>{{ projectState.streamOutput }}</pre>
      </div>
    </div>

    <!-- 普通预览只展示当前最新候选稿；初稿与版本对照只放在最后的人工审核弹窗。 -->
    <div v-if="candidateDraftPreview && !showApproval" class="draft-section">
      <div class="section-label">{{ isNovel ? '当前候选稿（最新版本）' : '当前候选内容（最新版本）' }}</div>
      <div class="draft-preview">
        <pre>{{ candidateDraftPreview }}</pre>
      </div>
    </div>

    <ApprovalModal
      :show="showApproval"
      :content="approvalContent"
      :original-content="initialDraftPreview"
      :mode="pendingMode"
      :has-h-i-t-l="!!hitlThreadId"
      :project-mode="props.mode"
      :can-finalize="canFinalizeCandidate"
      :finalizing="finalizingChapter"
      @decision="handleDecision"
      @finalize="handleFinalizeChapter"
    />

    <ContextPicker
      v-if="showContextPicker"
      :project-id="projectId"
      :outlines="outlineStore.entriesForProject(projectId)"
      :characters="characterStore.charactersForProject(projectId)"
       :world-entries="worldEntryStore.entriesForProject(projectId)"
      :hidden-threads="hiddenThreadStore.threadsForProject(projectId)"
      :current-chapter-num="currentUnitPosition"
      :mode="props.mode"
      @confirm="handleContextConfirm"
      @cancel="handleContextCancel"
    />

    <ArticleParamsPicker
      v-if="showArticleParams"
      :initial-params="articleParams"
      :mode="pendingMode"
      @confirm="handleArticleConfirm"
      @cancel="handleArticleCancel"
    />

    <EnhancePicker
      v-if="showEnhancePicker"
      :directions="enhanceDirections"
      :current-word-count="currentWritingUnitWordCount"
      :project-mode="props.mode"
      @confirm="handleEnhanceConfirm"
      @cancel="showEnhancePicker = false"
    />

    <TurnPicker
      v-if="showTurnPicker"
      :suggestions="turnSuggestions"
      :project-mode="props.mode"
      @confirm="handleTurnConfirm"
      @cancel="showTurnPicker = false"
    />

    <DirectionPicker
      v-if="showDirectionPicker"
      :options="directionOptions"
      :loading="directionLoading"
      @confirm="handleDirectionConfirm"
      @cancel="handleDirectionCancel"
      @skip="handleDirectionSkip"
    />

    <RevisionSuggestionPicker
      v-if="showRevisionPicker"
      :directions="revisionDirections"
      :revision-count="revisionCount"
      :max-revisions="maxRevisions"
      :project-mode="props.mode"
      @confirm="handleRevisionConfirm"
      @cancel="showRevisionPicker = false"
    />
  </div>
</template>

<style scoped>
.agent-panel {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.agent-command {
  display: flex;
  flex-direction: column;
  gap: var(--sp-3);
  padding: var(--sp-4);
  border: 1px solid color-mix(in srgb, var(--border) 86%, transparent);
  border-radius: 16px;
  background: color-mix(in srgb, var(--bg-panel) 84%, transparent);
  box-shadow: 0 1px 0 rgba(255,255,255,0.035) inset, var(--shadow-sm);
}
.command-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: var(--sp-3);
}
.command-heading {
  min-width: 0;
}
.command-kicker {
  display: block;
  margin-bottom: 2px;
  font-size: 10px;
  font-weight: 700;
  color: var(--text-tertiary);
}
.command-heading h3 {
  font-size: var(--text-lg);
  font-weight: 800;
  margin: 0;
  color: var(--text);
}
.unit-context {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
  padding: 9px 11px;
  border: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  border-radius: 10px;
  background: color-mix(in srgb, var(--bg) 74%, transparent);
}
.unit-context.muted {
  opacity: 0.65;
}
.unit-ordinal {
  flex: 0 0 auto;
  font-size: 11px;
  font-weight: 700;
  color: var(--accent);
}
.unit-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--text-secondary);
  font-size: var(--text-sm);
  font-weight: 650;
}

.context-stats {
  font-size: 12px;
  color: var(--text-tertiary, #768390);
  padding: 0 4px;
  margin-top: 4px;
  line-height: 1.6;
}
.primary-action {
  display: flex;
}
.link-configure {
  flex: 0 0 auto;
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  transition: color var(--transition);
}
.link-configure:hover {
  color: var(--accent);
  text-decoration: none;
}
.btn-generate {
  width: 100%;
  height: 44px;
  padding: 0 16px;
  background: var(--accent);
  color: var(--text-inverse);
  border: 1px solid var(--accent);
  border-radius: 12px;
  font-size: var(--text-sm);
  font-weight: 700;
  box-shadow: 0 10px 22px color-mix(in srgb, var(--accent) 22%, transparent);
  transition: background var(--transition), transform var(--transition), box-shadow var(--transition);
}
.btn-generate:disabled {
  background: var(--status-pending);
  border-color: var(--status-pending);
  box-shadow: none;
  cursor: not-allowed;
  opacity: 0.55;
}
.btn-generate:hover:not(:disabled) {
  background: var(--accent-hover);
  transform: translateY(-1px);
  box-shadow: 0 14px 30px color-mix(in srgb, var(--accent) 28%, transparent);
}
.btn-cancel-stream {
  width: 100%;
  height: 44px;
  padding: 0 16px;
  background: var(--status-error);
  color: var(--text-inverse);
  border: 1px solid var(--status-error);
  border-radius: 12px;
  font-size: var(--text-sm);
  font-weight: 700;
  transition: opacity var(--transition), transform var(--transition);
  cursor: pointer;
}
.btn-cancel-stream:hover {
  opacity: 0.9;
  transform: translateY(-1px);
}

/* M-1: 生成前交互模式配置 */
.pre-gen-config {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 8px;
  padding-top: 8px;
  border-top: 1px solid color-mix(in srgb, var(--border, #2a3342) 60%, transparent);
  font-size: 13px;
}
.pre-gen-label {
  font-weight: 600;
  color: var(--text-secondary, #cbd5e1);
}
.pre-gen-option {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  cursor: pointer;
  color: var(--text, #f8fafc);
}
.pre-gen-option input[type="radio"] {
  margin: 0;
  cursor: pointer;
}
.pre-gen-rounds {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-left: auto;
  color: var(--text-tertiary, #94a3b8);
  font-size: 12px;
}
.rounds-input {
  width: 48px;
  padding: 2px 4px;
  border: 1px solid var(--border, #2a3342);
  border-radius: 4px;
  background: var(--bg-elevated, #1e293b);
  color: var(--text, #f8fafc);
  font-size: 12px;
  text-align: center;
}

.quick-actions {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
}
.quick-actions button {
  min-width: 0;
  min-height: 38px;
  padding: 0 10px;
  border: 1px solid color-mix(in srgb, var(--border) 86%, transparent);
  border-radius: 10px;
  background: color-mix(in srgb, var(--bg-panel) 86%, transparent);
  font-size: var(--text-xs);
  font-weight: 720;
  color: var(--text-secondary);
  text-align: center;
  white-space: nowrap;
  box-shadow: 0 1px 0 rgba(255,255,255,0.035) inset, var(--shadow-sm);
  transition: background var(--transition), border-color var(--transition), color var(--transition), opacity var(--transition);
}
.quick-actions button:hover {
  background: color-mix(in srgb, var(--accent-subtle) 72%, var(--bg-panel));
  border-color: var(--border-focus);
  color: var(--accent);
}
.quick-actions button:disabled {
  cursor: not-allowed;
  opacity: 0.48;
}
.workflow-section {
  min-width: 0;
}

.section-label {
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--text-tertiary);
  text-transform: uppercase;
  letter-spacing: 0.5px;
  margin-bottom: var(--sp-2);
}

.review-section {
  display: flex;
  flex-direction: column;
  gap: var(--sp-2);
}
.review-comment {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: var(--sp-2);
  padding: 12px 14px;
  border-radius: 14px;
  font-size: var(--text-xs);
  line-height: 1.5;
  border-left: 3px solid transparent;
}
.review-comment.info { background: var(--sev-info-bg); border-left-color: var(--sev-info); }
.review-comment.warning { background: var(--sev-warning-bg); border-left-color: var(--sev-warning); }
.review-comment.critical { background: var(--sev-critical-bg); border-left-color: var(--sev-critical); }
.comment-expert {
  font-weight: 600;
  margin-right: var(--sp-2);
}
.comment-text { color: var(--text-secondary); }
.comment-main {
  min-width: 0;
  flex: 1;
}
.resolve-comment-btn {
  flex: 0 0 auto;
  border: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  background: color-mix(in srgb, var(--bg-panel) 88%, transparent);
  color: var(--text-secondary);
  border-radius: 8px;
  padding: 3px 8px;
  font-size: var(--text-xs);
  cursor: pointer;
}
.resolve-comment-btn:hover {
  color: var(--text);
  border-color: var(--border);
}
.comment-resolved {
  flex: 0 0 auto;
  color: var(--text-tertiary);
}

.stream-section {
  display: flex;
  flex-direction: column;
}
.stream-output {
  background: color-mix(in srgb, var(--code-block-bg) 88%, transparent);
  border: 1px solid color-mix(in srgb, var(--border) 86%, transparent);
  border-radius: 14px;
  padding: 14px;
  max-height: 280px;
  overflow-y: auto;
  box-shadow: inset 0 1px 0 color-mix(in srgb, var(--bg-panel) 72%, transparent);
}
.stream-output pre {
  font-size: var(--text-xs);
  line-height: 1.65;
  white-space: pre-wrap;
  color: var(--text);
  font-family: var(--font-mono);
}

.draft-section {
  display: flex;
  flex-direction: column;
}
.draft-preview {
  background: var(--paper-bg);
  border: 1px solid var(--paper-border);
  border-radius: 16px;
  padding: 18px;
  max-height: 320px;
  overflow-y: auto;
  box-shadow: var(--paper-shadow);
}
.draft-preview pre {
  font-family: var(--font-serif);
  font-size: var(--text-base);
  line-height: 2;
  white-space: pre-wrap;
  color: var(--text);
}
</style>
