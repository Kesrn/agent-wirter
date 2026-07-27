import { describe, expect, it } from 'vitest'

import taskCardReviewSource from './TaskCardReviewPanel.vue?raw'
import clarificationPanelSource from './ClarificationPanel.vue?raw'
import agentPanelSource from './AgentPanel.vue?raw'

const SOURCE = taskCardReviewSource
const CLARIFICATION = clarificationPanelSource
const AGENT_PANEL = agentPanelSource

describe('TaskCardReviewPanel L-2 embedded clarification', () => {
  it('emits task-card decisions and clarification refresh', () => {
    expect(SOURCE).toContain('approved: [taskCard: TaskCardPayload, userNote?: string]')
    expect(SOURCE).toContain('rejected: []')
    expect(SOURCE).toContain('clarificationRefresh: [answers: Record<string, string>, userNote?: string]')
    expect(SOURCE).toContain('contextRefresh: [excludedKeys: string[]]')
  })

  it('renders embedded clarification controls', () => {
    expect(SOURCE).toContain('clarification-section')
    expect(SOURCE).toContain('回答后重新规划')
    expect(SOURCE).toContain('btn-refresh-card')
    expect(SOURCE).toContain('clarificationAnswers')
  })

  it('does not render clarification questions already marked complete', () => {
    expect(SOURCE).toContain('props.clarification?.needs_clarification')
    expect(SOURCE).toContain('hasPendingClarification.value')
  })

  it('explains when AI found no clarification is needed', () => {
    expect(SOURCE).toContain("props.clarificationStatus === 'not_needed'")
    expect(SOURCE).toContain('AI 已分析当前章节的上下文与任务要求，无需进一步澄清')
    expect(SOURCE).toContain('请确认任务卡后开始编写章节')
  })

  it('renders a structured context preview with source metadata', () => {
    expect(SOURCE).toContain('TaskCardContextSummary')
    expect(SOURCE).toContain('查看本次上下文')
    expect(SOURCE).toContain('previous_chapter_ending')
    expect(SOURCE).toContain('confirmed_memories')
    expect(SOURCE).toContain('knowledge_sources')
    expect(SOURCE).toContain('用户选择')
    expect(SOURCE).toContain('应用选择并重新规划')
    expect(SOURCE).toContain('excluded_context_keys')
  })

  it('keeps editable task card, user note, and approve guard', () => {
    expect(SOURCE).toContain('editableTaskCard')
    expect(SOURCE).toContain('userNote')
    expect(SOURCE).toContain('if (approving.value) return')
    expect(SOURCE).toContain('按此任务卡生成')
    expect(SOURCE).toContain("rawJsonEdit.value && !applyRawJson()")
    expect(SOURCE).toContain('任务卡 JSON 必须是对象')
  })

  it('overlay remains visible when parent panel is hidden', () => {
    expect(SOURCE).toContain('visibility: visible')
  })

  it('supports keyboard dismissal and accessible task-card controls', () => {
    expect(SOURCE).toContain("event.key === 'Escape'")
    expect(SOURCE).toContain('window.addEventListener')
    expect(SOURCE).toContain('aria-label="`移除场景 ${idx + 1}`"')
    expect(SOURCE).toContain('for="task-card-user-note"')
  })
})

describe('ClarificationPanel legacy standalone interview', () => {
  it('supports standalone question rendering', () => {
    expect(CLARIFICATION).toContain('ClarificationQuestion')
    expect(CLARIFICATION).toContain("q.type === 'single_choice'")
    expect(CLARIFICATION).toContain("q.type === 'free_text'")
    expect(CLARIFICATION).toContain("q.type === 'multi_choice'")
    expect(CLARIFICATION).toContain("q.type === 'number'")
  })

  it('disables skip on STRICT first round', () => {
    expect(CLARIFICATION).toContain('PreGenerationMode')
    expect(CLARIFICATION).toContain("props.preGenerationMode === 'STRICT' && props.round <= 1")
    expect(CLARIFICATION).toContain('严格模式首轮需回答')
    expect(CLARIFICATION).toContain(':disabled="skipDisabled"')
  })
})

describe('AgentPanel L-2 graph resume flow', () => {
  it('keeps the legacy standalone clarification event compatible', () => {
    const clarificationCase = AGENT_PANEL.slice(
      AGENT_PANEL.indexOf("case 'clarification_required'"),
      AGENT_PANEL.indexOf("case 'done'"),
    )
    expect(clarificationCase).toContain('clarificationState.value = payload')
    expect(clarificationCase).toContain('hitlThreadId.value = payload.thread_id')
    expect(clarificationCase).toContain('expertStore.stopGenerating(pid.value)')
  })

  it('task-card event carries embedded clarification payload', () => {
    const reviewCase = AGENT_PANEL.slice(
      AGENT_PANEL.indexOf("case 'task_card_review_required'"),
      AGENT_PANEL.indexOf("case 'critic_output'"),
    )
    expect(reviewCase).toContain('showTaskCardReview.value = true')
    expect(reviewCase).toContain('澄清已完成，章节任务卡已生成')
    expect(reviewCase).toContain('embeddedClarification.value = payload.clarification')
    expect(reviewCase).toContain('taskCardClarificationStatus.value = payload.clarification_status')
    expect(reviewCase).toContain('taskCardContextSummary.value = payload.context_summary')
  })

  it('keeps the task card open until the writer has actually started', () => {
    const section = AGENT_PANEL.slice(
      AGENT_PANEL.indexOf('async function handleTaskCardApproved'),
      AGENT_PANEL.indexOf('async function handleTaskCardRejected'),
    )
    expect(section).toContain('let writerStarted = false')
    expect(section).toContain("envelope.event === 'agent_start'")
    expect(section).toContain("agentToStepId(payload.agent) === 'writer'")
    expect(section).toContain("throw new Error(resumeError || '任务卡确认未能启动正文创作，请重试')")
    expect(section).toContain('taskCardReviewKey.value += 1')
    expect(AGENT_PANEL).toContain("chapter_writer: 'writer'")
  })

  it('does not discard un-applied clarification or context changes', () => {
    expect(SOURCE).toContain('已填写澄清回答，请先点击“回答后重新规划”')
    expect(SOURCE).toContain('上下文选择已修改，请先点击“应用选择并重新规划”')
  })

  it('submitting clarification resumes the LangGraph thread', () => {
    const section = AGENT_PANEL.slice(
      AGENT_PANEL.indexOf('async function handleClarificationSubmit'),
      AGENT_PANEL.indexOf('async function handleClarificationSkip'),
    )
    expect(section).toContain('if (hitlResuming) return')
    expect(section).toContain("'submit_clarification'")
    expect(section).toContain('createSSEWatchdog')
    expect(section).toContain('watchdog.clear()')
  })

  it('refreshes the task card in the same review surface', () => {
    const section = AGENT_PANEL.slice(
      AGENT_PANEL.indexOf('async function handleTaskCardClarificationRefresh'),
      AGENT_PANEL.indexOf('async function handleTaskCardRejected'),
    )
    expect(section).toContain("'refresh_task_card'")
    expect(section).toContain('clarification_answers: answers')
    expect(section).toContain("envelope.event === 'task_card_review_required'")
    expect(section).toContain('embeddedClarification.value = payload.clarification')
  })

  it('refreshes the task card context without touching clarification rounds', () => {
    const section = AGENT_PANEL.slice(
      AGENT_PANEL.indexOf('async function handleTaskCardContextRefresh'),
      AGENT_PANEL.indexOf('async function handleTaskCardRejected'),
    )
    expect(section).toContain("'refresh_task_card_context'")
    expect(section).toContain('excluded_context_keys: excludedKeys')
    expect(section).toContain('context_summary')
  })

  it('skipping clarification also resumes the LangGraph thread', () => {
    const section = AGENT_PANEL.slice(
      AGENT_PANEL.indexOf('async function handleClarificationSkip'),
      AGENT_PANEL.indexOf('// ─── L-1: Task Card Review ───'),
    )
    expect(section).toContain('if (hitlResuming) return')
    expect(section).toContain("'skip_clarification'")
    expect(section).toContain('createSSEWatchdog')
    expect(section).toContain('watchdog.clear()')
  })

  it('still uses the SSE watchdog and persisted pre-generation mode', () => {
    expect(AGENT_PANEL).toContain('function createSSEWatchdog')
    expect(AGENT_PANEL).toContain('preGenerationMode = ref<PreGenerationMode>')
    expect(AGENT_PANEL).toContain('maxClarificationRounds = ref(')
    expect(AGENT_PANEL).toContain('pre_generation_mode: preGenerationMode.value')
    expect(AGENT_PANEL).toContain('max_clarification_rounds: maxClarificationRounds.value')
  })
})
