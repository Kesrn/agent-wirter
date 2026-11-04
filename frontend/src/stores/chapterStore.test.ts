import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../api/client'
import { useChapterStore } from './index'
import type { ApiChapter } from '../api/types'

function apiChapter(overrides: Partial<ApiChapter> = {}): ApiChapter {
  return {
    id: 'ch-1',
    project_id: 'project-1',
    title: '第一章',
    content: '正文',
    outline: '大纲',
    sequence_number: 1,
    word_count: 2,
    status: 'draft',
    created_at: '2026-05-24T00:00:00Z',
    updated_at: '2026-05-24T00:00:00Z',
    ...overrides,
  }
}

describe('useChapterStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('replaces the current chapter with the backend response so the editor keeps in sync', async () => {
    const updateChapter = vi.spyOn(api, 'updateChapter').mockResolvedValue(
      apiChapter({ title: '后端标题', content: '后端正文', status: 'reviewing' }),
    )

    const store = useChapterStore()
    store.chapters.push({
      id: 'ch-1',
      project_id: 'project-1',
      chapter_num: 1,
      title: '旧标题',
      summary: '旧大纲',
      draft: '保存前正文',
      final_text: '',
      status: 'draft',
      review_comment_ids: [],
      review_round: 0,
    })
    store.setCurrentChapter(1)
    const before = store.currentChapterForProject('project-1')

    await store.saveCurrentChapter('project-1')

    expect(updateChapter).toHaveBeenCalledWith('project-1', 1, {
      content: '保存前正文',
      outline: '旧大纲',
      status: 'draft',
    })
    const after = store.currentChapterForProject('project-1')
    // 保存后的正文与标题必须来自后端返回结果
    expect(after?.id).toBe('ch-1')
    expect(after?.title).toBe('后端标题')
    expect(after?.draft).toBe('后端正文')
    expect(after?.status).toBe('reviewing')
    // 对象被整体替换（id 不变、引用变化），这正是编辑器 watch(currentUnit)
    // 触发“同一章节回写”路径、需要保留滚动位置的原因。
    expect(after).not.toBe(before)
  })
})
