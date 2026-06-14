import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import type { Project, Chapter, DocumentUnit, Expert, WorkflowStep, ReviewComment, CharacterRelation, CharacterEvent, ProjectMode, ExpertCreatePayload, WorldEntry, Character, OutlineItem, HiddenThread, ChapterVersion, DocumentRevision, DiffHunk, GenerationRecord } from '../api/types'
import type { ApiProject, ApiChapter, ApiDocument, ApiExpert, ApiWorldEntry, ApiCharacter, ApiCharacterRelation, ApiOutline, ApiHiddenThread, ApiChapterVersion, ApiDocumentVersion, ApiGenerationRecordListItem, ApiGenerationRecord } from '../api/types'
import type { CharacterRelationCreatePayload, CharacterRelationUpdatePayload, CharacterEventUpsertPayload, OutlineUpdatePayload, HiddenThreadUpdatePayload, WorldEntryCreatePayload, WorldEntryUpdatePayload, CharacterCreatePayload, CharacterUpdatePayload, CharacterMergePayload, ProjectUpdatePayload } from '../api/types'
import { api, ApiError } from '../api/client'
import { MOCK_PROJECTS, MOCK_CHAPTERS, DEFAULT_EXPERTS, MOCK_REVIEW_COMMENTS, MOCK_CHARACTER_RELATIONS, MOCK_WORLD_ENTRIES, MOCK_CHARACTERS, MOCK_OUTLINE, MOCK_HIDDEN_THREADS } from '../mock/data'

// ─── API → UI conversion helpers ───

function apiProjectToProject(ap: ApiProject): Project {
  return {
    id: ap.id,
    title: ap.title,
    genre: ap.genre ?? '',
    style: ap.style ?? '',
    overall_outline: ap.overall_outline ?? '',
    status: ap.status,
    mode: ap.mode as ProjectMode,
    description: ap.description ?? '',
    target_words: ap.target_words,
    created_at: ap.created_at,
    updated_at: ap.updated_at,
  }
}

const DRAFT_STATUS_MAP: Record<string, Chapter['status']> = {
  draft: 'draft',
  writing: 'draft',
  reviewing: 'reviewing',
  revision: 'revision',
  final: 'final',
  approved: 'final',
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
    status: DRAFT_STATUS_MAP[ac.status] ?? 'draft',
    review_comment_ids: [],
    review_round: 0,
  }
}

function apiDocumentToDocumentUnit(doc: ApiDocument, projectId: string): DocumentUnit {
  return {
    id: doc.id,
    project_id: projectId,
    position: doc.position,
    title: doc.title,
    draft: doc.content ?? '',
    final_text: '',
    status: DRAFT_STATUS_MAP[doc.status] ?? 'draft',
    word_count: doc.word_count,
    created_at: doc.created_at,
    updated_at: doc.updated_at,
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
      genre: payload.genre,
      style: payload.style,
      target_words: payload.target_words,
      mode: payload.mode,
    })
    const p = apiProjectToProject(ap)
    projects.value.unshift(p)
    return p
  }

  async function deleteProjectRemote(projectId: string): Promise<void> {
    await api.deleteProject(projectId)
    projects.value = projects.value.filter(p => p.id !== projectId)
    if (currentProjectId.value === projectId) currentProjectId.value = null
  }

  async function updateProjectRemote(projectId: string, payload: ProjectUpdatePayload): Promise<Project> {
    const updated = apiProjectToProject(await api.updateProject(projectId, payload))
    const idx = projects.value.findIndex(p => p.id === projectId)
    if (idx !== -1) projects.value[idx] = updated
    return updated
  }

  return {
    projects, currentProjectId, currentProject, loadError,
    setCurrent, addProject, createProject, loadProjects, createProjectRemote, updateProjectRemote, deleteProjectRemote,
  }
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

  function nextChapterNum(projectId: string): number {
    const projectChapters = chaptersForProject(projectId)
    if (!projectChapters.length) return 1
    return Math.max(...projectChapters.map(c => c.chapter_num)) + 1
  }

  async function loadChapters(projectId: string) {
    loadError.value = ''
    try {
      const list = (await api.listChapters(projectId)).map(ac => apiChapterToChapter(ac, projectId))
      // Replace chapters for this project, keep others
      const otherChapters = chapters.value.filter(c => c.project_id !== projectId)
      chapters.value = [...otherChapters, ...list]
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载章节失败'
      loadError.value = msg
    }
  }

  async function createChapterRemote(projectId: string, payload: { title: string; chapter_num: number; summary?: string }): Promise<Chapter | null> {
    const request = {
      title: payload.title,
      outline: payload.summary,
      sequence_number: payload.chapter_num,
    }
    const ch = apiChapterToChapter(await api.createChapter(projectId, request), projectId)
    chapters.value.push(ch)
    return ch
  }

  async function saveCurrentChapter(projectId: string) {
    const ch = currentChapterForProject(projectId)
    if (!ch) return
    saving.value = true
    try {
      const request = {
        content: ch.draft,
        outline: ch.summary || undefined,
        status: ch.status,
      }
      // Sync local state from response
      const updated = apiChapterToChapter(await api.updateChapter(projectId, ch.chapter_num, request), projectId)
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

  async function updateChapterTitle(projectId: string, chapterNum: number, title: string) {
    const updated = apiChapterToChapter(await api.updateChapter(projectId, chapterNum, { title }), projectId)
    const idx = chapters.value.findIndex(c => c.chapter_num === chapterNum && c.project_id === projectId)
    if (idx !== -1) chapters.value[idx] = updated
  }

  async function deleteChapterRemote(projectId: string, chapterNum: number) {
    await api.deleteChapter(projectId, chapterNum)
    chapters.value = chapters.value.filter(c => !(c.chapter_num === chapterNum && c.project_id === projectId))
    if (currentChapterNum.value === chapterNum) {
      currentChapterNum.value = 0
    }
  }

  return {
    chapters, reviewComments, currentChapterNum, loadError, saving,
    chaptersForProject, currentChapterForProject, reviewCommentsForChapter,
    setCurrentChapter, updateDraft, nextChapterNum,
    loadChapters, createChapterRemote, saveCurrentChapter, updateChapterTitle, deleteChapterRemote,
  }
})

export const useDocumentStore = defineStore('document', () => {
  const documents = ref<DocumentUnit[]>([])
  const currentDocumentPosition = ref(1)
  const loadError = ref('')
  const saving = ref(false)

  function documentsForProject(projectId: string): DocumentUnit[] {
    return documents.value.filter(d => d.project_id === projectId)
  }

  function currentDocumentForProject(projectId: string): DocumentUnit | undefined {
    return documents.value.find(d => d.project_id === projectId && d.position === currentDocumentPosition.value)
  }

  function setCurrentDocument(position: number) {
    currentDocumentPosition.value = position
  }

  function ensureCurrentDocument(projectId: string) {
    const projectDocs = documentsForProject(projectId)
    if (!projectDocs.length) {
      currentDocumentPosition.value = 0
      return
    }
    const hasCurrent = projectDocs.some(d => d.position === currentDocumentPosition.value)
    if (!hasCurrent) {
      currentDocumentPosition.value = projectDocs[0].position
    }
  }

  function updateDraft(projectId: string, text: string) {
    const doc = currentDocumentForProject(projectId)
    if (doc) doc.draft = text
  }

  function nextDocumentPosition(projectId: string): number {
    const projectDocs = documentsForProject(projectId)
    if (!projectDocs.length) return 1
    return Math.max(...projectDocs.map(d => d.position)) + 1
  }

  async function loadDocuments(projectId: string) {
    loadError.value = ''
    try {
      const list = await api.listDocuments(projectId)
      const otherDocs = documents.value.filter(d => d.project_id !== projectId)
      documents.value = [...otherDocs, ...list.map(doc => apiDocumentToDocumentUnit(doc, projectId))]
      ensureCurrentDocument(projectId)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载稿件失败'
      loadError.value = msg
    }
  }

  async function createDocumentRemote(projectId: string, payload: { title: string; position: number }): Promise<DocumentUnit | null> {
    const doc = apiDocumentToDocumentUnit(await api.createDocument(projectId, {
      title: payload.title,
      position: payload.position,
    }), projectId)
    documents.value.push(doc)
    return doc
  }

  async function saveCurrentDocument(projectId: string) {
    const doc = currentDocumentForProject(projectId)
    if (!doc) {
      const error = new Error('请先选择一个稿件')
      loadError.value = error.message
      throw error
    }
    saving.value = true
    try {
      const updated = apiDocumentToDocumentUnit(await api.updateDocument(projectId, doc.id, {
        content: doc.draft,
        status: doc.status,
      }), projectId)
      const idx = documents.value.findIndex(d => d.id === doc.id)
      if (idx !== -1) documents.value[idx] = updated
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '保存失败'
      loadError.value = msg
      throw e
    } finally {
      saving.value = false
    }
  }

  async function updateDocumentTitle(projectId: string, position: number, title: string) {
    const doc = documents.value.find(d => d.project_id === projectId && d.position === position)
    if (!doc) throw new Error('稿件不存在')
    const updated = apiDocumentToDocumentUnit(await api.updateDocument(projectId, doc.id, { title }), projectId)
    const idx = documents.value.findIndex(d => d.id === doc.id)
    if (idx !== -1) documents.value[idx] = updated
  }

  async function deleteDocumentRemote(projectId: string, position: number) {
    const doc = documents.value.find(d => d.project_id === projectId && d.position === position)
    if (!doc) throw new Error('稿件不存在')
    await api.deleteDocument(projectId, doc.id)
    documents.value = documents.value.filter(d => d.id !== doc.id)
    if (currentDocumentPosition.value === position) {
      ensureCurrentDocument(projectId)
    }
  }

  return {
    documents, currentDocumentPosition, loadError, saving,
    documentsForProject, currentDocumentForProject, setCurrentDocument, updateDraft,
    nextDocumentPosition, loadDocuments, createDocumentRemote, saveCurrentDocument,
    updateDocumentTitle, deleteDocumentRemote,
  }
})

export interface ExpertProjectState {
  isGenerating: boolean
  streamOutput: string
  finalDraft: string
  workflowSteps: WorkflowStep[]
  expertSkills: Record<string, string>
  revisionCount: number
  maxRevisions: number
}

const EMPTY_EXPERT_STATE: ExpertProjectState = {
  isGenerating: false,
  streamOutput: '',
  finalDraft: '',
  workflowSteps: [],
  expertSkills: {},
  revisionCount: 0,
  maxRevisions: 3,
}

export const useExpertStore = defineStore('expert', () => {
  const experts = ref<Expert[]>([])
  const activeExpertId = ref<string | null>(null)
  const states = ref<Record<string, ExpertProjectState>>({})
  const loading = ref(false)
  const loadError = ref('')

  const activeExpert = computed(() => experts.value.find(e => e.id === activeExpertId.value))

  function getExpertName(id: string): string {
    return experts.value.find(e => e.id === id)?.name ?? id
  }

  function setActive(id: string) {
    activeExpertId.value = id
  }

  // ─── Per-project generation state ───

  function getState(pid: string): ExpertProjectState {
    return states.value[pid] ?? { ...EMPTY_EXPERT_STATE, workflowSteps: [] }
  }

  function ensureState(pid: string): ExpertProjectState {
    if (!states.value[pid]) {
      states.value[pid] = { ...EMPTY_EXPERT_STATE, workflowSteps: [] }
    }
    return states.value[pid]
  }

  function startGenerating(pid: string) {
    const s = ensureState(pid)
    s.isGenerating = true
    s.streamOutput = ''
    s.finalDraft = ''
    s.workflowSteps = []
    s.expertSkills = {}
    s.revisionCount = 0
    s.maxRevisions = 3
  }

  function stopGenerating(pid: string, cancelled = false) {
    const s = ensureState(pid)
    s.isGenerating = false
    if (cancelled) {
      for (const step of s.workflowSteps) {
        if (step.status === 'running') {
          step.status = 'cancelled'
        }
      }
    }
  }

  function appendOutput(pid: string, text: string) {
    ensureState(pid).streamOutput += text
  }

  function appendDraft(pid: string, token: string) {
    ensureState(pid).finalDraft += token
  }

  function setDraft(pid: string, text: string) {
    ensureState(pid).finalDraft = text
  }

  function setWorkflowSteps(pid: string, steps: WorkflowStep[]) {
    ensureState(pid).workflowSteps = steps
  }

  function updateStepStatus(pid: string, stepId: string, status: WorkflowStep['status']) {
    const step = ensureState(pid).workflowSteps.find(s => s.id === stepId)
    if (step) step.status = status
  }

  function clearProjectState(pid: string) {
    delete states.value[pid]
  }

  function setExpertSkill(pid: string, expert: string, skill: string) {
    ensureState(pid).expertSkills[expert] = skill
  }

  function setRevisionInfo(pid: string, revisionCount: number, maxRevisions: number) {
    const s = ensureState(pid)
    s.revisionCount = revisionCount
    s.maxRevisions = maxRevisions
  }

  // ─── Expert config (shared across projects) ───

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

  return {
    experts, activeExpertId, activeExpert, states, loading, loadError,
    getExpertName, setActive,
    getState, startGenerating, stopGenerating, appendOutput, appendDraft, setDraft,
    setWorkflowSteps, updateStepStatus, clearProjectState,
      setExpertSkill, setRevisionInfo,
    loadExperts, addExpert, addCustomExpert, toggleExpert,
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
    scope_type: awe.scope_type === 'chapter' ? 'chapter' : 'global',
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
const CHARACTER_SCOPE_MAP: Record<string, Character['scope_type']> = {
  core: 'core',
  recurring: 'recurring',
  chapter: 'chapter',
  cameo: 'cameo',
}

function apiCharacterToCharacter(ac: ApiCharacter): Character {
  return {
    id: ac.id,
    project_id: ac.project_id,
    name: ac.name,
    role_type: ROLE_TYPE_MAP[ac.role_type] ?? 'minor',
    scope_type: CHARACTER_SCOPE_MAP[ac.scope_type] ?? 'recurring',
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

  async function mergeCharacter(projectId: string, characterId: string, payload: CharacterMergePayload) {
    try {
      const ac = await api.mergeCharacter(projectId, characterId, payload)
      const merged = apiCharacterToCharacter(ac)
      characters.value = characters.value.filter(c => c.id !== characterId)
      const idx = characters.value.findIndex(c => c.id === merged.id)
      if (idx !== -1) characters.value[idx] = merged
      else characters.value.push(merged)
      return merged
    } catch (e: unknown) {
      const msg = e instanceof ApiError ? e.message : '合并角色失败'
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

  return { characters, loading, loadError, charactersForProject, loadCharacters, addCharacter, updateCharacter, mergeCharacter, removeCharacter }
})

// ─── Character event store ───
export const useCharacterEventStore = defineStore('characterEvent', () => {
  const events = ref<CharacterEvent[]>([])
  const loading = ref(false)
  const loadError = ref('')

  function eventsForProject(projectId: string): CharacterEvent[] {
    return events.value.filter(event => event.project_id === projectId)
  }

  function eventsForCharacter(projectId: string, characterId: string): CharacterEvent[] {
    return events.value
      .filter(event => event.project_id === projectId && event.character_id === characterId)
      .sort((a, b) => a.chapter_sequence_number - b.chapter_sequence_number)
  }

  async function loadCharacterEvents(projectId: string) {
    loading.value = true
    loadError.value = ''
    try {
      const list = await api.listCharacterEvents(projectId)
      const others = events.value.filter(event => event.project_id !== projectId)
      events.value = [...others, ...list]
    } catch (e: unknown) {
      loadError.value = e instanceof ApiError ? e.message : '加载角色事件失败'
      throw e
    } finally {
      loading.value = false
    }
  }

  async function upsertCharacterEvent(projectId: string, characterId: string, sequenceNumber: number, payload: CharacterEventUpsertPayload) {
    const updated = await api.upsertCharacterEvent(projectId, characterId, sequenceNumber, payload)
    const idx = events.value.findIndex(event => event.id === updated.id)
    if (idx !== -1) events.value[idx] = updated
    else events.value.push(updated)
    return updated
  }

  async function removeCharacterEvent(projectId: string, characterId: string, sequenceNumber: number) {
    await api.deleteCharacterEvent(projectId, characterId, sequenceNumber)
    events.value = events.value.filter(event => {
      return !(event.project_id === projectId && event.character_id === characterId && event.chapter_sequence_number === sequenceNumber)
    })
  }

  return { events, loading, loadError, eventsForProject, eventsForCharacter, loadCharacterEvents, upsertCharacterEvent, removeCharacterEvent }
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

// ─── Chapter Version History store ───

function apiChapterVersionToVersion(av: ApiChapterVersion): ChapterVersion {
  return {
    id: av.id,
    chapterId: av.chapter_id,
    versionNumber: av.version_number,
    wordCount: av.word_count,
    source: av.source,
    createdAt: av.created_at,
    content: av.content ?? null,
  }
}

export const useChapterVersionStore = defineStore('chapterVersion', () => {
  const versions = ref<ChapterVersion[]>([])
  const loading = ref(false)
  const loadError = ref('')
  const diffResult = ref<{ versionA: number; versionB: number; leftTitle: string; rightTitle: string; diff: DiffHunk[] } | null>(null)

  async function loadVersions(projectId: string, sequenceNumber: number) {
    loading.value = true
    loadError.value = ''
    try {
      versions.value = (await api.listChapterVersions(projectId, sequenceNumber)).map(apiChapterVersionToVersion)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载版本历史失败'
      loadError.value = msg
    } finally {
      loading.value = false
    }
  }

  async function getVersionContent(projectId: string, sequenceNumber: number, versionId: string): Promise<string | null> {
    try {
      const av = await api.getChapterVersion(projectId, sequenceNumber, versionId)
      return av.content ?? null
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载版本内容失败'
      loadError.value = msg
      return null
    }
  }

  async function loadDiff(projectId: string, sequenceNumber: number, versionIdA: string, versionIdB: string) {
    loading.value = true
    loadError.value = ''
    try {
      const result = await api.diffChapterVersions(projectId, sequenceNumber, {
        version_id_a: versionIdA,
        version_id_b: versionIdB,
      })
      diffResult.value = {
        versionA: result.version_a,
        versionB: result.version_b,
        leftTitle: `历史 v${result.version_a}`,
        rightTitle: result.version_b === 0 ? '当前正文' : `历史 v${result.version_b}`,
        diff: result.diff,
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载版本对比失败'
      loadError.value = msg
    } finally {
      loading.value = false
    }
  }

  async function loadDiffWithCurrent(projectId: string, sequenceNumber: number, versionId: string, versionNumber: number, currentContent: string) {
    loading.value = true
    loadError.value = ''
    try {
      const result = await api.diffChapterVersions(projectId, sequenceNumber, {
        version_id_a: versionId,
        current_content: currentContent,
      })
      diffResult.value = {
        versionA: result.version_a,
        versionB: result.version_b,
        leftTitle: `历史 v${versionNumber}`,
        rightTitle: '当前正文',
        diff: result.diff,
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载版本对比失败'
      loadError.value = msg
    } finally {
      loading.value = false
    }
  }

  function clearState() {
    versions.value = []
    loading.value = false
    loadError.value = ''
    diffResult.value = null
  }

  return { versions, loading, loadError, diffResult, loadVersions, getVersionContent, loadDiff, loadDiffWithCurrent, clearState }
})

// ─── Document Revision History store ───

function apiDocumentVersionToRevision(av: ApiDocumentVersion): DocumentRevision {
  return {
    id: av.id,
    documentId: av.document_id,
    versionNumber: av.version_number,
    wordCount: av.word_count,
    source: av.source,
    createdAt: av.created_at,
    content: av.content ?? null,
  }
}

export const useDocumentRevisionStore = defineStore('documentRevision', () => {
  const revisions = ref<DocumentRevision[]>([])
  const loading = ref(false)
  const loadError = ref('')
  const diffResult = ref<{ versionA: number; versionB: number; leftTitle: string; rightTitle: string; diff: DiffHunk[] } | null>(null)

  async function loadRevisions(projectId: string, documentId: string) {
    loading.value = true
    loadError.value = ''
    try {
      revisions.value = (await api.listDocumentVersions(projectId, documentId)).map(apiDocumentVersionToRevision)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载修订历史失败'
      loadError.value = msg
    } finally {
      loading.value = false
    }
  }

  async function getRevisionContent(projectId: string, documentId: string, revisionId: string): Promise<string | null> {
    try {
      const revision = await api.getDocumentVersion(projectId, documentId, revisionId)
      return revision.content ?? null
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载修订内容失败'
      loadError.value = msg
      return null
    }
  }

  async function restoreRevision(projectId: string, documentId: string, revisionId: string): Promise<DocumentUnit | null> {
    try {
      const doc = await api.restoreDocumentVersion(projectId, documentId, revisionId)
      return apiDocumentToDocumentUnit(doc, projectId)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '恢复修订失败'
      loadError.value = msg
      return null
    }
  }

  async function loadDiff(projectId: string, documentId: string, revisionIdA: string, revisionIdB: string) {
    loading.value = true
    loadError.value = ''
    try {
      const result = await api.diffDocumentVersions(projectId, documentId, {
        version_id_a: revisionIdA,
        version_id_b: revisionIdB,
      })
      diffResult.value = {
        versionA: result.version_a,
        versionB: result.version_b,
        leftTitle: `修订 v${result.version_a}`,
        rightTitle: result.version_b === 0 ? '当前正文' : `修订 v${result.version_b}`,
        diff: result.diff,
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载修订对比失败'
      loadError.value = msg
    } finally {
      loading.value = false
    }
  }

  async function loadDiffWithCurrent(projectId: string, documentId: string, revisionId: string, revisionNumber: number, currentContent: string) {
    loading.value = true
    loadError.value = ''
    try {
      const result = await api.diffDocumentVersions(projectId, documentId, {
        version_id_a: revisionId,
        current_content: currentContent,
      })
      diffResult.value = {
        versionA: result.version_a,
        versionB: result.version_b,
        leftTitle: `修订 v${revisionNumber}`,
        rightTitle: '当前正文',
        diff: result.diff,
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载修订对比失败'
      loadError.value = msg
    } finally {
      loading.value = false
    }
  }

  function clearState() {
    revisions.value = []
    loading.value = false
    loadError.value = ''
    diffResult.value = null
  }

  return { revisions, loading, loadError, diffResult, loadRevisions, getRevisionContent, restoreRevision, loadDiff, loadDiffWithCurrent, clearState }
})

// ─── AI Generation History store ───

function apiGenerationRecordToRecord(record: ApiGenerationRecordListItem | ApiGenerationRecord): GenerationRecord {
  return {
    id: record.id,
    projectId: record.project_id,
    chapterId: record.chapter_id,
    documentId: record.document_id,
    mode: record.mode,
    expertId: record.expert_id,
    direction: record.direction,
    wordCount: record.word_count,
    status: record.status,
    langfuseTraceId: record.langfuse_trace_id ?? null,
    createdAt: record.created_at,
    content: 'content' in record ? record.content : null,
  }
}

export const useGenerationHistoryStore = defineStore('generationHistory', () => {
  const records = ref<GenerationRecord[]>([])
  const loading = ref(false)
  const loadError = ref('')
  const diffResult = ref<{ versionA: number; versionB: number; leftTitle: string; rightTitle: string; diff: DiffHunk[] } | null>(null)

  async function loadChapterRecords(projectId: string, sequenceNumber: number) {
    loading.value = true
    loadError.value = ''
    try {
      records.value = (await api.listChapterGenerations(projectId, sequenceNumber)).map(apiGenerationRecordToRecord)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载 AI 生成历史失败'
      loadError.value = msg
    } finally {
      loading.value = false
    }
  }

  async function loadDocumentRecords(projectId: string, documentId: string) {
    loading.value = true
    loadError.value = ''
    try {
      records.value = (await api.listDocumentGenerations(projectId, documentId)).map(apiGenerationRecordToRecord)
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载 AI 生成历史失败'
      loadError.value = msg
    } finally {
      loading.value = false
    }
  }

  async function getRecordContent(projectId: string, recordId: string): Promise<string | null> {
    try {
      const detail = await api.getGenerationRecord(projectId, recordId)
      const record = apiGenerationRecordToRecord(detail)
      const idx = records.value.findIndex(r => r.id === recordId)
      if (idx !== -1) records.value[idx] = record
      return record.content
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载生成内容失败'
      loadError.value = msg
      return null
    }
  }

  async function updateRecordStatus(projectId: string, recordId: string, status: GenerationRecord['status']) {
    try {
      const updated = apiGenerationRecordToRecord(await api.updateGenerationRecord(projectId, recordId, { status }))
      const idx = records.value.findIndex(r => r.id === recordId)
      if (idx !== -1) records.value[idx] = updated
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '更新生成历史失败'
      loadError.value = msg
    }
  }

  async function markLatestRecord(projectId: string, status: GenerationRecord['status']) {
    const latest = records.value[0]
    if (!latest) return
    await updateRecordStatus(projectId, latest.id, status)
  }

  async function loadDiffWithCurrent(projectId: string, recordId: string, currentContent: string) {
    loading.value = true
    loadError.value = ''
    try {
      const result = await api.diffGenerationRecord(projectId, recordId, { current_content: currentContent })
      diffResult.value = {
        versionA: 0,
        versionB: 0,
        leftTitle: 'AI生成',
        rightTitle: '当前正文',
        diff: result.diff,
      }
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : '加载生成对比失败'
      loadError.value = msg
    } finally {
      loading.value = false
    }
  }

  function upsertCandidate(projectId: string, recordId: string) {
    if (records.value.some(r => r.id === recordId)) return
    records.value.unshift({
      id: recordId,
      projectId,
      chapterId: null,
      documentId: null,
      mode: 'full_pipeline',
      expertId: null,
      direction: null,
      wordCount: 0,
      status: 'candidate',
      langfuseTraceId: null,
      createdAt: new Date().toISOString(),
      content: null,
    })
  }

  function clearState() {
    records.value = []
    loading.value = false
    loadError.value = ''
    diffResult.value = null
  }

  return {
    records, loading, loadError, diffResult,
    loadChapterRecords, loadDocumentRecords, getRecordContent,
    updateRecordStatus, markLatestRecord, loadDiffWithCurrent, upsertCandidate, clearState,
  }
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
      dismissToast(id)
    }, 4000)
  }

  function dismissToast(id: number) {
    toasts.value = toasts.value.filter(t => t.id !== id)
  }

  return { toasts, showToast, dismissToast }
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
