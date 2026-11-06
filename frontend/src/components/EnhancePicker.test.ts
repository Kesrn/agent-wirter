import { describe, expect, it } from 'vitest'
import source from './EnhancePicker.vue?raw'
import agentPanelSource from './AgentPanel.vue?raw'

describe('润色方向选择', () => {
  it('允许用户直接指定润色方向', () => {
    expect(source).toContain("自定义{{ isArticle ? '改写' : '润色' }}方向")
    expect(source).toContain('v-model="customDirection"')
    expect(source).toContain("selectedMode.value === 'custom'")
  })

  it('去掉补充说明，并把自定义内容作为最终润色方向提交', () => {
    expect(source).not.toContain('补充说明')
    expect(source).toContain("emit('confirm', finalDirection.value, targetWords.value)")
    expect(agentPanelSource).toContain('function handleEnhanceConfirm(direction: string, targetWords: number)')
    expect(agentPanelSource).toContain('runGenerateStream(undefined, undefined, undefined, undefined, targetWords, direction)')
  })
})
