import { describe, expect, it } from 'vitest'

import caSource from './ConversationAssistant.vue?raw'

describe('ConversationAssistant：任务卡展示 AI 真实理解（P0-3）', () => {
  it('确认前展示 AI 解析后的执行指令（resolved_instruction）', () => {
    expect(caSource).toContain('store.taskCard.resolved_instruction')
    expect(caSource).toContain('AI 解析的执行指令')
    expect(caSource).toContain('ca-tc-resolved')
  })

  it('同时展示用户原始要求 / 重点人物 / 具体要求', () => {
    expect(caSource).toContain('你的原始要求')
    expect(caSource).toContain('constraints?.user_instruction')
    expect(caSource).toContain('重点人物')
    expect(caSource).toContain('mentioned_entities')
    expect(caSource).toContain('具体要求')
    expect(caSource).toContain('requirements')
  })

  it('AI 理解区块位于确认按钮之前（先看理解，再确认）', () => {
    const resolvedIdx = caSource.indexOf('AI 解析的执行指令')
    const confirmIdx = caSource.indexOf('确认并生成候选稿')
    expect(resolvedIdx).toBeGreaterThanOrEqual(0)
    expect(confirmIdx).toBeGreaterThan(resolvedIdx)
  })
})
