import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

function readSource(relativePath: string): string {
  return readFileSync(new URL(relativePath, import.meta.url), 'utf-8')
}

describe('F9 visual and layout contracts', () => {
  const writingEditorSource = readSource('./components/WritingEditor.vue')
  const workspaceSource = readSource('./views/Workspace.vue')
  const styleSource = readSource('./style.css')

  it('WritingEditor constrains editor width and centers it with typography tokens', () => {
    expect(writingEditorSource).toContain('max-width: var(--editor-width)')
    expect(writingEditorSource).toContain('width: min(var(--editor-width), 100%)')
    expect(writingEditorSource).toContain('margin: 0 auto')
    expect(writingEditorSource).toContain('font-family: var(--editor-font)')
    expect(writingEditorSource).toContain('font-size: var(--editor-size)')
    expect(writingEditorSource).toContain('line-height: var(--editor-lh)')
    expect(writingEditorSource).toContain('letter-spacing: 0.01em')
    expect(writingEditorSource).toContain('caret-color: var(--accent)')
    expect(writingEditorSource).toContain('.editor-textarea::selection')
  })

  it('WritingEditor font switcher labels serif option as 宋体 mapped to var(--editor-font)', () => {
    expect(writingEditorSource).toContain("{ label: '宋体', value: 'var(--editor-font)' }")
    expect(writingEditorSource).toContain("ref('var(--editor-font)')")
  })

  it('Workspace styles active unit item with subtle accent and left bar', () => {
    expect(workspaceSource).toContain('.unit-item.active {')
    expect(workspaceSource).toContain('background: var(--accent-subtle)')
    expect(workspaceSource).toContain('inset 3px 0 0 var(--accent)')
  })

  it('Workspace configures unit status dots and generating pulse animation with reduced motion support', () => {
    expect(workspaceSource).toContain('.unit-status::before')
    expect(workspaceSource).toContain('.unit-status.draft::before')
    expect(workspaceSource).toContain('.unit-status.reviewing::before')
    expect(workspaceSource).toContain('.unit-status.revision::before')
    expect(workspaceSource).toContain('.unit-status.final::before')
    expect(workspaceSource).toContain('animation: pulse-dot')
    expect(workspaceSource).toContain('@keyframes pulse-dot')
    expect(workspaceSource).toContain('@media (prefers-reduced-motion: reduce)')
  })

  it('Workspace and style.css configure 2px accent indicator tabs', () => {
    expect(workspaceSource).toContain('border-bottom: 2px solid transparent')
    expect(workspaceSource).toContain('border-bottom-color: var(--accent)')
    expect(styleSource).toContain('.tabs')
    expect(styleSource).toContain('border-bottom-color: var(--accent)')
  })
})
