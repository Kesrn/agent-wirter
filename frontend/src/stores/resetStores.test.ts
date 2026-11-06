import { describe, it, expect, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import {
  resetAllStores,
  useProjectStore,
  useChapterStore,
  useDocumentStore,
  useUiStore,
  useLLMSettingsStore,
  useConversationStore,
} from './index'

describe('Pinia stores resetAllStores contract', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
  })

  it('resets dirty project, chapter, document, ui, llmSettings and conversation store states', () => {
    const pinia = createPinia()
    setActivePinia(pinia)

    const projectStore = useProjectStore(pinia)
    const chapterStore = useChapterStore(pinia)
    const documentStore = useDocumentStore(pinia)
    const uiStore = useUiStore(pinia)
    const llmStore = useLLMSettingsStore(pinia)
    const convStore = useConversationStore(pinia)

    // 1. Mutate store states (simulate user A logged in and loaded data)
    projectStore.projects.unshift({
      id: 'user-a-proj',
      title: 'User A Secret Project',
      genre: 'fantasy',
      style: 'epic',
      overall_outline: '',
      status: 'active',
      target_words: 50000,
      mode: 'novel',
      description: 'Secret description',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    })
    projectStore.setCurrent('user-a-proj')

    chapterStore.chapters.unshift({
      id: 'c-user-a',
      project_id: 'user-a-proj',
      chapter_num: 99,
      title: 'Secret Chapter',
      summary: '',
      draft: 'Secret Draft',
      final_text: '',
      status: 'draft',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      review_comment_ids: [],
      review_round: 0,
    } as any)
    chapterStore.setCurrentChapter(99)

    documentStore.documents.push({
      id: 'doc-user-a',
      project_id: 'user-a-proj',
      position: 1,
      title: 'Secret Doc',
      draft: 'Doc draft',
      final_text: '',
      word_count: 100,
      status: 'draft',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
    })

    uiStore.showToast('Test Toast User A', 'info')

    llmStore.configs = [
      {
        id: 'cfg-user-a',
        provider: 'openai',
        name: 'User A Config',
        api_key_set: true,
        api_key_masked: 'sk-***',
        base_url: null,
        model_id: 'gpt-4o',
        is_active: true,
        source: 'user',
        created_at: new Date().toISOString(),
        updated_at: new Date().toISOString(),
      },
    ]
    llmStore.selectedConfigId = 'cfg-user-a'

    convStore.sessionId = 'session-user-a'
    convStore.messages = [
      {
        id: 'msg-1',
        session_id: 'session-user-a',
        role: 'user',
        content: 'hello',
        created_at: new Date().toISOString(),
      },
    ]

    expect(projectStore.projects.some(p => p.id === 'user-a-proj')).toBe(true)
    expect(projectStore.currentProjectId).toBe('user-a-proj')
    expect(chapterStore.chapters.some(c => c.id === 'c-user-a')).toBe(true)
    expect(documentStore.documents.length).toBe(1)
    expect(uiStore.toasts.length).toBe(1)
    expect(llmStore.configs.length).toBe(1)
    expect(convStore.sessionId).toBe('session-user-a')

    // 2. Perform resetAllStores()
    resetAllStores()

    // 3. Verify all states are completely reset
    expect(projectStore.projects.some(p => p.id === 'user-a-proj')).toBe(false)
    expect(projectStore.currentProjectId).toBeNull()
    expect(chapterStore.chapters.some(c => c.id === 'c-user-a')).toBe(false)
    expect(chapterStore.currentChapterNum).toBe(1)
    expect(documentStore.documents.length).toBe(0)
    expect(uiStore.toasts.length).toBe(0)
    expect(llmStore.configs.length).toBe(0)
    expect(llmStore.selectedConfigId).toBeNull()
    expect(convStore.sessionId).toBeNull()
    expect(convStore.messages.length).toBe(0)
  })

  it('safely does nothing if active Pinia is undefined', () => {
    setActivePinia(undefined)
    expect(() => resetAllStores()).not.toThrow()
  })
})
