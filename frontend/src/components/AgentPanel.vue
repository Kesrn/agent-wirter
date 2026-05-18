<script setup lang="ts">
import { ref } from 'vue'
import { useChapterStore, useExpertStore, useUiStore, useOutlineStore, useCharacterStore, useWorldEntryStore, friendlyError } from '../stores'
import type { WorkflowStep, SSEEnvelope, GenerateMode } from '../api/types'
import type { AgentStartPayload, AgentOutputPayload, AgentDonePayload, ProgressPayload, DonePayload, ErrorPayload, WriterOutputPayload, CriticOutputPayload, ConsistencyCheckPayload, EnhanceDirectionsPayload, TurnSuggestionsPayload } from '../api/types'
import { api } from '../api/client'
import ApprovalModal from './ApprovalModal.vue'
import AgentWorkflow from './AgentWorkflow.vue'
import ContextPicker from './ContextPicker.vue'
import EnhancePicker from './EnhancePicker.vue'
import TurnPicker from './TurnPicker.vue'

const props = defineProps<{ projectId: string }>()
const chapterStore = useChapterStore()
const expertStore = useExpertStore()
const outlineStore = useOutlineStore()
const characterStore = useCharacterStore()
const worldEntryStore = useWorldEntryStore()
const ui = useUiStore()

const showApproval = ref(false)
const approvalContent = ref('')
const showContextPicker = ref(false)
const showEnhancePicker = ref(false)
const showTurnPicker = ref(false)
const enhanceDirections = ref<string[]>([])
const turnSuggestions = ref<string[]>([])
const pendingMode = ref<GenerateMode>('full_pipeline')

/** Abort controller for cancelling in-flight SSE requests */
let currentAbort: AbortController | null = null

const defaultWorkflow: WorkflowStep[] = [
  { id: 'context', name: 'ContextLoader', status: 'pending', expert_id: 'system' },
  { id: 'writer', name: 'Writer', status: 'pending', expert_id: 'renderer' },
  { id: 'critic', name: 'Critic', status: 'pending', expert_id: 'cruel' },
  { id: 'consistency', name: 'ConsistencyChecker', status: 'pending', expert_id: 'editor' },
  { id: 'review', name: 'HumanReview', status: 'pending', expert_id: 'user' },
]

function agentToStepId(agent: string): string | null {
  const map: Record<string, string> = {
    writer: 'writer',
    critic: 'critic',
    consistency_checker: 'consistency',
    context_loader: 'context',
    human_review: 'review',
  }
  return map[agent] ?? null
}

// ─── Generate (full pipeline) ───

function handleGenerate() {
  pendingMode.value = 'full_pipeline'
  showContextPicker.value = true
}

function handleContextConfirm(outlineIds: string[], characterIds: string[], worldEntryIds: string[], targetWords: number) {
  showContextPicker.value = false
  expertStore.startGenerating()
  expertStore.setWorkflowSteps(defaultWorkflow.map(s => ({ ...s, status: 'pending' as const })))
  runGenerateStream(outlineIds, characterIds, worldEntryIds, targetWords)
}

function handleContextCancel() {
  showContextPicker.value = false
}

// ─── Quick actions ───

function handleQuickAction(action: string) {
  if (action === '润色') {
    pendingMode.value = 'enhance'
    expertStore.startGenerating()
    runEnhanceDirections()
  } else if (action === '转折建议') {
    pendingMode.value = 'continue'
    expertStore.startGenerating()
    runTurnSuggestions()
  } else if (action === '读者视角') {
    pendingMode.value = 'summarize'
    expertStore.startGenerating()
    runGenerateStream()
  }
}

// ─── Enhance flow ───

async function runEnhanceDirections() {
  const chapter = chapterStore.currentChapterForProject(props.projectId)
  if (!chapter) {
    expertStore.stopGenerating()
    ui.showToast('请先选择一个章节', 'error')
    return
  }
  currentAbort = new AbortController()
  try {
    await api.generateStream(
      props.projectId,
      { chapter_num: chapter.chapter_num, mode: 'enhance' },
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
      currentAbort.signal,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, '获取润色方向失败')
    expertStore.appendOutput(`\n[错误] ${msg}`)
    expertStore.stopGenerating()
    ui.showToast(msg, 'error')
  }
}

function handleEnhanceConfirm(direction: string, userNote: string) {
  showEnhancePicker.value = false
  expertStore.startGenerating()
  runGenerateStream(undefined, undefined, undefined, undefined, direction, undefined, userNote)
}

// ─── Turn flow ───

async function runTurnSuggestions() {
  const chapter = chapterStore.currentChapterForProject(props.projectId)
  if (!chapter) {
    expertStore.stopGenerating()
    ui.showToast('请先选择一个章节', 'error')
    return
  }
  currentAbort = new AbortController()
  try {
    await api.generateStream(
      props.projectId,
      { chapter_num: chapter.chapter_num, mode: 'continue' },
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
      currentAbort.signal,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, '获取转折建议失败')
    expertStore.appendOutput(`\n[错误] ${msg}`)
    expertStore.stopGenerating()
    ui.showToast(msg, 'error')
  }
}

function handleTurnConfirm(direction: string, userNote: string) {
  showTurnPicker.value = false
  expertStore.startGenerating()
  runGenerateStream(undefined, undefined, undefined, undefined, undefined, direction, userNote)
}

// ─── Core generate stream ───

async function runGenerateStream(
  selectedOutlineIds?: string[],
  selectedCharacterIds?: string[],
  selectedWorldEntryIds?: string[],
  targetWords?: number,
  enhanceDirection?: string,
  turnDirection?: string,
  userNote?: string,
) {
  const chapter = chapterStore.currentChapterForProject(props.projectId)
  if (!chapter) {
    expertStore.stopGenerating()
    ui.showToast('请先选择一个章节', 'error')
    return
  }

  currentAbort = new AbortController()

  try {
    await api.generateStream(
      props.projectId,
      {
        chapter_num: chapter.chapter_num,
        mode: pendingMode.value,
        selected_outline_ids: selectedOutlineIds,
        selected_character_ids: selectedCharacterIds,
        selected_world_entry_ids: selectedWorldEntryIds,
        target_words: targetWords,
        enhance_direction: enhanceDirection,
        turn_direction: turnDirection,
        user_note: userNote,
      },
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
      currentAbort.signal,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, '生成失败')
    expertStore.appendOutput(`\n\n[错误] ${msg}`)
    expertStore.stopGenerating()
    ui.showToast(msg, 'error')
  }
}

// ─── SSE event handling ───

function handleSSEEvent(envelope: SSEEnvelope) {
  const { event, data } = envelope

  switch (event) {
    case 'progress': {
      const payload = data as unknown as ProgressPayload
      expertStore.appendOutput(`[系统] ${payload.message}\n`)
      break
    }
    case 'agent_start': {
      const payload = data as unknown as AgentStartPayload
      const stepId = agentToStepId(payload.agent)
      if (stepId) expertStore.updateStepStatus(stepId, 'running')
      expertStore.appendOutput(`\n[${payload.agent}] 开始...\n`)
      break
    }
    case 'agent_output': {
      const payload = data as unknown as AgentOutputPayload
      expertStore.appendOutput(payload.token)
      break
    }
    case 'agent_done': {
      const payload = data as unknown as AgentDonePayload
      const stepId = agentToStepId(payload.agent)
      if (stepId) expertStore.updateStepStatus(stepId, 'success')
      break
    }
    case 'writer_output': {
      const payload = data as unknown as WriterOutputPayload
      if (payload.token) {
        // For continue/enhance modes: append directly to chapter editor
        if (pendingMode.value === 'continue') {
          const chapter = chapterStore.currentChapterForProject(props.projectId)
          if (chapter) {
            chapterStore.updateDraft(props.projectId, chapter.draft + payload.token)
          }
        } else if (pendingMode.value === 'enhance') {
          // Enhance replaces content — accumulate in finalDraft first
          expertStore.appendDraft(payload.token)
        } else {
          // full_pipeline: accumulate in finalDraft
          expertStore.appendDraft(payload.token)
        }
      } else if (payload.content) {
        if (pendingMode.value === 'continue') {
          const chapter = chapterStore.currentChapterForProject(props.projectId)
          if (chapter) {
            chapterStore.updateDraft(props.projectId, chapter.draft + payload.content)
          }
        } else {
          expertStore.appendDraft(payload.content)
        }
      }
      expertStore.updateStepStatus('writer', 'running')
      break
    }
    case 'critic_output': {
      const payload = data as unknown as CriticOutputPayload
      const text = payload.critiques?.join('\n') ?? ''
      if (text) expertStore.appendOutput(`\n[审校意见]\n${text}\n`)
      expertStore.updateStepStatus('critic', 'success')
      break
    }
    case 'consistency_check': {
      const payload = data as unknown as ConsistencyCheckPayload
      if (payload.report) expertStore.appendOutput(`\n[一致性检查]\n${payload.report}\n`)
      expertStore.updateStepStatus('consistency', 'success')
      break
    }
    case 'enhance_directions': {
      const payload = data as unknown as EnhanceDirectionsPayload
      enhanceDirections.value = payload.directions
      showEnhancePicker.value = true
      expertStore.stopGenerating()
      break
    }
    case 'turn_suggestions': {
      const payload = data as unknown as TurnSuggestionsPayload
      turnSuggestions.value = payload.suggestions
      showTurnPicker.value = true
      expertStore.stopGenerating()
      break
    }
    case 'done': {
      for (const step of expertStore.workflowSteps) {
        if (step.status === 'pending' || step.status === 'running') {
          expertStore.updateStepStatus(step.id, 'success')
        }
      }
      expertStore.stopGenerating()

      if (pendingMode.value === 'full_pipeline') {
        // Only show approval modal for full pipeline — other modes auto-apply
        approvalContent.value = expertStore.finalDraft
        showApproval.value = true
      } else if (pendingMode.value === 'enhance') {
        // Enhance: replace chapter content with finalDraft
        const chapter = chapterStore.currentChapterForProject(props.projectId)
        if (chapter && expertStore.finalDraft) {
          chapterStore.updateDraft(props.projectId, expertStore.finalDraft)
          chapterStore.saveCurrentChapter(props.projectId).catch(() => {})
        }
      }
      // continue mode: tokens already appended to editor in real-time, no action needed
      // summarize mode: feedback only, no content change
      break
    }
    case 'error': {
      const payload = data as unknown as ErrorPayload
      expertStore.appendOutput(`\n[错误] ${payload.message}`)
      expertStore.stopGenerating()
      ui.showToast(payload.message, 'error')
      break
    }
  }
}

// ─── Approval ───

function handleDecision(decision: 'accept' | 'accept_with_mods' | 'reject') {
  showApproval.value = false
  expertStore.stopGenerating()
  if (decision === 'accept') {
    const chapter = chapterStore.currentChapterForProject(props.projectId)
    if (chapter) {
      chapterStore.updateDraft(props.projectId, expertStore.finalDraft)
      chapterStore.saveCurrentChapter(props.projectId).catch(() => {})
    }
  } else if (decision === 'accept_with_mods') {
    const chapter = chapterStore.currentChapterForProject(props.projectId)
    if (chapter) {
      chapterStore.updateDraft(props.projectId, expertStore.finalDraft)
    }
  }
}

// ─── Test expert ───

async function testExpert(expertId: string, sampleText: string) {
  expertStore.startGenerating()
  currentAbort = new AbortController()
  try {
    await api.testExpertStream(
      props.projectId,
      expertId,
      { test_text: sampleText },
      (envelope: SSEEnvelope) => handleSSEEvent(envelope),
      currentAbort.signal,
    )
  } catch (e: unknown) {
    if (currentAbort?.signal.aborted) return
    const msg = friendlyError(e, '测试失败')
    expertStore.appendOutput(`\n[错误] ${msg}`)
    expertStore.stopGenerating()
    ui.showToast(msg, 'error')
  }
}

function cancelStream() {
  if (currentAbort) {
    currentAbort.abort()
    currentAbort = null
  }
  expertStore.stopGenerating()
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
          v-if="expertStore.isGenerating"
          class="btn-cancel-stream"
          @click="cancelStream"
        >
          取消
        </button>
        <button
          v-else
          class="btn-generate"
          :disabled="!chapterStore.currentChapterForProject(projectId)"
          @click="handleGenerate"
        >
          章节生成
        </button>
      </div>
    </div>

    <div class="quick-actions">
      <button @click="handleQuickAction('转折建议')">转折建议</button>
      <button @click="handleQuickAction('润色')">润色</button>
      <button @click="handleQuickAction('读者视角')">读者视角</button>
    </div>

    <!-- Workflow progress -->
    <AgentWorkflow v-if="pendingMode === 'full_pipeline'" />

    <!-- Review comments -->
    <div v-if="chapterStore.currentChapterForProject(projectId)" class="review-section">
      <div class="section-label">审核意见</div>
      <div
        v-for="comment in chapterStore.reviewCommentsForChapter(chapterStore.currentChapterForProject(projectId)!.id)"
        :key="comment.id"
        class="review-comment"
        :class="comment.severity"
      >
        <span class="comment-expert">{{ expertStore.getExpertName(comment.expert_id) }}</span>
        <span class="comment-text">{{ comment.comment }}</span>
      </div>
    </div>

    <!-- Stream output -->
    <div v-if="expertStore.streamOutput" class="stream-section">
      <div class="section-label">输出</div>
      <div class="stream-output">
        <pre>{{ expertStore.streamOutput }}</pre>
      </div>
    </div>

    <ApprovalModal
      v-if="showApproval"
      :content="approvalContent"
      @decision="handleDecision"
    />

    <ContextPicker
      v-if="showContextPicker"
      :project-id="projectId"
      :outlines="outlineStore.entriesForProject(projectId)"
      :characters="characterStore.charactersForProject(projectId)"
      :world-entries="worldEntryStore.entriesForProject(projectId)"
      @confirm="handleContextConfirm"
      @cancel="handleContextCancel"
    />

    <EnhancePicker
      v-if="showEnhancePicker"
      :directions="enhanceDirections"
      @confirm="handleEnhanceConfirm"
      @cancel="showEnhancePicker = false"
    />

    <TurnPicker
      v-if="showTurnPicker"
      :suggestions="turnSuggestions"
      @confirm="handleTurnConfirm"
      @cancel="showTurnPicker = false"
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
}
.panel-header h3 {
  font-size: var(--text-base);
  font-weight: 600;
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
  border: none;
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 500;
  transition: background var(--transition);
}
.btn-generate:disabled { background: var(--status-pending); cursor: not-allowed; }
.btn-generate:hover:not(:disabled) { background: var(--accent-hover); }
.btn-cancel-stream {
  padding: var(--sp-2) var(--sp-4);
  background: var(--status-error);
  color: var(--text-inverse);
  border: none;
  border-radius: var(--radius);
  font-size: var(--text-sm);
  font-weight: 500;
  transition: background var(--transition);
  cursor: pointer;
}
.btn-cancel-stream:hover { opacity: 0.9; }

.quick-actions {
  display: flex;
  gap: var(--sp-2);
}
.quick-actions button {
  flex: 1;
  padding: var(--sp-2);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-panel);
  font-size: var(--text-xs);
  color: var(--text-secondary);
  transition: background var(--transition), border-color var(--transition);
}
.quick-actions button:hover {
  background: var(--bg-hover);
  border-color: var(--border-focus);
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
  background: var(--bg-panel);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: var(--sp-3);
  max-height: 280px;
  overflow-y: auto;
}
.stream-output pre {
  font-size: var(--text-xs);
  line-height: 1.6;
  white-space: pre-wrap;
  color: var(--text);
}
</style>