import { describe, expect, it } from 'vitest'
import workspaceSource from '../views/Workspace.vue?raw'
import writingEditorSource from './WritingEditor.vue?raw'

function cssBlock(source: string, selector: string): string {
  const match = source.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{([^}]*)\\}`))
  return match?.[1] ?? ''
}

describe('writing editor layout scroll contract', () => {
  it('keeps Workspace editor area as a non-scrolling flex shell', () => {
    const editorArea = cssBlock(workspaceSource, '.editor-area')
    const mobileEditor = cssBlock(workspaceSource, '.mobile-editor')

    expect(editorArea).toContain('display: flex')
    expect(editorArea).toContain('overflow: hidden')
    expect(editorArea).toContain('min-height: 0')
    expect(mobileEditor).toContain('overflow: hidden')
  })

  it('keeps WritingEditor content as the only scroll layer', () => {
    const rootBlock = cssBlock(writingEditorSource, '.writing-editor')
    const bodyBlock = cssBlock(writingEditorSource, '.editor-body')
    const contentBlock = cssBlock(writingEditorSource, '.editor-content')
    const textareaBlock = cssBlock(writingEditorSource, '.editor-textarea')

    expect(rootBlock).toContain('min-height: 0')
    expect(bodyBlock).toContain('overflow: hidden')
    expect(bodyBlock).toContain('min-height: 0')
    expect(contentBlock).toContain('overflow-y: auto')
    expect(contentBlock).toContain('-webkit-overflow-scrolling: touch')
    expect(contentBlock).toContain('overscroll-behavior: contain')
    expect(contentBlock).toContain('min-height: 0')
    expect(textareaBlock).toContain('overflow-y: hidden')
    expect(textareaBlock).not.toContain('overflow-y: auto')
  })

  it('resizes the textarea to content height instead of creating nested scroll after save', () => {
    expect(writingEditorSource).toContain("el.style.height = 'auto'")
    expect(writingEditorSource).toContain('Math.max(minHeight, el.scrollHeight)')
    expect(writingEditorSource).toContain('即使正文未变化，也要恢复')
  })
})
