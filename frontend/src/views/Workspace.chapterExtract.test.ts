import { describe, expect, it } from 'vitest'

import workspaceSource from './Workspace.vue?raw'

describe('chapter memory extraction entry', () => {
  it('puts a single-chapter extraction button beside every novel chapter', () => {
    expect(workspaceSource).toContain('@click.stop="extractChapterMemory(ch)"')
    expect(workspaceSource).toContain("extractingChapterId === ch.id ? '提炼中...' : '提炼'")
    expect(workspaceSource).not.toContain("'更新记忆'")
  })

  it('keeps the preview bound to the chapter that initiated extraction', () => {
    expect(workspaceSource).toContain('const structureTargetChapterNum')
    expect(workspaceSource).toContain('api.extractChapterStructure(projectId.value, sequenceNumber')
  })
})
