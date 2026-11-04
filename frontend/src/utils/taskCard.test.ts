import { describe, expect, it } from 'vitest'

import { normalizeTaskCardPayload } from './taskCard'

describe('normalizeTaskCardPayload', () => {
  it('normalizes model string variants before review and submission', () => {
    const result = normalizeTaskCardPayload({
      core_task: '推进本章冲突',
      scenes: ['进入现场', '发现证据'],
      information_rules: '不得提前揭示凶手身份',
      forbidden: '新增角色, 改写结局',
      word_budget: '约 2200 字',
    })

    expect(result.scenes).toEqual([
      { title: '场景1', scene_goal: '进入现场' },
      { title: '场景2', scene_goal: '发现证据' },
    ])
    expect(result.information_rules).toEqual({
      may_reveal: [],
      hint_only: [],
      forbidden: ['不得提前揭示凶手身份'],
    })
    expect(result.forbidden).toEqual(['新增角色', '改写结局'])
    expect(result.word_budget).toBe(2200)
  })

  it('returns editable safe defaults for invalid runtime values', () => {
    expect(normalizeTaskCardPayload('invalid')).toEqual({
      scenes: [],
      information_rules: { may_reveal: [], hint_only: [], forbidden: [] },
      character_goals: [],
      tension_design: [],
      forbidden: [],
    })
  })
})
