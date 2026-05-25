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

    expect(rootBlock).toContain('min-height: 0')
    expect(bodyBlock).toContain('overflow: hidden')
    expect(bodyBlock).toContain('min-height: 0')
    expect(contentBlock).toContain('overflow: auto')
    expect(contentBlock).toContain('-webkit-overflow-scrolling: touch')
    expect(contentBlock).toContain('min-height: 0')
  })
})
