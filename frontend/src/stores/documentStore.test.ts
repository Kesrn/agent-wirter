import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { api } from '../api/client'
import { useDocumentStore, useGenerationHistoryStore } from './index'
import type { ApiDocument, ApiGenerationRecord, ApiGenerationRecordListItem } from '../api/types'

function apiDocument(overrides: Partial<ApiDocument> = {}): ApiDocument {
  return {
    id: 'doc-1',
    project_id: 'project-1',
    title: '稿件标题',
    content: '初稿',
    position: 3,
    word_count: 2,
    status: 'draft',
    created_at: '2026-05-24T00:00:00Z',
    updated_at: '2026-05-24T00:00:00Z',
    ...overrides,
  }
}

function apiGenerationRecord(overrides: Partial<ApiGenerationRecord> = {}): ApiGenerationRecord {
  return {
    id: 'gen-1',
    project_id: 'project-1',
    chapter_id: null,
    document_id: 'doc-1',
    mode: 'continue',
    expert_id: null,
    direction: '补充案例',
    word_count: 20,
    status: 'candidate',
    created_at: '2026-05-25T00:00:00Z',
    content: 'AI 生成内容',
    user_note: null,
    target_words: null,
    accepted_version_id: null,
    review_results: null,
    request_params: null,
    updated_at: '2026-05-25T00:00:00Z',
    ...overrides,
  }
}

function apiGenerationListItem(overrides: Partial<ApiGenerationRecordListItem> = {}): ApiGenerationRecordListItem {
  const detail = apiGenerationRecord(overrides)
  return {
    id: detail.id,
    project_id: detail.project_id,
    chapter_id: detail.chapter_id,
    document_id: detail.document_id,
    mode: detail.mode,
    expert_id: detail.expert_id,
    direction: detail.direction,
    word_count: detail.word_count,
    status: detail.status,
    created_at: detail.created_at,
  }
}

describe('useDocumentStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('maps API documents to document units with position, not chapter_num', async () => {
    vi.spyOn(api, 'listDocuments').mockResolvedValue([apiDocument({ position: 7, status: 'approved' })])

    const store = useDocumentStore()
    await store.loadDocuments('project-1')

    expect(store.documents).toHaveLength(1)
    expect(store.documents[0].position).toBe(7)
    expect(store.documents[0].status).toBe('final')
    expect('chapter_num' in store.documents[0]).toBe(false)
  })

  it('creates documents with a document payload position', async () => {
    const createDocument = vi
      .spyOn(api, 'createDocument')
      .mockResolvedValue(apiDocument({ id: 'doc-created', position: 4 }))

    const store = useDocumentStore()
    const created = await store.createDocumentRemote('project-1', {
      title: '新稿件',
      position: 4,
    })

    expect(createDocument).toHaveBeenCalledWith('project-1', {
      title: '新稿件',
      position: 4,
    })
    expect(created?.id).toBe('doc-created')
    expect(created?.position).toBe(4)
  })

  it('saves, renames, and deletes documents by document id', async () => {
    const updateDocument = vi
      .spyOn(api, 'updateDocument')
      .mockResolvedValue(apiDocument({ id: 'doc-1', content: '已保存', position: 2 }))
    const deleteDocument = vi
      .spyOn(api, 'deleteDocument')
      .mockResolvedValue({ ok: true })

    const store = useDocumentStore()
    store.documents.push({
      id: 'doc-1',
      project_id: 'project-1',
      position: 2,
      title: '旧标题',
      draft: '已保存',
      final_text: '',
      status: 'draft',
      word_count: 3,
      created_at: '2026-05-24T00:00:00Z',
      updated_at: '2026-05-24T00:00:00Z',
    })
    store.setCurrentDocument(2)

    await store.saveCurrentDocument('project-1')
    await store.updateDocumentTitle('project-1', 2, '新标题')
    await store.deleteDocumentRemote('project-1', 2)

    expect(updateDocument).toHaveBeenNthCalledWith(1, 'project-1', 'doc-1', {
      content: '已保存',
      status: 'draft',
    })
    expect(updateDocument).toHaveBeenNthCalledWith(2, 'project-1', 'doc-1', {
      title: '新标题',
    })
    expect(deleteDocument).toHaveBeenCalledWith('project-1', 'doc-1')
  })

  it('selects an existing document after load and reports missing selection on save', async () => {
    vi.spyOn(api, 'listDocuments').mockResolvedValue([
      apiDocument({ id: 'doc-2', position: 2 }),
    ])

    const store = useDocumentStore()
    await store.loadDocuments('project-1')

    expect(store.currentDocumentPosition).toBe(2)
    expect(store.currentDocumentForProject('project-1')?.id).toBe('doc-2')

    store.setCurrentDocument(99)
    await expect(store.saveCurrentDocument('project-1')).rejects.toThrow('请先选择一个稿件')
  })
})

describe('useGenerationHistoryStore', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.restoreAllMocks()
  })

  it('loads document generation records without content, then fetches detail content', async () => {
    vi.spyOn(api, 'listDocumentGenerations').mockResolvedValue([apiGenerationListItem()])
    vi.spyOn(api, 'getGenerationRecord').mockResolvedValue(apiGenerationRecord())

    const store = useGenerationHistoryStore()
    await store.loadDocumentRecords('project-1', 'doc-1')

    expect(store.records).toHaveLength(1)
    expect(store.records[0].content).toBeNull()

    const content = await store.getRecordContent('project-1', 'gen-1')
    expect(content).toBe('AI 生成内容')
    expect(store.records[0].content).toBe('AI 生成内容')
  })

  it('updates generation record status and stores diff results', async () => {
    vi.spyOn(api, 'updateGenerationRecord').mockResolvedValue(apiGenerationRecord({ status: 'applied' }))
    vi.spyOn(api, 'diffGenerationRecord').mockResolvedValue({
      generation_id: 'gen-1',
      diff: [{ tag: 'replace', lines_a: ['AI'], lines_b: ['当前'] }],
    })

    const store = useGenerationHistoryStore()
    store.upsertCandidate('project-1', 'gen-1')

    await store.updateRecordStatus('project-1', 'gen-1', 'applied')
    await store.loadDiffWithCurrent('project-1', 'gen-1', '当前')

    expect(store.records[0].status).toBe('applied')
    expect(store.diffResult?.leftTitle).toBe('AI生成')
    expect(store.diffResult?.diff).toHaveLength(1)
  })
})
