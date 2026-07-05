import { describe, expect, it } from 'vitest'
import source from './RevisionSuggestionPicker.vue?raw'

// 现有前端测试环境没有 DOM runner，这里沿用源码契约断言。
const scriptBlock = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)?.[1] ?? ''

describe('RevisionSuggestionPicker custom direction contract', () => {
  it('supports a custom revision direction mode independent of AI suggestions', () => {
    expect(scriptBlock).toContain("selectedMode = ref<'suggestion' | 'custom'>('suggestion')")
    expect(scriptBlock).toContain("selectedMode.value === 'custom'")
    expect(scriptBlock).toContain('customDirection.value.trim()')
    expect(source).toContain('自定义修改方向')
    expect(source).toContain('也可以不选上面的建议，直接写你自己的修改方向')
  })

  it('switches to custom mode when the custom textarea is used', () => {
    expect(source).toContain('@focus="selectedMode = \'custom\'"')
    expect(source).toContain('@input="selectedMode = \'custom\'"')
    expect(source).toContain(':class="{ active: selectedMode === \'custom\' }"')
  })

  it('keeps AI suggestions selectable and only highlights them in suggestion mode', () => {
    expect(source).toContain("@click=\"selectedMode = 'suggestion'; selectedDirection = dir\"")
    expect(source).toContain("selectedMode === 'suggestion' && selectedDirection === dir")
  })

  it('emits the final custom or suggestion direction as the primary revision feedback', () => {
    expect(scriptBlock).toContain('const finalDirection = computed')
    expect(scriptBlock).toContain('emit(\'confirm\', finalDirection.value, userNote.value)')
    expect(scriptBlock).toContain('const canConfirm = computed')
    expect(source).toContain(':disabled="!canConfirm"')
  })
})
