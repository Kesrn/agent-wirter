import { describe, expect, it } from 'vitest'
import panelSource from './ClarificationPanel.vue?raw'
import typesSource from '../api/types.ts?raw'
import clientSource from '../api/client.ts?raw'
import agentPanelSource from './AgentPanel.vue?raw'

// 由于现有测试环境无 DOM（无 @vue/test-utils / happy-dom），这里沿用源码断言
// 的方式（与 writingEditorLayout.test.ts / ContextPicker.test.ts 一致）。
const scriptBlock = panelSource.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)?.[1] ?? ''
// 注意：template 内含嵌套 <template v-if> 标签，非贪婪匹配会在第一个 </template> 截断，
// 因此 template 相关断言直接对完整 panelSource 进行。

describe('ClarificationPanel source contract', () => {
  it('declares the required props (runId/questions/round/maxRounds/assumptionsIfSkipped)', () => {
    expect(scriptBlock).toContain('defineProps<{')
    expect(scriptBlock).toContain('runId: string')
    expect(scriptBlock).toContain('questions: ClarificationQuestion[]')
    expect(scriptBlock).toContain('round: number')
    expect(scriptBlock).toContain('maxRounds: number')
    expect(scriptBlock).toContain('assumptionsIfSkipped: string[]')
  })

  it('emits submit and skip events', () => {
    expect(scriptBlock).toContain("defineEmits<{")
    expect(scriptBlock).toContain("submit: [answers: Record<string, string>]")
    expect(scriptBlock).toContain("skip: []")
  })

  it('renders single_choice as radio buttons with option label + description', () => {
    // single_choice 分支使用 type="radio"
    expect(panelSource).toContain("q.type === 'single_choice'")
    expect(panelSource).toContain('type="radio"')
    expect(panelSource).toContain(':name="`clar-${q.id}`"')
    expect(panelSource).toContain('opt.label')
    expect(panelSource).toContain('opt.description')
  })

  it('renders free_text as a textarea', () => {
    expect(panelSource).toContain("q.type === 'free_text'")
    expect(panelSource).toContain('class="q-textarea"')
    expect(panelSource).toContain('<textarea')
  })

  it('provides multi_choice (checkbox) and number input fallback controls', () => {
    expect(panelSource).toContain("q.type === 'multi_choice'")
    expect(panelSource).toContain('type="checkbox"')
    expect(panelSource).toContain("q.type === 'number'")
    expect(panelSource).toContain('type="number"')
  })

  it('shows each question reason text', () => {
    expect(panelSource).toContain('q.reason')
  })

  it('guards submit on required questions and supports submit + skip', () => {
    // canSubmit 校验 required 必填
    expect(scriptBlock).toContain('q.required')
    expect(scriptBlock).toContain('canSubmit')
    // 主提交按钮
    expect(panelSource).toContain('提交回答')
    // 跳过按钮 + 假设展示
    expect(panelSource).toContain('跳过，使用默认假设')
    expect(panelSource).toContain('assumptionsIfSkipped')
  })

  it('enters a loading/disabled state while submitting', () => {
    expect(scriptBlock).toContain('submitting')
    expect(panelSource).toContain('提交中...')
    expect(panelSource).toContain(':disabled="!canSubmit"')
  })

  it('distinguishes "生成前澄清" from final review in copy', () => {
    expect(panelSource).toContain('生成前澄清')
    expect(panelSource).toContain('最终审核')
  })

  it('shows round indicator 第 {round}/{maxRounds} 轮', () => {
    expect(panelSource).toContain('第 {{ round }}/{{ maxRounds }} 轮')
  })
})

describe('clarification types in types.ts', () => {
  it('defines ClarificationQuestion / ClarificationOption', () => {
    expect(typesSource).toContain('export interface ClarificationQuestion')
    expect(typesSource).toContain('export interface ClarificationOption')
    expect(typesSource).toContain("'single_choice' | 'multi_choice' | 'free_text' | 'number'")
  })

  it('defines ClarificationState response', () => {
    expect(typesSource).toContain('export interface ClarificationState')
    expect(typesSource).toContain('run_id: string')
    expect(typesSource).toContain('interrupt_id: string | null')
    expect(typesSource).toContain('round: number')
    expect(typesSource).toContain('max_rounds: number')
    expect(typesSource).toContain('assumptions_if_skipped: string[]')
    expect(typesSource).toContain('existing_answers: Record<string, string>')
    expect(typesSource).toContain('clarification_summary: string')
    expect(typesSource).toContain('resolved: boolean')
  })

  it('defines ClarificationAnswerRequest with submit/skip action', () => {
    expect(typesSource).toContain('export interface ClarificationAnswerRequest')
    expect(typesSource).toContain("action: 'submit' | 'skip'")
  })

  it('adds clarification_required to SSEEventType union', () => {
    expect(typesSource).toContain("'clarification_required'")
  })
})

describe('clarification API in client.ts', () => {
  it('exposes getClarification against /ai-runs/{runId}/clarification', () => {
    expect(clientSource).toContain('getClarification')
    expect(clientSource).toContain('/ai-runs/${runId}/clarification')
    expect(clientSource).toContain('ClarificationState')
  })

  it('exposes submitClarificationAnswers as POST to /ai-runs/{runId}/clarification-answers', () => {
    expect(clientSource).toContain('submitClarificationAnswers')
    expect(clientSource).toContain('/ai-runs/${runId}/clarification-answers')
    expect(clientSource).toContain('ClarificationAnswerRequest')
    expect(clientSource).toContain("method: 'POST'")
  })
})

describe('AgentPanel clarification_required wiring', () => {
  it('imports ClarificationPanel and the ClarificationRequiredPayload type', () => {
    expect(agentPanelSource).toContain("import ClarificationPanel from './ClarificationPanel.vue'")
    expect(agentPanelSource).toContain('ClarificationRequiredPayload')
  })

  it('handles the clarification_required SSE event in handleSSEEvent', () => {
    expect(agentPanelSource).toContain("case 'clarification_required'")
    expect(agentPanelSource).toContain('clarificationState.value = payload')
  })

  it('renders ClarificationPanel conditionally with submit/skip handlers', () => {
    expect(agentPanelSource).toContain('v-if="clarificationState"')
    expect(agentPanelSource).toContain(':run-id="clarificationState.run_id"')
    expect(agentPanelSource).toContain(':questions="clarificationState.questions"')
    expect(agentPanelSource).toContain(':round="clarificationState.round"')
    expect(agentPanelSource).toContain(':max-rounds="clarificationState.max_rounds"')
    expect(agentPanelSource).toContain(':assumptions-if-skipped="clarificationState.assumptions_if_skipped"')
    expect(agentPanelSource).toContain('@submit="handleClarificationSubmit"')
    expect(agentPanelSource).toContain('@skip="handleClarificationSkip"')
  })

  it('calls submitClarificationAnswers for both submit and skip actions', () => {
    expect(agentPanelSource).toContain("api.submitClarificationAnswers(runId, { action: 'submit', answers })")
    expect(agentPanelSource).toContain("api.submitClarificationAnswers(runId, { action: 'skip', answers: {} })")
  })
})
