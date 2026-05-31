<script setup lang="ts">
import { ref, computed } from 'vue'
import { useChapterStore, useDocumentStore, useExpertStore, useUiStore, useOutlineStore, useCharacterStore, useWorldEntryStore, useHiddenThreadStore, useGenerationHistoryStore, friendlyError } from '../stores'
import type { WorkflowStep, SSEEnvelope, GenerateMode, ProjectMode, ArticleGenerateParams, WritingUnit } from '../api/types'
import type { AgentStartPayload, AgentOutputPayload, AgentDonePayload, ProgressPayload, ErrorPayload, WriterOutputPayload, CriticOutputPayload, ConsistencyCheckPayload, EnhanceDirectionsPayload, TurnSuggestionsPayload, RevisionSuggestionsPayload, SkillPackPayload, ArticleReviewPayload, GenerationRecordPayload } from '../api/types'
import { api } from '../api/client'
import ApprovalModal from './ApprovalModal.vue'
import AgentWorkflow from './AgentWorkflow.vue'
import ContextPicker from './ContextPicker.vue'
import EnhancePicker from './EnhancePicker.vue'
import TurnPicker from './TurnPicker.vue'
import RevisionSuggestionPicker from './RevisionSuggestionPicker.vue'
import ArticleParamsPicker from './ArticleParamsPicker.vue'

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
const candidateDraftPreview = computed(() => cleanGeneratedContent(projectState.value.finalDraft))

function unitPosition(unit: WritingUnit): number {
  return 'position' in unit ? unit.position : unit.chapter_num
}

const showApproval = ref(false)
const approvalContent = ref('')
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
const showArticleParams = ref(false)
const latestGenerationRecordId = ref<string | null>(null)

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

function handleContextConfirm(outlineIds: string[], characterIds: string[], worldEntryIds: string[], hiddenThreadIds: string[], targetWords: number) {
  showContextPicker.value = false
  expertStore.startGenerating(pid.value)
  expertStore.setWorkflowSteps(pid.value, defaultWorkflow.map(s => ({ ...s, status: 'pending' as const })))
  runGenerateStream(outlineIds, characterIds, worldEntryIds, hiddenThreadIds, targetWords)
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
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
      currentAbort.signal,
      props.mode,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, isNovel.value ? '获取润色方向失败' : '获取改写方向失败')
    expertStore.appendOutput(pid.value, `\n[错误] ${msg}`)
    expertStore.stopGenerating(pid.value)
    ui.showToast(msg, 'error')
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
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
      currentAbort.signal,
      props.mode,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, isNovel.value ? '获取转折建议失败' : '获取内容方向失败')
    expertStore.appendOutput(pid.value, `\n[错误] ${msg}`)
    expertStore.stopGenerating(pid.value)
    ui.showToast(msg, 'error')
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
  try {
    await api.resumeGeneration(
      pid.value,
      threadId,
      'review',
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
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
  try {
    await api.resumeGeneration(
      pid.value,
      hitlThreadId.value!,
      'revise',
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
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
) {
  const unit = currentWritingUnit.value
  if (!unit) {
    expertStore.stopGenerating(pid.value)
    ui.showToast(`请先选择一个${isNovel.value ? '章节' : '稿件'}`, 'error')
    return
  }

  currentAbort = new AbortController()
  latestGenerationRecordId.value = null

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
        target_words: targetWords,
        enhance_direction: enhanceDirection,
        turn_direction: turnDirection,
        user_note: userNote,
        ...(articleParams.value ?? {}),
      },
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
      currentAbort.signal,
      props.mode,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, '生成失败')
    expertStore.appendOutput(pid.value, `\n\n[错误] ${msg}`)
    expertStore.stopGenerating(pid.value)
    ui.showToast(msg, 'error')
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
      } else if (payload.content) {
        expertStore.appendDraft(pid.value, payload.content)
      }
      expertStore.updateStepStatus(pid.value, 'writer', 'running')
      break
    }
    case 'content_output': {
      const payload = data as unknown as WriterOutputPayload
      if (payload.token) {
        expertStore.appendDraft(pid.value, payload.token)
      } else if (payload.content) {
        expertStore.appendDraft(pid.value, payload.content)
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
    case 'critic_output': {
      const payload = data as unknown as CriticOutputPayload
      const text = payload.critiques?.join('\n') ?? ''
      if (text) expertStore.appendOutput(pid.value, `\n[审校意见]\n${text}\n`)
      expertStore.updateStepStatus(pid.value, 'critic', 'success')
      break
    }
    case 'consistency_check': {
      const payload = data as unknown as ConsistencyCheckPayload
      if (payload.report) expertStore.appendOutput(pid.value, `\n[一致性检查]\n${payload.report}\n`)
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
      expertStore.setExpertSkill(pid.value, payload.expert, payload.skill)
      break
    }
    case 'generation_record': {
      const payload = data as unknown as GenerationRecordPayload
      latestGenerationRecordId.value = payload.id
      generationHistoryStore.upsertCandidate(pid.value, payload.id)
      refreshGenerationHistory()
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
  expertStore.stopGenerating(pid.value, true)
}

defineExpose({ testExpert, cancelStream })
</script>

<template>
  <div class="agent-panel">
    <div class="panel-header">
      <h3>Agent 协作</h3>
      <div class="panel-actions">
        <router-link :to="`/projects/${projectId}/experts`" class="link-configure">配置 Agent</router-link>
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
    </div>

    <div class="quick-actions">
      <button v-for="action in QUICK_ACTIONS" :key="action.key" @click="handleQuickAction(action.key)">
        {{ quickActionLabel(action) }}
      </button>
    </div>

    <!-- Workflow progress -->
    <AgentWorkflow v-if="pendingMode === 'full_pipeline'" :project-id="projectId" :mode="props.mode" />

    <!-- Review comments -->
    <div v-if="isNovel && currentWritingUnit" class="review-section">
      <div class="section-label">审核意见</div>
      <div
        v-for="comment in chapterStore.reviewCommentsForChapter(currentWritingUnit.id)"
        :key="comment.id"
        class="review-comment"
        :class="comment.severity"
      >
        <span class="comment-expert">{{ expertStore.getExpertName(comment.expert_id) }}</span>
        <span class="comment-text">{{ comment.comment }}</span>
      </div>
    </div>

    <!-- Stream output (agent logs, critiques, etc.) -->
    <div v-if="projectState.streamOutput" class="stream-section">
      <div class="section-label">输出</div>
      <div class="stream-output">
        <pre>{{ projectState.streamOutput }}</pre>
      </div>
    </div>

    <!-- Candidate draft preview (separate from stream logs) -->
    <div v-if="candidateDraftPreview && !showApproval" class="draft-section">
      <div class="section-label">{{ isNovel ? '候选稿' : '候选内容' }}</div>
      <div class="draft-preview">
        <pre>{{ candidateDraftPreview }}</pre>
      </div>
    </div>

    <ApprovalModal
      :show="showApproval"
      :content="approvalContent"
      :mode="pendingMode"
      :has-h-i-t-l="!!hitlThreadId"
      :project-mode="props.mode"
      @decision="handleDecision"
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
  gap: var(--sp-4);
}
.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: var(--sp-3);
}
.panel-header h3 {
  font-size: var(--text-base);
  font-weight: 760;
  margin: 0;
}
.panel-actions {
  display: flex;
  align-items: center;
  gap: var(--sp-2);
}
.link-configure {
  font-size: var(--text-xs);
  color: var(--text-tertiary);
  transition: color var(--transition);
}
.link-configure:hover {
  color: var(--accent);
  text-decoration: none;
}
.btn-generate {
  padding: var(--sp-2) var(--sp-4);
  background: var(--accent);
  color: var(--text-inverse);
  border: 1px solid var(--accent);
  border-radius: var(--radius);
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
  padding: var(--sp-2) var(--sp-4);
  background: var(--status-error);
  color: var(--text-inverse);
  border: 1px solid var(--status-error);
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 700;
  transition: opacity var(--transition), transform var(--transition);
  cursor: pointer;
}
.btn-cancel-stream:hover {
  opacity: 0.9;
  transform: translateY(-1px);
}

.quick-actions {
  display: flex;
  gap: var(--sp-2);
}
.quick-actions button {
  flex: 1;
  padding: 9px var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  font-size: var(--text-xs);
  font-weight: 650;
  color: var(--text-secondary);
  box-shadow: var(--shadow-sm);
  transition: background var(--transition), border-color var(--transition), color var(--transition);
}
.quick-actions button:hover {
  background: var(--accent-subtle);
  border-color: var(--border-focus);
  color: var(--accent);
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
  padding: var(--sp-2) var(--sp-3);
  border-radius: var(--radius);
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

.stream-section {
  display: flex;
  flex-direction: column;
}
.stream-output {
  background: var(--code-block-bg);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-3);
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
  border-radius: var(--radius);
  padding: var(--sp-4);
  max-height: 320px;
  overflow-y: auto;
  box-shadow: var(--shadow-sm);
}
.draft-preview pre {
  font-family: var(--font-serif);
  font-size: var(--text-base);
  line-height: 2;
  white-space: pre-wrap;
  color: var(--text);
}
</style>
