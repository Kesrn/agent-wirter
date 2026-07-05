import { describe, expect, it } from 'vitest'

import chapterConfigSource from './ChapterConfig.vue?raw'

function cssBlock(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = source.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\n\\}`))
  return match?.[1] ?? ''
}

describe('chapter config layout scroll contract', () => {
  it('keeps the config body as a real scroll container inside the workspace column', () => {
    const rootBlock = cssBlock(chapterConfigSource, '.chapter-config')
    const bodyBlock = cssBlock(chapterConfigSource, '.config-body')

    expect(rootBlock).toContain('height: 100%')
    expect(rootBlock).toContain('min-height: 0')
    expect(rootBlock).toContain('overflow: hidden')
    expect(bodyBlock).toContain('flex: 1')
    expect(bodyBlock).toContain('min-height: 0')
    expect(bodyBlock).toContain('overflow-y: auto')
    expect(bodyBlock).toContain('-webkit-overflow-scrolling: touch')
    expect(bodyBlock).toContain('overscroll-behavior: contain')
    expect(bodyBlock).toContain('padding-bottom: max(160px')
    expect(bodyBlock).toContain('scroll-padding-bottom: 160px')
  })

  it('auto-grows textareas so wheel events scroll the chapter config page instead of nested fields', () => {
    expect(chapterConfigSource).toContain('const vAutoGrow')
    expect(chapterConfigSource).toContain('function resizeTextArea')
    expect(chapterConfigSource).toContain('v-auto-grow')
    expect(chapterConfigSource).toContain('.form-textarea {\n  resize: none')
    expect(chapterConfigSource).toContain('overflow-y: hidden')
  })
})
