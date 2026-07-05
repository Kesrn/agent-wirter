import { describe, expect, it } from 'vitest'

import workspaceSource from './Workspace.vue?raw'

function cssBlock(source: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const match = source.match(new RegExp(`${escaped}\\s*\\{([\\s\\S]*?)\\n\\}`))
  return match?.[1] ?? ''
}

describe('workspace chapter config modal contract', () => {
  it('opens chapter config as a modal instead of replacing the editor area', () => {
    expect(workspaceSource).toContain('const showChapterConfigModal = ref(false)')
    expect(workspaceSource).toContain('showChapterConfigModal.value = true')
    expect(workspaceSource).toContain('<Teleport to="body">')
    expect(workspaceSource).toContain('class="chapter-config-modal-overlay"')
    expect(workspaceSource).toContain('class="chapter-config-modal"')
    expect(workspaceSource).toContain('role="dialog"')
    expect(workspaceSource).toContain('aria-modal="true"')
    expect(workspaceSource).not.toContain("viewMode = 'chapterConfig'")
    expect(workspaceSource).not.toContain('viewMode === \'chapterConfig\'')
  })

  it('keeps the writing editor mounted while chapter config is open', () => {
    expect(workspaceSource).toContain('<main class="editor-area">')
    expect(workspaceSource).toContain('<WritingEditor v-if="currentWritingUnit"')
    expect(workspaceSource).not.toContain('<ChapterConfig v-if="viewMode')
  })

  it('does not dismiss chapter config by backdrop click and provides a real scrollable modal shell', () => {
    const overlayBlock = cssBlock(workspaceSource, '.chapter-config-modal-overlay')
    const modalBlock = cssBlock(workspaceSource, '.chapter-config-modal')

    expect(workspaceSource).not.toContain('chapter-config-modal-overlay" @click')
    expect(overlayBlock).toContain('position: fixed')
    expect(overlayBlock).toContain('z-index: 2500')
    expect(modalBlock).toContain('min-height: 0')
    expect(modalBlock).toContain('display: flex')
    expect(modalBlock).toContain('overflow: hidden')
  })
})
