import { describe, expect, it } from 'vitest'
import source from './ApprovalModal.vue?raw'

// 无 DOM 测试环境，沿用源码断言方式（与 writingEditorLayout.test.ts 一致）。
// 注意：模板块内含嵌套 <template v-if>，需贪婪匹配到最外层 </template>。
const scriptBlock = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)?.[1] ?? ''
const templateBlock = source.match(/<template>([\s\S]*)<\/template>/)?.[1] ?? ''

describe('ApprovalModal comparison copy', () => {
  it('copies the full original text from the original side', () => {
    // 润色前原文侧：复制完整原文（originalContent），不是 diff 分段
    expect(scriptBlock).toContain("side === 'original' ? (props.originalContent ?? '') : props.content")
    expect(templateBlock).toContain("@click=\"copySideContent('original')\"")
    expect(templateBlock).toContain(':disabled="!originalContent"')
  })

  it('copies the full candidate text from the candidate side', () => {
    expect(templateBlock).toContain("@click=\"copySideContent('candidate')\"")
    expect(templateBlock).toContain(':disabled="!content"')
  })

  it('uses the clipboard API and shows transient feedback', () => {
    expect(scriptBlock).toContain('await navigator.clipboard.writeText(text)')
    expect(scriptBlock).toContain("copiedSide.value = side")
    expect(scriptBlock).toContain('setTimeout')
    expect(scriptBlock).toContain('copiedSide.value = null')
    // 反馈文案：复制中短暂显示“已复制”
    expect(templateBlock).toContain("copiedSide === 'original' ? '已复制' : '复制'")
    expect(templateBlock).toContain("copiedSide === 'candidate' ? '已复制' : '复制'")
  })
})
