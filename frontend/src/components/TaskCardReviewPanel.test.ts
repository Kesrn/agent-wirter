import { describe, expect, it } from 'vitest'

import taskCardReviewSource from './TaskCardReviewPanel.vue?raw'

const SOURCE = taskCardReviewSource


describe('TaskCardReviewPanel', () => {

  it('has clarification prop', () => {
    expect(SOURCE).toContain('clarification')
  })

  it('has clarificationAnswered emit', () => {
    expect(SOURCE).toContain('clarificationAnswered')
  })

  it('has clarificationSkipped emit', () => {
    expect(SOURCE).toContain('clarificationSkipped')
  })

  it('has clarification data ref', () => {
    expect(SOURCE).toContain('clarificationAnswers')
    expect(SOURCE).toContain('clarificationHidden')
  })

  it('has isAnswered helper', () => {
    expect(SOURCE).toContain('function isAnswered')
  })

  it('renders clarification section in template', () => {
    expect(SOURCE).toContain('clarification-section')
    expect(SOURCE).toContain('提交回答并刷新任务卡')
  })

  it('renders single_choice options', () => {
    expect(SOURCE).toContain("q.type === 'single_choice'")
    expect(SOURCE).toContain('type="radio"')
  })

  it('renders multi_choice options', () => {
    expect(SOURCE).toContain("q.type === 'multi_choice'")
    expect(SOURCE).toContain('type="checkbox"')
  })

  it('renders free_text input', () => {
    expect(SOURCE).toContain("q.type === 'free_text'")
  })

  it('renders number input', () => {
    expect(SOURCE).toContain("q.type === 'number'")
    expect(SOURCE).toContain('v-model.number')
  })

  it('has skip button', () => {
    expect(SOURCE).toContain('跳过澄清')
  })

  it('has canSubmitClarification computed', () => {
    expect(SOURCE).toContain('canSubmitClarification')
  })

  it('has clarificationAnswers as wide type', () => {
    expect(SOURCE).toContain("Record<string, string | string[] | number>")
  })

  it('submitClarification converts arrays to strings', () => {
    expect(SOURCE).toContain('stringAnswers')
    expect(SOURCE).toContain("Array.isArray(val)")
  })
})
