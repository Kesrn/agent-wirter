import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Project, Chapter, Expert, WorkflowStep, ReviewComment, CharacterRelation, ProjectMode, ExpertCreatePayload, WorldEntry, Character, OutlineItem, HiddenThread } from '../api/types'
import type { ApiProject, ApiChapter, ApiExpert, ApiWorldEntry, ApiCharacter, ApiCharacterRelation, ApiOutline, ApiHiddenThread } from '../api/types'
import type { CharacterRelationCreatePayload, CharacterRelationUpdatePayload, OutlineUpdatePayload, HiddenThreadUpdatePayload, WorldEntryCreatePayload, WorldEntryUpdatePayload, CharacterCreatePayload, CharacterUpdatePayload } from '../api/types'
import { api, ApiError } from '../api/client'
import { MOCK_PROJECTS, MOCK_CHAPTERS, DEFAULT_EXPERTS, MOCK_REVIEW_COMMENTS, MOCK_CHARACTER_RELATIONS, MOCK_WORLD_ENTRIES, MOCK_CHARACTERS, MOCK_OUTLINE, MOCK_HIDDEN_THREADS } from '../mock/data'

// ─── API → UI conversion helpers ───

function apiProjectToProject(ap: ApiProject): Project {
  return {
    id: ap.id,
    title: ap.title,
    genre: '',
    style: '',
    status: ap.status,
    mode: ap.mode as ProjectMode,
    description: ap.description ?? '',
    target_words: ap.target_words,
    created_at: ap.created_at,
    updated_at: ap.updated_at,
  }
}

const CHAPTER_STATUS_MAP: Record<string, Chapter['status']> = {
  draft: 'draft',
  writing: 'draft',
  reviewing: 'reviewing',
  revision: 'revision',
  final: 'final',
  completed: 'final',
}

function apiChapterToChapter(ac: ApiChapter, projectId: string): Chapter {
  return {
    id: ac.id,
    project_id: projectId,
    chapter_num: ac.sequence_number,
    title: ac.title,
    summary: ac.outline ?? '',
    draft: ac.content ?? '',
    final_text: '',
    status: CHAPTER_STATUS_MAP[ac.status] ?? 'draft',
    review_comment_ids: [],
    review_round: 0,
  }
}

function apiExpertToExpert(ae: ApiExpert): Expert {
  return {
    id: ae.id,
    role: ae.role_type,
    name: ae.name,
    role_type: ae.role_type,
    description: ae.description,
    system_prompt: ae.system_prompt,
    temperature: ae.temperature,
    max_tokens: ae.max_tokens,
    workflow_position: ae.workflow_position,
    context_scope: ae.context_scope,
    trigger: ae.trigger,
    is_builtin: ae.is_builtin,
    is_enabled: ae.is_enabled,
    color: ae.color,
  }
}

export const useProjectStore = defineStore('project', () => {
  const projects = ref<Project[]>([...MOCK_PROJECTS])
  const currentProjectId = ref<string | null>(null)
  const currentProject = computed(() => projects.value.find(p => p.id === currentProjectId.value))
  const loadError = ref('')

  function setCurrent(id: string) {
    currentProjectId.value = id
  }

  function addProject(p: Project) {
    projects.value.unshift(p)
  }

  async function createProject(payload: { title: string; mode: ProjectMode; genre?: string; style?: string; description?: string; target_words?: number }): Promise<Project> {
    return createProjectRemote(payload)
  }

  async function loadProjects() {
    loadError.value = ''
    try {
      const list = await api.listProjects()
      projects.value = list.map(apiProjectToProject)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载项目失败'
      loadError.value = msg
      // Keep mock data as fallback
    }
  }

  async function createProjectRemote(payload: { title: string; mode: ProjectMode; genre?: string; style?: string; description?: string; target_words?: number }): Promise<Project> {
    const ap = await api.createProject({
      title: payload.title,
      description: payload.description,
      target_words: payload.target_words,
      mode: payload.mode,
    })
    const p = apiProjectToProject(ap)
    projects.value.unshift(p)
    return p
  }

  return { projects, currentProjectId, currentProject, loadError, setCurrent, addProject, createProject, loadProjects, createProjectRemote }
})

export const useChapterStore = defineStore('chapter', () => {
  const chapters = ref<Chapter[]>([...MOCK_CHAPTERS])
  const reviewComments = ref<ReviewComment[]>([...MOCK_REVIEW_COMMENTS])
  const currentChapterNum = ref(1)
  const loadError = ref('')
  const saving = ref(false)

  function chaptersForProject(projectId: string): Chapter[] {
    return chapters.value.filter(c => c.project_id === projectId)
  }

  function currentChapterForProject(projectId: string): Chapter | undefined {
    return chapters.value.find(c => c.project_id === projectId && c.chapter_num === currentChapterNum.value)
  }

  function reviewCommentsForChapter(chapterId: string): ReviewComment[] {
    const chapter = chapters.value.find(c => c.id === chapterId)
    if (!chapter) return []
    return reviewComments.value.filter(rc => chapter.review_comment_ids.includes(rc.id))
  }

  function setCurrentChapter(num: number) {
    currentChapterNum.value = num
  }

  function updateDraft(projectId: string, text: string) {
    const ch = currentChapterForProject(projectId)
    if (ch) ch.draft = text
  }

  async function addChapter(projectId: string, payload: { title: string; chapter_num: number; summary?: string }): Promise<string | null> {
    try {
      const ch = await createChapterRemote(projectId, payload)
      return ch ? null : '创建章节失败'
    } catch (e: unknown) {
      if (e instanceof ApiError && e.message.includes('已存在')) return e.message
      return friendlyError(e, '创建章节失败')
    }
  }

  function nextChapterNum(projectId: string): number {
    const projectChapters = chaptersForProject(projectId)
    if (!projectChapters.length) return 1
    return Math.max(...projectChapters.map(c => c.chapter_num)) + 1
  }

  async function loadChapters(projectId: string) {
    loadError.value = ''
    try {
      const list = await api.listChapters(projectId)
      // Replace chapters for this project, keep others
      const otherChapters = chapters.value.filter(c => c.project_id !== projectId)
      chapters.value = [...otherChapters, ...list.map(ac => apiChapterToChapter(ac, projectId))]
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载章节失败'
      loadError.value = msg
    }
  }

  async function createChapterRemote(projectId: string, payload: { title: string; chapter_num: number; summary?: string }): Promise<Chapter | null> {
    const ac = await api.createChapter(projectId, {
      title: payload.title,
      outline: payload.summary,
      sequence_number: payload.chapter_num,
    })
    const ch = apiChapterToChapter(ac, projectId)
    chapters.value.push(ch)
    return ch
  }

  async function saveCurrentChapter(projectId: string) {
    const ch = currentChapterForProject(projectId)
    if (!ch) return
    saving.value = true
    try {
      const ac = await api.updateChapter(projectId, ch.chapter_num, {
        content: ch.draft,
        outline: ch.summary || undefined,
        status: ch.status,
      })
      // Sync local state from response
      const updated = apiChapterToChapter(ac, projectId)
      const idx = chapters.value.findIndex(c => c.id === ch.id)
      if (idx !== -1) chapters.value[idx] = updated
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '保存失败'
      loadError.value = msg
      throw e
    } finally {
      saving.value = false
    }
  }

  return {
    chapters, reviewComments, currentChapterNum, loadError, saving,
    chaptersForProject, currentChapterForProject, reviewCommentsForChapter,
    setCurrentChapter, updateDraft, addChapter, nextChapterNum,
    loadChapters, createChapterRemote, saveCurrentChapter,
  }
})

export const useExpertStore = defineStore('expert', () => {
  const experts = ref<Expert[]>([])
  const activeExpertId = ref<string | null>(null)
  const isGenerating = ref(false)
  const streamOutput = ref('')
  const finalDraft = ref('')
  const workflowSteps = ref<WorkflowStep[]>([])
  const loading = ref(false)
  const loadError = ref('')

  const activeExpert = computed(() => experts.value.find(e => e.id === activeExpertId.value))

  function getExpertName(id: string): string {
    return experts.value.find(e => e.id === id)?.name ?? id
  }

  function setActive(id: string) {
    activeExpertId.value = id
  }

  function startGenerating() {
    isGenerating.value = true
    streamOutput.value = ''
    finalDraft.value = ''
  }

  function appendOutput(text: string) {
    streamOutput.value += text
  }

  function appendDraft(text: string) {
    finalDraft.value += text
  }

  function replaceDraft(text: string) {
    finalDraft.value = text
  }

  function stopGenerating() {
    isGenerating.value = false
  }

  /** Load experts from backend API; fall back to DEFAULT_EXPERTS on failure */
  async function loadExperts(projectId: string) {
    loading.value = true
    loadError.value = ''
    try {
      const list = await api.listExperts(projectId)
      experts.value = list.map(apiExpertToExpert)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载专家失败'
      loadError.value = msg
      // Fallback to local defaults when backend is unavailable
      if (!experts.value.length) {
        experts.value = [...DEFAULT_EXPERTS]
      }
    } finally {
      loading.value = false
    }
  }

  /** Add a custom expert via backend API; on failure add locally as fallback */
  async function addExpert(projectId: string, payload: ExpertCreatePayload): Promise<Expert | null> {
    try {
      const ae = await api.createCustomExpert(projectId, payload)
      const expert = apiExpertToExpert(ae)
      experts.value.push(expert)
      return expert
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '创建专家失败'
      loadError.value = msg
      throw e
    }
  }

  function addCustomExpert(expert: Expert) {
    expert.is_builtin = false
    experts.value.push(expert)
  }

  async function toggleExpert(projectId: string, expertId: string) {
    const expert = experts.value.find(e => e.id === expertId)
    if (!expert) return
    try {
      const ae = await api.updateExpert(projectId, expertId, { is_enabled: !expert.is_enabled })
      const updated = apiExpertToExpert(ae)
      const idx = experts.value.findIndex(e => e.id === expertId)
      if (idx !== -1) experts.value[idx] = updated
    } catch (e: unknown) {
      loadError.value = friendlyError(e, '更新专家失败')
      throw e
    }
  }

  function setWorkflowSteps(steps: WorkflowStep[]) {
    workflowSteps.value = steps
  }

  function updateStepStatus(stepId: string, status: WorkflowStep['status'], output?: string) {
    const step = workflowSteps.value.find(s => s.id === stepId)
    if (step) {
      step.status = status
      if (output) step.output = output
    }
  }

  return {
    experts, activeExpertId, activeExpert, isGenerating, streamOutput, finalDraft,
    workflowSteps, loading, loadError,
    getExpertName, setActive, startGenerating, appendOutput, appendDraft, replaceDraft, stopGenerating,
    loadExperts, addExpert, addCustomExpert, toggleExpert, setWorkflowSteps, updateStepStatus,
  }
})

// ─── CharacterRelation store ───

function apiCharacterRelationToRelation(acr: ApiCharacterRelation): CharacterRelation {
  return {
    id: acr.id,
    project_id: acr.project_id,
    source_character_id: acr.source_character_id,
    target_character_id: acr.target_character_id,
    description: acr.description,
  }
}

export const useRelationStore = defineStore('relation', () => {
  const relations = ref<CharacterRelation[]>([...MOCK_CHARACTER_RELATIONS])
  const loading = ref(false)
  const loadError = ref('')

  function getRelationsForProject(projectId: string): CharacterRelation[] {
    return relations.value.filter(r => r.project_id === projectId)
  }

  function getRelationsForCharacter(characterId: string): CharacterRelation[] {
    return relations.value.filter(r => r.source_character_id === characterId)
  }

  async function loadRelations(projectId: string) {
    loading.value = true
    loadError.value = ''
    try {
      const list = await api.listCharacterRelations(projectId)
      const otherRelations = relations.value.filter(r => r.project_id !== projectId)
      relations.value = [...otherRelations, ...list.map(apiCharacterRelationToRelation)]
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载角色关系失败'
      loadError.value = msg
      // Fallback: keep mock data for this project if present
    } finally {
      loading.value = false
    }
  }

  async function addRelation(projectId: string, payload: CharacterRelationCreatePayload): Promise<CharacterRelation | null> {
    try {
      const acr = await api.createCharacterRelation(projectId, payload)
      const rel = apiCharacterRelationToRelation(acr)
      relations.value.push(rel)
      return rel
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '创建角色关系失败'
      loadError.value = msg
      throw e
    }
  }

  async function removeRelation(projectId: string, relationId: string) {
    try {
      await api.deleteCharacterRelation(projectId, relationId)
      relations.value = relations.value.filter(r => r.id !== relationId)
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '删除角色关系失败'
      loadError.value = msg
      throw e
    }
  }

  async function updateRelation(projectId: string, relationId: string, payload: CharacterRelationUpdatePayload) {
    try {
      const acr = await api.updateCharacterRelation(projectId, relationId, payload)
      const updated = apiCharacterRelationToRelation(acr)
      const idx = relations.value.findIndex(r => r.id === relationId)
      if (idx !== -1) relations.value[idx] = updated
      return updated
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '更新角色关系失败'
      loadError.value = msg
      throw e
    }
  }

  return { relations, loading, loadError, getRelationsForProject, getRelationsForCharacter, loadRelations, addRelation, removeRelation, updateRelation }
})

// ─── WorldEntry store ───

function apiWorldEntryToWorldEntry(awe: ApiWorldEntry): WorldEntry {
  return {
    id: awe.id,
    project_id: awe.project_id,
    title: awe.title,
    category: awe.category,
    content: awe.content,
    rules: awe.rules,
    confidence: (awe.confidence === 'low' || awe.confidence === 'medium' || awe.confidence === 'high')
      ? awe.confidence
      : 'medium',
    created_at: awe.created_at,
    updated_at: awe.updated_at,
  }
}

export const useWorldEntryStore = defineStore('worldEntry', () => {
  const worldEntries = ref<WorldEntry[]>([...MOCK_WORLD_ENTRIES])
  const loading = ref(false)
  const loadError = ref('')

  function entriesForProject(projectId: string): WorldEntry[] {
    return worldEntries.value.filter(we => we.project_id === projectId)
  }

  async function loadWorldEntries(projectId: string) {
    loading.value = true
    loadError.value = ''
    try {
      const list = await api.listWorldEntries(projectId)
      const otherEntries = worldEntries.value.filter(we => we.project_id !== projectId)
      worldEntries.value = [...otherEntries, ...list.map(apiWorldEntryToWorldEntry)]
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载世界观失败'
      loadError.value = msg
      // Fallback: keep mock data for this project if present
    } finally {
      loading.value = false
    }
  }

  async function addWorldEntry(projectId: string, payload: WorldEntryCreatePayload): Promise<WorldEntry | null> {
    try {
      const awe = await api.createWorldEntry(projectId, payload)
      const entry = apiWorldEntryToWorldEntry(awe)
      worldEntries.value.push(entry)
      return entry
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '创建世界观失败'
      loadError.value = msg
      throw e
    }
  }

  async function updateWorldEntry(projectId: string, entryId: string, payload: WorldEntryUpdatePayload) {
    try {
      const awe = await api.updateWorldEntry(projectId, entryId, payload)
      const updated = apiWorldEntryToWorldEntry(awe)
      const idx = worldEntries.value.findIndex(we => we.id === entryId)
      if (idx !== -1) worldEntries.value[idx] = updated
      return updated
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '更新世界观失败'
      loadError.value = msg
      throw e
    }
  }

  async function removeWorldEntry(projectId: string, entryId: string) {
    try {
      await api.deleteWorldEntry(projectId, entryId)
      worldEntries.value = worldEntries.value.filter(we => we.id !== entryId)
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '删除世界观失败'
      loadError.value = msg
      throw e
    }
  }

  return { worldEntries, loading, loadError, entriesForProject, loadWorldEntries, addWorldEntry, updateWorldEntry, removeWorldEntry }
})

// ─── Character store ───

const ROLE_TYPE_MAP: Record<string, Character['role_type']> = {
  protagonist: 'protagonist',
  antagonist: 'antagonist',
  supporting: 'supporting',
  minor: 'minor',
}

function apiCharacterToCharacter(ac: ApiCharacter): Character {
  return {
    id: ac.id,
    project_id: ac.project_id,
    name: ac.name,
    role_type: ROLE_TYPE_MAP[ac.role_type] ?? 'minor',
    profile: ac.profile,
    faction: ac.faction,
    appearance_count: ac.appearance_count,
    metadata: ac.metadata,
  }
}

export const useCharacterStore = defineStore('character', () => {
  const characters = ref<Character[]>([...MOCK_CHARACTERS])
  const loading = ref(false)
  const loadError = ref('')

  function charactersForProject(projectId: string): Character[] {
    return characters.value.filter(c => c.project_id === projectId)
  }

  async function loadCharacters(projectId: string) {
    loading.value = true
    loadError.value = ''
    try {
      const list = await api.listCharacters(projectId)
      const otherChars = characters.value.filter(c => c.project_id !== projectId)
      characters.value = [...otherChars, ...list.map(apiCharacterToCharacter)]
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载角色失败'
      loadError.value = msg
      // Fallback: keep mock data for this project if present
    } finally {
      loading.value = false
    }
  }

  async function addCharacter(projectId: string, payload: CharacterCreatePayload): Promise<Character | null> {
    try {
      const ac = await api.createCharacter(projectId, payload)
      const char = apiCharacterToCharacter(ac)
      characters.value.push(char)
      return char
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '创建角色失败'
      loadError.value = msg
      throw e
    }
  }

  async function updateCharacter(projectId: string, characterId: string, payload: CharacterUpdatePayload) {
    try {
      const ac = await api.updateCharacter(projectId, characterId, payload)
      const updated = apiCharacterToCharacter(ac)
      const idx = characters.value.findIndex(c => c.id === characterId)
      if (idx !== -1) characters.value[idx] = updated
      return updated
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '更新角色失败'
      loadError.value = msg
      throw e
    }
  }

  async function removeCharacter(projectId: string, characterId: string) {
    try {
      await api.deleteCharacter(projectId, characterId)
      characters.value = characters.value.filter(c => c.id !== characterId)
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '删除角色失败'
      loadError.value = msg
      throw e
    }
  }

  return { characters, loading, loadError, charactersForProject, loadCharacters, addCharacter, updateCharacter, removeCharacter }
})

// ─── Outline store ───

function apiOutlineToOutlineItem(ao: ApiOutline): OutlineItem {
  return {
    id: ao.id,
    project_id: ao.project_id,
    chapter_num: ao.sequence_number,
    title: ao.title,
    summary: ao.summary ?? '',
    turning_point: ao.turning_point ?? null,
    hidden_thread_ids: ao.hidden_thread_ids ?? [],
  }
}

export const useOutlineStore = defineStore('outline', () => {
  const outlines = ref<OutlineItem[]>([...MOCK_OUTLINE])
  const loading = ref(false)
  const loadError = ref('')

  function entriesForProject(projectId: string): OutlineItem[] {
    return outlines.value.filter(o => o.project_id === projectId)
  }

  async function loadOutlines(projectId: string) {
    loading.value = true
    loadError.value = ''
    try {
      const list = await api.listOutlines(projectId)
      const otherOutlines = outlines.value.filter(o => o.project_id !== projectId)
      outlines.value = [...otherOutlines, ...list.map(apiOutlineToOutlineItem)]
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载大纲失败'
      loadError.value = msg
      // Fallback: keep mock data for this project if present
    } finally {
      loading.value = false
    }
  }

  async function createOutlineItem(projectId: string, payload: { sequence_number: number; title: string; summary?: string; turning_point?: string }): Promise<OutlineItem | null> {
    try {
      const ao = await api.createOutline(projectId, payload)
      const item = apiOutlineToOutlineItem(ao)
      outlines.value.push(item)
      return item
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '创建大纲失败'
      loadError.value = msg
      throw e
    }
  }

  async function updateOutlineItem(projectId: string, outlineId: string, payload: OutlineUpdatePayload) {
    try {
      const ao = await api.updateOutline(projectId, outlineId, payload)
      const updated = apiOutlineToOutlineItem(ao)
      const idx = outlines.value.findIndex(o => o.id === outlineId)
      if (idx !== -1) outlines.value[idx] = updated
      return updated
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '更新大纲失败'
      loadError.value = msg
      throw e
    }
  }

  async function deleteOutlineItem(projectId: string, outlineId: string) {
    try {
      await api.deleteOutline(projectId, outlineId)
      outlines.value = outlines.value.filter(o => o.id !== outlineId)
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '删除大纲失败'
      loadError.value = msg
      throw e
    }
  }

  return { outlines, loading, loadError, entriesForProject, loadOutlines, createOutlineItem, updateOutlineItem, deleteOutlineItem }
})

// ─── HiddenThread store ───

function apiHiddenThreadToHiddenThread(aht: ApiHiddenThread): HiddenThread {
  return {
    id: aht.id,
    project_id: aht.project_id,
    name: aht.name,
    description: aht.description ?? '',
    chapter_nums: aht.chapter_nums ?? [],
    created_at: aht.created_at,
    updated_at: aht.updated_at,
  }
}

export const useHiddenThreadStore = defineStore('hiddenThread', () => {
  const hiddenThreads = ref<HiddenThread[]>([...MOCK_HIDDEN_THREADS])
  const loading = ref(false)
  const loadError = ref('')

  function threadsForProject(projectId: string): HiddenThread[] {
    return hiddenThreads.value.filter(ht => ht.project_id === projectId)
  }

  async function loadHiddenThreads(projectId: string) {
    loading.value = true
    loadError.value = ''
    try {
      const list = await api.listHiddenThreads(projectId)
      const otherThreads = hiddenThreads.value.filter(ht => ht.project_id !== projectId)
      hiddenThreads.value = [...otherThreads, ...list.map(apiHiddenThreadToHiddenThread)]
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载暗线失败'
      loadError.value = msg
      // Fallback: keep mock data for this project if present
    } finally {
      loading.value = false
    }
  }

  async function createHiddenThreadItem(projectId: string, payload: { name: string; description?: string; chapter_nums?: number[] }): Promise<HiddenThread | null> {
    try {
      const aht = await api.createHiddenThread(projectId, payload)
      const item = apiHiddenThreadToHiddenThread(aht)
      hiddenThreads.value.push(item)
      return item
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '创建暗线失败'
      loadError.value = msg
      throw e
    }
  }

  async function updateHiddenThreadItem(projectId: string, threadId: string, payload: HiddenThreadUpdatePayload) {
    try {
      const aht = await api.updateHiddenThread(projectId, threadId, payload)
      const updated = apiHiddenThreadToHiddenThread(aht)
      const idx = hiddenThreads.value.findIndex(ht => ht.id === threadId)
      if (idx !== -1) hiddenThreads.value[idx] = updated
      return updated
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '更新暗线失败'
      loadError.value = msg
      throw e
    }
  }

  async function deleteHiddenThreadItem(projectId: string, threadId: string) {
    try {
      await api.deleteHiddenThread(projectId, threadId)
      hiddenThreads.value = hiddenThreads.value.filter(ht => ht.id !== threadId)
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '删除暗线失败'
      loadError.value = msg
      throw e
    }
  }

  return { hiddenThreads, loading, loadError, threadsForProject, loadHiddenThreads, createHiddenThreadItem, updateHiddenThreadItem, deleteHiddenThreadItem }
})

export interface Toast {
  id: number
  message: string
  type: 'success' | 'error' | 'info'
}

export const useUiStore = defineStore('ui', () => {
  const toasts = ref<Toast[]>([])
  let nextId = 0

  function showToast(message: string, type: Toast['type'] = 'info') {
    const id = nextId++
    toasts.value.push({ id, message, type })
    setTimeout(() => {
      toasts.value = toasts.value.filter(t => t.id !== id)
    }, 4000)
  }

  return { toasts, showToast }
})

/** Helper: extract friendly message from ApiError or generic Error */
export function friendlyError(e: unknown, fallback: string): string {
  if (e instanceof ApiError) {
    // 401 means session expired — redirect handled elsewhere
    if (e.status === 401) return '登录已失效，请重新登录'
    return e.message
  }
  if (e instanceof Error) return e.message || fallback
  return fallback
}

export { useLLMSettingsStore } from './llmSettings'