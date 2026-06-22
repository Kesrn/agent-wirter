import {
  API_BASE_URL,
  type ApiProject, type ApiChapter, type ApiDocument, type ApiDocumentVersion, type ApiExpert, type ApiWorldEntry, type ApiCharacter,
  type ApiCharacterRelation, type ApiCharacterEvent, type ApiOutline, type ApiHiddenThread,
  type ProjectCreatePayload, type ProjectUpdatePayload, type ChapterCreatePayload, type DocumentCreatePayload, type DocumentUpdatePayload, type ExpertCreatePayload,
  type WorldEntryCreatePayload, type WorldEntryUpdatePayload,
  type CharacterCreatePayload, type CharacterUpdatePayload, type CharacterMergePayload,
  type CharacterRelationCreatePayload, type CharacterRelationUpdatePayload, type CharacterEventUpsertPayload,
  type OutlineCreatePayload, type OutlineUpdatePayload,
  type HiddenThreadCreatePayload, type HiddenThreadUpdatePayload,
  type GenerateRequest, type SSEEnvelope,
  type ExpertUpdatePayload, type ProjectMode,
  type LLMConfigCreatePayload, type ModelListRequest,
  type LLMConfigResponse, type LLMStatusResponse, type ModelInfo,
  type LoginRequest, type LoginResponse, type MeResponse,
  type RegisterRequest, type RegisterResponse,
  type ProjectImageUploadResponse,
  type TxtImportResponse, type ChapterStructureExtractRequest, type ChapterStructureExtractResponse,
  type ApiChapterVersion, type ChapterVersionDiffRequest, type ChapterVersionDiffResponse,
  type DocumentVersionDiffRequest, type DocumentVersionDiffResponse,
  type ApiGenerationRecordListItem, type ApiGenerationRecord,
  type GenerationRecordUpdatePayload, type GenerationRecordDiffRequest, type GenerationRecordDiffResponse,
  type ChapterReviewNote, type ChapterReviewNoteCreatePayload, type ChapterReviewNoteUpdatePayload,
  type ApiEvaluationDataset, type ApiEvaluationCase, type ApiEvaluationRun,
  type EvaluationDatasetCreatePayload, type EvaluationDatasetUpdatePayload,
  type EvaluationCaseCreatePayload, type EvaluationCaseUpdatePayload, type EvaluationRunCreatePayload,
} from './types'
import { clearAuthSession, getAuthToken } from '../utils/authSession'

function getToken(): string | null {
  return getAuthToken()
}

function authHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

function binaryAuthHeaders(contentType: string): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': contentType }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

function formAuthHeaders(): Record<string, string> {
  const headers: Record<string, string> = {}
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

function redirectToLogin(): void {
  if (import.meta.env.VITE_DESKTOP === 'true') {
    window.location.hash = '#/login'
    return
  }
  window.location.href = '/login'
}

/** Parse backend error body into a user-friendly Chinese message */
function parseApiError(status: number, body: string): string {
  const statusMap: Record<number, string> = {
    400: '请求参数有误',
    401: '登录已失效，请重新登录',
    403: '没有操作权限',
    404: '资源不存在',
    422: '输入内容不符合要求',
    500: '服务器异常，请稍后重试',
  }

  // Try to parse JSON body
  let parsed: unknown
  try { parsed = JSON.parse(body) } catch { /* not JSON */ }

  if (parsed && typeof parsed === 'object') {
    const detail = (parsed as Record<string, unknown>).detail
    // detail is a plain string — likely a Chinese business error message
    if (typeof detail === 'string' && detail.trim()) return detail.trim()
    // detail is a Pydantic validation error array
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as Record<string, unknown> | undefined
      if (first) {
        const msg = first.msg as string | undefined
        if (msg) {
          // Translate common Pydantic messages
          if (msg.includes('required') || msg.includes('missing')) return '请填写必填项'
          if (msg.includes('valid') || msg.includes('allowed')) return '输入内容不符合要求'
          return msg
        }
      }
      return '输入内容不符合要求'
    }
  }

  // Fallback to status-based message
  return statusMap[status] ?? `请求失败 (${status})`
}

export class ApiError extends Error {
  status: number
  constructor(status: number, message: string) {
    super(message)
    this.status = status
    this.name = 'ApiError'
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_BASE_URL}${path}`, {
    headers: authHeaders(),
    ...init,
  })
  if (!res.ok) {
    const body = await res.text().catch(() => '')
    if (res.status === 401) {
      clearAuthSession()
      redirectToLogin()
    }
    throw new ApiError(res.status, parseApiError(res.status, body))
  }
  if (res.status === 204 || res.headers.get('content-length') === '0') {
    return undefined as T
  }
  return res.json()
}

/** Parse a single SSE block into event + data */
function parseSSEBlock(block: string): SSEEnvelope | null {
  let event = ''
  let data = ''
  for (const line of block.split('\n')) {
    if (line.startsWith('event: ')) event = line.slice(7)
    else if (line.startsWith('data: ')) data = line.slice(6)
  }
  if (!event && !data) return null
  let parsed: Record<string, unknown> | string
  try { parsed = JSON.parse(data) } catch { parsed = data }
  return { event: event as SSEEnvelope['event'], data: parsed }
}

/** Parse standard SSE stream (event + data blocks separated by \n\n) */
async function parseSSEStream(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  onEvent: (envelope: SSEEnvelope) => void,
): Promise<void> {
  const decoder = new TextDecoder()
  let buffer = ''
  while (true) {
    const { done, value } = await reader.read()
    if (value) buffer += decoder.decode(value, { stream: true })
    const blocks = buffer.split('\n\n')
    buffer = blocks.pop() ?? ''
    for (const block of blocks) {
      const envelope = parseSSEBlock(block)
      if (envelope) onEvent(envelope)
    }
    if (done) {
      // Flush remaining buffer — last block may not end with \n\n
      if (buffer.trim()) {
        const envelope = parseSSEBlock(buffer)
        if (envelope) onEvent(envelope)
      }
      return
    }
  }
}

function sseHeaders(): Record<string, string> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' }
  const token = getToken()
  if (token) headers['Authorization'] = `Bearer ${token}`
  return headers
}

// 抽取任务控制相关类型
export type ExtractionStatus = {
  job_id: string | null; status: string; total_chapters: number;
  extracted_count: number; validated_count: number;
  merged_count: number; failed_count: number; pending_count: number;
  error_message: string | null;
  provider: string | null; is_mock: boolean; last_run_outcome: string;
  current_chapter_no: number | null; last_error: string | null;
  paused_at: string | null; cancelled_at: string | null;
  last_run_started_at: string | null; last_run_finished_at: string | null;
}

export type ExtractionFailure = {
  chapter_no: number; chapter_title: string | null;
  status: string; retry_count: number;
  error_message: string | null; updated_at: string | null;
}

export const api = {
  // ─── Auth ───
  login: (data: LoginRequest) =>
    request<LoginResponse>('/auth/login', { method: 'POST', body: JSON.stringify(data) }),
  register: (data: RegisterRequest) =>
    request<RegisterResponse>('/auth/register', { method: 'POST', body: JSON.stringify(data) }),
  me: () => request<MeResponse>('/auth/me'),

  // ─── Projects ───
  listProjects: () => request<ApiProject[]>('/projects'),
  getProject: (id: string) => request<ApiProject>(`/projects/${id}`),
  createProject: (data: ProjectCreatePayload) =>
    request<ApiProject>('/projects', { method: 'POST', body: JSON.stringify(data) }),
  updateProject: (id: string, data: ProjectUpdatePayload) =>
    request<ApiProject>(`/projects/${id}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteProject: (id: string) =>
    request<void>(`/projects/${id}`, { method: 'DELETE' }),
  importTxtProject: async (data: { file: File; title?: string; description?: string; target_words?: number }) => {
    const formData = new FormData()
    formData.append('file', data.file)
    if (data.title) formData.append('title', data.title)
    if (data.description) formData.append('description', data.description)
    if (data.target_words !== undefined) formData.append('target_words', String(data.target_words))

    const res = await fetch(`${API_BASE_URL}/projects/import-txt`, {
      method: 'POST',
      headers: formAuthHeaders(),
      body: formData,
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      if (res.status === 401) {
        clearAuthSession()
        redirectToLogin()
      }
      throw new ApiError(res.status, parseApiError(res.status, body))
    }
    return res.json() as Promise<TxtImportResponse>
  },
  uploadProjectImage: async (projectId: string, file: File) => {
    const res = await fetch(`${API_BASE_URL}/projects/${projectId}/assets/images`, {
      method: 'POST',
      headers: binaryAuthHeaders(file.type || 'application/octet-stream'),
      body: file,
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      if (res.status === 401) {
        clearAuthSession()
        redirectToLogin()
      }
      throw new ApiError(res.status, parseApiError(res.status, body))
    }
    return res.json() as Promise<ProjectImageUploadResponse>
  },

  // ─── Chapters ───
  listChapters: (projectId: string) => request<ApiChapter[]>(`/projects/${projectId}/chapters`),
  createChapter: (projectId: string, data: ChapterCreatePayload) =>
    request<ApiChapter>(`/projects/${projectId}/chapters`, { method: 'POST', body: JSON.stringify(data) }),
  updateChapter: (projectId: string, sequenceNumber: number, data: { title?: string; content?: string; outline?: string; status?: string }) =>
    request<ApiChapter>(`/projects/${projectId}/chapters/${sequenceNumber}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteChapter: (projectId: string, sequenceNumber: number) =>
    request<{ ok: boolean }>(`/projects/${projectId}/chapters/${sequenceNumber}`, { method: 'DELETE' }),
  extractChapterStructure: (projectId: string, sequenceNumber: number, data: ChapterStructureExtractRequest) =>
    request<ChapterStructureExtractResponse>(`/projects/${projectId}/chapters/${sequenceNumber}/extract-structure`, { method: 'POST', body: JSON.stringify(data) }),
  listChapterReviewNotes: (projectId: string, sequenceNumber: number) =>
    request<ChapterReviewNote[]>(`/projects/${projectId}/chapters/${sequenceNumber}/review-notes`),
  createChapterReviewNote: (projectId: string, sequenceNumber: number, data: ChapterReviewNoteCreatePayload) =>
    request<ChapterReviewNote>(`/projects/${projectId}/chapters/${sequenceNumber}/review-notes`, { method: 'POST', body: JSON.stringify(data) }),
  updateChapterReviewNote: (projectId: string, sequenceNumber: number, noteId: string, data: ChapterReviewNoteUpdatePayload) =>
    request<ChapterReviewNote>(`/projects/${projectId}/chapters/${sequenceNumber}/review-notes/${noteId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteChapterReviewNote: (projectId: string, sequenceNumber: number, noteId: string) =>
    request<void>(`/projects/${projectId}/chapters/${sequenceNumber}/review-notes/${noteId}`, { method: 'DELETE' }),

  // ─── Documents ───
  listDocuments: (projectId: string) => request<ApiDocument[]>(`/projects/${projectId}/documents`),
  createDocument: (projectId: string, data: DocumentCreatePayload) =>
    request<ApiDocument>(`/projects/${projectId}/documents`, { method: 'POST', body: JSON.stringify(data) }),
  updateDocument: (projectId: string, documentId: string, data: DocumentUpdatePayload) =>
    request<ApiDocument>(`/projects/${projectId}/documents/${documentId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteDocument: (projectId: string, documentId: string) =>
    request<{ ok: boolean }>(`/projects/${projectId}/documents/${documentId}`, { method: 'DELETE' }),

  // ─── Experts ───
  listExperts: (projectId: string) => request<ApiExpert[]>(`/projects/${projectId}/experts`),
  createCustomExpert: (projectId: string, data: ExpertCreatePayload) =>
    request<ApiExpert>(`/projects/${projectId}/experts`, { method: 'POST', body: JSON.stringify(data) }),
  updateExpert: (projectId: string, expertId: string, data: ExpertUpdatePayload) =>
    request<ApiExpert>(`/projects/${projectId}/experts/${expertId}`, { method: 'PATCH', body: JSON.stringify(data) }),

  // ─── World Entries ───
  listWorldEntries: (projectId: string) => request<ApiWorldEntry[]>(`/projects/${projectId}/world-entries`),
  createWorldEntry: (projectId: string, data: WorldEntryCreatePayload) =>
    request<ApiWorldEntry>(`/projects/${projectId}/world-entries`, { method: 'POST', body: JSON.stringify(data) }),
  updateWorldEntry: (projectId: string, entryId: string, data: WorldEntryUpdatePayload) =>
    request<ApiWorldEntry>(`/projects/${projectId}/world-entries/${entryId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteWorldEntry: (projectId: string, entryId: string) =>
    request<void>(`/projects/${projectId}/world-entries/${entryId}`, { method: 'DELETE' }),

  // ─── Characters ───
  listCharacters: (projectId: string) => request<ApiCharacter[]>(`/projects/${projectId}/characters`),
  createCharacter: (projectId: string, data: CharacterCreatePayload) =>
    request<ApiCharacter>(`/projects/${projectId}/characters`, { method: 'POST', body: JSON.stringify(data) }),
  updateCharacter: (projectId: string, characterId: string, data: CharacterUpdatePayload) =>
    request<ApiCharacter>(`/projects/${projectId}/characters/${characterId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  mergeCharacter: (projectId: string, characterId: string, data: CharacterMergePayload) =>
    request<ApiCharacter>(`/projects/${projectId}/characters/${characterId}/merge`, { method: 'POST', body: JSON.stringify(data) }),
  deleteCharacter: (projectId: string, characterId: string) =>
    request<void>(`/projects/${projectId}/characters/${characterId}`, { method: 'DELETE' }),
  listCharacterEvents: (projectId: string, params?: { character_id?: string; sequence_number?: number }) => {
    const search = new URLSearchParams()
    if (params?.character_id) search.set('character_id', params.character_id)
    if (params?.sequence_number) search.set('sequence_number', String(params.sequence_number))
    const suffix = search.toString() ? `?${search.toString()}` : ''
    return request<ApiCharacterEvent[]>(`/projects/${projectId}/character-events${suffix}`)
  },
  upsertCharacterEvent: (projectId: string, characterId: string, sequenceNumber: number, data: CharacterEventUpsertPayload) =>
    request<ApiCharacterEvent>(`/projects/${projectId}/characters/${characterId}/chapter-events/${sequenceNumber}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteCharacterEvent: (projectId: string, characterId: string, sequenceNumber: number) =>
    request<void>(`/projects/${projectId}/characters/${characterId}/chapter-events/${sequenceNumber}`, { method: 'DELETE' }),

  // ─── Character Relations ───
  listCharacterRelations: (projectId: string) => request<ApiCharacterRelation[]>(`/projects/${projectId}/character-relations`),
  createCharacterRelation: (projectId: string, data: CharacterRelationCreatePayload) =>
    request<ApiCharacterRelation>(`/projects/${projectId}/character-relations`, { method: 'POST', body: JSON.stringify(data) }),
  updateCharacterRelation: (projectId: string, relationId: string, data: CharacterRelationUpdatePayload) =>
    request<ApiCharacterRelation>(`/projects/${projectId}/character-relations/${relationId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteCharacterRelation: (projectId: string, relationId: string) =>
    request<void>(`/projects/${projectId}/character-relations/${relationId}`, { method: 'DELETE' }),

  // ─── Outlines ───
  listOutlines: (projectId: string) => request<ApiOutline[]>(`/projects/${projectId}/outlines`),
  createOutline: (projectId: string, data: OutlineCreatePayload) =>
    request<ApiOutline>(`/projects/${projectId}/outlines`, { method: 'POST', body: JSON.stringify(data) }),
  updateOutline: (projectId: string, outlineId: string, data: OutlineUpdatePayload) =>
    request<ApiOutline>(`/projects/${projectId}/outlines/${outlineId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteOutline: (projectId: string, outlineId: string) =>
    request<void>(`/projects/${projectId}/outlines/${outlineId}`, { method: 'DELETE' }),

  // ─── Hidden Threads ───
  listHiddenThreads: (projectId: string) => request<ApiHiddenThread[]>(`/projects/${projectId}/hidden-threads`),
  createHiddenThread: (projectId: string, data: HiddenThreadCreatePayload) =>
    request<ApiHiddenThread>(`/projects/${projectId}/hidden-threads`, { method: 'POST', body: JSON.stringify(data) }),
  updateHiddenThread: (projectId: string, threadId: string, data: HiddenThreadUpdatePayload) =>
    request<ApiHiddenThread>(`/projects/${projectId}/hidden-threads/${threadId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteHiddenThread: (projectId: string, threadId: string) =>
    request<void>(`/projects/${projectId}/hidden-threads/${threadId}`, { method: 'DELETE' }),

  // ─── SSE ───
  resumeGeneration: (
    projectId: string,
    threadId: string,
    action: 'approve' | 'reject' | 'review' | 'revise',
    onEvent: (envelope: SSEEnvelope) => void,
    feedback?: string,
    signal?: AbortSignal,
    mode: ProjectMode = 'novel',
  ) => {
    const params = new URLSearchParams({ thread_id: threadId, action })
    if (feedback) params.set('feedback', feedback)
    const unitPath = mode === 'article' ? 'documents' : 'chapters'
    const url = `${API_BASE_URL}/projects/${projectId}/${unitPath}/resume?${params.toString()}`
    return fetch(url, {
      method: 'POST',
      headers: sseHeaders(),
      signal,
    }).then(async (res) => {
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        throw new ApiError(res.status, parseApiError(res.status, text))
      }
      return parseSSEStream(res.body!.getReader(), onEvent)
    })
  },

  testExpertStream: (
    projectId: string,
    expertId: string,
    body: { test_text: string },
    onEvent: (envelope: SSEEnvelope) => void,
    signal?: AbortSignal,
  ) => {
    const url = `${API_BASE_URL}/projects/${projectId}/experts/${expertId}/test`
    return fetch(url, {
      method: 'POST',
      headers: sseHeaders(),
      body: JSON.stringify(body),
      signal,
    }).then(async (res) => {
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        throw new ApiError(res.status, parseApiError(res.status, text))
      }
      return parseSSEStream(res.body!.getReader(), onEvent)
    })
  },

  generateStream: (
    projectId: string,
    params: GenerateRequest,
    onEvent: (envelope: SSEEnvelope) => void,
    signal?: AbortSignal,
    mode: ProjectMode = 'novel',
  ) => {
    const unitPath = mode === 'article' ? 'documents' : 'chapters'
    const url = `${API_BASE_URL}/projects/${projectId}/${unitPath}/generate`
    return fetch(url, {
      method: 'POST',
      headers: sseHeaders(),
      body: JSON.stringify(params),
      signal,
    }).then(async (res) => {
      if (!res.ok) {
        const text = await res.text().catch(() => '')
        throw new ApiError(res.status, parseApiError(res.status, text))
      }
      return parseSSEStream(res.body!.getReader(), onEvent)
    })
  },

  // ─── Chapter Versions ───
  listChapterVersions: (projectId: string, sequenceNumber: number) =>
    request<ApiChapterVersion[]>(`/projects/${projectId}/chapters/${sequenceNumber}/versions`),
  getChapterVersion: (projectId: string, sequenceNumber: number, versionId: string) =>
    request<ApiChapterVersion>(`/projects/${projectId}/chapters/${sequenceNumber}/versions/${versionId}`),
  diffChapterVersions: (projectId: string, sequenceNumber: number, data: ChapterVersionDiffRequest) =>
    request<ChapterVersionDiffResponse>(`/projects/${projectId}/chapters/${sequenceNumber}/versions/diff`, { method: 'POST', body: JSON.stringify(data) }),

  // ─── Chapter Context Stats ───
  getChapterContext: (projectId: string, sequenceNumber: number) =>
    request<{
      stats: { characters: number; events: number; hidden_threads: number; world_entries: number; sources: number }
      chapter_goal: { outline: string; light_line: string }
    }>(`/projects/${projectId}/chapters/${sequenceNumber}/context`),

  // ─── Chapter Directions ───
  getChapterDirections: (projectId: string, sequenceNumber: number, selectedIds?: {
    selected_outline_ids?: string[]
    selected_character_ids?: string[]
    selected_world_entry_ids?: string[]
    selected_hidden_thread_ids?: string[]
  }) =>
    request<{
      options: Array<{ id: string; title: string; description: string; risk: string }>
    }>(`/projects/${projectId}/chapters/${sequenceNumber}/directions`, {
      method: 'POST',
      body: JSON.stringify(selectedIds ?? {}),
    }),

  // ─── Knowledge Library ───
  listKnowledgeSources: (projectId: string, params?: {
    source_type?: string; q?: string; sort?: string
  }) => {
    const qs = new URLSearchParams()
    if (params?.source_type) qs.set('source_type', params.source_type)
    if (params?.q) qs.set('q', params.q)
    if (params?.sort) qs.set('sort', params.sort)
    const query = qs.toString()
	    return request<Array<{
	      id: string; project_id: string; title: string; source_type: string
	      content_preview: string; content_truncated: boolean
	      summary: string | null; key_facts: string[] | null
	      constraints: string[] | null; characters: string[] | null; keywords: string[] | null
	      tags: string[] | null; always_inject: boolean; chunk_count: number; token_count: number
	      created_at: string; updated_at: string
    }>>(`/projects/${projectId}/knowledge/sources${query ? `?${query}` : ''}`)
  },

  getKnowledgeSource: (projectId: string, sourceId: string) =>
    request<{
      id: string; project_id: string; title: string; source_type: string
      content: string; summary: string | null; key_facts: string[] | null
      constraints: string[] | null; characters: string[] | null; keywords: string[] | null
      tags: string[] | null; always_inject: boolean; chunk_count: number; token_count: number
      created_at: string; updated_at: string
    }>(`/projects/${projectId}/knowledge/sources/${sourceId}`),

  createKnowledgeSource: (projectId: string, data: {
    title: string; source_type?: string; content?: string
    tags?: string[]; always_inject?: boolean
  }) =>
    request<{ id: string }>(`/projects/${projectId}/knowledge/sources`, {
      method: 'POST', body: JSON.stringify(data),
    }),

  updateKnowledgeSource: (projectId: string, sourceId: string, data: {
    title?: string; source_type?: string; content?: string
    tags?: string[]; always_inject?: boolean
  }) =>
    request<{ id: string }>(`/projects/${projectId}/knowledge/sources/${sourceId}`, {
      method: 'PATCH', body: JSON.stringify(data),
    }),

  deleteKnowledgeSource: (projectId: string, sourceId: string) =>
    request<void>(`/projects/${projectId}/knowledge/sources/${sourceId}`, { method: 'DELETE' }),

  reindexSource: (projectId: string, sourceId: string) =>
    request<{ source_id: string; chunk_count: number; fact_count: number }>(
      `/projects/${projectId}/knowledge/sources/${sourceId}/reindex`, { method: 'POST' },
    ),

  rebuildFacts: (projectId: string, sourceId?: string | null) =>
    request<{ source_count: number; chunk_count: number; fact_count: number }>(
      `/projects/${projectId}/knowledge/facts/rebuild`,
      { method: 'POST', body: JSON.stringify({ source_id: sourceId ?? null, fact_types: ['character_system'] }) },
    ),

  listFacts: (projectId: string, params?: { subject?: string; object?: string; limit?: number }) =>
    request<{ items: any[]; total: number }>(
      `/projects/${projectId}/knowledge/facts` + (params ? '?' + new URLSearchParams(
        Object.entries(params).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)]) as any
      ).toString() : ''),
    ),

  summarizeSource: (projectId: string, sourceId: string) =>
    request<{ source_id: string; chunk_count: number; summary: string; key_facts: string[] }>(
      `/projects/${projectId}/knowledge/sources/${sourceId}/summarize`, { method: 'POST' },
    ),

  listKnowledgeChunks: (projectId: string, sourceId: string, params?: { offset?: number; limit?: number }) => {
    const qs = new URLSearchParams()
    if (params?.offset !== undefined) qs.set('offset', String(params.offset))
    if (params?.limit !== undefined) qs.set('limit', String(params.limit))
    const query = qs.toString()
    return request<Array<{
      id: string; source_id: string; chunk_index: number
      content: string; summary: string | null; facts: string[] | null
      constraints: string[] | null; keywords: string[] | null; token_count: number
      created_at: string
    }>>(`/projects/${projectId}/knowledge/sources/${sourceId}/chunks${query ? `?${query}` : ''}`)
  },

  listKnowledgeSessions: (projectId: string) =>
    request<Array<{
      id: string; project_id: string; title: string; summary: string | null
      message_count: number; created_at: string; updated_at: string
    }>>(`/projects/${projectId}/knowledge/sessions`),

  createKnowledgeSession: (projectId: string) =>
    request<{
      id: string; project_id: string; title: string; summary: string | null
      message_count: number; created_at: string; updated_at: string
    }>(`/projects/${projectId}/knowledge/sessions`, { method: 'POST' }),

  updateKnowledgeSession: (projectId: string, sessionId: string, data: { title?: string }) =>
    request<{
      id: string; project_id: string; title: string; summary: string | null
      message_count: number; created_at: string; updated_at: string
    }>(`/projects/${projectId}/knowledge/sessions/${sessionId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),

  deleteKnowledgeSession: (projectId: string, sessionId: string) =>
    request<void>(`/projects/${projectId}/knowledge/sessions/${sessionId}`, {
      method: 'DELETE',
    }),

  listKnowledgeSessionMessages: (projectId: string, sessionId: string) =>
    request<Array<{
      id: string; session_id: string; role: string; content: string
      citations: Array<{
        source_kind: string; source_id: string; chunk_id: string | null
        title: string; snippet: string; evidence_type?: string; matched_query?: string; score?: number
      }> | null
      created_at: string
    }>>(`/projects/${projectId}/knowledge/sessions/${sessionId}/messages`),

  searchKnowledge: (projectId: string, query: string) =>
    request<{ results: Array<{
      source_kind: string; source_id: string; chunk_id: string
      title: string; snippet: string; score: number
    }> }>(`/projects/${projectId}/knowledge/search`, {
      method: 'POST', body: JSON.stringify({ query }),
    }),

  askKnowledge: (projectId: string, data: {
    question: string; conversation_id?: string; chapter_num?: number; include_structured?: boolean
    include_web?: boolean
  }) =>
    request<{
      answer: string; citations: Array<{
        source_kind: string; source_id: string; chunk_id: string | null
        title: string; snippet: string
        url?: string; evidence_type?: string; matched_query?: string; score?: number
      }>; conversation_id: string; conversation_summary_updated: boolean
      query_plan?: { intent: string; entities: string[]; search_queries: string[] }
      retrieval_stats?: { structured_hits: number; chunk_hits: number; web_hits?: number }
    }>(`/projects/${projectId}/knowledge/ask`, {
      method: 'POST', body: JSON.stringify(data),
    }),

  uploadKnowledgeFile: async (projectId: string, formData: FormData) => {
    const res = await fetch(`${API_BASE_URL}/projects/${projectId}/knowledge/upload`, {
      method: 'POST',
      headers: formAuthHeaders(),
      body: formData,
    })
    if (!res.ok) {
      const body = await res.text().catch(() => '')
      if (res.status === 401) { clearAuthSession(); redirectToLogin() }
      throw new ApiError(res.status, parseApiError(res.status, body))
    }
    return res.json() as Promise<{ id: string; title: string; chunk_count: number }>
  },

  // ─── Novel Extraction Pipeline ───

  // 抽取任务状态（含控制字段）
  // ExtractionStatus / ExtractionFailure 类型定义在下面，API 方法引用
  splitChapters: (projectId: string, sourceId: string) =>
    request<{ project_id: string; source_id: string; chapter_count: number; split_type: string }>(
      `/projects/${projectId}/knowledge/sources/${sourceId}/split-chapters`, { method: 'POST' },
    ),

  startExtraction: (projectId: string, sourceId: string, params: {
    genre?: string; canon_level?: string; origin?: string;
    chapter_no_start?: number; chapter_no_end?: number;
    max_chapters_per_run?: number; force_reextract?: boolean;
  }) =>
    request<ExtractionStatus>(`/projects/${projectId}/knowledge/sources/${sourceId}/extract`, {
      method: 'POST', body: JSON.stringify(params),
    }),

  getExtractionStatus: (projectId: string, sourceId: string) =>
    request<{
      job_id: string | null; status: string; total_chapters: number;
      extracted_count: number; validated_count: number;
      merged_count: number; failed_count: number; pending_count: number;
      error_message: string | null;
      provider: string | null; is_mock: boolean; last_run_outcome: string;
      current_chapter_no: number | null; last_error: string | null;
      paused_at: string | null; cancelled_at: string | null;
      last_run_started_at: string | null; last_run_finished_at: string | null;
    }>(`/projects/${projectId}/knowledge/sources/${sourceId}/extract/status`),

  resetExtraction: (projectId: string, sourceId: string) =>
    request<{
      status: string; deleted_jobs: number; deleted_staging: number;
      deleted_characters: number; deleted_abilities: number;
      deleted_events: number; deleted_world_rules: number;
    }>(`/projects/${projectId}/knowledge/sources/${sourceId}/extract/reset`, { method: 'POST' }),

  pauseExtraction: (projectId: string, sourceId: string) =>
    request<ExtractionStatus>(`/projects/${projectId}/knowledge/sources/${sourceId}/extract/pause`, { method: 'POST' }),

  cancelExtraction: (projectId: string, sourceId: string) =>
    request<ExtractionStatus>(`/projects/${projectId}/knowledge/sources/${sourceId}/extract/cancel`, { method: 'POST' }),

  listExtractionFailures: (projectId: string, sourceId: string) =>
    request<{ items: ExtractionFailure[]; total: number }>(`/projects/${projectId}/knowledge/sources/${sourceId}/extract/failures`),

  retryExtractionChapter: (projectId: string, sourceId: string, chapterNo: number) =>
    request<{ chapter_no: number; chapter_status: string; job_status: string; job_id: string; error_message: string | null }>(
      `/projects/${projectId}/knowledge/sources/${sourceId}/extract/retry-chapter`,
      { method: 'POST', body: JSON.stringify({ chapter_no: chapterNo, force_reextract: true }) },
    ),

  structuredQA: (projectId: string, question: string, conversationId?: string) =>
    request<{
      answer: string; citations: any[]; query_plan: any;
      retrieval_stats: any; conversation_id: string;
    }>(`/projects/${projectId}/knowledge/structured-qa`, {
      method: 'POST', body: JSON.stringify({
        question, conversation_id: conversationId ?? null,
      }),
    }),

  // 结构化知识表查询（人物/能力/事件/世界规则）
  listStructuredKnowledge: <T = any>(projectId: string, table: 'character_profile' | 'ability_profile' | 'event_timeline' | 'world_rule', params?: {
    limit?: number; offset?: number;
  }) => {
    const query = params
      ? '?' + new URLSearchParams(
          Object.entries(params).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)]) as any
        ).toString()
      : ''
    return request<{ items: T[]; total: number }>(
      `/projects/${projectId}/knowledge/structured/${table}${query}`,
    )
  },

  // 结构化知识表新增（手动修正）
  createStructuredRecord: (projectId: string, table: string, body: Record<string, any>) =>
    request<{ id: string; origin: string; canon_level: string; source_priority: number; evidence: string[] }>(
      `/projects/${projectId}/knowledge/structured/${table}`, { method: 'POST', body: JSON.stringify(body) },
    ),

  // 结构化知识表编辑（手动修正，保留原 evidence）
  updateStructuredRecord: (projectId: string, table: string, recordId: string, body: Record<string, any>) =>
    request<{ id: string; origin: string; canon_level: string; source_priority: number; previous_origin: string; evidence: string[] }>(
      `/projects/${projectId}/knowledge/structured/${table}/${recordId}`, { method: 'PATCH', body: JSON.stringify(body) },
    ),

  // 结构化知识表删除
  deleteStructuredRecord: (projectId: string, table: string, recordId: string) =>
    request<{ id: string; deleted: boolean; table: string }>(
      `/projects/${projectId}/knowledge/structured/${table}/${recordId}`, { method: 'DELETE' },
    ),

  // ─── Document Versions ───
  listDocumentVersions: (projectId: string, documentId: string) =>
    request<ApiDocumentVersion[]>(`/projects/${projectId}/documents/${documentId}/versions`),
  getDocumentVersion: (projectId: string, documentId: string, versionId: string) =>
    request<ApiDocumentVersion>(`/projects/${projectId}/documents/${documentId}/versions/${versionId}`),
  restoreDocumentVersion: (projectId: string, documentId: string, versionId: string) =>
    request<ApiDocument>(`/projects/${projectId}/documents/${documentId}/versions/${versionId}/restore`, { method: 'POST' }),
  diffDocumentVersions: (projectId: string, documentId: string, data: DocumentVersionDiffRequest) =>
    request<DocumentVersionDiffResponse>(`/projects/${projectId}/documents/${documentId}/versions/diff`, { method: 'POST', body: JSON.stringify(data) }),

  // ─── AI Generation History ───
  listChapterGenerations: (projectId: string, sequenceNumber: number) =>
    request<ApiGenerationRecordListItem[]>(`/projects/${projectId}/chapters/${sequenceNumber}/generations`),
  listDocumentGenerations: (projectId: string, documentId: string) =>
    request<ApiGenerationRecordListItem[]>(`/projects/${projectId}/documents/${documentId}/generations`),
  getGenerationRecord: (projectId: string, generationId: string) =>
    request<ApiGenerationRecord>(`/projects/${projectId}/generations/${generationId}`),
  updateGenerationRecord: (projectId: string, generationId: string, data: GenerationRecordUpdatePayload) =>
    request<ApiGenerationRecord>(`/projects/${projectId}/generations/${generationId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  diffGenerationRecord: (projectId: string, generationId: string, data: GenerationRecordDiffRequest) =>
    request<GenerationRecordDiffResponse>(`/projects/${projectId}/generations/${generationId}/diff`, { method: 'POST', body: JSON.stringify(data) }),

  // ─── Evaluation datasets ───
  listEvaluationDatasets: (projectId: string) =>
    request<ApiEvaluationDataset[]>(`/projects/${projectId}/eval-datasets`),
  createEvaluationDataset: (projectId: string, data: EvaluationDatasetCreatePayload) =>
    request<ApiEvaluationDataset>(`/projects/${projectId}/eval-datasets`, { method: 'POST', body: JSON.stringify(data) }),
  updateEvaluationDataset: (projectId: string, datasetId: string, data: EvaluationDatasetUpdatePayload) =>
    request<ApiEvaluationDataset>(`/projects/${projectId}/eval-datasets/${datasetId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteEvaluationDataset: (projectId: string, datasetId: string) =>
    request<void>(`/projects/${projectId}/eval-datasets/${datasetId}`, { method: 'DELETE' }),
  listEvaluationCases: (projectId: string, datasetId: string) =>
    request<ApiEvaluationCase[]>(`/projects/${projectId}/eval-datasets/${datasetId}/cases`),
  createEvaluationCase: (projectId: string, datasetId: string, data: EvaluationCaseCreatePayload) =>
    request<ApiEvaluationCase>(`/projects/${projectId}/eval-datasets/${datasetId}/cases`, { method: 'POST', body: JSON.stringify(data) }),
  updateEvaluationCase: (projectId: string, datasetId: string, caseId: string, data: EvaluationCaseUpdatePayload) =>
    request<ApiEvaluationCase>(`/projects/${projectId}/eval-datasets/${datasetId}/cases/${caseId}`, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteEvaluationCase: (projectId: string, datasetId: string, caseId: string) =>
    request<void>(`/projects/${projectId}/eval-datasets/${datasetId}/cases/${caseId}`, { method: 'DELETE' }),
  listEvaluationRuns: (projectId: string, datasetId: string) =>
    request<ApiEvaluationRun[]>(`/projects/${projectId}/eval-datasets/${datasetId}/runs`),
  getEvaluationRun: (projectId: string, datasetId: string, runId: string) =>
    request<ApiEvaluationRun>(`/projects/${projectId}/eval-datasets/${datasetId}/runs/${runId}`),
  runEvaluationDataset: (projectId: string, datasetId: string, data: EvaluationRunCreatePayload) =>
    request<ApiEvaluationRun>(`/projects/${projectId}/eval-datasets/${datasetId}/runs`, { method: 'POST', body: JSON.stringify(data) }),

  // ─── LLM Settings ───
  getLLMConfig: () => request<LLMConfigResponse>('/llm-settings'),
  updateLLMConfig: (data: LLMConfigCreatePayload) =>
    request<LLMConfigResponse>('/llm-settings', { method: 'PUT', body: JSON.stringify(data) }),
  fetchModels: (data: ModelListRequest) =>
    request<ModelInfo[]>('/llm-settings/models', { method: 'POST', body: JSON.stringify(data) }),
  getLLMStatus: () => request<LLMStatusResponse>('/llm-settings/status'),
}
