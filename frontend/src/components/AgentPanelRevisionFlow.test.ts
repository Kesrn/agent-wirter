import { describe, expect, it } from 'vitest'

import source from './AgentPanel.vue?raw'

describe('AgentPanel revision suggestions flow', () => {
  it('requires a revision_suggestions event before considering review complete', () => {
    const block = source.slice(
      source.indexOf('async function requestRevisionDirections'),
      source.indexOf('async function handleRevisionConfirm'),
    )

    expect(block).toContain('let receivedSuggestions = false')
    expect(block).toContain("envelope.event === 'revision_suggestions'")
    expect(block).toContain("throw new Error('服务未返回修改方向，请重试')")
  })

  it('normalizes malformed directions and opens custom input for an empty list', () => {
    const block = source.slice(
      source.indexOf("case 'revision_suggestions'"),
      source.indexOf("case 'skill_pack'"),
    )

    expect(block).toContain('Array.isArray(payload.directions)')
    expect(block).toContain('showRevisionPicker.value = true')
    expect(block).toContain('AI 未返回可用建议，请直接填写自定义修改方向')
  })
})
