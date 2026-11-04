import { describe, expect, it } from 'vitest'
import workspaceSource from './Workspace.vue?raw'
import storeSource from '../stores/index.ts?raw'

describe('章节标题与章节大纲标题同步', () => {
  it('修改章节标题时调用大纲标题的本地同步', () => {
    expect(storeSource).toContain('useOutlineStore().syncTitleForChapter(projectId, chapterNum, title)')
  })

  it('章节标题编辑仍通过章节标题保存入口提交', () => {
    expect(workspaceSource).toContain('await chapterStore.updateChapterTitle(projectId.value, position, title)')
  })
})
